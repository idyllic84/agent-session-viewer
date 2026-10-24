"""Analysis policy across BOTH Web Servers: local store, upload wire and cloud tables.

Synthetic fixtures only. Every case is imported locally, uploaded through the
real cloud ingest API, and checked in the local store, the wire bytes and the
cloud database.
"""
from contextlib import closing
import json
import sqlite3
import unittest

import test_integration as fixtures
from local.codec import unpack_json
from local.importer import Importer, LocalRepository
from local.sources.claude import ClaudeAdapter
from local.sources.pi import PiAdapter
from local.sources.codex import CodexAdapter
from local.sources.copilot_cli import CopilotCLIAdapter
from local.sources.vscode_chat import VSCodeChatAdapter

TS='2026-10-23T01:02:03Z'
MS=1792717323000


class AnalysisTests(unittest.TestCase):
    setUp=fixtures.LocalCloudIntegrationTests.setUp
    make_local=fixtures.LocalCloudIntegrationTests.make_local

    def sync(self,adapter):
        result=Importer(LocalRepository(self.local.archive),[adapter],quiet_seconds=0,log=lambda *_:None).run()
        if self.local.archive.connection() is None:self.local.save_connection(fixtures.ORIGIN,self.key)
        upload=self.local.uploader.run_now();self.assertIn(upload['state'],('complete','up to date'),upload)
        return result

    def messages(self,sid):
        local=self.local.archive.db.get_messages(sid)
        cloud=self.cloud.db.get_messages(self.local.archive.dataset+'/'+sid)
        if local is None:
            self.assertIsNone(cloud);return None
        strip=lambda m:{k:v for k,v in m.items() if k not in ('id','sessionId','orderId')}
        self.assertEqual([strip(m) for m in local['items']],[strip(m) for m in cloud['items']],'cloud mirrors the local analysis store')
        return local

    def session(self,sid):return self.local.archive.db.get_session(sid)
    def sessions(self):return self.local.archive.db.list_sessions()

    def assert_no_detailed_storage(self,markers):
        wire=b''.join(self.network.bodies).decode()
        for marker in markers:self.assertNotIn(marker,wire)
        for path,tables in ((self.local.archive.db.db_path,[('sessions','metadata',False),('messages','normalized_json',True),
                                                            ('sync_checkpoints','state',True),('projection_state','state',True),('change_log','payload',True)]),
                            (self.root/'cloud/cloud.db',[('sessions','metadata',False),('messages','normalized_json',True),('cloud_receipts','receipt',False)])):
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM projection_raw').fetchone()[0],0)
                values=[]
                for table,column,packed in tables:
                    for (value,) in conn.execute(f'SELECT {column} FROM {table}'):
                        values.append(json.dumps(unpack_json(value)) if packed else value)
                for marker in markers:self.assertNotIn(marker,' '.join(values),str(path))
            with closing(sqlite3.connect(self.local.archive.db.db_path)) as conn:
                for (state,) in conn.execute('SELECT state FROM sync_checkpoints'):
                    self.assertEqual(set(unpack_json(state)),{'format','cursor','snapshotBytes'})
                for (state,) in conn.execute('SELECT state FROM projection_state'):
                    self.assertEqual(set(unpack_json(state)),{'analysisDigest','count'})

    def test_exact_four_kinds_no_thoughts_events_attachments_or_result_bodies(self):
        path=self.source/'analysis.jsonl'
        args={'api_key':'KEEP_API_KEY','signature':'KEEP_SIGNATURE','nested':{'password':'KEEP_PASSWORD','result':'ordinary parameter'},
              'attachment':{'type':'image','source':{'type':'base64','data':'ATTACHMENT_BYTES'}}}
        fixtures.write(path,[
            {'type':'system','timestamp':TS,'content':'SYSTEM_EVENT_BYTES'},
            {'type':'user','sessionId':'analysis','uuid':'u','timestamp':TS,'message':{'content':[
                {'type':'text','text':'thinking about an idea, literal API key KEEP_USER_KEY'},
                {'type':'image','source':{'type':'base64','data':'ATTACHMENT_BYTES'}}]}},
            {'type':'assistant','uuid':'a','timestamp':TS,'message':{'model':'test-model','content':[
                {'type':'thinking','thinking':'THINKING_BYTES'},{'type':'text','text':'Final answer KEEP_ASSISTANT_KEY'},
                {'type':'tool_use','id':'call','name':'Agent','input':args}]}},
            {'type':'progress','timestamp':TS,'content':'SUBAGENT_INTERNAL_BYTES'},
            {'type':'user','uuid':'r','timestamp':TS,'message':{'content':[
                {'type':'tool_result','tool_use_id':'call','is_error':True,'content':'TOOL_RESULT_DETAILS_BYTES'}]}}])
        result=self.sync(ClaudeAdapter([path]));self.assertEqual(result['failed'],0,result)
        messages=self.messages('claude:analysis')['items']
        self.assertEqual([m['type'] for m in messages],['user','assistant','tool-use','tool-result'])
        self.assertTrue(all(m['timestamp']==MS and m['timestampSource']=='source' for m in messages))
        tool=next(m for m in messages if m['type']=='tool-use')['message']['content'][0]
        self.assertEqual(tool['input']['signature'],'KEEP_SIGNATURE')
        self.assertEqual(tool['input']['api_key'],'KEEP_API_KEY')
        self.assertEqual(tool['input']['nested'],args['nested'])
        self.assertNotIn('attachment',tool['input'])
        outcome=messages[-1];self.assertEqual(outcome['status'],'failure')
        self.assertEqual(outcome['message']['content'],[{'type':'tool_result','tool_use_id':'call','success':False,'content':'failure'}])
        for kept in ('KEEP_USER_KEY','KEEP_ASSISTANT_KEY','KEEP_PASSWORD'):
            self.assertIn(kept,b''.join(self.network.bodies).decode())
        self.assert_no_detailed_storage(['SYSTEM_EVENT_BYTES','THINKING_BYTES','ATTACHMENT_BYTES','SUBAGENT_INTERNAL_BYTES','TOOL_RESULT_DETAILS_BYTES'])
        for batch in self.network.bodies:
            for group in json.loads(batch)['groups']:
                for change in group['changes']:self.assertIn(change['op'],('session','message','delete_message'))

    def test_no_redaction_of_json_prose_structured_user_data_or_file_write_arguments(self):
        path=self.source/'literal.jsonl'
        literal=json.dumps({'encrypted_content':'KEEP_LITERAL_ENCRYPTED','signature':'KEEP_LITERAL_SIGNATURE'})
        args={'target':{'type':'file','path':'README.md','content':'KEEP_FILE_WRITE_BODY','signature':'KEEP_WRITE_SIGNATURE'}}
        fixtures.write(path,[{'type':'user','sessionId':'literal','message':{'content':literal}},
            {'type':'user','message':{'content':{'signature':'KEEP_STRUCTURED_SIGNATURE','token':'KEEP_STRUCTURED_TOKEN'}}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'write','name':'write','input':args}]}}])
        result=self.sync(ClaudeAdapter([path]));self.assertEqual(result['failed'],0,result)
        messages=self.messages('claude:literal')['items']
        self.assertEqual(messages[0]['message']['content'][0]['text'],literal)
        self.assertIn('KEEP_STRUCTURED_SIGNATURE',messages[1]['message']['content'][0]['text'])
        call=next(m for m in messages if m['type']=='tool-use')
        self.assertEqual(call['message']['content'][0]['input'],args)

    def test_success_failure_summaries_for_each_source(self):
        cases=[
          (ClaudeAdapter,'claude.jsonl','claude:c',[
            {'type':'user','sessionId':'c','timestamp':TS,'message':{'content':'question'}},
            {'type':'assistant','timestamp':TS,'message':{'content':[{'type':'tool_use','id':'c','name':'read','input':{'path':'a'}}]}},
            {'type':'user','timestamp':TS,'message':{'content':[{'type':'tool_result','tool_use_id':'c','is_error':False,'content':'RESULT_SECRET'}]}}],True),
          (PiAdapter,'pi.jsonl','pi:p',[
            {'type':'session','version':3,'id':'p'},
            {'type':'message','id':'u','timestamp':TS,'message':{'role':'user','content':'question'}},
            {'type':'message','id':'a','parentId':'u','timestamp':TS,'message':{'role':'assistant','content':[{'type':'toolCall','id':'c','name':'read','arguments':{}}]}},
            {'type':'message','id':'r','parentId':'a','timestamp':TS,'message':{'role':'toolResult','toolCallId':'c','isError':True,'content':[{'type':'text','text':'RESULT_SECRET'}]}}],False),
          (CopilotCLIAdapter,'events.jsonl','copilot_cli:cp',[
            {'type':'session.start','data':{'sessionId':'cp'}},
            {'type':'user.message','timestamp':TS,'data':{'content':'question'}},
            {'type':'assistant.message','timestamp':TS,'data':{'content':'answer','toolRequests':[{'toolCallId':'c','name':'read','arguments':{}}]}},
            {'type':'tool.execution_complete','timestamp':TS,'data':{'toolCallId':'c','success':False,'result':'RESULT_SECRET'}}],False),
          (CodexAdapter,'codex.jsonl','codex:cx',[
            {'type':'session_meta','payload':{'id':'cx'}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'function_call','call_id':'c','name':'shell','arguments':'{"command":"echo test"}'}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'function_call_output','call_id':'c','output':'Process exited with code 7\nFinal output:\nRESULT_SECRET'}}],False)]
        for cls,name,sid,rows,expected in cases:
            with self.subTest(source=cls.source):
                path=self.source/name;fixtures.write(path,rows);result=self.sync(cls([path]));self.assertEqual(result['failed'],0,result)
                outcomes=[m for m in self.messages(sid)['items'] if m['type']=='tool-result']
                self.assertEqual(len(outcomes),1);self.assertIs(outcomes[0]['message']['content'][0]['success'],expected)
                self.assertEqual(outcomes[0]['timestamp'],MS)
        path=self.source/'vs.json'
        state={'sessionId':'vs','creationDate':MS,'requests':[{'timestamp':MS,'message':{'text':'question'},
            'response':[{'kind':'toolInvocationSerialized','toolCallId':'c','toolId':'read','isComplete':True,
                'toolSpecificData':{'kind':'terminal','command':'echo test','exitCode':0},'resultDetails':'RESULT_SECRET'}],
            'result':{'metadata':{'toolCallResults':{'c':{'isError':False,'content':[{'type':'text','value':'RESULT_SECRET'}]}},
                'toolCallRounds':[{'toolCalls':[{'id':'c','name':'read','arguments':{'path':'a'}}]}]}}}]}
        path.write_text(json.dumps(state));result=self.sync(VSCodeChatAdapter([path]));self.assertEqual(result['failed'],0,result)
        outcomes=[m for m in self.messages('vscode_chat:vs')['items'] if m['type']=='tool-result']
        self.assertEqual(outcomes[0]['status'],'success');self.assertEqual(outcomes[0]['timestampSource'],'request')
        self.assert_no_detailed_storage(['RESULT_SECRET'])

    def test_missing_outcome_is_unknown_not_guessed_from_output_text(self):
        path=self.source/'events.jsonl'
        fixtures.write(path,[{'type':'session.start','data':{'sessionId':'unknown'}},
            {'type':'user.message','data':{'content':'question'}},
            {'type':'assistant.message','data':{'toolRequests':[{'toolCallId':'c','name':'read','arguments':{}}]}},
            {'type':'tool.execution_complete','data':{'toolCallId':'c','result':'error failed success all words are data'}}])
        self.sync(CopilotCLIAdapter([path]))
        result=next(m for m in self.messages('copilot_cli:unknown')['items'] if m['type']=='tool-result')
        self.assertEqual(result['status'],'unknown');self.assertIsNone(result['message']['content'][0]['success'])
        self.assertEqual(result['timestampSource'],'file_mtime');self.assertEqual(result['timestamp'],path.stat().st_mtime_ns//1_000_000)

    def test_only_system_thinking_and_attachments_advance_progress_but_store_no_messages(self):
        path=self.source/'empty-analysis.jsonl'
        fixtures.write(path,[{'type':'system','sessionId':'empty','content':'SYSTEM_SECRET'},
            {'type':'assistant','message':{'content':[{'type':'thinking','thinking':'THOUGHT_SECRET'}]}},
            {'type':'user','message':{'content':[{'type':'image','source':{'type':'base64','data':'IMAGE_SECRET'}}]}}])
        result=self.sync(ClaudeAdapter([path]));self.assertEqual(result['failed'],0,result)
        self.assertEqual(self.session('claude:empty')['messageCount'],0)
        self.assertEqual(self.sessions()[1],0)
        self.assertEqual(self.sync(ClaudeAdapter([path]))['unchanged'],1)
        self.assert_no_detailed_storage(['SYSTEM_SECRET','THOUGHT_SECRET','IMAGE_SECRET'])

    def test_codex_analysis_channel_text_is_not_final_answer(self):
        path=self.source/'channels.jsonl'
        fixtures.write(path,[{'type':'session_meta','payload':{'id':'channels'}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'HIDDEN_CHANNEL_THOUGHT'}]}},
            {'type':'response_item','timestamp':TS,'payload':{'type':'message','role':'assistant','channel':'final','content':[{'type':'output_text','text':'final answer'}]}}])
        result=self.sync(CodexAdapter([path]));self.assertEqual(result['failed'],0,result)
        self.assertEqual(self.session('codex:channels')['messageCount'],2)
        self.assert_no_detailed_storage(['HIDDEN_CHANNEL_THOUGHT'])

    def test_repeated_tool_result_updates_status_without_duplicates(self):
        path=self.source/'repeat.jsonl'
        fixtures.write(path,[{'type':'user','sessionId':'repeat','timestamp':TS,'message':{'content':'question'}},
            {'type':'assistant','timestamp':TS,'message':{'content':[{'type':'tool_use','id':'c','name':'read','input':{}}]}},
            {'type':'user','timestamp':TS,'message':{'content':[{'type':'tool_result','tool_use_id':'c','content':'OLD_RESULT_SECRET'}]}}])
        adapter=ClaudeAdapter([path]);self.sync(adapter)
        fixtures.append(path,[{'type':'user','timestamp':TS,'message':{'content':[{'type':'tool_result','tool_use_id':'c','is_error':True,'content':'NEW_RESULT_SECRET'}]}}])
        result=self.sync(adapter);self.assertEqual(result['failed'],0,result)
        messages=self.messages('claude:repeat')['items']
        self.assertEqual(len(messages),3);self.assertEqual([m['orderId'] for m in messages],[0,1,2])
        self.assertEqual(next(m for m in messages if m['type']=='tool-result')['status'],'failure')
        self.assertEqual(self.sync(adapter)['unchanged'],1)
        self.assert_no_detailed_storage(['OLD_RESULT_SECRET','NEW_RESULT_SECRET'])



if __name__=='__main__':unittest.main()
