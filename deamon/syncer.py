"""Stateless local daemon: cloud-commanded scans and cloud-owned sync progress."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import random
import secrets
from threading import Event,Thread,Lock
import time
from urllib.parse import urlsplit

import requests
from . import incremental
from .sources import default_adapters
from .sources.base import SourceFile
from server.contracts.session_policy import is_subagent,header_is_subagent
from server.contracts.protocol import Problem,digest,packet,encode_batch,VERSION,SOURCES
from server.contracts.analysis import content_digest
from .replay import Replay
from .batching import PrefixPath,Boundaries


class Remote:
    def __init__(self,url,token,*,allow_http=False):
        parsed=urlsplit(url)
        if parsed.scheme!='https' and not (allow_http and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')):
            raise ValueError('Verified HTTPS required (HTTP requires explicit loopback development mode)')
        if parsed.username or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('Use a cloud origin without credentials, path, query or fragment')
        if not token or any(c.isspace() for c in token):raise ValueError('Invalid device credential')
        self.url=url.rstrip('/');self.token=token

    def call(self,method,path,**kwargs):
        headers={'Authorization':'Bearer '+self.token,**kwargs.pop('headers',{})};deadline=time.monotonic()+120
        for attempt in range(5):
            try:
                left=max(1,deadline-time.monotonic())
                with requests.Session() as http:
                    http.trust_env=False
                    response=http.request(method,self.url+path,headers=headers,timeout=(min(10,left),min(90,left)),allow_redirects=False,**kwargs)
                if 300<=response.status_code<400:raise Problem('REDIRECT_REFUSED','Cloud redirect refused',403)
                try:value=response.json()
                except ValueError:raise Problem('INVALID_RESPONSE','Cloud returned non-JSON response',502)
                if response.ok:return value
                error=value.get('error',{});code=error.get('code','HTTP_ERROR') if isinstance(error,dict) else 'HTTP_ERROR'
                if response.status_code not in (429,500,502,503,504) or attempt==4 or time.monotonic()>=deadline:
                    raise Problem(code,error.get('message','Cloud request failed') if isinstance(error,dict) else 'Cloud request failed',response.status_code)
                try:delay=max(0,float(response.headers.get('Retry-After',0)))
                except ValueError:delay=0
            except (requests.ConnectionError,requests.Timeout) as exc:
                if attempt==4 or time.monotonic()>=deadline:raise Problem('NETWORK_UNAVAILABLE','Cloud unavailable; next task must re-query cloud progress',503) from exc
                delay=0
            delay=max(delay,random.uniform(0,2**attempt))
            if time.monotonic()+delay>=deadline:raise Problem('NETWORK_UNAVAILABLE','Retry budget exhausted; next task must re-query cloud progress',503)
            time.sleep(delay)


class HttpArchiveRepository:
    """No local DB, checkpoint, receipt, pending queue or persistent parser cache."""
    def __init__(self,remote):
        self.remote=remote;self.caps=remote.call('GET','/sync/v1/capabilities')
        if self.caps.get('version')!=VERSION:raise Problem('PROTOCOL_VERSION','Unsupported cloud protocol',426)
        self.job=None;self.worker=None;self.reset_run()

    def reset_run(self):
        self.revision=0;self.progress_window={};self.primary=None
        self.adapter=None;self.entry=None;self.current={};self.replay=Replay()

    def check_instance(self,value):
        if value['instance']!=self.caps['instance']:raise Problem('ARCHIVE_RESTORED','Cloud archive instance changed; reconnect',409)

    def query_progress(self,files):
        if not files:return
        value=self.remote.call('POST','/sync/v1/progress/query',json={'files':files})
        self.check_instance(value)
        # Only the current bounded discovery window is held in RAM, never disk.
        require_keys=[(f['source'],f['path']) for f in files]
        received={(f['source'],f['path']):f for f in value['files']}
        if set(received)!=set(require_keys):raise Problem('INVALID_RESPONSE','Incomplete progress query response',502)
        self.progress_window=received

    def file_progress(self,path,source):
        key=(source,path)
        if key not in self.progress_window:self.query_progress([{'path':path,'source':source}])
        value=self.progress_window[key]
        if value.get('error'):
            error=value['error'];raise Problem(error['code'],error['message'],error['status'])
        return value

    def context(self,path=None,sid=None,source=None):
        value=self.remote.call('POST','/sync/v1/context',json={'path':path,'sessionId':sid,'source':source})
        self.check_instance(value);self.revision=value['revision'];self.current=value
        self.primary=(value['context'] or {}).get('primary')
        return value

    def load_scope_state(self,sid,scope):return self.replay.load_scope_state(sid,scope)
    def load_scope_records(self,sid,scope):return self.replay.load_scope_records(sid,scope)

    def prepare(self,document,info):
        value=packet(document,info,raw_context=self.replay.records,scope_context=self.replay.scope_states)
        value['instance']=self.caps['instance']
        data=value['document'];cloud_scopes=self.current.get('scopes',{})
        snapshot=info['path'].endswith('.json') and self.primary is not None
        if document.changed_scopes or snapshot:
            groups={}
            for message in data['messages']:groups.setdefault(data['message_scopes'][message['id']],[]).append(message)
            candidates=(set(groups)|set(cloud_scopes)) if snapshot else set(document.changed_scopes)
            changed={scope for scope in candidates if content_digest(groups.get(scope,[]))!=cloud_scopes.get(scope,{}).get('analysisDigest')}
            value['changedScopes']=sorted(changed)
            data['messages']=[m for m in data['messages'] if data['message_scopes'][m['id']] in changed]
            ids={m['id'] for m in data['messages']}
            data['message_scopes']={k:v for k,v in data['message_scopes'].items() if k in ids}
            data['message_order']={k:v for k,v in data['message_order'].items() if k in ids}
        if (document.append_base or snapshot) and self.primary:
            p=self.primary;data['append_base']={'path':p['path'],'fingerprint':p['fingerprint'],
                'parserVersion':p['parser_version'],'messageCount':p['message_count']}
        if self.job:value.update(jobId=self.job,workerId=self.worker)
        return encode_batch(value)

    def send(self,document,info,body,verify):
        if len(body)>self.caps['maxRequestBytes']:raise Problem('TOO_LARGE','An indivisible record/state exceeds the advertised request maximum; not truncated',413)
        verify()
        # Stable across replay/restart for the same base and exact logical data.
        # No client outbox is needed: a new process queries committed cloud
        # progress; CAS protects against a delayed previous request committing.
        identity={'instance':self.caps['instance'],'device':self.caps['deviceId'],
                  'revision':self.revision,'batch':json.loads(body)}
        key=digest(identity)
        receipt=self.remote.call('POST','/sync/v1/batches',data=body,headers={
            'Content-Type':'application/json','Idempotency-Key':key,'If-Match':f'"{self.revision}"'})
        self.check_instance(receipt)
        self.replay.advance(document,info)
        # Force a fresh authoritative context for the next batch in this file.
        self.progress_window[(document.source,info['path'])]={'refresh':True,'state':None}
        return receipt

    def release_file(self):
        self.replay.reset();self.primary=None
        self.current={};self.adapter=None;self.entry=None


class Cancelled(Exception):pass


class SyncCoordinator:
    def __init__(self,repository,adapters,*,quiet_seconds=3600,batch_bytes=1024*1024,progress_query_files=64):
        if not isinstance(quiet_seconds,(int,float)) or isinstance(quiet_seconds,bool) or quiet_seconds<0:raise ValueError('Invalid quiet period')
        if not isinstance(batch_bytes,int) or isinstance(batch_bytes,bool) or batch_bytes<256:raise ValueError('Batch target must be >=256 bytes')
        if not isinstance(progress_query_files,int) or isinstance(progress_query_files,bool) or progress_query_files<1:raise ValueError('Invalid progress query window')
        self.repository=repository;self.adapters=adapters;self.quiet_seconds=quiet_seconds
        self.query_files=min(progress_query_files,repository.caps['maxProgressFiles'])
        self.batch_bytes=min(batch_bytes,repository.caps['maxRequestBytes']);self.progress={};self.desired='run';self.progress_lock=Lock();self.control_error=None

    def recent(self,entry):
        return self.quiet_seconds>0 and time.time_ns()-max(p.stat().st_mtime_ns for p in (entry.path,*entry.dependencies))<self.quiet_seconds*1e9

    def control_point(self):
        while True:
            if self.control_error:raise self.control_error
            if self.desired=='cancel':raise Cancelled()
            if self.desired!='pause':return
            time.sleep(.25)

    @contextmanager
    def heartbeat(self):
        stop=Event()
        def pulse():
            while not stop.wait(5):
                with self.progress_lock:progress=dict(self.progress)
                print('[syncer] working '+json.dumps(progress,ensure_ascii=False),flush=True)
                if self.repository.job:
                    try:self.desired=self.repository.remote.call('POST',f'/sync/v1/jobs/{self.repository.job}/pulse',json={'workerId':self.repository.worker,'progress':progress})['desired']
                    except Problem as exc:self.control_error=exc
        thread=Thread(target=pulse,daemon=True);thread.start()
        try:yield
        finally:stop.set();thread.join()

    def entries(self,adapter):
        """Batch only file descriptors. Bodies remain strictly sequential."""
        iterator=iter(adapter.discover());finished=False
        while not finished:
            window=[];error=None
            try:
                for _ in range(self.query_files):window.append(next(iterator))
            except StopIteration:finished=True
            except OSError as exc:error=exc;finished=True
            files=[{'source':adapter.source,'path':str(e.path)} for e in window if not is_subagent(adapter.source,e.path)]
            if files:
                self.control_point()
                with self.progress_lock:self.progress={'source':adapter.source,'step':'batch-query-cloud-progress','files':len(files)}
                self.repository.query_progress(files)
            yield from window
            if error:raise error

    def file(self,adapter,entry):
        repo=self.repository;path=str(entry.path);summary=repo.file_progress(path,adapter.source);state=summary['state']
        modified=entry.path.stat().st_mtime_ns//1_000_000
        if state and state['modified']==modified and state['parser_version']==adapter.parser_version and state['fingerprint']==entry.fingerprint():
            repo.remote.call('POST','/sync/v1/presence',json={'path':path});return 'unchanged',False
        if state or summary.get('refresh'):
            value=repo.context(path=path,source=adapter.source)
        else:
            value={'context':None,'checkpoint':None,'revision':0,'scopes':{}};repo.revision=0;repo.current=value;repo.primary=None
        context=value['context'];repo.adapter=adapter;repo.entry=entry
        checkpoint=value.get('checkpoint');cursor=(checkpoint or {}).get('state',{}).get('cursor')
        repo.replay.seed(adapter,entry,value)
        local_context,local_checkpoint=repo.replay.context()
        start=cursor['offset'] if cursor else 0;bounded=entry;bounds=None
        if entry.path.suffix=='.jsonl' and (cursor or context is None):
            bounds=Boundaries(entry.path,min(start,entry.path.stat().st_size));end=bounds.extend(self.batch_bytes)
            bounded=SourceFile(PrefixPath(entry.path,end),entry.project,entry.dependencies)
        def project(candidate):
            document=None
            if local_context:
                if adapter.source=='claude':document=adapter.parse_append(candidate,local_context['session'],local_context['primary'])
                else:document=incremental.update(adapter,candidate,local_context['session'],local_context['primary'],local_checkpoint,repo)
            if document is None:document=incremental.initialize(adapter,candidate,adapter.parse(candidate))
            conversation=[r for _,r in document.records if r.get('type') in ('user','assistant')]
            if conversation and all(r.get('isSidechain') is True or r.get('isSubagent') is True for r in conversation):
                raise Problem('SUBAGENT_EXCLUDED','Subagent internal conversations are not transmitted',422)
            info={'path':path,'fingerprint':candidate.snapshot_fingerprint(document.records),
                  'modified':document.records.modified_ns//1_000_000,'parserVersion':adapter.parser_version}
            return document,info,repo.prepare(document,info)
        document,info,body=project(bounded)
        if is_subagent(adapter.source,entry.path,document.metadata):return 'skippedSubagents',False
        if bounds:
            while len(body)<self.batch_bytes//2 and not bounds.eof and (not bounds.ends or bounds.ends[-1]-bounds.start<8*self.batch_bytes):
                bounds.extend(max(self.batch_bytes,2*(bounds.ends[-1]-bounds.start if bounds.ends else 1)))
                bounded=SourceFile(PrefixPath(entry.path,bounds.ends[-1] if bounds.ends else bounds.start),entry.project,entry.dependencies)
                document,info,body=project(bounded)
            if len(body)>self.batch_bytes and len(bounds.ends)>1:
                low,high=1,len(bounds.ends);best=None;first_retained=None
                while low<=high:
                    count=(low+high)//2;candidate=SourceFile(PrefixPath(entry.path,bounds.ends[count-1]),entry.project,entry.dependencies)
                    doc,meta,encoded=project(candidate);retained=True
                    if retained and (first_retained is None or count<first_retained[0]):first_retained=(count,candidate,doc,meta,encoded)
                    if not retained:low=count+1
                    elif len(encoded)<=self.batch_bytes:best=(candidate,doc,meta,encoded);low=count+1
                    else:high=count-1
                if best:bounded,document,info,body=best
                elif first_retained:_,bounded,document,info,body=first_retained
        if context is None:repo.context(sid=document.id)
        def verify():
            if bounded.snapshot_fingerprint(document.records)!=info['fingerprint']:raise Problem('SOURCE_CHANGED','Source snapshot changed before batch submission')
            if self.recent(entry):raise Problem('SOURCE_RECENT','Source changed within the quiet period')
            if self.control_error:raise self.control_error
        receipt=repo.send(document,info,body,verify)
        with self.progress_lock:self.progress.update(batchBytes=len(body),revision=receipt['revision'])
        more=document.records.byte_count<entry.path.stat().st_size and bounds is not None
        return ('incremental' if json.loads(body)['document']['append_base'] else 'initialized'),more

    def run(self,sources=None):
        self.repository.reset_run()
        result={key:0 for key in ('initialized','incremental','unchanged','skippedRecent','skippedSubagents','skippedEmpty','failed')}
        result['errors']=[];seen=[];completed=[]
        try:
            with self.heartbeat():
                for adapter in self.adapters:
                    if sources and adapter.source not in sources:continue
                    complete=bool(adapter.roots) and all(p.exists() for p in adapter.roots)
                    try:
                        for entry in self.entries(adapter):
                            self.control_point();path=str(entry.path);seen.append(path);started=time.monotonic();outcome='failed'
                            with self.progress_lock:self.progress={'source':adapter.source,'file':entry.path.name,'step':'stat/recent-check'}
                            print(f'[syncer] start {adapter.source} / {entry.path.name}',flush=True)
                            try:
                                if is_subagent(adapter.source,entry.path):outcome='skippedSubagents';result[outcome]+=1
                                elif self.recent(entry):
                                    self.repository.remote.call('POST','/sync/v1/presence',json={'path':path});outcome='skippedRecent';result[outcome]+=1
                                elif header_is_subagent(adapter.source,entry.path):outcome='skippedSubagents';result[outcome]+=1
                                else:
                                    more=True
                                    while more:
                                        self.control_point()
                                        with self.progress_lock:self.progress['step']='parse/filter/batch-post'
                                        for attempt in range(3):
                                            try:outcome,more=self.file(adapter,entry);break
                                            except Problem as exc:
                                                if exc.code=='REVISION_CONFLICT' and attempt<2:
                                                    self.repository.progress_window[(adapter.source,path)]={'refresh':True,'state':None};continue
                                                raise
                                        result[outcome]+=1
                            except Problem as exc:
                                if exc.status in (401,403,429,500,502,503,504) or exc.code in ('LEASE_LOST','ARCHIVE_RESTORED'):raise
                                if exc.code=='SUBAGENT_EXCLUDED':outcome='skippedSubagents';result[outcome]+=1
                                else:result['failed']+=1;result['errors'].append({'source':adapter.source,'file':entry.path.name,'code':exc.code})
                            except (ValueError,OSError) as exc:
                                result['failed']+=1;result['errors'].append({'source':adapter.source,'file':entry.path.name,'code':type(exc).__name__})
                            finally:
                                self.repository.release_file()
                                print(f'[syncer] end {outcome} / {entry.path.name} ({time.monotonic()-started:.2f}s)',flush=True)
                        self.control_point()
                    except Problem:raise
                    except OSError:
                        complete=False;result['failed']+=1;result['errors'].append({'source':adapter.source,'code':'DISCOVERY_FAILED'})
                    if complete:completed.append(adapter.source)
                self.repository.remote.call('POST','/sync/v1/scan/finish',json={'sources':completed,'seen':seen})
        except Cancelled:result['cancelled']=True
        finally:self.repository.reset_run()
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud',required=True);parser.add_argument('--token-file',required=True)
    parser.add_argument('--roots-file',help='Locally approved source roots JSON')
    parser.add_argument('--quiet-seconds',type=float,default=3600);parser.add_argument('--batch-bytes',type=int,default=1024*1024)
    parser.add_argument('--progress-query-files',type=int,default=64,help='Metadata-only cloud progress query window')
    parser.add_argument('--daemon',action='store_true',help='Compatibility flag; daemon is now the only/default mode')
    parser.add_argument('--allow-http-loopback',action='store_true')
    args=parser.parse_args()
    if args.quiet_seconds<0 or args.batch_bytes<256 or args.progress_query_files<1:
        parser.error('quiet-seconds must be nonnegative, batch-bytes >=256 and progress-query-files positive')
    roots=json.loads(Path(args.roots_file).read_text()) if args.roots_file else None
    if roots is not None:
        if not isinstance(roots,dict) or set(roots)-set(SOURCES):parser.error('roots-file must map known source IDs to local paths')
        roots={source:roots.get(source,[]) for source in SOURCES}
    adapters=default_adapters(roots)
    remote=Remote(args.cloud,Path(args.token_file).read_text().strip(),allow_http=args.allow_http_loopback)
    repo=HttpArchiveRepository(remote)
    if not repo.caps['remoteControl']:raise SystemExit('Enable remote sync control for this device in the cloud before starting its daemon')
    info={'sources':[{'source':a.source,'roots':[str(p) for p in a.roots]} for a in adapters],'remoteControl':True,'stateless':True}
    worker=secrets.token_hex(16)
    while True:
        try:
            remote.call('POST','/sync/v1/heartbeat',json=info);job=remote.call('POST','/sync/v1/jobs/claim',json={'workerId':worker})['job']
            if job:
                repo.job=job['id'];repo.worker=worker
                sync=SyncCoordinator(repo,adapters,quiet_seconds=args.quiet_seconds,batch_bytes=args.batch_bytes,progress_query_files=args.progress_query_files);sync.desired=job['desired']
                try:result=sync.run(json.loads(job['sources']))
                except Problem as exc:result={'failed':1,'errors':[{'code':exc.code}]}
                remote.call('POST',f'/sync/v1/jobs/{job["id"]}/pulse',json={'workerId':worker,'progress':sync.progress,'result':result})
                repo.job=None;repo.worker=None
        except Problem as exc:
            print(f'[syncer] cloud error: {exc.code}',flush=True)
            if exc.status in (401,403):raise SystemExit('Device credential expired/revoked; rotate or reauthorize it')
        time.sleep(3)


if __name__=='__main__':main()
