"""Primary analysis server entry point: python -m server."""
import argparse
from datetime import timedelta
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, session, redirect, send_from_directory
from werkzeug.exceptions import HTTPException
from .archive import CloudArchive,TASK_POLL_SECONDS,TAKEOVER_AFTER_SECONDS
from .http_compression import compress_response
from .contracts.analysis import POLICY
from .contracts.protocol import Problem, require, SOURCES, VERSION

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).with_name('static')


def create_app(archive, *, admin_key, session_secret, public_origin, allow_http=False):
    parsed = urlsplit(public_origin)
    if parsed.scheme!='https' and not (allow_http and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('Cloud origin must use HTTPS (HTTP requires explicit loopback-only development mode)')
    if parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username or not parsed.netloc:
        raise ValueError('public_origin must contain only scheme and host/port')
    public_origin = public_origin.rstrip('/')
    if len(admin_key)<32 or len(session_secret)<32:
        raise ValueError('Use independent random admin/session secrets of at least 32 characters')
    if hmac.compare_digest(admin_key,session_secret):
        raise ValueError('Admin and session secrets must be different')
    app = Flask(__name__,static_folder=None)
    app.config.update(SECRET_KEY=session_secret,SESSION_COOKIE_NAME='viewer_session',
                      SESSION_COOKIE_SECURE=parsed.scheme=='https',SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict',PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=archive.max_request)
    admin_version = hashlib.sha256(admin_key.encode()).hexdigest()
    failures = {}

    def owner():
        return session.get('owner')==admin_version

    @app.before_request
    def authorize():
        if request.host.lower()!=parsed.netloc.lower():
            raise Problem('UNTRUSTED_HOST','Untrusted Host header',403)
        if request.path=='/healthz' and request.method in ('GET','HEAD'):return
        if request.path.startswith('/sync/v1/'):
            header = request.headers.get('Authorization','')
            require(header.startswith('Bearer '),'Device bearer token required','UNAUTHORIZED',401)
            request.device = archive.authenticate(header[7:])
            require(bool(request.headers.get('X-Sync-Job'))==bool(request.headers.get('X-Sync-Worker')),'Both task headers are required',status=400)
            return
        if request.path=='/auth/login':
            if request.method=='POST':
                require(request.headers.get('Origin',public_origin)==public_origin,'Cross-origin login','CSRF',403)
            return
        if not owner():
            if request.path=='/':return redirect('/auth/login')
            raise Problem('UNAUTHORIZED','Login required',401)
        if request.method in ('POST','PUT','PATCH','DELETE'):
            require(request.headers.get('Origin',public_origin)==public_origin,'Cross-origin mutation','CSRF',403)
            require(hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session.get('csrf','!')),'CSRF token required','CSRF',403)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        if parsed.scheme=='https':response.headers['Strict-Transport-Security']='max-age=31536000'
        if (request.path.startswith('/sync/v1/') and '/jobs/' not in request.path
                and request.path!='/sync/v1/connect' and response.status_code<400
                and response.is_json and getattr(request,'device',None)):
            control=archive.contact(request.device['id'],request.headers.get('X-Sync-Job'),request.headers.get('X-Sync-Worker'))
            if control is not None:
                value=response.get_json();value['control']=control;response.set_data(app.json.dumps(value))
        return compress_response(response)

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc,Problem):status,code,message = exc.status,exc.code,str(exc)
        elif isinstance(exc,HTTPException):status,code,message = exc.code,'HTTP_ERROR',exc.description
        elif isinstance(exc,(ValueError,TypeError,KeyError)):status,code,message = 422,'INVALID_PAYLOAD','Invalid request data'
        elif isinstance(exc,sqlite3.IntegrityError):status,code,message = 422,'INTEGRITY_CONSTRAINT','Projection constraint failed; no checkpoint was advanced'
        elif isinstance(exc,sqlite3.OperationalError):status,code,message = 503,'ARCHIVE_BUSY','Archive unavailable; retry the same operation'
        else:
            app.logger.exception('Cloud operation failed')
            status,code,message = 500,'INTERNAL_ERROR','Operation failed; retain operation ID for status recovery'
        response = jsonify(error={'code':code,'message':message,'retryable':status in (429,500,502,503),
                                  'requestId':secrets.token_hex(8)})
        if status in (429,503):response.headers['Retry-After']='2'
        return response,status

    @app.get('/healthz')
    def health():
        with archive.db.get_connection() as conn:
            ready=conn.execute("SELECT 1 FROM dist_settings WHERE key='instance'").fetchone()
        require(ready is not None,'Archive not ready','ARCHIVE_NOT_READY',503)
        return jsonify(ok=True,protocol=VERSION,build=os.environ.get('VIEWER_BUILD_REVISION','unknown'))

    @app.route('/auth/login',methods=['GET','POST'])
    def login():
        if request.method=='GET':
            session['login_nonce']=secrets.token_urlsafe(24)
            return '<!doctype html><title>Viewer login</title><form method="post"><h1>Cloud Viewer</h1><input type="hidden" name="nonce" value="'+session['login_nonce']+'"><label>Owner key <input name="key" type="password" required autocomplete="current-password"></label><button>Login</button></form>'
        now=time.time();ip=request.remote_addr or 'unknown'
        if len(failures)>4096:failures.clear()
        attempts=[t for t in failures.get(ip,[]) if now-t<60]
        require(len(attempts)<10,'Too many login attempts','RATE_LIMIT',429)
        require(hmac.compare_digest(request.form.get('nonce',''),session.get('login_nonce','!')),'Login nonce required','CSRF',403)
        if not hmac.compare_digest(request.form.get('key',''),admin_key):
            failures[ip]=attempts+[now];raise Problem('UNAUTHORIZED','Invalid owner key',401)
        session.clear();session.update(owner=admin_version,csrf=secrets.token_urlsafe(32));session.permanent=True
        return redirect('/')

    @app.post('/auth/logout')
    def logout():session.clear();return jsonify(ok=True)

    @app.get('/')
    def index():
        # Read shared local assets, never edit them. Only cloud-specific additions
        # and a replacement HTTP adapter are served from the new directory.
        text=(STATIC/'index.html').read_text(encoding='utf-8')
        text=text.replace('</head>','<link rel="stylesheet" href="/cloud-assets/cloud.css"><meta name="cloud-csrf" content="'+html.escape(session['csrf'])+'"></head>')
        text=text.replace('<div class="container">','<details class="sync-status"><summary>Cloud devices / sync control</summary><div id="cloudDevices"></div><label>Device name <input id="deviceName" maxlength="160"></label><label><input id="deviceControl" type="checkbox">Allow remote sync control</label><label><input id="deviceAdopt" type="checkbox">Adopt existing unbound archive sessions</label><button id="createDevice">Create device</button><pre id="newDeviceToken"></pre><div id="cloudError" role="status"></div></details><div class="container">')
        text=text.replace('Local history only. Click Reload to import.','Cloud archive. Local files are read only by your Syncer.')
        text=text.replace('</body>','<script src="/cloud-assets/control.js" type="module"></script></body>')
        return text

    @app.get('/static/<path:name>')
    def shared_static(name):
        return send_from_directory(STATIC,name)

    @app.get('/cloud-assets/<path:name>')
    def cloud_static(name):return send_from_directory(STATIC,name)

    @app.get('/api/devices')
    def devices():return jsonify(archive.dashboard())

    @app.post('/api/devices')
    def add_device():
        value=request.get_json();return jsonify(archive.add_device(value['name'],control=value.get('control') is True,adopt=value.get('adopt') is True)),201

    @app.post('/api/devices/<device>/rotate')
    def rotate(device):return jsonify(archive.rotate_device(device))

    @app.post('/api/devices/<device>/revoke')
    def revoke(device):
        with archive.db.transaction() as conn:
            conn.execute('UPDATE dist_devices SET active=0 WHERE id=?',(device,))
        return jsonify(ok=True)

    @app.post('/api/devices/<device>/jobs')
    def enqueue(device):
        value=request.get_json();return jsonify(archive.enqueue(device,request.headers.get('Idempotency-Key'),value.get('sources',[]))),202

    @app.post('/api/devices/<device>/jobs/<jid>/<action>')
    def control(device,jid,action):archive.control(device,jid,action);return jsonify(ok=True)

    def page(default=2000,maximum=2000):
        offset=int(request.args.get('cursor','0'));limit=int(request.args.get('limit',default))
        require(offset>=0 and 1<=limit<=maximum,'Invalid page bounds',status=400)
        return offset,limit

    @app.get('/api/sources')
    def sources():return jsonify([{'id':key,'label':label,'available':True,'roots':[],
        'capabilities':{'readOnly':True,'sourceDelete':False}} for key,label in SOURCES.items()])

    @app.get('/api/sessions')
    def sessions():
        offset,limit=page(100,500)
        items,total=archive.db.list_sessions(source=request.args.get('source'),project=request.args.get('project'),query=request.args.get('q','').strip(),offset=offset,limit=limit)
        return jsonify(items=items,total=total,nextCursor=offset+len(items) if offset+len(items)<total else None)

    @app.get('/api/sessions/<path:sid>/messages')
    def messages(sid):
        offset,limit=page()
        with archive.db.transaction(write=False) as conn:
            row=conn.execute('SELECT revision FROM dist_sessions WHERE id=?',(sid,)).fetchone();revision=row[0] if row else 0
            expected=request.args.get('revision')
            require(expected is None or int(expected)==revision,'Session changed; reload before paging','SESSION_CHANGED',409)
            require(not request.args.get('branch'),'Branch replay is not stored in analysis mode','ANALYSIS_VIEW',400)
            value=archive.db.get_messages(sid,offset,limit)
            if value is None:raise Problem('NOT_FOUND','Unknown session',404)
            value['sessionRevision']=revision
            return jsonify(value)

    @app.get('/api/messages/<path:mid>/raw')
    def raw(mid):raise Problem('ORIGINALS_NOT_STORED','Analysis mode stores no original records',410)

    @app.get('/api/sessions/<path:sid>/records')
    def records(sid):raise Problem('ORIGINALS_NOT_STORED','Analysis mode stores no original records',410)

    @app.get('/api/sync/status')
    def legacy_status():return jsonify(running=False,result=None)

    def device_id():return request.device['id']

    @app.get('/sync/v1/capabilities')
    def capabilities():return jsonify(version=VERSION,instance=archive.instance,maxRequestBytes=archive.max_request,
        deviceId=device_id(),remoteControl=bool(request.device['control']),taskPollSeconds=TASK_POLL_SECONDS,
        takeoverAfterSeconds=TAKEOVER_AFTER_SECONDS,maxProgressFiles=128,fingerprintAlgorithm='legacy-path-sha256-v1',privacy=POLICY,compression=['identity'])

    @app.post('/sync/v1/progress/query')
    def progress_query():
        value=request.get_json()
        return jsonify(archive.query_progress(device_id(),value['files'],mark_seen=value.get('markSeen') is True,
                                             include_context=value.get('includeContext') is True))

    @app.post('/sync/v1/context')
    def context():
        value=request.get_json();return jsonify(archive.context(device_id(),value.get('path'),value.get('sessionId'),value.get('source')))

    @app.get('/sync/v1/sessions/<path:sid>/scope')
    def scope(sid):raise Problem('REPLAY_CONTEXT_NOT_STORED','Analysis mode stores no native replay context',410)

    @app.post('/sync/v1/batches')
    def batch():
        tag=request.headers.get('If-Match','')
        require(tag.startswith('"') and tag.endswith('"'),'Quoted revision ETag required',status=400)
        value=request.get_json()
        if request.headers.get('X-Sync-Job'):
            require(value.get('jobId')==request.headers['X-Sync-Job'] and value.get('workerId')==request.headers['X-Sync-Worker'],'Task header/body mismatch')
        return jsonify(archive.commit_batch(device_id(),request.headers.get('Idempotency-Key'),value,int(tag[1:-1])))

    @app.get('/sync/v1/receipts/<key>')
    def receipt(key):return jsonify(receipt=archive.receipt(device_id(),key))

    @app.post('/sync/v1/presence')
    def presence():archive.presence(device_id(),request.get_json()['path']);return jsonify(ok=True)

    @app.post('/sync/v1/scan/finish')
    def finish_scan():
        value=request.get_json();return jsonify(missing=archive.finish_scan(device_id(),value['sources'],value['seen']))

    @app.post('/sync/v1/connect')
    def connect():archive.connect(device_id(),request.get_json());return jsonify(ok=True)

    @app.get('/sync/v1/jobs/next')
    def next_job():return jsonify(delivery=archive.next_job(device_id(),request.args['workerId']))

    @app.post('/sync/v1/jobs/<jid>/received')
    def received(jid):
        value=request.get_json();return jsonify(archive.received(device_id(),jid,value['workerId'],value['deliveryToken']))

    @app.get('/sync/v1/jobs/<jid>/commands')
    def commands(jid):return jsonify(archive.commands(device_id(),jid,request.headers.get('X-Sync-Worker')))

    @app.post('/sync/v1/jobs/<jid>/finish')
    def finish(jid):
        value=request.get_json();return jsonify(archive.finish(device_id(),jid,value['workerId'],value.get('progress',{}),value['result']))

    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=str(Path(__file__).with_name('state')/'cloud.db'))
    parser.add_argument('--admin-key-file',required=True)
    parser.add_argument('--session-key-file',required=True)
    parser.add_argument('--public-origin',required=True)
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--max-request-mib',type=int,default=32,help='Explicit maximum for an indivisible batch/record')
    parser.add_argument('--allow-http-loopback',action='store_true')
    parser.add_argument('--development',action='store_true',help='Loopback-only development server instead of Waitress')
    args=parser.parse_args()
    if args.allow_http_loopback and args.host not in ('localhost','127.0.0.1','::1'):
        parser.error('Development HTTP may only bind loopback')
    if args.max_request_mib<1:parser.error('max-request-mib must be positive')
    app=create_app(CloudArchive(args.database,max_request=args.max_request_mib*1024*1024),admin_key=Path(args.admin_key_file).read_text().strip(),
        session_secret=Path(args.session_key_file).read_text().strip(),public_origin=args.public_origin,allow_http=args.allow_http_loopback)
    if args.development:
        if args.host not in ('localhost','127.0.0.1','::1'):parser.error('Development server may only bind loopback')
        app.run(host=args.host,port=args.port,threaded=True,use_reloader=False,debug=False)
    else:
        try:from waitress import serve
        except ImportError:parser.error('Install server/requirements.txt (Waitress required for production)')
        serve(app,host=args.host,port=args.port,threads=8,max_request_body_size=app.config['MAX_CONTENT_LENGTH'])


if __name__=='__main__':main()
