"""Cloud HTTP boundaries: owner session vs. upload API key, CSRF, limits."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from server.app import create_app
from server.archive import CloudArchive
from server.tests.test_archive import batch, session, message, DATASET

ORIGIN = 'https://cloud.test'


class CloudAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.archive = CloudArchive(Path(self.temp.name) / 'cloud.db', max_request=1024 * 1024)
        self.archive.set_password('owner password 123', require_current=False)
        self.app = create_app(self.archive, session_secret='s' * 48, public_origin=ORIGIN)
        self.key = self.archive.create_key('laptop')['key']
        self.machine = {'Authorization': 'Bearer ' + self.key}

    def owner(self):
        client = self.app.test_client()
        page = client.get('/auth/login', base_url=ORIGIN)
        nonce = re.search('name="nonce" value="([^"]+)"', page.text).group(1)
        self.assertEqual(client.post('/auth/login', base_url=ORIGIN, headers={'Origin': ORIGIN},
                                     data={'nonce': nonce, 'password': 'owner password 123'}).status_code, 302)
        csrf = re.search('name="cloud-csrf" content="([^"]+)"', client.get('/', base_url=ORIGIN).text).group(1)
        return client, {'X-CSRF-Token': csrf, 'Origin': ORIGIN}

    def ingest(self, method, path, **kwargs):
        return self.app.test_client().open(path, method=method, base_url=ORIGIN, headers={**self.machine, **kwargs.pop('headers', {})}, **kwargs)

    def test_api_key_cannot_browse_and_owner_cookie_cannot_ingest(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/api/sessions', base_url=ORIGIN, headers=self.machine).status_code, 401)
        self.assertEqual(client.get('/api/keys', base_url=ORIGIN, headers=self.machine).status_code, 401)
        owner, headers = self.owner()
        self.assertEqual(owner.get('/ingest/v1/status', base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.ingest('GET', '/ingest/v1/status', query_string={'datasetId': DATASET}).status_code, 200)
        self.assertEqual(self.app.test_client().get('/ingest/v1/status', base_url=ORIGIN, headers={'Authorization': 'Bearer nope'}).status_code, 401)

    def test_owner_console_requires_csrf_and_same_origin_and_hides_hashes(self):
        owner, headers = self.owner()
        self.assertEqual(owner.post('/api/keys', base_url=ORIGIN, json={'name': 'x'}).status_code, 403)
        self.assertEqual(owner.post('/api/keys', base_url=ORIGIN, json={'name': 'x'}, headers={**headers, 'Origin': 'https://evil.test'}).status_code, 403)
        created = owner.post('/api/keys', base_url=ORIGIN, json={'name': 'desktop'}, headers=headers)
        self.assertEqual(created.status_code, 201); self.assertTrue(created.json['key'].startswith('asv_'))
        listing = owner.get('/api/keys', base_url=ORIGIN)
        self.assertNotIn(created.json['key'].split('_', 2)[2], listing.text); self.assertNotIn('key_hash', listing.text)
        self.assertIn('Upload API keys', owner.get('/', base_url=ORIGIN).text)
        self.assertEqual(owner.post('/auth/logout', base_url=ORIGIN, headers=headers).status_code, 200)
        self.assertEqual(owner.get('/api/keys', base_url=ORIGIN).status_code, 401)

    def test_password_change_signs_out_other_sessions_and_login_is_rate_limited(self):
        first, headers = self.owner(); second, _ = self.owner()
        self.assertEqual(first.post('/api/account/password', base_url=ORIGIN, headers=headers,
                                    json={'currentPassword': 'owner password 123', 'newPassword': 'new owner password'}).status_code, 200)
        self.assertEqual(first.get('/api/keys', base_url=ORIGIN).status_code, 200)
        self.assertEqual(second.get('/api/keys', base_url=ORIGIN).status_code, 401)
        client = self.app.test_client(); statuses = []
        for _ in range(11):
            nonce = re.search('name="nonce" value="([^"]+)"', client.get('/auth/login', base_url=ORIGIN).text).group(1)
            statuses.append(client.post('/auth/login', base_url=ORIGIN, headers={'Origin': ORIGIN}, data={'nonce': nonce, 'password': 'guess guess guess'}).status_code)
        self.assertEqual(statuses[-1], 429)

    def test_ingest_flow_paging_revision_and_size_limit(self):
        bound = self.ingest('POST', '/ingest/v1/bind', json={'datasetId': DATASET}).json
        self.assertEqual((bound['boundDataset'], bound['ackSeq']), (DATASET, 0))
        value = batch(0, [('session', 'claude:s', session('claude:s')), ('message', 'claude:s', {'message': message('claude:s', 1), 'order': [1, 0]})])
        response = self.ingest('POST', '/ingest/v1/batches', data=json.dumps(value), headers={'Content-Type': 'application/json', 'Idempotency-Key': 'k'})
        self.assertEqual(response.status_code, 200, response.json); self.assertEqual(response.json['ackSeq'], 2)
        self.assertEqual(self.ingest('GET', '/ingest/v1/receipts/k').json['receipt']['ackSeq'], 2)
        conflict = self.ingest('POST', '/ingest/v1/batches', data=json.dumps(value), headers={'Content-Type': 'application/json', 'Idempotency-Key': 'other'})
        self.assertEqual((conflict.status_code, conflict.json['error']['code']), (409, 'SEQ_CONFLICT'))
        huge = self.ingest('POST', '/ingest/v1/batches', data=b'x' * (1024 * 1024 + 10), headers={'Content-Type': 'application/json', 'Idempotency-Key': 'h'})
        self.assertEqual((huge.status_code, huge.json['error']['code']), (413, 'TOO_LARGE'))
        owner, _ = self.owner(); sid = DATASET + '/claude:s'
        listed = owner.get('/api/sessions', base_url=ORIGIN).json['items'][0]
        self.assertEqual((listed['id'], listed['uploadedBy']), (sid, 'laptop'))
        page = owner.get(f'/api/sessions/{sid}/messages', base_url=ORIGIN).json
        self.assertEqual(page['sessionRevision'], 1)
        self.assertEqual(owner.get(f'/api/sessions/{sid}/messages?cursor=1&revision=0', base_url=ORIGIN).status_code, 409)
        for path in (f'/api/sessions/{sid}/records', '/api/messages/x/raw'):
            self.assertEqual(owner.get(path, base_url=ORIGIN).status_code, 410)

    def test_no_task_queue_or_heartbeat_routes_exist(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertFalse(any(word in rule for rule in rules for word in ('jobs', 'heartbeat', 'devices', '/sync/v1')))


if __name__ == '__main__':
    unittest.main()
