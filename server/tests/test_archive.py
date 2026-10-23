"""Server project unit tests; no deamon/local imports or source discovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.archive import CloudArchive
from server.contracts.models import SessionDocument
from server.contracts.protocol import packet,decode,Problem


class ArchiveUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.archive=CloudArchive(self.root/'cloud.db');self.device=self.archive.add_device('unit',control=True)

    def batch(self):
        row={'type':'user','sessionId':'unit','timestamp':'2026-01-01T00:00:00Z','message':{'content':'analysis question'}}
        doc=SessionDocument('claude','unit','/unit',[(0,row)],name='Unit')
        doc.add(0,'user','analysis question',timestamp=row['timestamp'])
        doc.metadata['appendCheckpoint']={'offset':100,'nextLine':1,'prefixFingerprint':'a'*64,'parserVersion':2}
        info={'path':'/unit/session.jsonl','fingerprint':'a'*64,'modified':1000000000000,'parserVersion':2}
        body=packet(doc,info);body['instance']=self.archive.instance
        return json.loads(json.dumps(body))

    def test_commit_atomically_stores_analysis_and_receipt_but_no_raw(self):
        value=self.batch();receipt=self.archive.commit_batch(self.device['deviceId'],'key',value,0)
        self.assertEqual(receipt['messageCount'],1)
        self.assertEqual(self.archive.commit_batch(self.device['deviceId'],'key',value,0),receipt)
        with self.archive.db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dist_receipts').fetchone()[0],1)
        self.assertEqual(self.archive.db.get_sync_checkpoint('claude:unit')['state']['cursor']['offset'],100)

    def test_failure_rolls_back_messages_search_and_progress_together(self):
        value=self.batch()
        with patch.object(self.archive.db,'_store_files',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.archive.commit_batch(self.device['deviceId'],'key',value,0)
        self.assertIsNone(self.archive.receipt(self.device['deviceId'],'key'))
        self.assertIsNone(self.archive.db.get_session('claude:unit'))
        self.assertEqual(self.archive.db.list_sessions(query='analysis')[1],0)
        self.assertEqual(self.archive.db.file_states(),{})

    def test_same_key_changed_payload_and_stale_revision_are_not_accepted(self):
        value=self.batch();self.archive.commit_batch(self.device['deviceId'],'key',value,0)
        with self.assertRaises(Problem):self.archive.commit_batch(self.device['deviceId'],'new',value,0)
        value['document']['name']='different'
        with self.assertRaises(Problem):self.archive.commit_batch(self.device['deviceId'],'key',value,0)

    def test_revoked_device_cannot_read_progress_or_write(self):
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_devices SET active=0')
        with self.assertRaises(Problem):self.archive.authenticate(self.device['token'])
        with self.assertRaises(Problem):self.archive.query_progress(self.device['deviceId'],[{'source':'claude','path':'/unit/s'}])
        with self.assertRaises(Problem):self.archive.commit_batch(self.device['deviceId'],'key',self.batch(),0)

    def test_wire_rejects_hidden_native_channels_thoughts_and_attachments(self):
        for field in ('records','scope_states','raw_scopes'):
            value=self.batch();value['document'][field]={'hidden':'must not store'}
            with self.assertRaises(Problem):decode(value)
        value=self.batch();value['document']['messages'][0]['message']['content']=[{'type':'thinking','thinking':'hidden'}]
        with self.assertRaises(Problem):decode(value)
        value=self.batch();value['document']['sync_state']['header']={'body':'hidden'}
        with self.assertRaises(Problem):decode(value)

    def test_cursor_query_has_no_conversation_body(self):
        self.archive.commit_batch(self.device['deviceId'],'key',self.batch(),0)
        result=self.archive.query_progress(self.device['deviceId'],[{'source':'claude','path':'/unit/session.jsonl'}])
        self.assertEqual(result['files'][0]['checkpoint'],{'offset':100,'nextLine':1})
        self.assertNotIn('analysis question',json.dumps(result))

    def test_other_device_cannot_take_session_ownership(self):
        self.archive.commit_batch(self.device['deviceId'],'key',self.batch(),0)
        other=self.archive.add_device('other')
        with self.assertRaises(Problem):self.archive.context(other['deviceId'],sid='claude:unit')


if __name__=='__main__':unittest.main()
