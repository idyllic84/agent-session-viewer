"""Only user/assistant prose should contribute to left-side session search."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local.db import SEARCH_POLICY_VERSION
from local.models import indexed_message_text
from local.service import ClaudeViewerService
from local.sources.claude import ClaudeAdapter


class SearchRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'s.jsonl'
        self.rows=[
            {'type':'user','sessionId':'s','uuid':'u','message':{'content':'USER_UNIQUE question'}},
            {'type':'assistant','uuid':'a','message':{'content':[{'type':'text','text':'ASSISTANT_UNIQUE answer'},
             {'type':'thinking','thinking':'THINKING_UNIQUE reasoning'},
             {'type':'tool_use','name':'TOOL_NAME_UNIQUE','id':'call','input':{'path':'TOOL_ARGS_UNIQUE'}}]}},
            {'type':'system','content':'SYSTEM_UNIQUE event'},
            {'type':'user','uuid':'tool','message':{'content':[{'type':'tool_result','tool_use_id':'call','content':'TOOL_OUTPUT_UNIQUE'}]}}]
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in self.rows),encoding='utf-8')
        self.viewer=ClaudeViewerService(self.root/'archive.db',[ClaudeAdapter([self.root])],skip_recent_seconds=0)
        self.viewer.initialize();self.viewer.sync_sessions()

    def test_search_matches_only_user_assistant_and_clear_thinking(self):
        for term in ('USER_UNIQUE','ASSISTANT_UNIQUE','THINKING_UNIQUE'):
            self.assertEqual(len(self.viewer.search_sessions_by_content(term)),1,term)
        for term in ('SYSTEM_UNIQUE','TOOL_NAME_UNIQUE','TOOL_ARGS_UNIQUE','TOOL_OUTPUT_UNIQUE'):
            self.assertEqual(self.viewer.search_sessions_by_content(term),[],term)
        self.assertEqual(self.viewer.get_messages('claude:s')['total'],4)
        records=self.viewer.get_records('claude:s')['items']
        self.assertEqual(records[2]['record'],self.rows[2])
        self.assertEqual(records[3]['record'],self.rows[3])

    def test_tool_only_assistant_and_tool_result_user_role_not_indexed(self):
        self.assertEqual(indexed_message_text({'role':'assistant','message':{'content':[{'type':'tool_use','name':'read','input':{'text':'secret'}}]}}),'')
        self.assertEqual(indexed_message_text({'role':'user','message':{'content':[{'type':'tool_result','content':'output'}]}}),'')
        self.assertEqual(indexed_message_text({'role':'system','message':{'content':[{'type':'text','text':'system'}]}}),'')

    def test_migration_removes_old_system_index_without_changing_messages_raw_or_checkpoints(self):
        database=self.viewer._db
        with database.get_connection() as conn:
            before=[tuple(row) for row in conn.execute('select message_pk,normalized_json from messages order by message_pk')]
            raw_before=[tuple(row) for row in conn.execute('select record_index,raw_json from raw_records order by record_index')]
            old_pk=before[2][0]
            database._store_search_text(conn,old_pk,'SYSTEM_UNIQUE legacy text')
            conn.execute("update archive_settings set value='old-everything' where key='search_policy'")
        self.assertEqual(len(self.viewer.search_sessions_by_content('SYSTEM_UNIQUE')),1)
        states=database.file_states()
        self.assertEqual(database.ensure_search_policy(),2)
        self.assertEqual(self.viewer.search_sessions_by_content('SYSTEM_UNIQUE'),[])
        self.assertEqual(len(self.viewer.search_sessions_by_content('ASSISTANT_UNIQUE')),1)
        with database.get_connection() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('select message_pk,normalized_json from messages order by message_pk')],before)
            self.assertEqual([tuple(row) for row in conn.execute('select record_index,raw_json from raw_records order by record_index')],raw_before)
            self.assertEqual(conn.execute("select value from archive_settings where key='search_policy'").fetchone()[0],SEARCH_POLICY_VERSION)
            if database.has_trigram:conn.execute("insert into message_search(message_search,rank) values('integrity-check',1)")
        self.assertEqual(database.file_states(),states)
        self.assertEqual(database.ensure_search_policy(),0)

    def test_migration_failure_rolls_back_old_index_and_trigger_ddl(self):
        database=self.viewer._db
        with database.get_connection() as conn:
            conn.execute("update archive_settings set value='old-everything' where key='search_policy'")
        with patch('local.db.indexed_message_text',side_effect=RuntimeError('simulated migration failure')):
            with self.assertRaisesRegex(RuntimeError,'simulated'):
                database.ensure_search_policy()
        self.assertEqual(len(self.viewer.search_sessions_by_content('ASSISTANT_UNIQUE')),1)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("select value from archive_settings where key='search_policy'").fetchone()[0],'old-everything')
            self.assertIsNotNone(conn.execute("select name from sqlite_master where name='content_search_insert'").fetchone())
        database.ensure_search_policy()


if __name__=='__main__':unittest.main()
