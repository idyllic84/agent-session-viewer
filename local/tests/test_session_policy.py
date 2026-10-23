"""Empty/subagent archives are not sessions; list counts are user inputs only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local.db import Database
from local.service import ClaudeViewerService
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter
from local.sources.codex import CodexAdapter
from local.sources.vscode_chat import VSCodeChatAdapter
from local.session_policy import is_subagent


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def viewer(self,adapters):
        service=ClaudeViewerService(self.root/'archive.db',adapters,skip_recent_seconds=0);service.initialize();return service

    def test_empty_file_is_cached_without_creating_session_then_grows_normally(self):
        path=self.root/'empty.json';path.write_text(json.dumps({'sessionId':'empty','requests':[]}),encoding='utf-8')
        adapter=VSCodeChatAdapter([path]);viewer=self.viewer([adapter])
        result=viewer.sync_sessions();self.assertEqual(result['skippedEmpty'],1)
        self.assertEqual(viewer.get_all_sessions(),[])
        with patch.object(adapter,'parse',side_effect=AssertionError('Empty cache should avoid parsing')):
            self.assertEqual(viewer.sync_sessions()['skippedEmpty'],1)
        path.write_text(json.dumps({'sessionId':'empty','requests':[{'message':{'text':'now nonempty'},'response':[]}]}),encoding='utf-8')
        self.assertEqual(viewer.sync_sessions()['added'],1)
        self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],1)

    def test_existing_empty_archive_is_removed_but_original_file_remains(self):
        path=self.root/'empty.json';path.write_text(json.dumps({'sessionId':'empty','requests':[]}),encoding='utf-8')
        adapter=VSCodeChatAdapter([path]);viewer=self.viewer([adapter]);entry=SourceFile(path)
        doc=adapter.parse(entry);fp=entry.fingerprint()
        viewer._db.replace_session(doc,path,fp,1,adapter.parser_version,[(str(path),'vscode_chat',doc.id,fp,adapter.parser_version,1,0)])
        self.assertEqual(viewer._db.list_sessions()[1],0)
        self.assertEqual(viewer._db.prune_empty_sessions(),1)
        self.assertIsNone(viewer._db.get_session(doc.id));self.assertTrue(path.exists())
        self.assertIn(str(path),viewer._db.file_states())

    def test_claude_subagent_skipped_before_body_hash_and_not_listed(self):
        path=self.root/'agent-child.jsonl';path.write_text('not even parsed\n',encoding='utf-8')
        adapter=ClaudeAdapter([self.root]);viewer=self.viewer([adapter])
        with patch.object(SourceFile,'fingerprint',side_effect=AssertionError('No subagent hash')), \
             patch.object(adapter,'parse',side_effect=AssertionError('No subagent parse')):
            result=viewer.sync_sessions()
        self.assertEqual(result['skippedSubagents'],1);self.assertEqual(result['failed'],0)
        self.assertEqual(viewer.get_all_sessions(),[]);self.assertTrue(path.exists())

    def test_codex_header_subagent_skipped_without_full_parse_fork_kept(self):
        child=self.root/'child.jsonl';fork=self.root/'fork.jsonl'
        child.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','source':{'subagent':{'thread_spawn':{'parent_thread_id':'parent'}}}}})+'\n',encoding='utf-8')
        fork.write_text(json.dumps({'type':'session_meta','payload':{'id':'fork','forked_from_id':'parent'}})+'\n'+
                        json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'fork task'}]}})+'\n',encoding='utf-8')
        viewer=self.viewer([CodexAdapter([self.root])]);result=viewer.sync_sessions()
        self.assertEqual(result['skippedSubagents'],1)
        self.assertEqual([s['id'] for s in viewer.get_all_sessions()],['codex:fork'])
        self.assertFalse(is_subagent('pi',self.root/'normal.jsonl',header={'type':'session','parentSession':'fork-parent.jsonl'}))

    def test_pruning_old_subagent_index_retains_parent_shared_search_text(self):
        parent=self.root/'parent.jsonl';child=self.root/'agent-child.jsonl'
        record={'type':'user','sessionId':'parent','uuid':'u','message':{'content':'shared text'}}
        for path in (parent,child):path.write_text(json.dumps(record)+'\n',encoding='utf-8')
        adapter=ClaudeAdapter([self.root]);viewer=self.viewer([adapter]);viewer.sync_sessions()
        doc=adapter.parse(SourceFile(child));fp=SourceFile(child).fingerprint()
        viewer._db.replace_session(doc,child,fp,1,2,[(str(child),'claude',doc.id,fp,2,1,len(doc.messages))])
        self.assertIsNone(viewer._db.get_session(doc.id))
        self.assertEqual(viewer._db.prune_subagent_sessions(),1)
        self.assertEqual([s['id'] for s in viewer.search_sessions_by_content('shared text')],['claude:parent'])
        self.assertTrue(child.exists())

    def test_user_count_excludes_claude_tool_results_and_is_incremental(self):
        path=self.root/'session.jsonl'
        records=[{'type':'user','sessionId':'s','uuid':'u','message':{'content':'question'}},
                 {'type':'assistant','uuid':'a','message':{'content':[{'type':'tool_use','id':'c','name':'read','input':{}}]}},
                 {'type':'user','uuid':'t','message':{'content':[{'type':'tool_result','tool_use_id':'c','content':'output'}]}},
                 {'type':'system','content':'notification'}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
        viewer=self.viewer([ClaudeAdapter([self.root])]);viewer.sync_sessions()
        session=viewer.get_all_sessions()[0];self.assertEqual(session['messageCount'],4);self.assertEqual(session['userMessageCount'],1)
        with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'type':'user','sessionId':'s','uuid':'next','message':{'content':'next question'}})+'\n')
        self.assertEqual(viewer.sync_sessions()['appendedMessages'],1)
        self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],2)
        with viewer._db.get_connection() as conn:
            meta=json.loads(conn.execute('select metadata from sessions').fetchone()[0]);meta.pop('userMessageCount')
            conn.execute('update sessions set metadata=?',(json.dumps(meta),))
        self.assertEqual(viewer._db.backfill_user_counts(),1)
        self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],2)

    def test_zero_user_nonempty_session_is_hidden_but_retained_and_can_grow(self):
        path=self.root/'system.jsonl'
        path.write_text(json.dumps({'type':'system','sessionId':'s','content':'system only'})+'\n',encoding='utf-8')
        viewer=self.viewer([ClaudeAdapter([self.root])]);viewer.sync_sessions()
        self.assertEqual(viewer.get_all_sessions(),[])
        self.assertEqual(viewer._db.get_session('claude:s')['messageCount'],1)
        self.assertEqual(viewer._db.prune_empty_sessions(),0)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'user','sessionId':'s','uuid':'u','message':{'content':'real user'}})+'\n')
        self.assertEqual(viewer.sync_sessions()['appendedMessages'],1)
        session=viewer.get_all_sessions()[0]
        self.assertEqual((session['userMessageCount'],session['messageCount']),(1,2))

    def test_vscode_dirty_request_updates_user_count_not_tool_or_chunk_count(self):
        path=self.root/'vs.jsonl'
        state={'sessionId':'vs','requests':[{'message':{'text':'first'},'response':[{'value':'a'},{'value':'b'}]}]}
        path.write_text(json.dumps({'kind':0,'v':state})+'\n',encoding='utf-8')
        viewer=self.viewer([VSCodeChatAdapter([path])]);viewer.sync_sessions()
        self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],1)
        with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'kind':2,'k':['requests'],'v':[{'message':{'text':'second'},'response':[{'value':'c'}]}]})+'\n')
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)
        self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],2)
        with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'kind':2,'k':['requests'],'i':1})+'\n')
        viewer.sync_sessions();self.assertEqual(viewer.get_all_sessions()[0]['userMessageCount'],1)


if __name__=='__main__':unittest.main()
