"""Single-owner cloud archive, atomic batch POSTs, durable idempotent receipts."""
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from threading import local
import time

from .codec import pack_json, unpack_json
from .db import Database, encode
from .contracts.protocol import Problem, decode, digest, require, SOURCES
from .contracts.analysis import POLICY,content_digest


class AtomicDatabase(Database):
    def __init__(self,path):
        super().__init__(path);self._local=local()

    @contextmanager
    def get_connection(self):
        active=getattr(self._local,'connection',None)
        if active is not None:yield active
        else:
            with super().get_connection() as conn:yield conn

    @contextmanager
    def transaction(self,*,write=True):
        if getattr(self._local,'connection',None) is not None:raise RuntimeError('Nested outer transaction')
        with super().get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN');self._local.connection=conn
            try:yield conn
            finally:del self._local.connection


class CloudArchive:
    def __init__(self,path,*,max_request=32*1024*1024):
        path=Path(path).resolve();original=Path(__file__).resolve().parents[1]
        if path in (original/'viewer.db',original/'claude.db',original/'local/viewer.db',original/'local/claude.db'):
            raise ValueError('Distributed mode must use a separate database, not the original local archive')
        if path.exists() and path.stat().st_size:
            probe=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            try:
                tables={r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                nonempty='sessions' in tables and probe.execute('SELECT 1 FROM sessions LIMIT 1').fetchone()
                policy=probe.execute("SELECT value FROM dist_settings WHERE key='content_policy'").fetchone() if 'dist_settings' in tables else None
                if nonempty and (not policy or policy[0]!=POLICY):
                    raise ValueError('Existing archive is not analysis-only. Run server.manage adopt into a NEW database; no automatic data deletion/migration.')
            finally:probe.close()
        self.db=AtomicDatabase(path);self.max_request=max_request;self.db.init_schema()
        with self.db.get_connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS dist_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dist_devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,active INTEGER NOT NULL,control INTEGER NOT NULL,
                    last_seen REAL NOT NULL DEFAULT 0,info TEXT NOT NULL DEFAULT '{}',expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS dist_sessions(id TEXT PRIMARY KEY,device TEXT NOT NULL,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS dist_receipts(device TEXT NOT NULL,operation_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(device,operation_key));
                CREATE TABLE IF NOT EXISTS dist_jobs(id TEXT PRIMARY KEY,device TEXT NOT NULL,
                    operation_key TEXT NOT NULL,state TEXT NOT NULL,desired TEXT NOT NULL,sources TEXT NOT NULL,
                    worker TEXT,lease_until REAL NOT NULL DEFAULT 0,created REAL NOT NULL,expires REAL NOT NULL,
                    progress TEXT NOT NULL DEFAULT '{}',result TEXT,UNIQUE(device,operation_key));
            ''')
            conn.execute("INSERT OR IGNORE INTO dist_settings VALUES('content_policy',?)",(POLICY,))
            conn.execute("INSERT OR IGNORE INTO dist_settings VALUES('instance',?)",(secrets.token_hex(16),))
            self.instance=conn.execute("SELECT value FROM dist_settings WHERE key='instance'").fetchone()[0]

    def add_device(self,name,*,control=False,adopt=False):
        require(isinstance(name,str) and 0<len(name)<=160,'Invalid device name',status=400)
        device=secrets.token_hex(16);token=secrets.token_urlsafe(48)
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO dist_devices(id,name,token_hash,active,control,expires) VALUES(?,?,?,1,?,?)',
                         (device,name,hashlib.sha256(token.encode()).hexdigest(),int(control),time.time()+30*86400))
            if adopt:conn.execute('INSERT OR IGNORE INTO dist_sessions SELECT id,?,1 FROM sessions',(device,))
        return {'deviceId':device,'token':token,'control':bool(control)}

    def authenticate(self,token):
        sha=hashlib.sha256(token.encode()).hexdigest()
        with self.db.get_connection() as conn:
            row=conn.execute('SELECT * FROM dist_devices WHERE token_hash=? AND active=1 AND expires>?',(sha,time.time())).fetchone()
            if row is None or not hmac.compare_digest(row['token_hash'],sha):raise Problem('UNAUTHORIZED','Invalid or revoked device credential',401)
            return dict(row)

    def device(self,conn,device):
        row=conn.execute('SELECT * FROM dist_devices WHERE id=? AND active=1',(device,)).fetchone()
        require(row is not None and row['expires']>time.time(),'Device revoked or expired','UNAUTHORIZED',401)
        return row

    def rotate_device(self,device):
        token=secrets.token_urlsafe(48)
        with self.db.transaction() as conn:
            require(conn.execute('SELECT 1 FROM dist_devices WHERE id=?',(device,)).fetchone() is not None,'Unknown device','NOT_FOUND',404)
            conn.execute('UPDATE dist_devices SET token_hash=?,active=1,expires=? WHERE id=?',
                         (hashlib.sha256(token.encode()).hexdigest(),time.time()+30*86400,device))
        return {'deviceId':device,'token':token}

    def binding(self,conn,device,sid,expected=None):
        self.device(conn,device);row=conn.execute('SELECT * FROM dist_sessions WHERE id=?',(sid,)).fetchone()
        if row:
            require(row['device']==device,'Session owned by another device','REPLICA_CONFLICT');revision=row['revision']
        else:
            require(not conn.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone(),'Existing archive requires explicit device adoption','ADOPTION_REQUIRED')
            revision=0
        if expected is not None:require(revision==expected,'Archive revision changed','REVISION_CONFLICT',412)
        return revision

    def context(self,device,path=None,sid=None,source=None):
        with self.db.transaction(write=False) as conn:
            self.device(conn,device)
            state=conn.execute('SELECT * FROM source_files WHERE path=?',(path,)).fetchone() if path else None
            if state:
                require(state['source']==source,'Source identity changed','SOURCE_CHANGED');sid=state['session_id']
            revision=self.binding(conn,device,sid) if sid else 0
            context=self.db.append_context(sid) if sid else None
            scopes={r[0]:unpack_json(r[1]) for r in conn.execute('SELECT scope,state FROM projection_state WHERE session_id=?',(sid,))} if context else {}
            return {'instance':self.instance,'state':dict(state) if state else None,'context':context,
                    'checkpoint':self.db.get_sync_checkpoint(sid) if context else None,'revision':revision,'scopes':scopes}

    def query_progress(self,device,files):
        """One revision-consistent metadata query; no message/scope bodies."""
        require(isinstance(files,list) and 0<len(files)<=128,'Query 1..128 file descriptors',status=400)
        for item in files:
            require(isinstance(item,dict) and item.get('source') in SOURCES and
                    isinstance(item.get('path'),str) and 0<len(item['path'])<=8192,'Invalid file descriptor',status=400)
        paths=list({item['path'] for item in files});marks=','.join('?' for _ in paths)
        with self.db.transaction(write=False) as conn:
            self.device(conn,device)
            states={r['path']:dict(r) for r in conn.execute(f'SELECT * FROM source_files WHERE path IN ({marks})',paths)}
            sids=list({r['session_id'] for r in states.values()})
            bindings={};existing=set();metadata={};checkpoints={}
            if sids:
                placeholders=','.join('?' for _ in sids)
                bindings={r['id']:dict(r) for r in conn.execute(f'SELECT * FROM dist_sessions WHERE id IN ({placeholders})',sids)}
                metadata={r[0]:json.loads(r[1]) for r in conn.execute(f'SELECT id,metadata FROM sessions WHERE id IN ({placeholders})',sids)}
                existing=set(metadata)
                checkpoints={r[0]:unpack_json(r[1]).get('cursor') for r in conn.execute(f'SELECT session_id,state FROM sync_checkpoints WHERE session_id IN ({placeholders})',sids)}
            result=[]
            for item in files:
                state=states.get(item['path']);entry={'path':item['path'],'source':item['source'],'state':None,'revision':0,'checkpoint':None}
                code=None
                if state:
                    binding=bindings.get(state['session_id'])
                    if state['source']!=item['source']:code='SOURCE_CHANGED'
                    elif binding and binding['device']!=device:code='REPLICA_CONFLICT'
                    elif not binding and state['session_id'] in existing:code='ADOPTION_REQUIRED'
                    else:
                        cursor=checkpoints.get(state['session_id'])
                        compact={k:cursor[k] for k in ('offset','nextLine') if k in cursor} if cursor else None
                        entry.update(state=state,revision=binding['revision'] if binding else 0,checkpoint=compact)
                if code:entry['error']={'code':code,'message':'File progress is not available to this device/source','status':409}
                result.append(entry)
            return {'instance':self.instance,'files':result}

    def receipt(self,device,key):
        with self.db.get_connection() as conn:
            self.device(conn,device)
            row=conn.execute('SELECT receipt FROM dist_receipts WHERE device=? AND operation_key=?',(device,key)).fetchone()
            return json.loads(row[0]) if row else None

    def commit_batch(self,device,key,value,revision):
        require(isinstance(key,str) and 0<len(key)<=160,'Idempotency key required',status=400)
        payload_hash=digest(value)
        with self.db.transaction() as conn:
            self.device(conn,device)
            previous=conn.execute('SELECT * FROM dist_receipts WHERE device=? AND operation_key=?',(device,key)).fetchone()
            if previous:
                require(previous['payload_hash']==payload_hash,'Same key with different batch','IDEMPOTENCY_MISMATCH')
                return json.loads(previous['receipt'])
            require(value.get('instance')==self.instance,'Cloud instance changed; reconnect','ARCHIVE_RESTORED')
            document,info=decode(value);sid=document.id;self.binding(conn,device,sid,revision)
            file_owner=conn.execute('SELECT session_id FROM source_files WHERE path=?',(info['path'],)).fetchone()
            if file_owner:self.binding(conn,device,file_owner['session_id'])
            if value.get('jobId'):self.check_lease(conn,device,value['jobId'],value.get('workerId'))
            old=self.db.append_context(sid);base=document.append_base
            if base:
                require(old is not None,'Increment requires an existing session','REVISION_CONFLICT',412)
                require(old['primary']['path']==info['path'],'Source replica path changed','SOURCE_CHANGED')
            elif old:
                previous={content_digest([unpack_json(r[0])]) for r in conn.execute('SELECT normalized_json FROM messages WHERE session_id=?',(sid,))}
                supplied={content_digest([m]) for m in document.messages}
                require(previous<=supplied,'Reconciliation would discard existing analytical history','SOURCE_DIVERGED')
            # Empty analytical batches may advance a cloud cursor. They contain
            # no system/attachment events and never appear in the session list.
            # A repeated terminal event can update a previous result summary
            # without duplicating it. Scope replacements already handle their
            # own IDs; individual upserts must preserve other scope members.
            extra_scopes=set();removed_users=0;obsolete=set()
            if base:
                from .contracts.session_policy import is_user_message
                for message in document.messages:
                    previous=conn.execute('''SELECT m.message_pk,m.normalized_json,p.scope FROM messages m
                        JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.id=? AND m.session_id=?''',
                        (message['id'],sid)).fetchone()
                    if previous and message['type']=='tool-result' and message['message']['content'][0]['success'] is None:
                        prior=unpack_json(previous['normalized_json'])
                        if prior.get('type')=='tool-result':
                            block=prior['message']['content'][0]
                            if isinstance(block.get('success'),bool):
                                message['message']['content'][0]=dict(block)
                                message['status']=prior['status']
                    if previous and previous['scope'] not in document.changed_scopes:
                        extra_scopes.add(previous['scope'])
                        removed_users+=int(is_user_message(unpack_json(previous['normalized_json'])))
                        obsolete.update(r[0] for r in conn.execute('SELECT content_id FROM message_texts WHERE message_pk=?',(previous['message_pk'],)))
                        conn.execute('DELETE FROM messages WHERE message_pk=?',(previous['message_pk'],))
                if removed_users:
                    meta=json.loads(conn.execute('SELECT metadata FROM sessions WHERE id=?',(sid,)).fetchone()[0])
                    meta['userMessageCount']=meta.get('userMessageCount',0)-removed_users
                    conn.execute('UPDATE sessions SET metadata=? WHERE id=?',(encode(meta),sid))
                for cid in obsolete:
                    conn.execute('DELETE FROM search_content WHERE content_id=? AND NOT EXISTS (SELECT 1 FROM message_texts WHERE content_id=?)',(cid,cid))
            info_tuple=(info['path'],document.source,sid,info['fingerprint'],info['parserVersion'],info['modified'],len(document.messages))
            method=(self.db.apply_delta if document.sync_state else self.db.append_session) if base else self.db.replace_session
            method(document,info['path'],info['fingerprint'],info['modified'],info['parserVersion'],[info_tuple])
            if extra_scopes:
                conn.execute('UPDATE messages SET order_id=-order_id-1 WHERE session_id=?',(sid,))
                conn.execute('''WITH ordered AS (SELECT m.message_pk,ROW_NUMBER() OVER
                    (ORDER BY p.major,p.minor,m.message_pk)-1 AS n FROM messages m
                    JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?)
                    UPDATE messages SET order_id=(SELECT n FROM ordered WHERE ordered.message_pk=messages.message_pk)
                    WHERE session_id=?''',(sid,sid))
            touched=set(document.changed_scopes)|set(document.message_scopes.values())|extra_scopes
            if not base:
                conn.execute('DELETE FROM projection_state WHERE session_id=?',(sid,))
            self.refresh_scopes(conn,sid,touched)
            conn.execute('INSERT INTO dist_sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',(sid,device,revision+1))
            saved=self.db.get_session(sid)
            receipt={'operationId':key,'sessionId':sid,'revision':revision+1,'instance':self.instance,
                     'messageCount':saved['messageCount'],'userMessageCount':saved['userMessageCount'],
                     'changedScopes':len(document.changed_scopes),'fingerprint':info['fingerprint']}
            # SAME transaction as raw/messages/search/checkpoint, not a later ACK.
            conn.execute('INSERT INTO dist_receipts VALUES(?,?,?,?)',(device,key,payload_hash,encode(receipt)))
            return receipt

    def refresh_scopes(self,conn,sid,scopes):
        for scope in scopes:
            rows=[unpack_json(r[0]) for r in conn.execute('''SELECT m.normalized_json FROM messages m
                JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=? AND p.scope=?
                ORDER BY p.major,p.minor,m.message_pk''',(sid,scope))]
            if rows:
                conn.execute('INSERT INTO projection_state VALUES(?,?,?) ON CONFLICT(session_id,scope) DO UPDATE SET state=excluded.state',
                    (sid,scope,pack_json({'analysisDigest':content_digest(rows),'count':len(rows)})))
            else:conn.execute('DELETE FROM projection_state WHERE session_id=? AND scope=?',(sid,scope))

    def presence(self,device,path):
        with self.db.transaction() as conn:
            self.device(conn,device);row=conn.execute('SELECT * FROM source_files WHERE path=?',(path,)).fetchone()
            if row:
                self.binding(conn,device,row['session_id'])
                conn.execute('UPDATE source_files SET available=1 WHERE path=?',(path,))
                conn.execute('UPDATE sessions SET available=1 WHERE id=? AND path=?',(row['session_id'],path))

    def finish_scan(self,device,sources,seen):
        require(isinstance(sources,list) and all(s in SOURCES for s in sources),'Invalid completed sources')
        require(isinstance(seen,list) and all(isinstance(p,str) for p in seen),'Invalid presence manifest')
        present=set(seen)
        with self.db.transaction() as conn:
            self.device(conn,device)
            rows=conn.execute('SELECT f.path,f.source FROM source_files f JOIN dist_sessions s ON s.id=f.session_id WHERE s.device=?',(device,)).fetchall()
            missing=[r['path'] for r in rows if r['source'] in sources and r['path'] not in present]
            conn.executemany('UPDATE source_files SET available=0 WHERE path=?',((p,) for p in missing))
            conn.execute('''UPDATE sessions SET available=CASE WHEN EXISTS
                (SELECT 1 FROM source_files f WHERE f.session_id=sessions.id AND f.path=sessions.path AND f.available=1)
                THEN 1 ELSE 0 END WHERE id IN (SELECT id FROM dist_sessions WHERE device=?)
                AND EXISTS (SELECT 1 FROM source_files f WHERE f.session_id=sessions.id)''',(device,))
            return len(missing)

    def heartbeat(self,device,info):
        require(isinstance(info,dict),'Invalid device info')
        with self.db.transaction() as conn:
            self.device(conn,device)
            conn.execute('UPDATE dist_devices SET last_seen=?,info=? WHERE id=?',(time.time(),encode(info),device))

    def enqueue(self,device,key,sources):
        require(isinstance(key,str) and 0<len(key)<=160,'Invalid command key',status=400)
        require(isinstance(sources,list) and all(s in SOURCES for s in sources),'Invalid source filter',status=400)
        with self.db.transaction() as conn:
            dev=self.device(conn,device);require(dev['control'],'Remote control not enabled','CONTROL_DISABLED',403)
            old=conn.execute('SELECT * FROM dist_jobs WHERE device=? AND operation_key=?',(device,key)).fetchone()
            if old:
                require(json.loads(old['sources'])==sources,'Command key reused','IDEMPOTENCY_MISMATCH');return dict(old)
            self.expire_jobs(conn)
            active=conn.execute("SELECT * FROM dist_jobs WHERE device=? AND state IN ('QUEUED','RUNNING','PAUSED')",(device,)).fetchone()
            if active:return dict(active)
            jid=secrets.token_hex(16);now=time.time()
            conn.execute('INSERT INTO dist_jobs(id,device,operation_key,state,desired,sources,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                         (jid,device,key,'QUEUED','run',encode(sources),now,now+600))
            return dict(conn.execute('SELECT * FROM dist_jobs WHERE id=?',(jid,)).fetchone())

    def expire_jobs(self,conn):conn.execute("UPDATE dist_jobs SET state='EXPIRED' WHERE state='QUEUED' AND expires<?",(time.time(),))

    def claim(self,device,worker):
        require(isinstance(worker,str) and 0<len(worker)<=128,'Invalid worker')
        with self.db.transaction() as conn:
            self.device(conn,device);self.expire_jobs(conn)
            job=conn.execute("SELECT * FROM dist_jobs WHERE device=? AND state IN ('QUEUED','RUNNING','PAUSED') ORDER BY created LIMIT 1",(device,)).fetchone()
            if not job:return None
            require(job['worker'] in (None,worker) or job['lease_until']<time.time(),'Another worker owns this task','WORKER_BUSY')
            state='PAUSED' if job['desired']=='pause' else 'RUNNING'
            conn.execute('UPDATE dist_jobs SET worker=?,lease_until=?,state=? WHERE id=?',(worker,time.time()+45,state,job['id']))
            return dict(conn.execute('SELECT * FROM dist_jobs WHERE id=?',(job['id'],)).fetchone())

    def check_lease(self,conn,device,jid,worker,*,allow_expired=False):
        job=conn.execute('SELECT * FROM dist_jobs WHERE id=? AND device=?',(jid,device)).fetchone()
        require(job is not None,'Unknown job','NOT_FOUND',404)
        require(job['state'] in ('RUNNING','PAUSED') and job['worker']==worker and (allow_expired or job['lease_until']>=time.time()),'Worker lease expired/replaced','LEASE_LOST')
        return job

    def pulse(self,device,jid,worker,progress,result=None):
        with self.db.transaction() as conn:
            self.device(conn,device)
            previous=conn.execute('SELECT * FROM dist_jobs WHERE id=? AND device=?',(jid,device)).fetchone()
            if previous and previous['worker']==worker and previous['result'] is not None and result is not None:
                require(json.loads(previous['result'])==result,'Terminal result is immutable','IDEMPOTENCY_MISMATCH')
                return {'desired':previous['desired'],'state':previous['state']}
            job=self.check_lease(conn,device,jid,worker,allow_expired=True)
            conn.execute('UPDATE dist_devices SET last_seen=? WHERE id=?',(time.time(),device))
            require(isinstance(progress,dict),'Invalid progress');state='PAUSED' if job['desired']=='pause' else 'RUNNING'
            if result is not None:
                require(isinstance(result,dict),'Invalid result')
                state='CANCELLED' if job['desired']=='cancel' else 'FAILED' if result.get('failed') else 'SUCCEEDED'
            conn.execute('UPDATE dist_jobs SET progress=?,result=?,lease_until=?,state=? WHERE id=?',
                         (encode(progress),encode(result) if result is not None else None,time.time()+45,state,jid))
            return {'desired':job['desired'],'state':state}

    def control(self,device,jid,action):
        require(action in ('pause','run','cancel'),'Unknown control action',status=400)
        with self.db.transaction() as conn:
            dev=self.device(conn,device);require(dev['control'],'Remote control disabled','CONTROL_DISABLED',403)
            job=conn.execute('SELECT * FROM dist_jobs WHERE id=? AND device=?',(jid,device)).fetchone()
            require(job is not None,'Unknown job','NOT_FOUND',404)
            require(job['state'] in ('QUEUED','RUNNING','PAUSED'),'Job is terminal','JOB_FINISHED')
            state='CANCELLED' if action=='cancel' and job['state']=='QUEUED' else job['state']
            conn.execute('UPDATE dist_jobs SET desired=?,state=? WHERE id=?',(action,state,jid))

    def dashboard(self):
        with self.db.transaction() as conn:
            self.expire_jobs(conn)
            devices=[dict(r) for r in conn.execute('SELECT id,name,active,control,last_seen,info,expires FROM dist_devices ORDER BY name')]
            for dev in devices:
                dev['info']=json.loads(dev['info']);dev['online']=time.time()-dev['last_seen']<30
                dev['job']=next((dict(r) for r in conn.execute('SELECT * FROM dist_jobs WHERE device=? ORDER BY created DESC LIMIT 1',(dev['id'],))),None)
            return devices
