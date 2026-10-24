"""Local Web Server: loopback security, console API and upload state machine.

The cloud is replaced by an in-memory fake here; real cross-project uploads
are tested in the repository-root integration tests.
"""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from local.app import create_app
from local.archive import LocalArchive
from local.service import LocalService
from local.sources.claude import ClaudeAdapter
from server.contracts.protocol import Problem

TS = '2026-10-23T01:02:03Z'
KEY = 'asv_0123456789abcdef_' + 'k' * 43


class FakeCloud:
    """Sequence-CAS semantics of the real cloud, kept in memory."""
    def __init__(self):
        self.bound = None; self.ack = 0; self.instance = 'i' * 32; self.bodies = []; self.keys = []
        self.lose_next_response = False; self.fail_status = None; self.gate = None; self.receipts = {}; self.max_request = 1 << 20

    def factory(self, url, key):
        cloud = self

        class Client:
            def status(self, dataset):
                if cloud.fail_status:
                    raise cloud.fail_status
                return {'protocolVersion': 6, 'instance': cloud.instance, 'contentPolicy': 'analysis-v1', 'maxRequestBytes': cloud.max_request,
                        'client': {'id': 'c1', 'name': 'laptop'}, 'boundDataset': cloud.bound,
                        'ackSeq': cloud.ack if cloud.bound == dataset else None}

            def bind(self, dataset):
                cloud.bound = cloud.bound or dataset; return self.status(dataset)

            def send(self, body, key):
                cloud.bodies.append(body); cloud.keys.append(key)
                if cloud.gate:
                    cloud.gate.wait(5)
                if key in cloud.receipts:
                    return cloud.receipts[key]
                value = json.loads(body)
                if len(body) > cloud.max_request:
                    raise Problem('TOO_LARGE', 'too large', 413)
                if value['expectedSeq'] != cloud.ack:
                    raise Problem('SEQ_CONFLICT', 'conflict', 409)
                cloud.ack = value['throughSeq']
                receipt = {'ackSeq': cloud.ack, 'datasetId': value['datasetId'], 'instance': cloud.instance}
                cloud.receipts[key] = receipt
                if cloud.lose_next_response:
                    cloud.lose_next_response = False
                    raise Problem('NETWORK_UNAVAILABLE', 'response lost', 503)
                return receipt

            def close(self):
                pass
        return Client()


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.source.mkdir()
        self.cloud = FakeCloud()
        self.path = self.source / 'c.jsonl'
        self.path.write_text(json.dumps({'type': 'user', 'sessionId': 'c', 'timestamp': TS, 'message': {'content': 'hello world'}}) + '\n')
        self.service = LocalService(LocalArchive(self.root / 'state/local.db'), [ClaudeAdapter([self.source])], quiet_seconds=0,
                                    credential_backend='file', client_factory=self.cloud.factory, upload_batch_bytes=1024, log=lambda *_: None)
        self.service.import_history()
        self.app = create_app(self.service); self.client = self.app.test_client()
        self.token = self.app.config['LOCAL_CSRF_TOKEN']

    def post(self, path, body=None, method='POST', **headers):
        return self.client.open(path, method=method, json=body, base_url='http://localhost:8000',
                                headers={'X-CSRF-Token': self.token, 'Origin': 'http://localhost:8000', **headers})

    def connect(self):
        response = self.post('/api/cloud/connection', {'url': 'https://cloud.example', 'key': KEY}, method='PUT')
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def test_no_login_but_loopback_host_origin_and_csrf_are_enforced(self):
        page = self.client.get('/', base_url='http://localhost:8000')
        self.assertEqual(page.status_code, 200); self.assertIn(self.token, page.text)
        self.assertNotIn('Set-Cookie', page.headers)
        self.assertEqual(self.client.get('/api/sessions', base_url='http://127.0.0.1:8000').json['total'], 1)
        self.assertEqual(self.client.get('/api/sessions', base_url='http://evil.example:8000').status_code, 403)
        self.assertEqual(self.client.post('/api/sync/start', base_url='http://localhost:8000').status_code, 403)
        self.assertEqual(self.post('/api/sync/start', **{'Origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(self.post('/api/sync/start', **{'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.client.put('/api/cloud/connection', base_url='http://localhost:8000', data='url=x',
                                         headers={'X-CSRF-Token': self.token, 'Content-Type': 'application/x-www-form-urlencoded'}).status_code, 415)
        self.assertNotIn('Access-Control-Allow-Origin', page.headers)

    def test_connection_is_verified_saved_without_echoing_key_and_without_uploading(self):
        self.assertEqual(self.post('/api/cloud/connection', {'url': 'http://cloud.example', 'key': KEY}, method='PUT').status_code, 400)
        self.assertEqual(self.post('/api/cloud/connection', {'url': 'https://cloud.example/path', 'key': KEY}, method='PUT').status_code, 400)
        tested = self.post('/api/cloud/test', {'url': 'https://cloud.example', 'key': KEY}).json
        self.assertTrue(tested['reachable']); self.assertEqual(tested['localLastSeq'], 2)
        self.connect()
        state = self.client.get('/api/cloud', base_url='http://localhost:8000')
        self.assertNotIn(KEY, state.text); self.assertNotIn(KEY.split('_', 2)[2], state.text)
        self.assertEqual(state.json['connection']['keyHint'], 'asv_0123456789abcdef')
        self.assertEqual(state.json['pendingChanges'], 2)
        self.assertEqual(self.cloud.bodies, [], 'saving a connection never uploads')
        stored = list((self.root / 'state/credentials').iterdir())
        self.assertEqual(len(stored), 1); self.assertNotIn(KEY.encode(), (self.root / 'state/local.db').read_bytes())
        self.assertEqual(self.post('/api/cloud/connection', method='DELETE').status_code, 200)
        self.assertEqual(list((self.root / 'state/credentials').iterdir()), [])

    def test_upload_runs_only_on_request_and_is_idempotent_after_lost_response(self):
        self.connect(); self.cloud.lose_next_response = True
        state = self.service.uploader.run_now()
        self.assertEqual(state['state'], 'failed'); self.assertEqual(self.cloud.ack, 2)
        pending = self.service.archive.connection()['pending']; self.assertIsNotNone(pending)
        state = self.service.uploader.run_now()
        self.assertEqual(state['state'], 'complete'); self.assertEqual(self.service.archive.connection()['acked_seq'], 2)
        self.assertEqual(len(self.cloud.bodies), 1, 'cloud head already covered the pending batch: not resent')
        self.assertIsNone(self.service.archive.connection()['pending'])
        self.assertEqual(self.service.uploader.run_now()['state'], 'complete')
        self.assertEqual(len(self.cloud.bodies), 1)

    def test_pending_batch_is_resent_byte_for_byte_with_same_key(self):
        self.connect(); self.cloud.fail_status = None
        original = self.cloud.factory

        def no_delivery(url, key):
            client = original(url, key)

            def send(body, key):
                self.cloud.bodies.append(body); self.cloud.keys.append(key)
                raise Problem('NETWORK_UNAVAILABLE', 'never delivered', 503)
            client.send = send; return client
        self.service.uploader.client_factory = no_delivery
        self.assertEqual(self.service.uploader.run_now()['state'], 'failed')
        self.service.uploader.client_factory = original
        self.assertEqual(self.service.uploader.run_now()['state'], 'complete')
        self.assertEqual(self.cloud.bodies[0], self.cloud.bodies[1]); self.assertEqual(self.cloud.keys[0], self.cloud.keys[1])

    def test_high_water_is_fixed_and_batches_are_sized_by_bytes(self):
        for i in range(6):
            (self.source / f'{i}.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': f's{i}', 'timestamp': TS,
                                                               'message': {'content': 'x' * 300}}) + '\n')
        self.service.import_history(); self.connect()
        gate = threading.Event(); self.cloud.gate = gate
        self.service.start_upload()
        for _ in range(500):
            if self.cloud.bodies:
                break
            threading.Event().wait(.01)
        (self.source / 'late.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': 'late', 'timestamp': TS, 'message': {'content': 'late'}}) + '\n')
        self.service.import_history(); late_seq = self.service.archive.last_seq()
        gate.set(); state = self.service.uploader.wait(10)
        self.assertEqual(state['state'], 'complete'); self.assertLess(self.cloud.ack, late_seq)
        self.assertGreater(len(self.cloud.bodies), 2)
        self.assertTrue(all(len(json.loads(b)['groups']) >= 1 for b in self.cloud.bodies))
        self.cloud.gate = None
        self.assertEqual(self.service.uploader.run_now()['ackSeq'], late_seq)

    def test_only_a_group_larger_than_the_cloud_limit_is_split_at_change_boundaries(self):
        rows = [{'type': 'user', 'sessionId': 'huge', 'uuid': str(i), 'timestamp': TS, 'message': {'content': f'{i} ' + 'z' * 900}} for i in range(150)]
        (self.source / 'huge.jsonl').write_text(''.join(json.dumps(r) + chr(10) for r in rows))
        self.service.import_history(); self.connect()
        self.cloud.max_request = 100 * 1024; self.service.uploader.batch_bytes = 1024 * 1024
        state = self.service.uploader.run_now()
        self.assertEqual(state['state'], 'complete', state); self.assertEqual(self.cloud.ack, self.service.archive.last_seq())
        ids = [g['id'] for b in self.cloud.bodies for g in json.loads(b)['groups']]
        self.assertTrue(all(len(b) <= 100 * 1024 for b in self.cloud.bodies))
        self.assertGreater(len(ids), len(set(ids)), 'one oversized commit group spans several POSTs')

    def test_pause_and_cancel_take_effect_between_batches(self):
        for i in range(4):
            (self.source / f'{i}.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': f'p{i}', 'timestamp': TS,
                                                               'message': {'content': 'y' * 400}}) + '\n')
        self.service.import_history(); self.connect()
        gate = threading.Event(); self.cloud.gate = gate
        self.service.start_upload()
        for _ in range(500):
            if self.cloud.bodies:
                break
            threading.Event().wait(.01)
        self.service.pause_upload(); gate.set()
        state = self.service.uploader.wait(10)
        self.assertEqual(state['state'], 'paused'); self.assertEqual(len(self.cloud.bodies), 1)
        high = state['highWater']; self.cloud.gate = None
        self.service.start_upload(); self.assertEqual(self.service.uploader.wait(10)['ackSeq'], high)

    def test_cloud_ahead_of_local_or_other_dataset_blocks_upload(self):
        self.connect(); self.cloud.bound = self.service.archive.dataset; self.cloud.ack = 999
        state = self.service.uploader.run_now()
        self.assertEqual(state['error']['code'], 'LOCAL_BEHIND_CLOUD'); self.assertEqual(self.cloud.bodies, [])
        self.cloud.bound = 'f' * 32; self.cloud.ack = 0
        self.assertEqual(self.service.uploader.run_now()['error']['code'], 'DATASET_MISMATCH')
        self.assertEqual(self.post('/api/cloud/connection', {'url': 'https://cloud.example', 'key': KEY}, method='PUT').status_code, 409)

    def test_analysis_view_has_no_raw_records_or_branches(self):
        sid = 'claude:c'
        self.assertEqual(self.client.get(f'/api/sessions/{sid}/records', base_url='http://localhost:8000').status_code, 410)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}/messages?branch=active', base_url='http://localhost:8000').status_code, 400)
        page = self.client.get(f'/api/sessions/{sid}/messages', base_url='http://localhost:8000').json
        self.assertEqual(page['sessionRevision'], 1)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}/messages?cursor=1&revision=0', base_url='http://localhost:8000').status_code, 409)


if __name__ == '__main__':
    unittest.main()
