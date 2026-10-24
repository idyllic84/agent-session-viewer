"""Cloud archive: password, API keys, dataset binding and the ingest stream.

Synthetic batches only; never touches real databases.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from server.archive import CloudArchive, hash_password, verify_password
from server.contracts.protocol import Problem, canonical

DATASET = 'a' * 32
TS = 1792717323000


def message(sid, n, kind='user', text=None, **extra):
    mid = f'{sid}:m{n}'
    role = {'tool-use': 'assistant', 'tool-result': 'tool'}.get(kind, kind)
    if kind in ('user', 'assistant'):
        content = [{'type': 'text', 'text': text or f'text {n}'}]
    elif kind == 'tool-use':
        content = [{'type': 'tool_use', 'id': f'call{n}', 'name': 'shell', 'input': {'command': text or 'ls'}}]
        extra['toolCallId'] = f'call{n}'
    else:
        success = extra.pop('success', None)
        content = [{'type': 'tool_result', 'tool_use_id': f'call{n}', 'success': success,
                    'content': 'success' if success is True else 'failure' if success is False else 'unknown'}]
        extra.update(toolCallId=f'call{n}', status=content[0]['content'])
    return {'id': mid, 'sessionId': sid, 'source': sid.split(':')[0], 'type': kind, 'role': role, 'uuid': mid,
            'parentUuid': None, 'timestamp': TS, 'timestampUnit': 'ms', 'timestampSource': 'source', 'rawRecord': n,
            'message': {'role': role, 'content': content}, **extra}


def session(sid, available=True, name='Session'):
    source, external = sid.split(':', 1)
    return {'id': sid, 'source': source, 'externalId': external, 'project': '/work', 'name': name,
            'lastModified': TS, 'available': available, 'metadata': {'contentPolicy': 'analysis-v1'}}


def batch(expected, *groups):
    out = []; seq = expected
    for index, changes in enumerate(groups):
        items = []
        for op, sid, data in changes:
            seq += 1; items.append({'seq': seq, 'op': op, 'sessionId': sid, 'data': data})
        out.append({'id': f'g{expected}-{index}', 'firstSeq': items[0]['seq'], 'lastSeq': seq, 'changes': items})
    return {'protocolVersion': 6, 'datasetId': DATASET, 'expectedSeq': expected, 'throughSeq': seq, 'groups': out}


class CloudArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.archive = CloudArchive(self.root / 'cloud.db')
        self.key = self.archive.create_key('laptop'); self.client = self.archive.authenticate(self.key['key'])
        self.archive.bind(self.client, DATASET)

    def commit(self, value, key=None):
        return self.archive.commit(self.client, key or canonical(value)[:150], value)

    def cid(self, sid):
        return DATASET + '/' + sid

    def test_password_hash_and_rotation_invalidate_versions(self):
        stored = hash_password('correct horse battery')
        self.assertTrue(stored.startswith('scrypt$')); self.assertNotIn('correct', stored)
        self.assertTrue(verify_password('correct horse battery', stored)); self.assertFalse(verify_password('wrong', stored))
        with self.assertRaises(Problem):
            self.archive.set_password('short', require_current=False)
        version = self.archive.set_password('first password!', require_current=False)
        with self.assertRaises(Problem):
            self.archive.set_password('second password!', current='nope')
        self.assertGreater(self.archive.set_password('second password!', current='first password!'), version)
        self.assertTrue(self.archive.check_password('second password!')); self.assertFalse(self.archive.check_password('first password!'))

    def test_keys_rotate_revoke_expire_and_never_store_secret(self):
        secret = self.key['key'].split('_', 2)[2]
        with closing(sqlite3.connect(self.root / 'cloud.db')) as conn:
            dump = '\n'.join(str(r) for r in conn.execute('SELECT * FROM cloud_clients'))
        self.assertNotIn(secret, dump)
        for bad in ('', 'asv_x_y', self.key['key'] + 'x', 'Bearer ' + self.key['key']):
            with self.assertRaises(Problem):
                self.archive.authenticate(bad)
        self.commit(batch(0, [('session', 'claude:s', session('claude:s'))]))
        rotated = self.archive.rotate_key(self.client['id'])
        with self.assertRaises(Problem):
            self.archive.authenticate(self.key['key'])
        client = self.archive.authenticate(rotated['key'])
        self.assertEqual(self.archive.status(client, DATASET)['ackSeq'], 1, 'rotation keeps upload progress')
        self.archive.revoke_key(client['id'])
        with self.assertRaises(Problem):
            self.archive.authenticate(rotated['key'])
        expiring = self.archive.create_key('temp', expires_days=1)
        with closing(sqlite3.connect(self.root / 'cloud.db')) as conn:
            conn.execute('UPDATE cloud_clients SET expires=? WHERE name=?', (time.time() - 1, 'temp')); conn.commit()
        with self.assertRaises(Problem):
            self.archive.authenticate(expiring['key'])

    def test_dataset_binding_is_exclusive_and_explicitly_resettable(self):
        with self.assertRaises(Problem) as error:
            self.archive.bind(self.client, 'b' * 32)
        self.assertEqual(error.exception.code, 'DATASET_MISMATCH')
        other = self.archive.authenticate(self.archive.create_key('desktop')['key'])
        with self.assertRaises(Problem):
            self.archive.bind(other, DATASET)
        self.assertIsNone(self.archive.status(other, DATASET)['ackSeq'])
        self.archive.unbind_key(self.client['id'])
        self.assertEqual(self.archive.bind(self.client, 'b' * 32)['boundDataset'], 'b' * 32)

    def test_sequence_cas_idempotent_receipts_and_mismatch(self):
        first = batch(0, [('session', 'claude:s', session('claude:s')), ('message', 'claude:s', {'message': message('claude:s', 1), 'order': [1, 0]})])
        receipt = self.commit(first, 'k1')
        self.assertEqual(receipt['ackSeq'], 2)
        self.assertEqual(self.commit(first, 'k1'), receipt, 'same key + same body returns the original receipt')
        with self.assertRaises(Problem) as error:
            self.commit(first, 'k2')
        self.assertEqual(error.exception.code, 'SEQ_CONFLICT')
        changed = json.loads(json.dumps(first)); changed['groups'][0]['changes'][0]['data']['name'] = 'other'
        with self.assertRaises(Problem) as error:
            self.commit(changed, 'k1')
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_MISMATCH')
        self.assertEqual(self.archive.db.get_messages(self.cid('claude:s'))['total'], 1)

    def test_strict_shape_rejects_gaps_forbidden_kinds_and_extra_detail(self):
        good = batch(0, [('session', 'claude:s', session('claude:s')), ('message', 'claude:s', {'message': message('claude:s', 1), 'order': [1, 0]})])
        variants = []
        gap = json.loads(json.dumps(good)); gap['groups'][0]['changes'][1]['seq'] = 5; variants.append(gap)
        partial = json.loads(json.dumps(good)); partial['groups'][0]['changes'].pop(); partial['throughSeq'] = 1; variants.append(partial)
        thought = json.loads(json.dumps(good)); thought['groups'][0]['changes'][1]['data']['message']['message']['content'] = [{'type': 'thinking', 'thinking': 'NO'}]; variants.append(thought)
        output = json.loads(json.dumps(good)); output['groups'][0]['changes'][1]['data']['message']['toolOutput'] = 'NO'; variants.append(output)
        raw = json.loads(json.dumps(good)); raw['groups'][0]['changes'][0]['data']['metadata']['toolCallResults'] = 'NO'; variants.append(raw)
        path = json.loads(json.dumps(good)); path['groups'][0]['changes'][0]['data']['path'] = 'C:/x'; variants.append(path)
        system = json.loads(json.dumps(good)); system['groups'][0]['changes'][1]['data']['message']['type'] = 'system'; variants.append(system)
        result = batch(0, [('session', 'claude:s', session('claude:s')), ('message', 'claude:s', {'message': message('claude:s', 2, 'tool-result', success=True), 'order': [2, 0]})])
        result['groups'][0]['changes'][1]['data']['message']['message']['content'][0]['content'] = 'stdout body'; variants.append(result)
        orphan = batch(0, [('message', 'claude:x', {'message': message('claude:x', 1), 'order': [1, 0]})]); variants.append(orphan)
        foreign = json.loads(json.dumps(good)); foreign['datasetId'] = 'c' * 32; variants.append(foreign)
        for index, value in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(Problem):
                self.commit(value, f'bad{index}')
        self.assertEqual(self.archive.status(self.client, DATASET)['ackSeq'], 0)
        self.assertEqual(self.archive.db.list_sessions()[1], 0)

    def test_batch_is_atomic_including_sequence_and_receipt(self):
        value = batch(0, [('session', 'claude:s', session('claude:s')), ('message', 'claude:s', {'message': message('claude:s', 1), 'order': [1, 0]})])
        with patch.object(CloudArchive, 'reorder', side_effect=RuntimeError('crash after messages')):
            with self.assertRaises(RuntimeError):
                self.commit(value, 'atomic')
        self.assertEqual(self.archive.status(self.client, DATASET)['ackSeq'], 0)
        self.assertIsNone(self.archive.receipt(self.client, 'atomic'))
        self.assertEqual(self.archive.db.list_sessions(query='text 1')[1], 0)
        self.commit(value, 'atomic'); self.assertEqual(self.archive.db.list_sessions(query='text 1')[1], 1)

    def test_upserts_ordering_counts_deletes_and_search(self):
        sid = 'pi:p'
        self.commit(batch(0, [('session', sid, session(sid)),
                              ('message', sid, {'message': message(sid, 1, text='first question'), 'order': [1, 0]}),
                              ('message', sid, {'message': message(sid, 2, 'tool-use'), 'order': [2, 0]}),
                              ('message', sid, {'message': message(sid, 3, 'tool-result'), 'order': [2, 1]})]))
        self.commit(batch(4, [('message', sid, {'message': message(sid, 5, 'assistant', text='appended answer'), 'order': [5, 0]})]))
        self.commit(batch(5, [('message', sid, {'message': message(sid, 0, text='earlier question'), 'order': [0, 0]})]))
        self.commit(batch(6, [('message', sid, {'message': message(sid, 3, 'tool-result', success=False), 'order': [2, 1]})]))
        page = self.archive.db.get_messages(self.cid(sid))
        self.assertEqual([m['orderId'] for m in page['items']], [0, 1, 2, 3, 4])
        self.assertEqual([m['id'].split(':')[-1] for m in page['items']], ['m0', 'm1', 'm2', 'm3', 'm5'])
        self.assertEqual(page['items'][3]['status'], 'failure')
        self.assertEqual(page['session']['userMessageCount'], 2); self.assertEqual(page['session']['messageCount'], 5)
        self.assertTrue(all(m['sessionId'] == self.cid(sid) and m['id'].startswith(self.cid(sid)) for m in page['items']))
        self.commit(batch(7, [('delete_message', sid, {'id': f'{sid}:m0'}), ('session', sid, session(sid, available=False, name='Renamed'))]))
        self.assertEqual(self.archive.db.list_sessions(query='earlier question')[1], 0)
        self.assertEqual(self.archive.db.list_sessions(query='appended answer')[1], 1)
        saved = self.archive.db.get_session(self.cid(sid))
        self.assertEqual((saved['messageCount'], saved['userMessageCount'], saved['name'], saved['available']), (4, 1, 'Renamed', False))
        self.assertEqual(self.archive.session_revision(self.cid(sid)), 5)

    def test_sessions_are_namespaced_per_dataset(self):
        self.commit(batch(0, [('session', 'claude:same', session('claude:same', name='mine'))]))
        other = self.archive.authenticate(self.archive.create_key('desktop')['key']); self.archive.bind(other, 'b' * 32)
        value = batch(0, [('session', 'claude:same', session('claude:same', name='theirs'))]); value['datasetId'] = 'b' * 32
        self.archive.commit(other, 'other', value)
        names = sorted(s['name'] for s in self.archive.db.list_sessions(query='')[0] + [self.archive.db.get_session('b' * 32 + '/claude:same'), self.archive.db.get_session(self.cid('claude:same'))] if s)
        self.assertEqual(names, ['mine', 'theirs'])

    def test_old_task_queue_and_foreign_databases_are_not_modified(self):
        old = self.root / 'v5.db'
        with closing(sqlite3.connect(old)) as conn:
            conn.execute('CREATE TABLE dist_settings(key,value)'); conn.execute('CREATE TABLE sessions(id)'); conn.commit()
        before = old.read_bytes()
        with self.assertRaisesRegex(ValueError, 'retired'):
            CloudArchive(old)
        self.assertEqual(old.read_bytes(), before)
        foreign = self.root / 'foreign.db'
        with closing(sqlite3.connect(foreign)) as conn:
            conn.execute('CREATE TABLE change_log(seq)'); conn.commit()
        with self.assertRaises(ValueError):
            CloudArchive(foreign)


if __name__ == '__main__':
    unittest.main()
