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
        self.assertIn('Agent (read-only API)', owner.get('/', base_url=ORIGIN).text)
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

    def upload(self, key, dataset, sid, text):
        client = self.app.test_client(); headers = {'Authorization': 'Bearer ' + key}
        self.assertEqual(client.post('/ingest/v1/bind', base_url=ORIGIN, headers=headers, json={'datasetId': dataset}).status_code, 200)
        value = batch(0, [('session', sid, session(sid)), ('message', sid, {'message': message(sid, 1, text=text), 'order': [1, 0]})])
        value['datasetId'] = dataset
        response = client.post('/ingest/v1/batches', base_url=ORIGIN, data=json.dumps(value),
                               headers={**headers, 'Content-Type': 'application/json', 'Idempotency-Key': dataset})
        self.assertEqual(response.status_code, 200, response.json)

    def test_agent_api_lists_sessions_metadata_and_pages_messages(self):
        desktop = self.archive.create_key('desktop')['key']
        self.upload(self.key, DATASET, 'claude:same', 'from the laptop')
        self.upload(desktop, 'b' * 32, 'claude:same', 'from the desktop')
        for n in range(3):
            self.upload_more(self.key, DATASET, 2 + 2 * n, f'claude:extra{n}')
        owner, headers = self.owner()
        created = owner.post('/api/keys', base_url=ORIGIN, headers=headers, json={'name': 'analysis agent', 'kind': 'agent'})
        self.assertEqual((created.status_code, created.json['kind']), (201, 'agent'))
        agent = {'Authorization': 'Bearer ' + created.json['key']}; client = self.app.test_client()
        # 1) list every session with a stable keyset cursor
        seen = []; cursor = None
        while True:
            page = client.get('/agent/v1/sessions', base_url=ORIGIN, headers=agent,
                              query_string={'limit': 2, **({'cursor': cursor} if cursor else {})}).json
            self.assertEqual(page['total'], 5); seen += page['items']; cursor = page['nextCursor']
            if not cursor:
                break
        self.assertEqual([s['id'] for s in seen], sorted(s['id'] for s in seen))
        self.assertEqual(len({s['id'] for s in seen}), 5)
        first = next(s for s in seen if s['id'] == DATASET + '/claude:same')
        self.assertEqual(set(first), {'id', 'machine', 'source', 'externalId', 'project', 'name', 'messageCount',
                                      'userMessageCount', 'lastModified', 'available', 'revision'})
        self.assertEqual((first['machine']['name'], first['messageCount'], first['revision']), ('laptop', 1, 1))
        self.assertIn(('desktop', 'b' * 32 + '/claude:same'), [(s['machine']['name'], s['id']) for s in seen])
        # 2) one session's metadata
        sid = DATASET + '/claude:same'
        detail = client.get(f'/agent/v1/sessions/{sid}', base_url=ORIGIN, headers=agent).json['session']
        self.assertEqual(detail['machine']['name'], 'laptop'); self.assertEqual(detail['metadata']['contentPolicy'], 'analysis-v1')
        self.assertEqual(client.get(f'/agent/v1/sessions/{DATASET}/claude:none', base_url=ORIGIN, headers=agent).status_code, 404)
        # 3) paged history, compact message shape, revision guard
        messages = client.get(f'/agent/v1/sessions/{sid}/messages', base_url=ORIGIN, headers=agent, query_string={'limit': 1}).json
        self.assertEqual((messages['total'], messages['revision'], messages['nextCursor']), (1, 1, None))
        item = messages['items'][0]
        self.assertEqual(set(item), {'id', 'order', 'type', 'role', 'timestamp', 'timestampSource', 'content'})
        self.assertEqual(item['content'], [{'type': 'text', 'text': 'from the laptop'}])
        self.assertEqual(client.get(f'/agent/v1/sessions/{sid}/messages', base_url=ORIGIN, headers=agent,
                                    query_string={'revision': 9}).status_code, 409)
        # Read-only and nothing else.
        self.assertEqual(client.post('/agent/v1/sessions', base_url=ORIGIN, headers=agent).status_code, 405)
        self.assertEqual(client.get('/ingest/v1/status', base_url=ORIGIN, headers=agent).status_code, 403)
        for path in ('/api/keys', '/api/sessions', '/api/machines', '/'):
            self.assertIn(client.get(path, base_url=ORIGIN, headers=agent).status_code, (302, 401), path)
        self.assertEqual(client.get('/agent/v1/sessions', base_url=ORIGIN, headers=self.machine).status_code, 403)
        self.assertEqual(client.get('/agent/v1/sessions', base_url=ORIGIN).status_code, 401)
        self.assertEqual(client.get('/agent/v1/sessions?cursor=%%%', base_url=ORIGIN, headers=agent).status_code, 400)
        # The signed-in owner can call the same API.
        self.assertEqual(owner.get('/agent/v1/sessions', base_url=ORIGIN).json['total'], 5)
        listing = owner.get('/api/keys', base_url=ORIGIN).json
        self.assertEqual({k['name']: k['kind'] for k in listing}, {'laptop': 'upload', 'desktop': 'upload', 'analysis agent': 'agent'})
        agent_id = next(k['clientId'] for k in listing if k['kind'] == 'agent')
        self.assertEqual(owner.post(f'/api/keys/{agent_id}/unbind', base_url=ORIGIN, headers=headers).status_code, 404)
        self.assertEqual(owner.post(f'/api/keys/{agent_id}/revoke', base_url=ORIGIN, headers=headers).status_code, 200)
        self.assertEqual(client.get('/agent/v1/sessions', base_url=ORIGIN, headers=agent).status_code, 401)
        self.assertNotIn('analysis agent', json.dumps(owner.get('/api/machines', base_url=ORIGIN).json), 'agent keys are not machines')

    def upload_more(self, key, dataset, expected, sid):
        value = batch(expected, [('session', sid, session(sid)), ('message', sid, {'message': message(sid, 1, text='more'), 'order': [1, 0]})])
        value['datasetId'] = dataset
        response = self.app.test_client().post('/ingest/v1/batches', base_url=ORIGIN, data=json.dumps(value),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Idempotency-Key': sid})
        self.assertEqual(response.status_code, 200, response.json)

    def test_cloud_page_machine_filter_and_date_range(self):
        desktop = self.archive.create_key('desktop')['key']
        self.upload(self.key, DATASET, 'claude:same', 'from the laptop')
        self.upload(desktop, 'b' * 32, 'claude:same', 'from the desktop')
        owner, _ = self.owner()
        machines = owner.get('/api/machines', base_url=ORIGIN).json
        laptop = next(m['id'] for m in machines if m['name'] == 'laptop')
        self.assertEqual(owner.get('/api/sessions', base_url=ORIGIN, query_string={'machine': laptop}).json['total'], 1)
        self.assertIn('machineSelect', owner.get('/', base_url=ORIGIN).text)
        self.assertNotIn('dateFrom', owner.get('/', base_url=ORIGIN).text)

    def test_old_query_keys_become_agent_keys(self):
        import sqlite3
        from contextlib import closing
        old = self.archive.create_key('legacy reader', kind='agent')['key']
        path = Path(self.temp.name) / 'cloud.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE cloud_clients SET kind='query' WHERE name='legacy reader'"); conn.commit()
        self.assertEqual(CloudArchive(path).authenticate(old, 'agent')['kind'], 'agent')

    def test_existing_databases_gain_key_kinds_without_losing_keys(self):
        import sqlite3
        from contextlib import closing
        path = Path(self.temp.name) / 'cloud.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('ALTER TABLE cloud_clients DROP COLUMN kind'); conn.commit()
        reopened = CloudArchive(path)
        self.assertEqual(reopened.authenticate(self.key, 'upload')['kind'], 'upload')

    def test_database_errors_are_classified_and_logged_without_payload(self):
        import sqlite3
        from unittest.mock import patch
        self.ingest('POST', '/ingest/v1/bind', json={'datasetId': DATASET})
        value = batch(0, [('session', 'claude:s', session('claude:s'))])
        cases = [(sqlite3.OperationalError('database or disk is full'), 507, 'STORAGE_FULL', False),
                 (sqlite3.OperationalError('database is locked'), 503, 'ARCHIVE_BUSY', True),
                 (sqlite3.OperationalError('disk I/O error'), 500, 'DATABASE_ERROR', False)]
        for exc, status, code, retryable in cases:
            with self.subTest(code=code), patch.object(CloudArchive, 'commit', side_effect=exc), \
                    self.assertLogs(self.app.logger, 'ERROR') as logs:
                response = self.ingest('POST', '/ingest/v1/batches', data=json.dumps(value),
                                       headers={'Content-Type': 'application/json', 'Idempotency-Key': code})
                self.assertEqual((response.status_code, response.json['error']['code'], response.json['error']['retryable']),
                                 (status, code, retryable))
                self.assertIn(str(exc), logs.output[0]); self.assertNotIn('claude:s', logs.output[0])
        self.assertEqual('Retry-After' in response.headers, False)

    def test_no_task_queue_or_heartbeat_routes_exist(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertFalse(any(word in rule for rule in rules for word in ('jobs', 'heartbeat', 'devices', '/sync/v1')))


if __name__ == '__main__':
    unittest.main()
