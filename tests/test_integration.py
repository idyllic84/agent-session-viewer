"""Isolated synthetic archives only. Never access real user histories or DBs."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from deamon import incremental
from deamon.sources.base import SourceFile
from deamon.sources.claude import ClaudeAdapter
from deamon.sources.pi import PiAdapter
from deamon.sources.codex import CodexAdapter
from deamon.sources.copilot_cli import CopilotCLIAdapter
from deamon.sources.vscode_chat import VSCodeChatAdapter
from server.archive import CloudArchive
from server.app import create_app
from server.contracts.protocol import Problem, packet, encode_batch
from deamon.syncer import HttpArchiveRepository, SyncCoordinator, Remote


def write(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')


def append(path,rows):
    with path.open('a',encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))


class FlaskRemote:
    def __init__(self,app,token):
        self.client=app.test_client();self.token=token;self.url='https://cloud.test';self.calls=[]
        self.lose_commit=False;self.lose_received=False;self.fail_next=False;self.batches=[];self.batch_keys=[];self.progress_requests=[]

    def call(self,method,path,**kwargs):
        self.calls.append((method,path))
        if path=='/sync/v1/progress/query':self.progress_requests.append(kwargs['json']['files'])
        if path=='/sync/v1/batches':
            self.batches.append(kwargs['data']);self.batch_keys.append(kwargs['headers']['Idempotency-Key'])
            if self.fail_next:
                self.fail_next=False;raise Problem('NETWORK_UNAVAILABLE','Request not delivered',503)
        params=kwargs.pop('params',None)
        headers={'Authorization':'Bearer '+self.token,**kwargs.pop('headers',{})}
        response=self.client.open(path,method=method,headers=headers,query_string=params,
                                  base_url=self.url,**kwargs)
        value=response.get_json()
        if response.status_code>=400:
            error=value['error'];raise Problem(error['code'],error['message'],response.status_code)
        if self.lose_received and path.endswith('/received'):
            self.lose_received=False;raise Problem('NETWORK_UNAVAILABLE','Lost task receipt confirmation response',503)
        if self.lose_commit and path=='/sync/v1/batches':
            self.lose_commit=False;raise Problem('NETWORK_UNAVAILABLE','Lost commit response',503)
        return value


class ServerDeamonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.archive=CloudArchive(self.root/'cloud.db')
        self.admin='a'*48
        self.app=create_app(self.archive,admin_key=self.admin,session_secret='s'*48,public_origin='https://cloud.test')
        self.device=self.archive.add_device('synthetic device',control=True)
        self.remote=FlaskRemote(self.app,self.device['token'])
        self.repo=HttpArchiveRepository(self.remote)

    def sync(self,adapter,quiet=0,repo=None):return SyncCoordinator(repo or self.repo,[adapter],quiet_seconds=quiet).run()

    def claude(self,text='question'):
        path=self.source/'c.jsonl'
        write(path,[{'type':'user','sessionId':'c','uuid':'u','message':{'content':text}}])
        return path,ClaudeAdapter([self.source])

    def planned(self,adapter,path):
        entry=SourceFile(path);doc=incremental.initialize(adapter,entry,adapter.parse(entry))
        info={'path':str(path),'fingerprint':entry.snapshot_fingerprint(doc.records),
              'modified':doc.records.modified_ns//1_000_000,'parserVersion':adapter.parser_version}
        return doc,info

    def batch(self,doc,info):
        value=packet(doc,info);value['instance']=self.archive.instance
        return json.loads(encode_batch(value))

    def login(self):
        client=self.app.test_client()
        response=client.get('/auth/login',base_url='https://cloud.test')
        nonce=re.search('name="nonce" value="([^"]+)"',response.text).group(1)
        response=client.post('/auth/login',base_url='https://cloud.test',data={'nonce':nonce,'key':self.admin})
        self.assertEqual(response.status_code,302)
        page=client.get('/',base_url='https://cloud.test')
        csrf=re.search('name="cloud-csrf" content="([^"]+)"',page.text).group(1)
        return client,{'X-CSRF-Token':csrf,'Origin':'https://cloud.test'}

    def test_auth_all_history_routes_and_separate_machine_scope(self):
        client=self.app.test_client()
        for path in ['/api/sessions','/api/sources','/api/messages/anything/raw','/static/js/app.js','/api/devices','/sync/v1/capabilities']:
            self.assertEqual(client.get(path,base_url='https://cloud.test').status_code,401,path)
        self.assertEqual(client.get('/',base_url='https://cloud.test').status_code,302)
        self.assertEqual(client.get('/api/sessions',base_url='https://evil.test').status_code,403)
        self.assertEqual(client.get('/api/sessions',base_url='https://cloud.test',headers={'Authorization':'Bearer '+self.device['token']}).status_code,401)
        owner,headers=self.login()
        self.assertEqual(owner.get('/sync/v1/capabilities',base_url='https://cloud.test').status_code,401)
        self.assertEqual(owner.post('/api/devices',base_url='https://cloud.test',json={'name':'bad'}).status_code,403)
        self.assertEqual(owner.post('/api/devices',base_url='https://cloud.test',headers={**headers,'Origin':'https://evil.test'},json={'name':'bad'}).status_code,403)
        self.assertEqual(owner.post('/api/devices',base_url='https://cloud.test',headers=headers,json={'name':'new'}).status_code,201)
        self.assertNotIn('token_hash',owner.get('/api/devices',base_url='https://cloud.test').text)

    def test_expiry_revocation_and_rotation(self):
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_devices SET expires=0')
        with self.assertRaises(Problem):self.remote.call('GET','/sync/v1/capabilities')
        rotated=self.archive.rotate_device(self.device['deviceId'])
        with self.assertRaises(Problem):self.remote.call('GET','/sync/v1/capabilities')
        self.remote.token=rotated['token'];self.remote.call('GET','/sync/v1/capabilities')
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_devices SET active=0')
        with self.assertRaises(Problem):self.remote.call('GET','/sync/v1/capabilities')

    def test_claude_incremental_and_only_prose_search(self):
        path,adapter=self.claude();self.assertEqual(self.sync(adapter)['initialized'],1)
        append(path,[{'type':'assistant','uuid':'a','message':{'content':'readable answer'}},
                     {'type':'system','content':'SYSTEM_NOT_SEARCHABLE'}])
        with patch.object(adapter,'parse',side_effect=AssertionError('Full parse forbidden')):
            result=self.sync(adapter)
        self.assertEqual(result['incremental'],1,result)
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'],2)
        self.assertEqual(self.archive.db.list_sessions(query='readable answer')[1],1)
        self.assertEqual(self.archive.db.list_sessions(query='SYSTEM_NOT_SEARCHABLE')[1],0)
        self.assertEqual(self.sync(adapter)['unchanged'],1)

    def test_pi_append_preserves_old_blobs_and_restores_state(self):
        path=self.source/'p.jsonl';adapter=PiAdapter([self.source])
        write(path,[{'type':'session','version':3,'id':'p'},
            {'type':'message','id':'u','message':{'role':'user','content':'question'}}])
        self.assertEqual(self.sync(adapter)['initialized'],1)
        with self.archive.db.get_connection() as conn:before=tuple(conn.execute('SELECT * FROM messages').fetchone())
        append(path,[{'type':'message','id':'a','parentId':'u','message':{'role':'assistant','content':'answer'}}])
        new=HttpArchiveRepository(self.remote)
        with patch.object(adapter,'parse',side_effect=AssertionError('Full parse forbidden')):
            self.assertEqual(self.sync(adapter,repo=new)['incremental'],1)
        with self.archive.db.get_connection() as conn:self.assertEqual(tuple(conn.execute('SELECT * FROM messages ORDER BY message_pk LIMIT 1').fetchone()),before)

    def test_copilot_and_codex_replay_prefix_locally_then_upload_only_analytical_delta(self):
        cases=[
          (CopilotCLIAdapter, 'events.jsonl', 'copilot_cli:cp',
           [{'type':'session.start','data':{'sessionId':'cp'}},
            {'type':'user.message','id':'u','data':{'content':'question'}},
            {'type':'assistant.message','id':'a','data':{'content':'working','toolRequests':[{'toolCallId':'c','name':'read','arguments':{}}]}},
            {'type':'tool.execution_start','data':{'toolCallId':'c','toolName':'read'}}],
           {'type':'tool.execution_complete','data':{'toolCallId':'c','success':True,'result':'late output'}}),
          (CodexAdapter, 'cx.jsonl', 'codex:cx',
           [{'type':'session_meta','payload':{'id':'cx','history_mode':'paginated'}},
            {'type':'turn_context','payload':{'turn_id':'one','model':'test'}},
            {'type':'event_msg','payload':{'type':'item_completed','thread_id':'cx','turn_id':'one','item':{'type':'UserMessage','id':'u','content':[{'type':'text','text':'question'}]}}},
            {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'streaming'}]}}],
           {'type':'event_msg','payload':{'type':'item_completed','thread_id':'cx','turn_id':'one','item':{'type':'AgentMessage','id':'a','content':[{'type':'text','text':'completed'}]}}})]
        for cls,name,sid,rows,tail in cases:
            with self.subTest(source=cls.source):
                path=self.source/name;write(path,rows);adapter=cls([path])
                self.assertEqual(self.sync(adapter)['initialized'],1)
                committed_bytes=path.stat().st_size;original_parse=adapter.parse
                append(path,[tail])
                def old_prefix_only(entry):
                    self.assertEqual(entry.path.stat().st_size,committed_bytes)
                    return original_parse(entry)
                with patch.object(adapter,'parse',side_effect=old_prefix_only):result=self.sync(adapter)
                self.assertEqual(result['incremental'],1,result)
                expected='late output' if cls==CopilotCLIAdapter else 'completed'
                if cls==CopilotCLIAdapter:
                    self.assertNotIn(expected,str(self.archive.db.get_messages(sid)))
                    self.assertIn('read',str(self.archive.db.get_messages(sid)))
                else:self.assertIn(expected,str(self.archive.db.get_messages(sid)))
                self.assertFalse(any('/scope' in p for _,p in self.remote.calls))

    def test_vscode_json_and_mutation_log_dirty_request(self):
        for jsonl in (True,False):
            sid='vs'+str(jsonl);path=self.source/(sid+('.jsonl' if jsonl else '.json'))
            state={'sessionId':sid,'requests':[{'message':{'text':'question'},'response':[{'value':'old'}]}]}
            if jsonl:write(path,[{'kind':0,'v':state}])
            else:path.write_text(json.dumps(state),encoding='utf-8')
            adapter=VSCodeChatAdapter([path]);self.assertEqual(self.sync(adapter)['initialized'],1)
            committed_bytes=path.stat().st_size;original_parse=adapter.parse
            if jsonl:append(path,[{'kind':1,'k':['requests',0,'response',0,'value'],'v':'updated'}])
            else:
                state['requests'][0]['response'][0]['value']='updated';path.write_text(json.dumps(state),encoding='utf-8')
            def permitted_parse(entry):
                if jsonl:self.assertEqual(entry.path.stat().st_size,committed_bytes)
                return original_parse(entry)
            with patch.object(adapter,'parse',side_effect=permitted_parse):result=self.sync(adapter)
            self.assertEqual(result['incremental'],1,result)
            self.assertIn('updated',str(self.archive.db.get_messages('vscode_chat:'+sid)))
            with self.archive.db.get_connection() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],0)

    def test_commit_reply_lost_then_recovered_without_duplicates(self):
        path,adapter=self.claude();self.remote.lose_commit=True
        with self.assertRaises(Problem):self.sync(adapter)
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'],1)
        # No local outbox/checkpoint exists. A fresh daemon asks the cloud and
        # observes the commit despite the lost acknowledgement.
        restarted=HttpArchiveRepository(self.remote)
        self.assertEqual(self.sync(adapter,repo=restarted)['unchanged'],1)
        self.assertFalse(hasattr(restarted,'state'))
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'],1)

    def test_batch_request_lost_retries_exact_body_and_never_exposes_partial(self):
        path,adapter=self.claude('large '+('x'*10000));self.remote.fail_next=True
        with self.assertRaises(Problem):self.sync(adapter)
        self.assertIsNone(self.archive.db.get_session('claude:c'))
        restarted=HttpArchiveRepository(self.remote)
        self.assertEqual(self.sync(adapter,repo=restarted)['initialized'],1)
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'],1)
        self.assertEqual(self.remote.batches[0],self.remote.batches[1])
        self.assertEqual(self.remote.batch_keys[0],self.remote.batch_keys[1])
        with self.archive.db.get_connection() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM dist_receipts').fetchone()[0],1)

    def test_atomic_receipt_projection_checkpoint_rollback(self):
        path,adapter=self.claude();doc,info=self.planned(adapter,path);value=self.batch(doc,info);key='atomic'
        original=self.archive.db._store_files
        def failure(*args):original(*args);raise RuntimeError('simulated after all projections')
        with patch.object(self.archive.db,'_store_files',side_effect=failure):
            with self.assertRaises(RuntimeError):self.archive.commit_batch(self.device['deviceId'],key,value,0)
        self.assertIsNone(self.archive.db.get_session('claude:c'))
        self.assertIsNone(self.archive.receipt(self.device['deviceId'],key))
        self.assertEqual(self.archive.db.list_sessions(query='question')[1],0)
        receipt=self.archive.commit_batch(self.device['deviceId'],key,value,0)
        self.assertEqual(self.archive.commit_batch(self.device['deviceId'],key,value,0),receipt)
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'],1)

    def test_conflicts_and_upload_integrity(self):
        path,adapter=self.claude();doc,info=self.planned(adapter,path)
        value=self.batch(doc,info)
        receipt=self.archive.commit_batch(self.device['deviceId'],'one',value,0)
        self.assertEqual(self.archive.commit_batch(self.device['deviceId'],'one',value,0),receipt)
        with self.assertRaises(Problem) as error:self.archive.commit_batch(self.device['deviceId'],'two',value,0)
        self.assertEqual(error.exception.code,'REVISION_CONFLICT')
        changed=json.loads(json.dumps(value));changed['document']['name']='different'
        with self.assertRaises(Problem):self.archive.commit_batch(self.device['deviceId'],'one',changed,0)
        other=self.archive.add_device('other')
        self.assertIsNone(self.archive.receipt(other['deviceId'],'one'))
        with self.assertRaises(Problem):self.archive.context(other['deviceId'],sid='claude:c')

    def test_reconcile_does_not_silently_drop_old_history(self):
        path,adapter=self.claude('old history');self.sync(adapter)
        write(path,[{'type':'user','sessionId':'c','uuid':'new','message':{'content':'short replacement'}}])
        result=self.sync(adapter)
        self.assertEqual(result['failed'],1);self.assertEqual(result['errors'][0]['code'],'SOURCE_DIVERGED')
        self.assertIn('old history',str(self.archive.db.get_messages('claude:c')))

    def test_recent_and_subagent_files_not_read(self):
        path,adapter=self.claude();agent=self.source/'agent-child.jsonl';write(agent,[{'type':'user','message':{'content':'hidden'}}])
        with patch.object(adapter,'parse',side_effect=AssertionError('Must not read bodies')),patch('deamon.syncer.header_is_subagent',side_effect=AssertionError('Must not read headers')):
            result=self.sync(adapter,quiet=3600)
        self.assertEqual(result['skippedRecent'],1);self.assertEqual(result['skippedSubagents'],1)
        self.assertEqual(self.archive.db.list_sessions()[1],0)

    def test_metadata_prefetch_but_sequential_body_commits_and_missing_sources_preserve(self):
        adapter=ClaudeAdapter([self.source]);entries=[]
        for i in range(3):
            path=self.source/f'{i}.jsonl';write(path,[{'type':'user','sessionId':str(i),'message':{'content':str(i)}}]);entries.append(SourceFile(path))
        original_parse=adapter.parse
        def parse(entry):
            self.assertEqual(self.archive.db.list_sessions()[1],int(entry.path.stem))
            self.assertTrue(any(p=='/sync/v1/progress/query' for _,p in self.remote.calls))
            return original_parse(entry)
        with patch.object(adapter,'discover',return_value=iter(entries)),patch.object(adapter,'parse',side_effect=parse):
            self.assertEqual(self.sync(adapter)['initialized'],3)
        def failed():
            yield entries[0]
            raise OSError('root disappeared')
        with patch.object(adapter,'discover',side_effect=failed):self.assertEqual(self.sync(adapter)['failed'],1)
        self.assertTrue(self.archive.db.get_session('claude:1')['available'])
        entries[1].path.unlink();self.sync(adapter)
        self.assertFalse(self.archive.db.get_session('claude:1')['available'])
        self.assertEqual(self.archive.db.list_sessions()[1],3)

    def test_fifo_delivery_ack_and_execution_fencing_through_api(self):
        device=self.device['deviceId'];first=self.archive.enqueue(device,'start',[])
        second=self.archive.enqueue(device,'second',[])
        self.assertEqual(self.archive.enqueue(device,'start',[])['id'],first['id'])
        delivery=self.remote.call('GET','/sync/v1/jobs/next',params={'workerId':'worker1'})['delivery']
        self.assertEqual(delivery['job']['id'],first['id']);self.assertEqual(self.archive.dashboard()[0]['queueCount'],2)
        ack={'workerId':'worker1','deliveryToken':delivery['deliveryToken']}
        received=self.remote.call('POST',f'/sync/v1/jobs/{first["id"]}/received',json=ack)
        self.assertFalse(received['terminal']);self.assertEqual(self.archive.dashboard()[0]['queueCount'],1)
        self.remote.call('POST',f'/sync/v1/jobs/{first["id"]}/received',json=ack)
        self.assertEqual(self.archive.dashboard()[0]['queueCount'],1)
        self.assertIsNone(self.archive.next_job(device,'worker2'))
        self.archive.control(device,first['id'],'pause')
        self.assertEqual(self.archive.commands(device,first['id'],'worker1')['control']['desired'],'pause')
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_jobs SET lease_until=0 WHERE id=?',(first['id'],))
        resume=self.archive.next_job(device,'worker2');self.assertEqual(resume['job']['id'],first['id'])
        self.archive.received(device,first['id'],'worker2',resume['deliveryToken'])
        with self.assertRaises(Problem):self.archive.commands(device,first['id'],'worker1')
        self.archive.control(device,first['id'],'cancel')
        self.assertEqual(self.archive.finish(device,first['id'],'worker2',{}, {'failed':0})['state'],'CANCELLED')
        self.assertEqual(self.archive.next_job(device,'worker2')['job']['id'],second['id'])
        disabled=self.archive.add_device('upload only')
        with self.assertRaises(Problem):self.archive.enqueue(disabled['deviceId'],'x',[])

    def test_viewer_paging_revision_and_original_assets_unchanged(self):
        path,adapter=self.claude();self.sync(adapter);client,headers=self.login()
        page=client.get('/api/sessions/claude:c/messages',base_url='https://cloud.test').json
        self.assertEqual(page['sessionRevision'],1)
        append(path,[{'type':'assistant','uuid':'a','message':{'content':'new'}}]);self.sync(adapter)
        self.assertEqual(client.get('/api/sessions/claude:c/messages?cursor=1&revision=1',base_url='https://cloud.test').status_code,409)
        self.assertEqual(client.get('/api/sessions/claude:c/messages?limit=2000',base_url='https://cloud.test').status_code,200)
        self.assertIn('Cloud devices',client.get('/',base_url='https://cloud.test').text)
        for path in ('/static/js/api.js','/static/js/app.js'):
            response=client.get(path,base_url='https://cloud.test');self.assertEqual(response.status_code,200);response.close()

    def test_dynamic_byte_batches_split_one_file_and_vary_message_count(self):
        path=self.source/'many.jsonl';rows=[]
        for i in range(55):
            rows.append({'type':'user','sessionId':'many','uuid':str(i),
                         'message':{'content':str(i)+' '+('x'*(40 if i<30 else 1700))}})
        write(path,rows);adapter=ClaudeAdapter([path])
        result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,batch_bytes=8192).run()
        self.assertEqual(result['failed'],0,result)
        self.assertGreater(len(self.remote.batches),2)
        batches=[json.loads(b) for b in self.remote.batches]
        counts=[len(b['document']['messages']) for b in batches]
        self.assertGreater(len(set(counts)),1)
        self.assertTrue(all(len(b)<=8192 or len(json.loads(b)['document']['messages'])==1 for b in self.remote.batches))
        self.assertEqual(self.archive.db.get_messages('claude:many')['total'],55)
        self.assertEqual(sum(counts),55)
        self.assertFalse(any('uploads' in p or 'chunks' in p for _,p in self.remote.calls))
        self.assertTrue(all(b['file']['path']==str(path) for b in batches))

    def test_no_tool_or_subagent_trace_bytes_in_raw_messages_or_wire_state(self):
        secret='FORBIDDEN_PROCESS_BODY_17001';path=self.source/'private.jsonl'
        write(path,[{'type':'user','sessionId':'private','uuid':'u','message':{'content':'ordinary question'}},
          {'type':'assistant','uuid':'a','message':{'content':[{'type':'text','text':'ordinary answer'},
           {'type':'tool_use','id':'call','name':'Agent','input':{'prompt':'allowed task','output':'legitimate parameter'}}]}},
          {'type':'progress','data':{'type':'agent_progress','messages':[{'role':'assistant','content':secret}]}},
          {'type':'user','uuid':'out','toolUseResult':{'nested':secret},'message':{'content':[{'type':'tool_result','tool_use_id':'call','content':secret}]}}])
        self.assertEqual(self.sync(ClaudeAdapter([path]))['initialized'],1)
        self.assertTrue(all(secret.encode() not in body for body in self.remote.batches))
        page=self.archive.db.get_messages('claude:private');self.assertEqual(page['total'],4)
        self.assertIn('allowed task',str(page));self.assertIn('legitimate parameter',str(page))
        self.assertNotIn(secret,str(self.archive.db.get_records('claude:private')))
        self.assertEqual(self.archive.db.list_sessions(query=secret)[1],0)

    def test_vscode_tool_process_mutations_use_cloud_progress_and_ram_only(self):
        secret='PRIVATE_VSCODE_TOOL_PROCESS_99001';path=self.source/'private-vs.jsonl'
        state={'sessionId':'private-vs','requests':[{'message':{'text':'question'},
          'response':[{'kind':'markdown','value':'ordinary answer'},
                      {'kind':'toolInvocationSerialized','toolCallId':'call','toolId':'read',
                       'toolSpecificData':{'kind':'terminal','command':'echo allowed','terminalCommandOutput':secret},
                       'resultDetails':secret}],
          'result':{'metadata':{'toolCallResults':{'call':secret},'toolCallRounds':[{'response':secret,
                       'toolCalls':[{'id':'call','name':'read','arguments':{'path':'allowed.txt'}}]}]}}}]}
        write(path,[{'kind':0,'v':state}]);adapter=VSCodeChatAdapter([path])
        self.assertEqual(self.sync(adapter)['initialized'],1)
        append(path,[{'kind':1,'k':['requests',0,'result','metadata','toolCallResults','call'],'v':secret+' second'}])
        self.assertEqual(self.sync(adapter)['incremental'],1)
        self.assertIn('allowed.txt',str(self.archive.db.get_messages('vscode_chat:private-vs')))
        # A fresh daemon has no parser cache/checkpoint. Restore any necessary
        # original prefix in RAM from the cloud cursor, without uploading traces.
        restarted=HttpArchiveRepository(self.remote)
        append(path,[{'kind':1,'k':['requests',0,'response',0,'value'],'v':'updated answer'}])
        self.assertEqual(self.sync(adapter,repo=restarted)['incremental'],1)
        self.assertTrue(all(secret.encode() not in body for body in self.remote.batches))
        self.assertNotIn(secret,str(self.archive.db.get_records('vscode_chat:private-vs')))
        self.assertIn('updated answer',str(self.archive.db.get_messages('vscode_chat:private-vs')))

    def test_server_rejects_unfiltered_tool_results(self):
        path,adapter=self.claude();doc,info=self.planned(adapter,path)
        value=packet(doc,info);value['instance']=self.archive.instance
        value['document']['metadata']['toolCallResults']={'call':'must not be stored'}
        with self.assertRaises(Problem) as error:self.archive.commit_batch(self.device['deviceId'],'bad',value,0)
        self.assertEqual(error.exception.code,'CONTENT_POLICY')
        self.assertIsNone(self.archive.db.get_session('claude:c'))

    def test_sanitized_adoption_keeps_legacy_only_prose_without_copying_processes(self):
        from local.service import ClaudeViewerService
        from local.sources.claude import ClaudeAdapter as LocalClaudeAdapter
        from server.manage import adopt
        path=self.source/'adopt.jsonl';secret='OLD_TOOL_PROCESS_MUST_NOT_LEAVE_712'
        write(path,[{'type':'user','sessionId':'adopt','message':{'content':'retained question'}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'x','name':'Agent','input':{'prompt':'retained task'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'x','content':secret}]}},
            {'type':'progress','content':secret}])
        oldpath=self.root/'original.db';old=ClaudeViewerService(oldpath,[LocalClaudeAdapter([path])],skip_recent_seconds=0)
        old.initialize();old.sync_sessions();path.unlink()
        with old._db.get_connection() as conn:conn.execute('UPDATE sessions SET available=0')
        before=hashlib.sha256(oldpath.read_bytes()).hexdigest();target=self.root/'filtered.db'
        self.assertEqual(adopt(oldpath,target)['retained'],1)
        new=CloudArchive(target)
        self.assertEqual(new.db.get_messages('claude:adopt')['total'],3)
        self.assertIn('retained task',str(new.db.get_messages('claude:adopt')))
        self.assertNotIn(secret,str(new.db.get_records('claude:adopt')))
        self.assertFalse(new.db.get_session('claude:adopt')['available'])
        self.assertEqual(hashlib.sha256(oldpath.read_bytes()).hexdigest(),before)
        self.assertIn(secret,str(old._db.get_records('claude:adopt')))

    def test_inline_subagent_transcript_is_skipped_before_any_batch_post(self):
        path=self.source/'not-named-agent.jsonl'
        write(path,[{'type':'user','sessionId':'child','isSidechain':True,'message':{'content':'PRIVATE_CHILD_USER'}},
                    {'type':'assistant','isSidechain':True,'message':{'content':'PRIVATE_CHILD_ASSISTANT'}}])
        result=self.sync(ClaudeAdapter([path]))
        self.assertEqual(result['skippedSubagents'],1,result)
        self.assertFalse(self.remote.batches)

    def test_real_http_transport_uses_simple_batch_post(self):
        from werkzeug.serving import make_server
        from threading import Thread
        server=make_server('127.0.0.1',0,None,threaded=True)
        origin=f'http://127.0.0.1:{server.server_port}'
        server.app=create_app(self.archive,admin_key=self.admin,session_secret='s'*48,public_origin=origin,allow_http=True)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            path,adapter=self.claude('HTTP synthetic question')
            remote=Remote(origin,self.device['token'],allow_http=True)
            repo=HttpArchiveRepository(remote)
            self.assertEqual(self.sync(adapter,repo=repo)['initialized'],1)
            self.assertEqual(self.archive.db.get_messages('claude:c')['total'],1)
        finally:server.shutdown();thread.join(5);server.server_close()

    def test_single_large_invocation_is_not_split_or_truncated(self):
        path=self.source/'large.jsonl';args={'prompt':'P'*12000}
        write(path,[{'type':'user','sessionId':'large','message':{'content':'question'}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'call','name':'Agent','input':args}]}}])
        result=SyncCoordinator(self.repo,[ClaudeAdapter([path])],quiet_seconds=0,batch_bytes=4096).run()
        self.assertEqual(result['failed'],0,result)
        self.assertTrue(any(len(body)>4096 for body in self.remote.batches))
        calls=[b for m in self.archive.db.get_messages('claude:large')['items'] for b in m['message']['content'] if b['type']=='tool_use']
        self.assertEqual(calls[0]['input'],args)

    def test_batch_progress_is_compact_cloud_owned_and_device_scoped(self):
        path,adapter=self.claude('PRIVATE_MESSAGE_NOT_IN_PROGRESS')
        self.assertEqual(self.sync(adapter)['initialized'],1)
        response=self.remote.call('POST','/sync/v1/progress/query',json={'files':[
            {'source':'claude','path':str(path)},{'source':'claude','path':str(self.source/'unknown.jsonl')}]})
        self.assertEqual(response['files'][0]['checkpoint'],{'offset':path.stat().st_size,'nextLine':1})
        self.assertEqual(response['files'][0]['revision'],1)
        self.assertIsNone(response['files'][1]['state'])
        self.assertNotIn('PRIVATE_MESSAGE_NOT_IN_PROGRESS',json.dumps(response))
        self.assertFalse(any('context' in f or 'messages' in f for f in response['files']))
        other=self.archive.add_device('other')
        foreign=FlaskRemote(self.app,other['token']).call('POST','/sync/v1/progress/query',json={'files':[{'source':'claude','path':str(path)}]})
        self.assertIsNone(foreign['files'][0]['state'])
        self.assertEqual(foreign['files'][0]['error']['code'],'REPLICA_CONFLICT')
        with self.assertRaises(Problem):self.remote.call('POST','/sync/v1/progress/query',json={'files':[{'source':'claude','path':'x'}]*129})

    def test_progress_query_windows_prefetch_descriptors_not_bodies(self):
        adapter=ClaudeAdapter([self.source]);entries=[]
        for i in range(7):
            path=self.source/f'{i}.jsonl';write(path,[{'type':'user','sessionId':str(i),'message':{'content':str(i)}}]);entries.append(SourceFile(path))
        original=adapter.parse
        def parse(entry):
            self.assertEqual(self.archive.db.list_sessions()[1],int(entry.path.stem))
            self.assertIn(str(entry.path),[f['path'] for f in self.remote.progress_requests[-1]])
            return original(entry)
        with patch.object(adapter,'discover',return_value=iter(entries)),patch.object(adapter,'parse',side_effect=parse):
            result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,progress_query_files=3).run()
        self.assertEqual(result['initialized'],7,result)
        self.assertEqual([len(files) for files in self.remote.progress_requests],[3,3,1])
        self.assertTrue(all(set(f)=={'source','path','modified','size','parserVersion'} for group in self.remote.progress_requests for f in group))

    def test_restart_mid_file_uses_cloud_cursor_without_local_state(self):
        path=self.source/'restart.jsonl';rows=[{'type':'user','sessionId':'restart','uuid':str(i),
            'message':{'content':str(i)+' '+('x'*500)}} for i in range(40)]
        write(path,rows);before=hashlib.sha256(path.read_bytes()).hexdigest();adapter=ClaudeAdapter([path])
        self.remote.lose_commit=True
        with self.assertRaises(Problem):SyncCoordinator(self.repo,[adapter],quiet_seconds=0,batch_bytes=8192).run()
        committed=self.archive.db.get_session('claude:restart')['messageCount']
        self.assertGreater(committed,0);self.assertLess(committed,40)
        self.assertEqual(self.repo.progress_window,{})
        fresh=HttpArchiveRepository(self.remote)
        with patch.object(adapter,'parse',side_effect=AssertionError('Must resume from cloud cursor, not replay the file')):
            result=SyncCoordinator(fresh,[adapter],quiet_seconds=0,batch_bytes=8192).run()
        self.assertEqual(result['failed'],0,result)
        self.assertEqual(self.archive.db.get_session('claude:restart')['messageCount'],40)
        self.assertEqual(self.archive.db.get_session('claude:restart')['userMessageCount'],40)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.assertFalse(hasattr(fresh,'state'))
        self.assertEqual(fresh.progress_window,{})
        self.assertEqual(fresh.replay.scope_states,{})
        self.assertFalse((self.root/'state.db').exists())

    def test_daemon_does_not_scan_until_cloud_command_and_writes_no_progress(self):
        import sys
        from deamon.syncer import main
        path,adapter=self.claude();token=self.root/'fixture.token';token.write_text(self.device['token'])
        roots=self.root/'roots.json';roots.write_text(json.dumps({'claude':[str(self.source)]}))
        files_before={str(p) for p in self.root.rglob('*') if p.is_file()}
        with patch.object(adapter,'discover',side_effect=AssertionError('Idle daemon must not scan')),\
             patch('deamon.syncer.Remote',return_value=self.remote),\
             patch('deamon.syncer.default_adapters',return_value=[adapter]),\
             patch('deamon.syncer.time.sleep',side_effect=KeyboardInterrupt),\
             patch.object(sys,'argv',['syncer','--cloud','https://cloud.test','--token-file',str(token),'--roots-file',str(roots)]):
            with self.assertRaises(KeyboardInterrupt):main()
        self.assertTrue(any(p=='/sync/v1/jobs/next' for _,p in self.remote.calls))
        self.assertFalse(any('heartbeat' in p or '/pulse' in p for _,p in self.remote.calls))
        self.assertFalse(self.remote.progress_requests)
        self.assertFalse(self.remote.batches)
        self.assertEqual({str(p) for p in self.root.rglob('*') if p.is_file()},files_before)

    def test_daemon_claims_cloud_task_then_queries_progress_and_syncs(self):
        import sys
        from deamon.syncer import main
        path,adapter=self.claude();token=self.root/'command.token';token.write_text(self.device['token'])
        roots=self.root/'command-roots.json';roots.write_text(json.dumps({'claude':[str(self.source)]}))
        job=self.archive.enqueue(self.device['deviceId'],'command-driven',['claude'])
        with patch('deamon.syncer.Remote',return_value=self.remote),\
             patch('deamon.syncer.default_adapters',return_value=[adapter]),\
             patch('deamon.syncer.time.sleep',side_effect=KeyboardInterrupt),\
             patch.object(sys,'argv',['syncer','--cloud','https://cloud.test','--token-file',str(token),
                                      '--roots-file',str(roots),'--quiet-seconds','0']):
            with self.assertRaises(KeyboardInterrupt):main()
        calls=[p for _,p in self.remote.calls]
        self.assertLess(calls.index('/sync/v1/jobs/next'),calls.index(f'/sync/v1/jobs/{job["id"]}/received'))
        self.assertLess(calls.index(f'/sync/v1/jobs/{job["id"]}/received'),calls.index('/sync/v1/progress/query'))
        self.assertEqual(calls.count('/sync/v1/jobs/next'),1)
        self.assertFalse(any('heartbeat' in p or '/pulse' in p for p in calls))
        self.assertLess(calls.index('/sync/v1/progress/query'),calls.index('/sync/v1/batches'))
        self.assertEqual(json.loads(self.remote.batches[0])['jobId'],job['id'])
        self.assertEqual(self.archive.db.get_session('claude:c')['messageCount'],1)
        self.assertEqual(self.archive.dashboard()[0]['job']['state'],'SUCCEEDED')
        self.assertFalse((self.root/'state.db').exists())

    def test_stateless_cli_has_no_state_option_or_sqlite_dependency(self):
        import subprocess,sys
        # Explicitly windowless subprocess on Windows; no user service is launched.
        result=subprocess.run([sys.executable,'-c',
            "import deamon.syncer,sys; assert 'sqlite3' not in sys.modules; assert not hasattr(deamon.syncer,'State')"],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr)
        result=subprocess.run([sys.executable,'-m','deamon','--help'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('--state',result.stdout)
        self.assertIn('--progress-query-files',result.stdout)

    def test_request_count_for_many_small_and_unchanged_files(self):
        paths=[]
        for i in range(20):
            path=self.source/f'load-{i}.jsonl';write(path,[{'type':'user','sessionId':f'load-{i}','message':{'content':'small'}}]);paths.append(path)
        adapter=ClaudeAdapter(paths);self.remote.calls.clear()
        result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,progress_query_files=8).run()
        self.assertEqual(result['initialized'],20,result)
        self.assertEqual(len(self.remote.calls),3+20+1)  # progress windows + POSTs + finish scan
        self.assertFalse(any(p.endswith('/context') or 'presence' in p for _,p in self.remote.calls))
        self.remote.calls.clear()
        result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,progress_query_files=8).run()
        self.assertEqual(result['unchanged'],20,result)
        self.assertEqual(len(self.remote.calls),3+1)
        self.assertFalse(any('presence' in p or 'heartbeat' in p or '/batches' in p for _,p in self.remote.calls))

    def test_commit_receipt_reuses_context_for_all_followup_batches(self):
        path,adapter=self.claude();self.sync(adapter)
        append(path,[{'type':'user','sessionId':'c','uuid':str(i),'message':{'content':'x'*500}} for i in range(40)])
        self.remote.calls.clear();self.remote.batches.clear()
        result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,batch_bytes=8192).run()
        self.assertEqual(result['failed'],0,result);self.assertGreater(len(self.remote.batches),1)
        self.assertFalse(any(p=='/sync/v1/context' for _,p in self.remote.calls))
        self.assertEqual(len(self.remote.calls),1+len(self.remote.batches)+1)
        self.assertEqual(self.archive.db.get_session('claude:c')['messageCount'],41)

    def test_no_history_upload_before_delivery_received_ack(self):
        job=self.archive.enqueue(self.device['deviceId'],'must-ack',[])
        delivery=self.remote.call('GET','/sync/v1/jobs/next',params={'workerId':'worker'})['delivery']
        path,adapter=self.claude();doc,info=self.planned(adapter,path);body=self.batch(doc,info)
        body.update(jobId=job['id'],workerId='worker')
        with self.assertRaises(Problem):self.archive.commit_batch(self.device['deviceId'],'before-ack',body,0)
        self.assertIsNone(self.archive.db.get_session('claude:c'))
        self.assertEqual(self.archive.dashboard()[0]['queueCount'],1)
        self.remote.call('POST',f'/sync/v1/jobs/{job["id"]}/received',json={'workerId':'worker','deliveryToken':delivery['deliveryToken']})
        self.archive.commit_batch(self.device['deviceId'],'after-ack',body,0)
        self.assertEqual(self.archive.db.get_session('claude:c')['messageCount'],1)

    def test_idle_poll_wait_is_one_minute_and_has_no_heartbeat(self):
        import sys
        from deamon.syncer import main
        path,adapter=self.claude();token=self.root/'idle.token';token.write_text(self.device['token'])
        with patch('deamon.syncer.Remote',return_value=self.remote),patch('deamon.syncer.default_adapters',return_value=[adapter]),\
             patch.object(adapter,'discover',side_effect=AssertionError('No command yet')),\
             patch('deamon.syncer.time.sleep',side_effect=KeyboardInterrupt) as sleep,\
             patch.object(sys,'argv',['syncer','--cloud','https://cloud.test','--token-file',str(token)]):
            with self.assertRaises(KeyboardInterrupt):main()
        sleep.assert_called_once_with(60)
        self.assertEqual(sum(p=='/sync/v1/jobs/next' for _,p in self.remote.calls),1)
        self.assertEqual(sum(p=='/sync/v1/connect' for _,p in self.remote.calls),1)
        self.assertFalse(any('heartbeat' in p or '/pulse' in p for _,p in self.remote.calls))

    def test_lost_task_received_response_resumes_same_cloud_task_after_restart(self):
        import sys
        from deamon.syncer import main
        path,adapter=self.claude();token=self.root/'receipt.token';token.write_text(self.device['token'])
        first=self.archive.enqueue(self.device['deviceId'],'lost-ack',['claude'])
        second=self.archive.enqueue(self.device['deviceId'],'later',['claude'])
        self.remote.lose_received=True
        arguments=['deamon','--cloud','https://cloud.test','--token-file',str(token),'--quiet-seconds','0']
        def once():
            with patch('deamon.syncer.Remote',return_value=self.remote),patch('deamon.syncer.default_adapters',return_value=[adapter]),\
                 patch('deamon.syncer.time.sleep',side_effect=KeyboardInterrupt),patch.object(sys,'argv',arguments):
                with self.assertRaises(KeyboardInterrupt):main()
        once()
        self.assertFalse(self.remote.batches)
        self.assertEqual(self.archive.dashboard()[0]['queueCount'],1)
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_jobs SET lease_until=0 WHERE id=?',(first['id'],))
        once()
        self.assertEqual(json.loads(self.remote.batches[0])['jobId'],first['id'])
        self.assertEqual(self.archive.dashboard()[0]['queue'][0]['id'],second['id'])
        with self.archive.db.get_connection() as conn:self.assertEqual(conn.execute('SELECT state FROM dist_jobs WHERE id=?',(first['id'],)).fetchone()[0],'SUCCEEDED')

    def test_daemon_finishes_each_task_before_fetching_next_fifo_task(self):
        import sys
        from deamon.syncer import main
        path,adapter=self.claude();token=self.root/'serial.token';token.write_text(self.device['token'])
        jobs=[self.archive.enqueue(self.device['deviceId'],f'fifo-{i}',['claude']) for i in range(3)]
        arguments=['deamon','--cloud','https://cloud.test','--token-file',str(token),'--quiet-seconds','0']
        with patch('deamon.syncer.Remote',return_value=self.remote),patch('deamon.syncer.default_adapters',return_value=[adapter]),\
             patch('deamon.syncer.time.sleep',side_effect=[None,KeyboardInterrupt]),patch.object(sys,'argv',arguments):
            with self.assertRaises(KeyboardInterrupt):main()
        paths=[p for _,p in self.remote.calls];gets=[i for i,p in enumerate(paths) if p=='/sync/v1/jobs/next']
        self.assertEqual(len(gets),2)
        self.assertLess(paths.index(f'/sync/v1/jobs/{jobs[0]["id"]}/finish'),gets[1])
        self.assertLess(gets[1],paths.index(f'/sync/v1/jobs/{jobs[1]["id"]}/received'))
        self.assertEqual(len(self.remote.batches),1)  # second task sees cloud progress, no duplicate upload
        self.assertEqual(self.archive.dashboard()[0]['queue'][0]['id'],jobs[2]['id'])

    def test_control_is_piggybacked_and_replayed_receipt_returns_latest_control(self):
        job=self.archive.enqueue(self.device['deviceId'],'control',[]);delivery=self.archive.next_job(self.device['deviceId'],'worker')
        self.archive.received(self.device['deviceId'],job['id'],'worker',delivery['deliveryToken'])
        path,adapter=self.claude();doc,info=self.planned(adapter,path);value=self.batch(doc,info);value.update(jobId=job['id'],workerId='worker')
        headers={'Content-Type':'application/json','Idempotency-Key':'control-key','If-Match':'"0"','X-Sync-Job':job['id'],'X-Sync-Worker':'worker'}
        first=self.remote.call('POST','/sync/v1/batches',data=encode_batch(value),headers=headers)
        self.assertEqual(first['control']['desired'],'run');self.assertIn('nextContext',first)
        self.archive.control(self.device['deviceId'],job['id'],'pause')
        repeated=self.remote.call('POST','/sync/v1/batches',data=encode_batch(value),headers=headers)
        self.assertEqual(repeated['control']['desired'],'pause')
        self.assertEqual(repeated['nextContext'],first['nextContext'])
        self.assertEqual(self.archive.db.get_session('claude:c')['messageCount'],1)

    def test_cancel_from_progress_query_stops_before_reading_history(self):
        path,adapter=self.claude();job=self.archive.enqueue(self.device['deviceId'],'cancel-before-body',[])
        delivery=self.archive.next_job(self.device['deviceId'],'worker')
        self.archive.received(self.device['deviceId'],job['id'],'worker',delivery['deliveryToken'])
        self.repo.job=job['id'];self.repo.worker='worker';self.archive.control(self.device['deviceId'],job['id'],'cancel')
        with patch.object(adapter,'parse',side_effect=AssertionError('Cancel before body')):
            result=self.sync(adapter)
        self.assertTrue(result['cancelled']);self.assertFalse(self.remote.batches)
        self.assertFalse(any('/commands' in p or 'heartbeat' in p for _,p in self.remote.calls))

    def test_tls_configuration_and_original_db_safeguards(self):
        for url in ['http://example.com','https://user:password@example.com','https://example.com/path','https://example.com?token=x']:
            with self.assertRaises(ValueError):Remote(url,'secret')
        with self.assertRaises(ValueError):create_app(self.archive,admin_key='a'*48,session_secret='b'*48,public_origin='http://example.com')
        original=Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):CloudArchive(original/'viewer.db')
        with self.assertRaises(ValueError):CloudArchive(original/'claude.db')


if __name__=='__main__':unittest.main()
