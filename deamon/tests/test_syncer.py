"""Deamon project unit tests. No Flask, SQLite, Docker or live cloud required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
from deamon.syncer import HttpArchiveRepository,SyncCoordinator,Remote
from deamon.sources.claude import ClaudeAdapter
from server.contracts.protocol import VERSION,decode,digest

ROOT=Path(__file__).resolve().parents[2]


class CloudStub:
    """Authoritative fake cloud; all state belongs to this peer, not the client."""
    url='https://unit.test'
    def __init__(self):self.files={};self.sessions={};self.calls=[];self.batches=[];self.queries=[]
    def call(self,method,path,**kw):
        self.calls.append((method,path))
        if path=='/sync/v1/capabilities':return {'version':VERSION,'instance':'unit','deviceId':'device','remoteControl':True,'maxRequestBytes':1024*1024,'maxProgressFiles':128}
        if path=='/sync/v1/progress/query':
            files=kw['json']['files'];self.queries.append(files)
            return {'instance':'unit','files':[{'source':f['source'],'path':f['path'],'state':self.files.get(f['path']),'revision':1 if f['path'] in self.files else 0,'checkpoint':None} for f in files]}
        if path=='/sync/v1/context':
            query=kw['json'];state=self.files.get(query.get('path'));sid=(state or {}).get('session_id') or query.get('sessionId')
            saved=self.sessions.get(sid)
            return {'instance':'unit','state':state,'context':saved,'checkpoint':None,'revision':1 if saved else 0,'scopes':{}}
        if path=='/sync/v1/batches':
            value=json.loads(kw['data']);doc,info=decode(value);self.batches.append(value)
            state={'path':info['path'],'source':doc.source,'session_id':doc.id,'fingerprint':info['fingerprint'],
                   'modified':info['modified'],'parser_version':info['parserVersion'],'message_count':len(doc.messages),'available':1}
            self.files[info['path']]=state
            self.sessions[doc.id]={'session':{'id':doc.id,'source':doc.source,'externalId':doc.external_id,'project':doc.project,
                    'name':doc.name,'metadata':doc.metadata,'messageCount':len(doc.messages)},
                    'primary':{'path':info['path'],'fingerprint':info['fingerprint'],'parser_version':info['parserVersion'],'message_count':len(doc.messages)}}
            return {'instance':'unit','revision':1,'fingerprint':info['fingerprint']}
        if path in ('/sync/v1/scan/finish','/sync/v1/presence','/sync/v1/heartbeat'):return {'ok':True}
        if path=='/sync/v1/jobs/claim':return {'job':None}
        raise AssertionError(path)


class DeamonUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.peer=CloudStub();self.repo=HttpArchiveRepository(self.peer)

    def source(self,name='s',content='question'):
        path=self.root/(name+'.jsonl');path.write_text(json.dumps({'type':'user','sessionId':name,'message':{'content':content}})+'\n')
        return path

    def test_cloud_progress_survives_a_new_stateless_client_without_local_files(self):
        path=self.source();before={p.name for p in self.root.iterdir()}
        result=SyncCoordinator(self.repo,[ClaudeAdapter([path])],quiet_seconds=0).run()
        self.assertEqual(result['initialized'],1)
        self.assertEqual({p.name for p in self.root.iterdir()},before)
        self.assertEqual(self.repo.progress_window,{})
        fresh=HttpArchiveRepository(self.peer)
        result=SyncCoordinator(fresh,[ClaudeAdapter([path])],quiet_seconds=0).run()
        self.assertEqual(result['unchanged'],1)
        self.assertEqual(len(self.peer.batches),1)

    def test_progress_query_is_bounded_metadata_not_a_full_scan_barrier(self):
        paths=[self.source(str(i)) for i in range(5)];adapter=ClaudeAdapter(paths)
        result=SyncCoordinator(self.repo,[adapter],quiet_seconds=0,progress_query_files=2).run()
        self.assertEqual(result['initialized'],5)
        self.assertEqual([len(q) for q in self.peer.queries],[2,2,1])
        self.assertTrue(all(set(f)=={'path','source'} for q in self.peer.queries for f in q))
        self.assertTrue(all('records' not in b['document'] for b in self.peer.batches))

    def test_recent_file_never_reads_header_or_body(self):
        path=self.source();adapter=ClaudeAdapter([path])
        with patch.object(adapter,'parse',side_effect=AssertionError('No body read')),patch('deamon.syncer.header_is_subagent',side_effect=AssertionError('No header read')):
            result=SyncCoordinator(self.repo,[adapter]).run()
        self.assertEqual(result['skippedRecent'],1);self.assertFalse(self.peer.batches)

    def test_idle_command_loop_does_not_discover_history(self):
        from deamon.syncer import main
        path=self.source();adapter=ClaudeAdapter([path]);key=self.root/'device.token';key.write_text('synthetic-token')
        with patch('deamon.syncer.Remote',return_value=self.peer),patch('deamon.syncer.default_adapters',return_value=[adapter]),\
             patch.object(adapter,'discover',side_effect=AssertionError('No command yet')),\
             patch('deamon.syncer.time.sleep',side_effect=KeyboardInterrupt),\
             patch.object(sys,'argv',['deamon','--cloud','https://unit.test','--token-file',str(key)]):
            with self.assertRaises(KeyboardInterrupt):main()
        self.assertFalse(self.peer.queries);self.assertFalse(self.peer.batches)

    def test_transient_http_retry_reuses_exact_body_and_credentials(self):
        remote=Remote('https://unit.test','synthetic-token');attempts=[]
        class Response:
            status_code=200;ok=True
            def json(self):return {'ok':True}
        class Session:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,*args,**kw):
                attempts.append(kw)
                if len(attempts)==1:raise requests.Timeout()
                return Response()
        with patch('deamon.syncer.requests.Session',Session),patch('deamon.syncer.time.sleep'):
            self.assertEqual(remote.call('POST','/sync/v1/batches',data=b'exact-body',headers={'Idempotency-Key':'key'}),{'ok':True})
        self.assertEqual(attempts[0]['data'],attempts[1]['data'])
        self.assertEqual(attempts[0]['headers'],attempts[1]['headers'])
        self.assertFalse(attempts[0]['allow_redirects']);self.assertNotEqual(attempts[0].get('verify'),False)

    def test_invalid_config_and_remote_origin_are_refused(self):
        for args in ({'quiet_seconds':-1},{'batch_bytes':0},{'progress_query_files':0}):
            with self.assertRaises(ValueError):SyncCoordinator(self.repo,[],**args)
        for url in ('http://public.test','https://key:secret@unit.test','https://unit.test/path'):
            with self.assertRaises(ValueError):Remote(url,'token')

    def test_client_import_has_no_server_runtime_dependencies(self):
        code="import deamon.syncer,sys; assert 'sqlite3' not in sys.modules; assert 'flask' not in sys.modules; assert 'server.app' not in sys.modules"
        r=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(r.returncode,0,r.stderr)


if __name__=='__main__':unittest.main()
