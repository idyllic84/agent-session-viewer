"""Local Web Server -> cloud Web Server, end to end. Synthetic data only.

Never accesses real Agent histories, real databases or real cloud services.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from local.archive import LocalArchive
from local.cloud import CloudClient
from local.codec import unpack_json
from local.service import LocalService
from local.sources.claude import ClaudeAdapter
from server.app import create_app
from server.archive import CloudArchive
from server.contracts.protocol import Problem

ORIGIN = 'https://cloud.test'
TS = '2026-10-23T01:02:03Z'


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')


def append(path, rows):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


class Network:
    """In-process transport to the real cloud Flask app with fault injection."""
    def __init__(self, app):
        self.app = app; self.calls = []; self.bodies = []; self.keys = []
        self.lose_response = 0; self.drop_request = 0

    def factory(self, url, key):
        network = self

        class Client(CloudClient):
            def call(self, method, path, **kwargs):
                network.calls.append((method, path)); headers = {'Authorization': 'Bearer ' + self.key, **kwargs.pop('headers', {})}
                if path == '/ingest/v1/batches':
                    network.bodies.append(kwargs['data']); network.keys.append(headers['Idempotency-Key'])
                    if network.drop_request:
                        network.drop_request -= 1; raise Problem('NETWORK_UNAVAILABLE', 'request lost', 503)
                response = network.app.test_client().open(path, method=method, base_url=ORIGIN, headers=headers,
                                                          query_string=kwargs.pop('params', None), **kwargs)
                value = response.get_json()
                if response.status_code >= 400:
                    raise Problem(value['error']['code'], value['error']['message'], response.status_code)
                if path == '/ingest/v1/batches' and network.lose_response:
                    network.lose_response -= 1; raise Problem('NETWORK_UNAVAILABLE', 'response lost', 503)
                return value
        return Client(url, key)


class LocalCloudIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.source.mkdir()
        self.cloud = CloudArchive(self.root / 'cloud/cloud.db', max_request=1024 * 1024)
        self.cloud.set_password('owner password 123', require_current=False)
        self.app = create_app(self.cloud, session_secret='s' * 48, public_origin=ORIGIN)
        self.network = Network(self.app)
        self.key = self.cloud.create_key('laptop')['key']
        self.local = self.make_local('local')

    def make_local(self, name, adapters=None, batch=2048, import_batch=1024 * 1024):
        return LocalService(LocalArchive(self.root / name / 'local.db'), adapters or [ClaudeAdapter([self.source])], quiet_seconds=0,
                            credential_backend='file', client_factory=self.network.factory, upload_batch_bytes=batch,
                            import_batch_bytes=import_batch, log=lambda *_: None)

    def claude(self, name='c', rows=None):
        path = self.source / f'{name}.jsonl'
        write(path, rows or [{'type': 'user', 'sessionId': name, 'uuid': 'u', 'timestamp': TS, 'message': {'content': f'question {name}'}}])
        return path

    def connect(self, service=None, key=None):
        service = service or self.local
        service.save_connection(ORIGIN, key or self.key)

    def sync(self, service=None):
        return (service or self.local).uploader.run_now()

    def cloud_messages(self, sid, service=None):
        return self.cloud.db.get_messages((service or self.local).archive.dataset + '/' + sid)

    def test_import_then_explicit_upload_then_incremental_upload(self):
        path = self.claude(); self.local.import_history()
        self.assertEqual(self.network.calls, [], 'startup import never contacts the cloud')
        self.connect()
        self.assertFalse(any(p == '/ingest/v1/batches' for _, p in self.network.calls), 'saving a connection does not upload')
        self.assertEqual(self.sync()['state'], 'complete')
        self.assertEqual(self.cloud_messages('claude:c')['total'], 1)
        append(path, [{'type': 'assistant', 'uuid': 'a', 'timestamp': TS, 'message': {'content': [
            {'type': 'text', 'text': 'answer'}, {'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {'command': 'ls -la'}}]}},
            {'type': 'user', 'uuid': 'r', 'timestamp': TS, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't', 'is_error': True, 'content': 'TOOL_OUTPUT'}]}}])
        self.local.import_history(); self.network.calls.clear()
        self.assertEqual(self.sync()['state'], 'complete')
        self.assertEqual([p for _, p in self.network.calls], ['/ingest/v1/status', '/ingest/v1/batches'])
        items = self.cloud_messages('claude:c')['items']
        self.assertEqual([m['type'] for m in items], ['user', 'assistant', 'tool-use', 'tool-result'])
        self.assertEqual(items[-1]['status'], 'failure'); self.assertEqual(items[2]['message']['content'][0]['input'], {'command': 'ls -la'})
        self.assertNotIn(b'TOOL_OUTPUT', b''.join(self.network.bodies))
        self.network.calls.clear(); self.sync()
        self.assertEqual([p for _, p in self.network.calls], ['/ingest/v1/status'], 'nothing new: one status read, no POST')

    def test_lost_response_and_lost_request_never_duplicate(self):
        for i in range(5):
            self.claude(f's{i}')
        self.local.import_history(); self.connect()
        self.network.lose_response = 1
        self.assertEqual(self.sync()['error']['code'], 'NETWORK_UNAVAILABLE')
        committed = self.cloud.keys()[0]['ackSeq']; self.assertGreater(committed, 0)
        self.network.drop_request = 1; dropped_at = len(self.network.bodies)
        self.assertEqual(self.sync()['state'], 'failed')
        restarted = self.make_local('local')   # new process: only durable state survives
        self.assertEqual(self.sync(restarted)['state'], 'complete')
        self.assertEqual(self.cloud.keys()[0]['ackSeq'], self.local.archive.last_seq())
        self.assertEqual(self.cloud.db.list_sessions()[1], 5)
        self.assertEqual(sum(self.cloud.db.get_session(s['id'])['messageCount'] for s in self.cloud.db.list_sessions()[0]), 5)
        self.assertEqual(self.network.bodies[dropped_at], self.network.bodies[dropped_at + 1])
        self.assertEqual(self.network.keys[dropped_at], self.network.keys[dropped_at + 1])

    def test_two_computers_are_isolated_and_keys_cannot_be_shared(self):
        self.claude('same'); self.local.import_history(); self.connect(); self.sync()
        other = self.make_local('other'); other.import_history()
        with self.assertRaises(Problem) as error:
            other.save_connection(ORIGIN, self.key)
        self.assertEqual(error.exception.code, 'DATASET_MISMATCH')
        other.save_connection(ORIGIN, self.cloud.create_key('desktop')['key']); self.sync(other)
        ids = sorted(s['id'] for s in self.cloud.db.list_sessions()[0])
        self.assertEqual(ids, sorted([self.local.archive.dataset + '/claude:same', other.archive.dataset + '/claude:same']))

    def test_revoked_key_and_restored_cloud_state_are_explicit_failures(self):
        self.claude(); self.local.import_history(); self.connect(); self.sync()
        client = self.cloud.keys()[0]['clientId']
        rotated = self.cloud.rotate_key(client)['key']
        self.claude('later'); self.local.import_history()
        self.assertEqual(self.sync()['error']['code'], 'UNAUTHORIZED')
        self.local.save_connection(ORIGIN, rotated)
        self.assertEqual(self.sync()['state'], 'complete', 'rotation keeps the cloud position; nothing re-uploaded')
        with closing(sqlite3.connect(self.root / 'cloud/cloud.db')) as conn:
            conn.execute('UPDATE cloud_clients SET ack_seq=ack_seq+100'); conn.commit()
        state = self.sync(); self.assertEqual(state['error']['code'], 'LOCAL_BEHIND_CLOUD')

    def test_backup_restored_cloud_reuploads_from_its_committed_position(self):
        path = self.claude(); self.local.import_history(); self.connect(); self.sync()
        append(path, [{'type': 'assistant', 'uuid': 'a', 'timestamp': TS, 'message': {'content': 'second'}}])
        self.local.import_history(); self.sync()
        with closing(sqlite3.connect(self.root / 'cloud/cloud.db')) as conn:
            conn.execute('UPDATE cloud_clients SET ack_seq=2'); conn.commit()   # simulate an older cloud backup
        self.assertEqual(self.sync()['state'], 'complete')
        self.assertEqual(self.cloud_messages('claude:c')['total'], 2)

    def test_wire_and_cloud_tables_hold_only_analysis_content(self):
        secret = 'FORBIDDEN_PROCESS_BODY_17001'
        self.claude('private', [
            {'type': 'system', 'sessionId': 'private', 'content': 'SYSTEM_' + secret},
            {'type': 'user', 'sessionId': 'private', 'uuid': 'u', 'timestamp': TS, 'message': {'content': [
                {'type': 'text', 'text': 'ordinary question KEEP_USER_KEY'}, {'type': 'image', 'source': {'type': 'base64', 'data': 'IMG_' + secret}}]}},
            {'type': 'assistant', 'uuid': 'a', 'timestamp': TS, 'message': {'content': [{'type': 'thinking', 'thinking': 'THOUGHT_' + secret},
                {'type': 'text', 'text': 'ordinary answer'}, {'type': 'tool_use', 'id': 'call', 'name': 'Agent',
                 'input': {'prompt': 'allowed task', 'api_key': 'KEEP_PARAMETER_SECRET'}}]}},
            {'type': 'progress', 'data': {'type': 'agent_progress', 'messages': [{'role': 'assistant', 'content': 'CHILD_' + secret}]}},
            {'type': 'user', 'uuid': 'out', 'toolUseResult': {'nested': secret}, 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'call', 'content': 'RESULT_' + secret}]}}])
        self.local.import_history(); self.connect(); self.sync()
        wire = b''.join(self.network.bodies).decode()
        self.assertNotIn(secret, wire)
        for kept in ('KEEP_USER_KEY', 'KEEP_PARAMETER_SECRET', 'allowed task', 'ordinary answer'):
            self.assertIn(kept, wire)
        with closing(sqlite3.connect(self.root / 'cloud/cloud.db')) as conn:
            values = [r[0] for r in conn.execute('SELECT metadata FROM sessions')]
            values += [json.dumps(unpack_json(r[0])) for r in conn.execute('SELECT normalized_json FROM messages')]
            values += [r[0] for r in conn.execute('SELECT receipt FROM cloud_receipts')]
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE path<>''").fetchone()[0], 0, 'no local paths in the cloud')
        self.assertNotIn(secret, '\n'.join(values))
        self.assertNotIn(str(self.source), wire, 'source file paths are not uploaded')
        kinds = [m['type'] for m in self.cloud_messages('claude:private')['items']]
        self.assertEqual(kinds, ['user', 'assistant', 'tool-use', 'tool-result'])

    def test_batches_are_dynamic_by_bytes_and_cover_whole_groups(self):
        rows = [{'type': 'user', 'sessionId': 'many', 'uuid': str(i), 'timestamp': TS,
                 'message': {'content': str(i) + ' ' + 'x' * (40 if i < 30 else 900)}} for i in range(60)]
        self.claude('many', rows)
        service = self.make_local('dynamic', batch=4096, import_batch=4096)
        service.import_history(); service.save_connection(ORIGIN, self.key)
        self.assertEqual(service.uploader.run_now()['state'], 'complete')
        bodies = [json.loads(b) for b in self.network.bodies]
        self.assertGreater(len(bodies), 2)
        self.assertGreater(len({sum(len(g['changes']) for g in b['groups']) for b in bodies}), 1, 'not a fixed message count')
        for previous, current in zip(bodies, bodies[1:]):
            self.assertEqual(current['expectedSeq'], previous['throughSeq'])
        self.assertEqual(self.cloud_messages('claude:many', service)['total'], 60)

    def test_real_https_style_transport_over_loopback_http(self):
        from werkzeug.serving import make_server
        server = make_server('127.0.0.1', 0, None, threaded=True)
        origin = f'http://127.0.0.1:{server.server_port}'
        server.app = create_app(self.cloud, session_secret='s' * 48, public_origin=origin, allow_http=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            self.claude('http'); service = LocalService(LocalArchive(self.root / 'real/local.db'), [ClaudeAdapter([self.source])], quiet_seconds=0,
                                                        credential_backend='file', log=lambda *_: None)
            service.import_history(); service.save_connection(origin, self.key)
            self.assertEqual(service.uploader.run_now()['state'], 'complete')
            self.assertEqual(self.cloud.db.get_messages(service.archive.dataset + '/claude:http')['total'], 1)
        finally:
            server.shutdown(); thread.join(5); server.server_close()

    def test_public_cloud_address_rules(self):
        for url in ('http://example.com', 'https://user:pw@example.com', 'https://example.com/path', 'https://example.com?x=1', 'ftp://x'):
            with self.assertRaises(ValueError):
                CloudClient(url, self.key)
        CloudClient('http://localhost:41880', self.key); CloudClient('https://history.example.com/', self.key)


if __name__ == '__main__':
    unittest.main()
