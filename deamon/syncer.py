"""Stateless local daemon: cloud-commanded scans and cloud-owned sync progress."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import random
import secrets
from threading import Event,Thread,Lock,local as thread_local
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
        self._thread=thread_local();self._sessions=[];self._sessions_lock=Lock()

    def session(self):
        if not hasattr(self._thread,'http'):
            http=requests.Session();http.trust_env=False;self._thread.http=http
            with self._sessions_lock:self._sessions.append(http)
        return self._thread.http

    def close(self):
        with self._sessions_lock:
            sessions,self._sessions=self._sessions,[]
        for http in sessions:http.close()

    def call(self,method,path,**kwargs):
        headers={'Authorization':'Bearer '+self.token,**kwargs.pop('headers',{})};deadline=time.monotonic()+120
        for attempt in range(5):
            try:
                left=max(1,deadline-time.monotonic())
                response=self.session().request(method,self.url+path,headers=headers,timeout=(min(10,left),min(90,left)),allow_redirects=False,**kwargs)
                try:
                    if 300<=response.status_code<400:raise Problem('REDIRECT_REFUSED','Cloud redirect refused',403)
                    try:value=response.json()
                    except ValueError:raise Problem('INVALID_RESPONSE','Cloud returned non-JSON response',502)
                    if response.ok:return value
                    error=value.get('error',{});code=error.get('code','HTTP_ERROR') if isinstance(error,dict) else 'HTTP_ERROR'
                    if response.status_code not in (429,500,502,503,504) or attempt==4 or time.monotonic()>=deadline:
                        raise Problem(code,error.get('message','Cloud request failed') if isinstance(error,dict) else 'Cloud request failed',response.status_code)
                    try:delay=max(0,float(response.headers.get('Retry-After',0)))
                    except ValueError:delay=0
                finally:
                    close=getattr(response,'close',None)
                    if close:close()
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
        self.job=None;self.worker=None;self.desired='run';self.last_control=time.monotonic();self.reset_run()

    def reset_run(self):
        self.revision=0;self.progress_window={};self.primary=None
        self.adapter=None;self.entry=None;self.current={};self.replay=Replay()

    def check_instance(self,value):
        if value['instance']!=self.caps['instance']:raise Problem('ARCHIVE_RESTORED','Cloud archive instance changed; reconnect',409)

    def call(self,method,path,**kwargs):
        headers=kwargs.pop('headers',{})
        if self.job:headers={**headers,'X-Sync-Job':self.job,'X-Sync-Worker':self.worker}
        value=self.remote.call(method,path,headers=headers,**kwargs)
        if isinstance(value,dict) and value.get('control') is not None:
            control=value['control'];self.desired=control['desired'];self.last_control=time.monotonic()
        return value

    def query_progress(self,files):
        if not files:return
        value=self.call('POST','/sync/v1/progress/query',json={'files':files,'markSeen':True,'includeContext':True})
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

    def use_context(self,value):
        self.check_instance(value)
        if 'scopesDelta' in value:
            scopes={} if value.get('resetScopes') else dict(self.current.get('scopes',{}))
            for scope,state in value['scopesDelta'].items():
                if state is None:scopes.pop(scope,None)
                else:scopes[scope]=state
            value={**value,'scopes':scopes};value.pop('scopesDelta',None)
        self.revision=value['revision'];self.current=value;self.primary=(value['context'] or {}).get('primary')
        return value

    def context(self,path=None,sid=None,source=None):
        return self.use_context(self.call('POST','/sync/v1/context',json={'path':path,'sessionId':sid,'source':source}))

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
        try:
            receipt=self.call('POST','/sync/v1/batches',data=body,headers={
                'Content-Type':'application/json','Idempotency-Key':key,'If-Match':f'"{self.revision}"'})
        except Problem as exc:
            if exc.code=='REVISION_CONFLICT':
                # New physical path referring to an existing logical session:
                # resolve only on conflict, not for every normal initial upload.
                context=self.context(sid=document.id)
                self.progress_window[(document.source,info['path'])]={'state':context['state'],'syncContext':context}
            raise
        self.check_instance(receipt)
        self.replay.advance(document,info)
        context=self.use_context(receipt['nextContext'])
        # This is the CLOUD'S committed ACK, held only in RAM. No extra context
        # round trip, independent local cursor or persistent progress is created.
        self.progress_window[(document.source,info['path'])]={'state':context['state'],'syncContext':context}
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
        self.batch_bytes=min(batch_bytes,repository.caps['maxRequestBytes']);self.progress={};self.desired='run';self.progress_lock=Lock()

    def recent(self,entry):
        return self.quiet_seconds>0 and time.time_ns()-max(p.stat().st_mtime_ns for p in (entry.path,*entry.dependencies))<self.quiet_seconds*1e9

    def control_point(self):
        while True:
            repo=self.repository
            if repo.job:
                remaining=60-(time.monotonic()-repo.last_control)
                if remaining<=0:repo.call('GET',f'/sync/v1/jobs/{repo.job}/commands')
                self.desired=repo.desired
            if self.desired=='cancel':raise Cancelled()
            if self.desired!='pause':return
            # Paused tasks poll only their own commands once per minute. They
            # never GET a new task or send a heartbeat while waiting.
            time.sleep(max(.1,60-(time.monotonic()-repo.last_control)))

    @contextmanager
    def working_log(self):
        stop=Event()
        def log_step():
            while not stop.wait(5):
                with self.progress_lock:progress=dict(self.progress)
                print('[syncer] working '+json.dumps(progress,ensure_ascii=False),flush=True)
        thread=Thread(target=log_step,daemon=True);thread.start()
        try:yield
        finally:stop.set();thread.join()

    def entries(self,adapter):
        """Batch only file descriptors. Bodies remain strictly sequential."""
        iterator=iter(adapter.discover());finished=False
        while not finished:
            self.control_point()
            window=[];error=None
            try:
                for _ in range(self.query_files):window.append(next(iterator))
            except StopIteration:finished=True
            except OSError as exc:error=exc;finished=True
            files=[{'source':adapter.source,'path':str(e.path),'modified':e.path.stat().st_mtime_ns//1_000_000,
                    'size':e.path.stat().st_size,'parserVersion':adapter.parser_version}
                   for e in window if not is_subagent(adapter.source,e.path)]
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
            return 'unchanged',False
        if summary.get('syncContext') is not None:
            value=repo.use_context(summary['syncContext'])
        elif state or summary.get('refresh'):
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
        def verify():
            self.control_point()
            if bounded.snapshot_fingerprint(document.records)!=info['fingerprint']:raise Problem('SOURCE_CHANGED','Source snapshot changed before batch submission')
            if self.recent(entry):raise Problem('SOURCE_RECENT','Source changed within the quiet period')
        receipt=repo.send(document,info,body,verify)
        with self.progress_lock:self.progress.update(batchBytes=len(body),revision=receipt['revision'])
        more=document.records.byte_count<entry.path.stat().st_size and bounds is not None
        return ('incremental' if json.loads(body)['document']['append_base'] else 'initialized'),more

    def run(self,sources=None):
        self.repository.reset_run()
        result={key:0 for key in ('initialized','incremental','unchanged','skippedRecent','skippedSubagents','skippedEmpty','failed')}
        result['errors']=[];seen=[];completed=[]
        try:
            with self.working_log():
                self.control_point()
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
                                    outcome='skippedRecent';result[outcome]+=1
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
                                                    if not self.repository.progress_window.get((adapter.source,path),{}).get('syncContext'):
                                                        self.repository.progress_window[(adapter.source,path)]={'refresh':True,'state':None}
                                                    continue
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
                self.repository.call('POST','/sync/v1/scan/finish',json={'sources':completed,'seen':seen})
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
    try:
        remote.call('POST','/sync/v1/connect',json=info)  # once per process, no heartbeat
        while True:
            try:
                delivery=remote.call('GET','/sync/v1/jobs/next',params={'workerId':worker})['delivery']
                if delivery and delivery.get('job'):
                    offered=delivery['job']
                    ack=remote.call('POST',f'/sync/v1/jobs/{offered["id"]}/received',
                        json={'workerId':worker,'deliveryToken':delivery['deliveryToken']})
                    if not ack['terminal']:
                        job=ack['job'];repo.job=job['id'];repo.worker=worker
                        repo.desired=ack['control']['desired'];repo.last_control=time.monotonic()
                        sync=SyncCoordinator(repo,adapters,quiet_seconds=args.quiet_seconds,batch_bytes=args.batch_bytes,progress_query_files=args.progress_query_files)
                        try:
                            try:result=sync.run(json.loads(job['sources']))
                            except Problem as exc:result={'failed':1,'errors':[{'code':exc.code}]}
                            remote.call('POST',f'/sync/v1/jobs/{job["id"]}/finish',json={'workerId':worker,'progress':sync.progress,'result':result})
                        finally:repo.job=None;repo.worker=None
                # Never fetch/prefetch another task while the current task is
                # executing or paused. Empty/finished polls wait a full minute.
            except Problem as exc:
                print(f'[syncer] cloud error: {exc.code}',flush=True)
                if exc.status in (401,403):raise SystemExit('Device credential expired/revoked; rotate or reauthorize it')
            time.sleep(60)
    finally:
        close=getattr(remote,'close',None)
        if close:close()


if __name__=='__main__':main()
