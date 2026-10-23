"""Legacy migration preserves old records without touching native watermarks."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local.db import Database
from local.migrate_legacy import migrate, project_legacy, read_legacy
from local.service import ClaudeViewerService
from local.sources.claude import ClaudeAdapter


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'old.db';self.target=self.root/'viewer.db'
        self.native=self.root/'native';self.native.mkdir()
        self.rows={
            'native':[{'type':'user','uuid':'n','sessionId':'native','cwd':'D:/Project','isSidechain':False,'message':{'content':'native conversation'}}],
            'lost':[{'type':'user','uuid':'u','sessionId':'lost','cwd':'D:/Project','isSidechain':False,'timestamp':'2026-06-11T02:42:59Z','message':{'content':'voice agent 情感分析'}},
                    {'type':'assistant','uuid':'a','parentUuid':'u','isSidechain':False,'message':{'content':[{'type':'text','text':'报告在 D:/Zero/stt-emotion-feasibility.md'}, {'type':'tool_use','name':'write','id':'call','input':{'text':'EXCLUDED_TOOL_PAYLOAD'}}]}},
                    {'type':'user','uuid':'t','isSidechain':False,'message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'EXCLUDED_TOOL_RESULT'}]}},
                    {'type':'system','uuid':'sys','content':'EXCLUDED_SYSTEM'}],
            'system-only':[{'type':'system','content':'system-only-archive'}],
            'agent-child':[{'type':'user','message':{'content':'do task'}}],
            'sidechain':[{'type':'user','isSidechain':True,'message':{'content':'subagent task'}}],
            'empty':[]}
        conn=sqlite3.connect(self.source)
        conn.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,project TEXT,message_count INTEGER,last_update_time INTEGER);
            CREATE TABLE messages(session_id TEXT,order_id INTEGER,timestamp INTEGER,type TEXT,raw_json TEXT,PRIMARY KEY(session_id,order_id));''')
        for sid,rows in self.rows.items():
            conn.execute('insert into sessions values(?,?,?,?)',(sid,'D--Project',len(rows),1782538106))
            conn.executemany('insert into messages values(?,?,?,?,?)',((sid,i,1781145779,row['type'],json.dumps(row,ensure_ascii=False)) for i,row in enumerate(rows)))
        conn.commit();conn.close()
        self.original=(self.native/'native.jsonl');self.original.write_text(json.dumps(self.rows['native'][0])+'\n',encoding='utf-8')
        self.viewer=ClaudeViewerService(self.target,[ClaudeAdapter([self.native])],skip_recent_seconds=0)
        self.viewer.initialize();self.viewer.sync_sessions()
        self.source_hash=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_migration(self):
        with patch('local.migrate_legacy.assert_offline'):
            return migrate(self.source,self.target,self.root/'result.log')

    def test_migrates_missing_preserves_native_and_source_with_idempotence(self):
        before=self.viewer._db.file_states();report=self.run_migration()
        self.assertEqual(report['imported'],2);self.assertEqual(report['existingSkipped'],1)
        self.assertEqual(report['excludedSubagents'],2);self.assertEqual(report['excludedEmpty'],1)
        self.assertEqual(report['coverage']['coveredSessions'],3)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.source_hash)
        self.assertTrue(Path(report['backup']).exists())
        db=Database(self.target);db.init_schema()
        self.assertEqual(db.file_states(),before)
        lost=db.get_session('claude:lost');self.assertFalse(lost['available'])
        self.assertEqual(lost['messageCount'],4);self.assertEqual(lost['userMessageCount'],1)
        self.assertEqual(lost['project'],'D:/Project')
        self.assertEqual(lost['metadata']['legacyArchive']['recordCount'],4)
        self.assertIsNone(db.get_sync_checkpoint('claude:lost'))
        self.assertEqual([item['record'] for item in db.get_records('claude:lost')['items']],self.rows['lost'])
        self.assertEqual(db.list_sessions(query='情感分析')[1],1)
        for query in ('EXCLUDED_TOOL_PAYLOAD','EXCLUDED_TOOL_RESULT','EXCLUDED_SYSTEM'):
            self.assertEqual(db.list_sessions(query=query)[1],0)
        self.assertEqual(db.list_sessions()[1],2)  # system-only preserved, hidden
        self.assertIsNotNone(db.get_session('claude:system-only'))
        again=self.run_migration();self.assertEqual(again['imported'],0)
        self.assertEqual(again['existingSkipped'],3);self.assertEqual(again['sessionsAfter'],3)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.source_hash)

    def test_followup_sync_keeps_archive_and_reappearing_native_can_reconcile(self):
        self.run_migration()
        self.viewer.initialize();self.viewer.sync_sessions()
        self.assertIsNotNone(self.viewer._db.get_session('claude:lost'))
        rows=self.rows['lost']+[{'type':'user','uuid':'new','sessionId':'lost','message':{'content':'new question'}}]
        (self.native/'lost.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        result=self.viewer.sync_sessions();self.assertEqual(result['failed'],0)
        session=self.viewer._db.get_session('claude:lost');self.assertTrue(session['available'])
        self.assertEqual(session['messageCount'],5);self.assertEqual(session['userMessageCount'],2)
        self.assertNotIn('legacyArchive',session['metadata'])

    def test_corrupt_legacy_leaves_target_and_source_untouched(self):
        c=sqlite3.connect(self.source);c.execute("update messages set raw_json='broken' where session_id='lost' and order_id=1");c.commit();c.close()
        before=hashlib.sha256(self.target.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):self.run_migration()
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(),before)
        self.assertIsNone(self.viewer._db.get_session('claude:lost'))

    def test_conflicting_overlap_fails_instead_of_overwriting_native(self):
        c=sqlite3.connect(self.source)
        c.execute("update messages set raw_json=? where session_id='native'",(json.dumps({'type':'user','message':{'content':'older missing branch'}}),));c.commit();c.close()
        before=hashlib.sha256(self.target.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError,'lacks older legacy records'):self.run_migration()
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(),before)
        self.assertIsNone(self.viewer._db.get_session('claude:lost'))

    def test_live_server_refused_and_same_database_rejected(self):
        with patch('local.migrate_legacy.assert_offline',side_effect=RuntimeError('Viewer server is running')):
            with self.assertRaisesRegex(RuntimeError,'server is running'):migrate(self.source,self.target)
        with self.assertRaises(ValueError):migrate(self.source,self.source)

    def test_original_indices_and_legacy_timestamp_column_are_preserved(self):
        c=sqlite3.connect(self.source);c.execute("update messages set order_id=12 where session_id='system-only'");c.commit();c.close()
        legacy=read_legacy(self.source)
        try:
            row=legacy.execute("select * from sessions where id='system-only'").fetchone()
            doc,_=project_legacy(legacy,row,self.source)
            self.assertEqual(doc.records[0][0],12)
            self.assertEqual(doc.messages[0]['rawRecord'],12)
            self.assertEqual(doc.metadata['legacyArchive']['timestampColumn'],[[12,1781145779]])
        finally:legacy.close()


if __name__=='__main__':unittest.main()
