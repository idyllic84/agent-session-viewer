"""Cloud Web Server: password-protected viewer/console + API-key ingest.

    python -m server --database server/state/cloud.db --session-key-file ... --public-origin https://...

The cloud never commands a local computer: there is no task queue, polling
or heartbeat. Local servers push ordinary batch POSTs with an upload API key.
"""
import argparse
import base64
from datetime import timedelta
import html
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, session, redirect, send_from_directory
from werkzeug.exceptions import HTTPException
from .archive import CloudArchive
from .http_compression import compress_response
from .contracts.protocol import Problem, require, SOURCES, INGEST_VERSION

STATIC = Path(__file__).with_name('static')
LOGIN_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Session Viewer · Cloud login</title><link rel="stylesheet" href="/login.css"></head><body><main>
<form method="post"><h1>Agent Session Viewer</h1><p>Cloud console</p>__MESSAGE__
<input type="hidden" name="nonce" value="__NONCE__"><label>Password <input name="password" type="password" required autofocus autocomplete="current-password"></label>
<button>Sign in</button></form></main></body></html>'''


def create_app(archive, *, session_secret, public_origin, allow_http=False):
    parsed = urlsplit(public_origin)
    if parsed.scheme != 'https' and not (allow_http and parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Cloud origin must use HTTPS (HTTP requires explicit loopback-only development mode)')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username or not parsed.netloc:
        raise ValueError('public_origin must contain only scheme and host/port')
    public_origin = public_origin.rstrip('/')
    if not isinstance(session_secret, str) or len(session_secret) < 32:
        raise ValueError('Use a random browser-session secret of at least 32 characters')
    app = Flask(__name__, static_folder=None)
    app.config.update(SECRET_KEY=session_secret, SESSION_COOKIE_NAME='viewer_session',
                      SESSION_COOKIE_SECURE=parsed.scheme == 'https', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=archive.max_request, CLOUD_ARCHIVE=archive)
    failures = {}

    def owner():
        return session.get('owner') == archive.instance and session.get('pv') == archive.password_version() and archive.has_password()

    def same_origin():
        origin = request.headers.get('Origin')
        require(origin is None or origin == public_origin, 'Cross-origin request refused', 'CSRF', 403)
        require(request.headers.get('Sec-Fetch-Site') != 'cross-site', 'Cross-site request refused', 'CSRF', 403)

    @app.before_request
    def authorize():
        if request.host.lower() != parsed.netloc.lower():
            raise Problem('UNTRUSTED_HOST', 'Untrusted Host header', 403)
        if request.path == '/healthz' and request.method in ('GET', 'HEAD'):
            return
        if request.path.startswith('/ingest/v1/'):
            header = request.headers.get('Authorization', '')
            require(header.startswith('Bearer '), 'Upload API key required', 'UNAUTHORIZED', 401)
            request.client = archive.authenticate(header[7:].strip(), 'upload')
            return
        if request.path.startswith('/agent/v1/'):
            # Read-only analysis API: an agent API key, or the signed-in owner.
            require(request.method in ('GET', 'HEAD'), 'The agent API is read-only', 'METHOD_NOT_ALLOWED', 405)
            header = request.headers.get('Authorization', '')
            if header.startswith('Bearer '):
                request.reader = archive.authenticate(header[7:].strip(), 'agent')
                return
            require(owner(), 'Agent API key or owner login required', 'UNAUTHORIZED', 401)
            return
        if request.path in ('/auth/login', '/login.css'):
            if request.method == 'POST':
                same_origin()
            return
        if not owner():
            if request.path == '/' and request.method == 'GET':
                return redirect('/auth/login')
            raise Problem('UNAUTHORIZED', 'Login required', 401)
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            same_origin()
            require(hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('csrf', '!')), 'CSRF token required', 'CSRF', 403)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'same-origin', 'Cross-Origin-Opener-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
        if parsed.scheme == 'https':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return compress_response(response)

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, Problem):
            status, code, message = exc.status, exc.code, str(exc)
        elif isinstance(exc, HTTPException):
            status, code, message = exc.code, 'HTTP_ERROR', exc.description
            if exc.code == 413:
                code, message = 'TOO_LARGE', 'Request exceeds the cloud maximum; nothing was stored'
        elif isinstance(exc, (ValueError, TypeError, KeyError)):
            status, code, message = 422, 'INVALID_PAYLOAD', 'Invalid request data'
        elif isinstance(exc, sqlite3.IntegrityError):
            status, code, message = 422, 'INTEGRITY_CONSTRAINT', 'Constraint failed; nothing was committed'
        elif isinstance(exc, sqlite3.OperationalError):
            # Only lock contention is transient. Full storage and other database
            # errors are deterministic: retrying the same batch cannot succeed.
            name = getattr(exc, 'sqlite_errorname', '') or ''
            text = str(exc).lower()
            if name.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED')) or 'locked' in text or 'busy' in text:
                status, code, message = 503, 'ARCHIVE_BUSY', 'Archive is busy; retry the same request'
            elif name.startswith('SQLITE_FULL') or 'full' in text:
                status, code, message = 507, 'STORAGE_FULL', 'Cloud storage or temporary space is full; nothing was committed'
            else:
                status, code, message = 500, 'DATABASE_ERROR', 'Cloud database error; nothing was committed'
            app.logger.error('SQLite %s on %s %s: %s', name or 'error', request.method, request.path, exc)
        else:
            app.logger.exception('Cloud operation failed')
            status, code, message = 500, 'INTERNAL_ERROR', 'Operation failed; retry the same request'
        retryable = status in (429, 502, 503) or (status == 500 and code == 'INTERNAL_ERROR')
        response = jsonify(error={'code': code, 'message': message, 'retryable': retryable,
                                  'requestId': secrets.token_hex(8)})
        if status in (429, 503):
            response.headers['Retry-After'] = '2'
        return response, status

    @app.get('/healthz')
    def health():
        require(archive.setting('instance') is not None, 'Archive not ready', 'ARCHIVE_NOT_READY', 503)
        return jsonify(ok=True, protocol=INGEST_VERSION, build=os.environ.get('VIEWER_BUILD_REVISION', 'unknown'))

    @app.get('/login.css')
    def login_css():
        return send_from_directory(STATIC, 'login.css')

    @app.route('/auth/login', methods=['GET', 'POST'])
    def login():
        def page(message='', status=200):
            session['login_nonce'] = secrets.token_urlsafe(24)
            text = LOGIN_PAGE.replace('__NONCE__', session['login_nonce']).replace('__MESSAGE__', message)
            return text, status, {'Content-Type': 'text/html; charset=utf-8'}
        if not archive.has_password():
            return page('<p role="alert">No owner password is configured yet. Set one with <code>python -m server.manage set-password</code>.</p>', 503)
        if request.method == 'GET':
            return page()
        now = time.time(); ip = request.remote_addr or 'unknown'
        if len(failures) > 4096:
            failures.clear()
        attempts = [t for t in failures.get(ip, []) if now - t < 300]
        if len(attempts) >= 10:
            return page('<p role="alert">Too many attempts. Wait a few minutes.</p>', 429)
        if not hmac.compare_digest(request.form.get('nonce', ''), session.get('login_nonce', '!')):
            return page('<p role="alert">The login form expired. Try again.</p>', 403)
        if not archive.check_password(request.form.get('password', '')):
            failures[ip] = attempts + [now]
            return page('<p role="alert">Incorrect password.</p>', 401)
        failures.pop(ip, None)
        session.clear()
        session.update(owner=archive.instance, pv=archive.password_version(), csrf=secrets.token_urlsafe(32)); session.permanent = True
        return redirect('/')

    @app.post('/auth/logout')
    def logout():
        session.clear(); return jsonify(ok=True)

    @app.get('/')
    def index():
        text = (STATIC / 'index.html').read_text(encoding='utf-8')
        text = text.replace('__CSRF_TOKEN__', html.escape(session['csrf']), 1)
        text = text.replace('<!--CONSOLE-->', (STATIC / 'console.html').read_text(encoding='utf-8'), 1)
        return text, 200, {'Content-Type': 'text/html; charset=utf-8'}

    @app.get('/static/<path:name>')
    def shared_static(name):
        return send_from_directory(STATIC, name)

    @app.get('/cloud-assets/<path:name>')
    def cloud_static(name):
        return send_from_directory(STATIC, name)

    # ----- console: API keys and owner password -----
    @app.get('/api/keys')
    def keys():
        return jsonify(archive.keys())

    @app.post('/api/keys')
    def create_key():
        value = request.get_json(silent=True) or {}
        return jsonify(archive.create_key(value.get('name'), expires_days=value.get('expiresDays'),
                                          kind=value.get('kind', 'upload'))), 201

    @app.post('/api/keys/<client>/<action>')
    def key_action(client, action):
        if action == 'rotate':
            return jsonify(archive.rotate_key(client))
        if action == 'revoke':
            archive.revoke_key(client); return jsonify(ok=True)
        if action == 'unbind':
            archive.unbind_key(client); return jsonify(ok=True)
        raise Problem('NOT_FOUND', 'Unknown action', 404)

    @app.post('/api/account/password')
    def change_password():
        value = request.get_json(silent=True) or {}
        version = archive.set_password(value.get('newPassword'), current=value.get('currentPassword'))
        session.update(pv=version, csrf=secrets.token_urlsafe(32))
        initial = app.config.get('INITIAL_PASSWORD_FILE')
        if initial is not None:
            Path(initial).unlink(missing_ok=True)  # generated bootstrap password is obsolete now
        return jsonify(ok=True)

    # ----- viewer -----
    def page(default=2000, maximum=2000):
        offset = int(request.args.get('cursor', '0')); limit = int(request.args.get('limit', default))
        require(offset >= 0 and 1 <= limit <= maximum, 'Invalid page bounds', status=400)
        return offset, limit

    @app.get('/api/sources')
    def sources():
        return jsonify([{'id': key, 'label': label, 'available': True, 'roots': [],
                         'capabilities': {'readOnly': True, 'sourceDelete': False}} for key, label in SOURCES.items()])

    def with_machines(items):
        machines = archive.session_clients([item['id'] for item in items])
        for item in items:
            machine = machines.get(item['id'])
            item['machine'] = machine; item['uploadedBy'] = machine['name'] if machine else None
        return items

    def millis(name):
        value = request.args.get(name) or None
        if value is None:
            return None
        require(value.isdigit(), f'{name} must be epoch milliseconds', status=400)
        return int(value)

    def history_page():
        offset, limit = page(100, 500)
        items, total = archive.db.list_sessions(source=request.args.get('source') or None, project=request.args.get('project') or None,
                                                query=request.args.get('q', '').strip(), offset=offset, limit=limit,
                                                client=request.args.get('machine') or None,
                                                modified_from=millis('from'), modified_to=millis('to'))
        return jsonify(items=with_machines(items), total=total, nextCursor=offset + len(items) if offset + len(items) < total else None)

    def history_messages(sid):
        offset, limit = page()
        require(not request.args.get('branch'), 'Branch replay is not stored in analysis mode', 'ANALYSIS_VIEW', 400)
        with archive.db.transaction(write=False):
            revision = archive.session_revision(sid); expected = request.args.get('revision')
            require(expected is None or int(expected) == revision, 'Session changed; reload before paging', 'SESSION_CHANGED', 409)
            value = archive.db.get_messages(sid, offset, limit)
            if value is None:
                raise Problem('NOT_FOUND', 'Unknown session', 404)
            value['sessionRevision'] = revision; with_machines([value['session']])
            return jsonify(value)

    @app.get('/api/machines')
    def machines():
        return jsonify(archive.machines())

    @app.get('/api/sessions')
    def sessions():
        return history_page()

    @app.get('/api/sessions/<path:sid>/messages')
    def messages(sid):
        return history_messages(sid)

    @app.get('/api/messages/<path:mid>/raw')
    def raw(mid):
        raise Problem('ORIGINALS_NOT_STORED', 'Analysis mode stores no original records', 410)

    @app.get('/api/sessions/<path:sid>/records')
    def records(sid):
        raise Problem('ORIGINALS_NOT_STORED', 'Analysis mode stores no original records', 410)

    @app.get('/api/sync/status')
    def legacy_status():
        return jsonify(running=False, result=None)

    # ----- agent API (agent API key or owner session): list, metadata, paged messages -----
    def bounded(name, default, maximum):
        value = int(request.args.get(name, default))
        require(1 <= value <= maximum, f'{name} must be between 1 and {maximum}', status=400)
        return value

    @app.get('/agent/v1/sessions')
    def agent_sessions():
        cursor = request.args.get('cursor') or None
        after = None
        if cursor:
            try:
                after = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True).decode('utf-8')
                require(after, 'Invalid cursor', 'INVALID_CURSOR', 400)
            except (ValueError, UnicodeDecodeError):
                raise Problem('INVALID_CURSOR', 'Invalid cursor', 400) from None
        items, total, last = archive.agent_sessions(after, bounded('limit', 100, 500))
        token = base64.urlsafe_b64encode(last.encode('utf-8')).decode().rstrip('=') if last else None
        return jsonify(items=items, total=total, nextCursor=token)

    @app.get('/agent/v1/sessions/<path:sid>/messages')
    def agent_messages(sid):
        offset = int(request.args.get('cursor', '0'))
        require(offset >= 0, 'Invalid cursor', 'INVALID_CURSOR', 400)
        revision = request.args.get('revision')
        return jsonify(archive.agent_messages(sid, offset, bounded('limit', 500, 2000), int(revision) if revision is not None else None))

    @app.get('/agent/v1/sessions/<path:sid>')
    def agent_session(sid):
        value = archive.agent_session(sid)
        if value is None:
            raise Problem('NOT_FOUND', 'Unknown session', 404)
        return jsonify(session=value)

    # ----- ingest (upload API key) -----
    @app.get('/ingest/v1/status')
    def ingest_status():
        return jsonify(archive.status(request.client, request.args.get('datasetId')))

    @app.post('/ingest/v1/bind')
    def ingest_bind():
        value = request.get_json(silent=True) or {}
        return jsonify(archive.bind(request.client, value.get('datasetId')))

    @app.post('/ingest/v1/batches')
    def ingest_batch():
        require(request.is_json, 'JSON body required', 'CONTENT_TYPE', 415)
        value = request.get_json()
        return jsonify(archive.commit(request.client, request.headers.get('Idempotency-Key'), value))

    @app.get('/ingest/v1/receipts/<key>')
    def ingest_receipt(key):
        return jsonify(receipt=archive.receipt(request.client, key))

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--database', default=str(Path(__file__).with_name('state') / 'cloud.db'))
    parser.add_argument('--session-key-file', required=True, help='Random browser-session signing secret (>=32 chars)')
    parser.add_argument('--public-origin', required=True)
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--max-request-mib', type=int, default=16, help='Explicit maximum for one batch request')
    parser.add_argument('--allow-http-loopback', action='store_true')
    parser.add_argument('--development', action='store_true', help='Loopback-only development server instead of Waitress')
    args = parser.parse_args()
    if args.allow_http_loopback and args.host not in ('localhost', '127.0.0.1', '::1'):
        parser.error('Development HTTP may only bind loopback')
    if args.max_request_mib < 1:
        parser.error('max-request-mib must be positive')
    archive = CloudArchive(args.database, max_request=args.max_request_mib * 1024 * 1024)
    if not archive.has_password():
        parser.error('Set the owner password first: python -m server.manage set-password --database ' + args.database)
    app = create_app(archive, session_secret=Path(args.session_key_file).read_text().strip(),
                     public_origin=args.public_origin, allow_http=args.allow_http_loopback)
    if args.development:
        if args.host not in ('localhost', '127.0.0.1', '::1'):
            parser.error('Development server may only bind loopback')
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False, debug=False)
    else:
        try:
            from waitress import serve
        except ImportError:
            parser.error('Install server/requirements.txt (Waitress required for production)')
        serve(app, host=args.host, port=args.port, threads=8, max_request_body_size=app.config['MAX_CONTENT_LENGTH'])


if __name__ == '__main__':
    main()
