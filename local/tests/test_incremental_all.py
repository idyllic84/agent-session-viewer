"""Every source advances durable cursors; each file commits before discovering the next."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local.codec import unpack_json
from local.service import ClaudeViewerService
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter
from local.sources.pi import PiAdapter
from local.sources.codex import CodexAdapter
from local.sources.copilot_cli import CopilotCLIAdapter
from local.sources.vscode_chat import VSCodeChatAdapter


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')


def append(path, records):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))


def pi_header(sid='pi'):
    return {'type':'session','version':3,'id':sid,'cwd':'/project'}


def pi_message(eid, parent, role, content):
    return {'type':'message','id':eid,'parentId':parent,'message':{'role':role,'content':content}}


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def viewer(self, adapter):
        viewer = ClaudeViewerService(self.root/'archive.db', [adapter], skip_recent_seconds=0)
        viewer.initialize(); viewer.sync_sessions()
        return viewer

    def blobs(self, viewer, sid):
        with viewer._db.get_connection() as conn:
            return {r[0]:(r[1],r[2]) for r in conn.execute(
                'SELECT id,message_pk,normalized_json FROM messages WHERE session_id=?', (sid,))}

    def test_pi_append_restores_branch_context_without_reparsing_old_entries(self):
        path=self.root/'pi.jsonl';adapter=PiAdapter([self.root])
        write(path,[pi_header(),pi_message('u',None,'user','question'),
            {'type':'model_change','id':'m','parentId':'u','modelId':'branch-model','provider':'test'},
            pi_message('a','m','assistant','answer')])
        viewer=self.viewer(adapter);before=self.blobs(viewer,'pi:pi')
        append(path,[pi_message('new','m','assistant','new branch answer'),
                     {'type':'session_info','id':'title','parentId':'new','name':'Renamed'}])
        with patch.object(adapter,'parse',side_effect=AssertionError('Full pi parse forbidden')):
            result=viewer.sync_sessions()
        self.assertEqual(result['incrementalSessions'],1);self.assertEqual(result['failed'],0)
        self.assertTrue(before.items()<=self.blobs(viewer,'pi:pi').items())
        page=viewer.get_messages('pi:pi',branch='active')
        self.assertEqual([m['uuid'] for m in page['items']],['u','m','new','title'])
        self.assertEqual(page['items'][-2]['model'],'branch-model')
        self.assertEqual(page['session']['name'],'Renamed')

    def test_pi_context_edit_does_not_delete_original_raw_history(self):
        path=self.root/'pi.jsonl';adapter=PiAdapter([self.root])
        write(path,[pi_header(),pi_message('u',None,'user','original'),pi_message('a','u','assistant','answer')])
        viewer=self.viewer(adapter)
        append(path,[{'type':'context_edit','id':'edit','parentId':'a','targetId':'u','replacement':None}])
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)
        self.assertEqual(viewer.get_messages('pi:pi')['items'][0]['message']['content'][0]['text'],'original')

    def test_copilot_late_completion_updates_only_its_turn(self):
        path=self.root/'events.jsonl';adapter=CopilotCLIAdapter([self.root])
        write(path,[{'type':'session.start','data':{'sessionId':'cp'}},
            {'type':'user.message','id':'u1','data':{'content':'first'}},
            {'type':'assistant.message','id':'a1','data':{'content':'working','toolRequests':[{'toolCallId':'c','name':'read','arguments':{}}]}},
            {'type':'tool.execution_start','id':'start','data':{'toolCallId':'c','toolName':'read'}},
            {'type':'user.message','id':'u2','data':{'content':'second'}},
            {'type':'assistant.message','id':'a2','data':{'content':'unrelated'}}])
        viewer=self.viewer(adapter);before=self.blobs(viewer,'copilot_cli:cp')
        append(path,[{'type':'tool.execution_complete','id':'end','data':{'toolCallId':'c','success':True,'result':'late output'}}])
        original=viewer._db.load_scope_records
        def only_first(sid,scope):
            self.assertNotEqual(scope,'user:4')
            return original(sid,scope)
        with patch.object(adapter,'parse',side_effect=AssertionError('Full copilot parse forbidden')), \
             patch.object(viewer._db,'load_scope_records',side_effect=only_first):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['reprojectedScopes'],1)
        after=self.blobs(viewer,'copilot_cli:cp')
        self.assertEqual(before['copilot_cli:cp:5:0'],after['copilot_cli:cp:5:0'])
        messages=viewer.get_messages('copilot_cli:cp')['items']
        self.assertEqual(next(m for m in messages if m['uuid']=='a1')['status'],'completed')
        self.assertEqual(messages[-1]['message']['content'][0]['content'],'late output')
        self.assertEqual([m['orderId'] for m in messages],list(range(len(messages))))

    def test_copilot_start_before_assistant_is_reconciled_without_duplicate_call(self):
        path=self.root/'events.jsonl';adapter=CopilotCLIAdapter([self.root])
        write(path,[{'type':'session.start','data':{'sessionId':'cp'}},
            {'type':'user.message','data':{'content':'question'}},
            {'type':'tool.execution_start','data':{'toolCallId':'c','toolName':'read','arguments':{}}}])
        viewer=self.viewer(adapter)
        append(path,[{'type':'assistant.message','data':{'content':'answer','toolRequests':[{'toolCallId':'c','name':'read','arguments':{}}]}}])
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)
        messages=viewer.get_messages('copilot_cli:cp')['items']
        calls=[block for m in messages for block in m['message']['content'] if block.get('type')=='tool_use']
        self.assertEqual(len(calls),1)
        self.assertIn('answer',str(messages))

    def test_codex_completed_item_reprojects_only_current_turn(self):
        path=self.root/'codex.jsonl';adapter=CodexAdapter([self.root])
        def completed(turn,eid,role,text):
            return {'type':'event_msg','payload':{'type':'item_completed','thread_id':'cx','turn_id':turn,
                'item':{'type':role,'id':eid,'content':[{'type':'text','text':text}]}}}
        write(path,[{'type':'session_meta','payload':{'id':'cx','history_mode':'paginated'}},
            {'type':'turn_context','payload':{'turn_id':'one','model':'old'}},
            completed('one','u1','UserMessage','first'),completed('one','a1','AgentMessage','closed answer'),
            {'type':'turn_context','payload':{'turn_id':'two','model':'new'}},
            completed('two','u2','UserMessage','second'),
            {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'streaming answer'}]}}])
        viewer=self.viewer(adapter);before=self.blobs(viewer,'codex:cx')
        append(path,[completed('two','a2','AgentMessage','completed answer')])
        original=viewer._db.load_scope_records
        def current_only(sid,scope):
            self.assertNotEqual(scope,'turn:one');return original(sid,scope)
        with patch.object(adapter,'parse',side_effect=AssertionError('Full codex parse forbidden')), \
             patch.object(viewer._db,'load_scope_records',side_effect=current_only):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['reprojectedScopes'],1)
        after=self.blobs(viewer,'codex:cx')
        self.assertEqual(before['codex:cx:3:0'],after['codex:cx:3:0'])
        items=viewer.get_messages('codex:cx')['items']
        self.assertEqual(sum('completed answer' in str(m['message']) for m in items),1)
        self.assertFalse(any('streaming answer' in str(m['message']) for m in items))
        self.assertEqual(items[-1]['model'],'new')

    def vs(self, jsonl=True):
        path=self.root/('vs.jsonl' if jsonl else 'vs.json')
        state={'sessionId':'vs','customTitle':'title','requests':[
            {'requestId':'r0','message':{'text':'old question'},'response':[{'value':'old answer'}]},
            {'requestId':'r1','message':{'text':'new question'},'response':[{'value':'用 '}]}]}
        if jsonl:write(path,[{'kind':0,'v':state}])
        else:path.write_text(json.dumps(state),encoding='utf-8')
        adapter=VSCodeChatAdapter([path]);viewer=self.viewer(adapter)
        return path,state,adapter,viewer

    def test_vscode_replays_tail_and_only_loads_dirty_request(self):
        path,state,adapter,viewer=self.vs();before=self.blobs(viewer,'vscode_chat:vs')
        append(path,[{'kind':2,'k':['requests',1,'response'],'v':[
            {'kind':'inlineReference','inlineReference':{'name':'event.item.model_dump()'}},
            {'value':' 转发上游 '},{'kind':'inlineReference','inlineReference':'conversation.item.added'}]}])
        original=viewer._db.load_scope_state
        def second_only(sid,scope):
            self.assertEqual(scope,'request:1');return original(sid,scope)
        with patch.object(adapter,'parse',side_effect=AssertionError('Full VS replay forbidden')), \
             patch.object(viewer._db,'load_scope_state',side_effect=second_only):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['incrementalSessions'],1)
        after=self.blobs(viewer,'vscode_chat:vs')
        self.assertEqual(before['vscode_chat:vs:request:0:1'],after['vscode_chat:vs:request:0:1'])
        items=viewer.get_messages('vscode_chat:vs')['items']
        self.assertEqual(items[-1]['message']['content'][0]['text'],
                         '用 event.item.model_dump() 转发上游 conversation.item.added')
        self.assertEqual(viewer.sync_sessions()['unchanged'],1)

    def test_vscode_deep_set_delete_and_truncate_request_array(self):
        path,state,adapter,viewer=self.vs()
        append(path,[{'kind':1,'k':['requests',1,'response',0,'value'],'v':'changed'},
                     {'kind':1,'k':['customTitle'],'v':'new title'}])
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)
        self.assertEqual(viewer.get_messages('vscode_chat:vs')['items'][-1]['message']['content'][0]['text'],'changed')
        append(path,[{'kind':2,'k':['requests'],'i':1}])
        result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0)
        page=viewer.get_messages('vscode_chat:vs');self.assertEqual(page['total'],2)
        self.assertEqual(page['session']['name'],'new title')
        with viewer._db.get_connection() as conn:
            self.assertFalse(conn.execute('pragma foreign_key_check').fetchall())

    def test_vscode_json_snapshot_diffs_requests_not_whole_projection(self):
        path,state,adapter,viewer=self.vs(jsonl=False);before=self.blobs(viewer,'vscode_chat:vs')
        state['requests'][1]['response']=[{'value':'snapshot changed'}]
        path.write_text(json.dumps(state),encoding='utf-8')
        with patch.object(adapter,'parse',side_effect=AssertionError('Full snapshot projection forbidden')):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['reprojectedScopes'],1)
        after=self.blobs(viewer,'vscode_chat:vs')
        self.assertEqual(before['vscode_chat:vs:request:0:1'],after['vscode_chat:vs:request:0:1'])
        self.assertEqual(viewer.get_messages('vscode_chat:vs')['items'][-1]['message']['content'][0]['text'],'snapshot changed')
        self.assertEqual(viewer.get_records('vscode_chat:vs')['items'][0]['record'],state)

    def test_vscode_invalid_tail_rolls_back_checkpoint_and_old_messages(self):
        path,state,adapter,viewer=self.vs();before=self.blobs(viewer,'vscode_chat:vs')
        checkpoint=viewer._db.get_sync_checkpoint('vscode_chat:vs')
        append(path,[{'kind':1,'k':['requests',1,'missing','deep'],'v':'invalid'}])
        result=viewer.sync_sessions();self.assertEqual(result['failed'],1)
        self.assertEqual(self.blobs(viewer,'vscode_chat:vs'),before)
        self.assertEqual(viewer._db.get_sync_checkpoint('vscode_chat:vs'),checkpoint)

    def test_vscode_delta_transaction_failure_restores_projection_state_and_fts(self):
        from local import incremental
        path,state,adapter,viewer=self.vs()
        before=self.blobs(viewer,'vscode_chat:vs')
        checkpoint=viewer._db.get_sync_checkpoint('vscode_chat:vs')
        append(path,[{'kind':1,'k':['requests',1,'response',0,'value'],'v':'must roll back'}])
        original=incremental.update
        def break_id(*args):
            document=original(*args)
            document.messages[0]['id']='vscode_chat:vs:request:0:0'  # Conflicts with untouched request.
            return document
        with patch('local.incremental.update',side_effect=break_id):result=viewer.sync_sessions()
        self.assertEqual(result['failed'],1)
        self.assertEqual(self.blobs(viewer,'vscode_chat:vs'),before)
        self.assertEqual(viewer._db.get_sync_checkpoint('vscode_chat:vs'),checkpoint)
        self.assertEqual(viewer.search_sessions_by_content('must roll back'),[])
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)

    def test_vscode_header_update_never_loads_old_requests(self):
        path,state,adapter,viewer=self.vs();before=self.blobs(viewer,'vscode_chat:vs')
        append(path,[{'kind':1,'k':['customTitle'],'v':'header only'}])
        with patch.object(viewer._db,'load_scope_state',side_effect=AssertionError('No requests should be restored')):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['reprojectedScopes'],0)
        self.assertEqual(self.blobs(viewer,'vscode_chat:vs'),before)
        self.assertEqual(viewer._db.get_session('vscode_chat:vs')['name'],'header only')

    def test_vscode_missing_request_checkpoint_reinitializes_safely(self):
        path,state,adapter,viewer=self.vs()
        with viewer._db.get_connection() as conn:
            conn.execute("delete from projection_state where scope='request:1'")
        append(path,[{'kind':1,'k':['requests',1,'response',0,'value'],'v':'recovered'}])
        with patch.object(adapter,'parse',wraps=adapter.parse) as parse:result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(parse.call_count,1)
        self.assertIn('recovered',str(viewer.get_messages('vscode_chat:vs')['items']))

    def test_vscode_initial_snapshot_operation_replaces_only_changed_units(self):
        path,state,adapter,viewer=self.vs();before=self.blobs(viewer,'vscode_chat:vs')
        state['requests'][1]['response']=[{'value':'new snapshot operation'}]
        append(path,[{'kind':0,'v':state}])
        with patch.object(adapter,'parse',side_effect=AssertionError('No old log replay')):
            result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(result['reprojectedScopes'],1)
        after=self.blobs(viewer,'vscode_chat:vs')
        self.assertEqual(before['vscode_chat:vs:request:0:1'],after['vscode_chat:vs:request:0:1'])

    def test_partial_tail_is_deferred_and_checkpoint_does_not_skip_repair(self):
        path=self.root/'pi.jsonl';adapter=PiAdapter([self.root])
        write(path,[pi_header(),pi_message('u',None,'user','first')]);viewer=self.viewer(adapter)
        with path.open('ab') as stream:stream.write(b'{"type":"message","id":"a"')
        result=viewer.sync_sessions();self.assertEqual(result['failed'],0)
        self.assertEqual(viewer.get_messages('pi:pi')['total'],1)
        with path.open('ab') as stream:stream.write(b',"parentId":"u","message":{"role":"assistant","content":"fixed"}}\n')
        self.assertEqual(viewer.sync_sessions()['incrementalSessions'],1)
        self.assertEqual(viewer.get_messages('pi:pi')['total'],2)

    def test_snapshot_truncation_and_rewrite_reinitialize_only_that_file(self):
        path,state,adapter,viewer=self.vs()
        state['requests'][1]['response']=[{'value':'rewritten source'}]
        write(path,[{'kind':0,'v':state}])
        with patch.object(adapter,'parse',wraps=adapter.parse) as parse:result=viewer.sync_sessions()
        self.assertEqual(result['failed'],0);self.assertEqual(parse.call_count,1)
        self.assertIn('rewritten source',str(viewer.get_messages('vscode_chat:vs')['items']))

    def test_later_duplicate_keeps_one_newest_nonempty_session(self):
        adapter=PiAdapter([self.root]);viewer=ClaudeViewerService(self.root/'archive.db',[adapter],skip_recent_seconds=0);viewer.initialize()
        import os
        first=self.root/'old.jsonl';second=self.root/'new.jsonl'
        write(first,[pi_header(),pi_message('u',None,'user','older')]);os.utime(first,(1700000000,1700000000))
        write(second,[pi_header(),pi_message('u',None,'user','newest')]);os.utime(second,(1700000001,1700000001))
        with patch.object(adapter,'discover',return_value=iter([SourceFile(first),SourceFile(second)])):
            result=viewer.sync_sessions()
        self.assertEqual(result['added'],1);self.assertEqual(result['updated'],0)
        self.assertEqual(viewer.get_messages('pi:pi')['items'][0]['message']['content'][0]['text'],'newest')

    def test_each_file_commits_before_discovering_the_next(self):
        adapter=PiAdapter([self.root]);viewer=ClaudeViewerService(self.root/'archive.db',[adapter],skip_recent_seconds=0)
        viewer.initialize()
        entries=[]
        for i in range(4):
            path=self.root/f'{i}.jsonl';write(path,[pi_header(str(i)),pi_message('u',None,'user',str(i))]);entries.append(SourceFile(path))
        def discover():
            for index, entry in enumerate(entries):
                self.assertEqual(len(viewer.get_all_sessions()),index,'Previous file must commit before requesting another path')
                yield entry
                self.assertEqual(len(viewer.get_all_sessions()),index+1,'No file prefetch or deferred import allowed')
        with patch.object(adapter,'discover',side_effect=discover):result=viewer.sync_sessions()
        self.assertEqual(result['added'],4);self.assertEqual(result['failed'],0)

    def test_partial_discovery_failure_preserves_already_yielded_files(self):
        adapter=PiAdapter([self.root]);viewer=ClaudeViewerService(self.root/'archive.db',[adapter],skip_recent_seconds=0);viewer.initialize()
        path=self.root/'one.jsonl';write(path,[pi_header(),pi_message('u',None,'user','one')])
        def discover():
            yield SourceFile(path)
            raise OSError('directory disappeared')
        with patch.object(adapter,'discover',side_effect=discover):result=viewer.sync_sessions()
        self.assertEqual(result['added'],1);self.assertEqual(result['failed'],1)


if __name__=='__main__':unittest.main()
