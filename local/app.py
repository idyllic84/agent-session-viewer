"""Local Web Server: loopback-only history viewer and cloud-sync console.

No password login. Protection instead comes from: binding to loopback only,
exact Host validation (DNS-rebinding defence), exact Origin checks, a random
per-process CSRF token embedded in the page for every state-changing request,
no CORS headers, and never returning the cloud API key to the browser.
"""
import html
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from server.contracts.protocol import Problem
from .service import ImportBusy

STATIC = Path(__file__).with_name('static')
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


def create_app(service):
    try:
        from flask_compress import Compress
    except ImportError:  # optional optimisation only
        Compress = None
    app = Flask(__name__, static_folder=None)
    if Compress is not None:
        Compress(app)
    app.config.update(MAX_CONTENT_LENGTH=1024 * 1024, LOCAL_SERVICE=service)
    csrf = secrets.token_urlsafe(32)
    app.config['LOCAL_CSRF_TOKEN'] = csrf

    @app.before_request
    def protect():
        host = urlsplit('//' + request.host).hostname
        if not host or host.lower() not in LOOPBACK_HOSTS:
            return jsonify(error={'code': 'UNTRUSTED_HOST', 'message': 'Local server accepts only localhost requests'}), 403
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error={'code': 'CROSS_SITE', 'message': 'Cross-site requests are refused'}), 403
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin is not None and origin != request.host_url.rstrip('/'):
                return jsonify(error={'code': 'CSRF', 'message': 'Cross-origin request refused'}), 403
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf):
                return jsonify(error={'code': 'CSRF', 'message': 'Reload the page (missing request token)'}), 403
            if request.content_length and not request.is_json:
                return jsonify(error={'code': 'CONTENT_TYPE', 'message': 'JSON body required'}), 415

    @app.after_request
    def headers(response):
        response.headers.update({'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Resource-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                                       "form-action 'none'; frame-ancestors 'none'"})
        if response.is_json or request.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, Problem):
            status, code, message = exc.status, exc.code, str(exc)
        elif isinstance(exc, HTTPException):
            status, code, message = exc.code, 'HTTP_ERROR', exc.description
        elif isinstance(exc, ImportBusy):
            status, code, message = 409, 'IMPORT_RUNNING', str(exc)
        elif isinstance(exc, LookupError):
            status, code, message = 400, 'NO_CREDENTIAL', str(exc)
        elif isinstance(exc, ValueError):
            status, code, message = 400, 'INVALID_INPUT', str(exc)
        else:
            app.logger.exception('Local request failed')
            status, code, message = 500, 'INTERNAL_ERROR', 'Local operation failed; see the server log'
        return jsonify(error={'code': code, 'message': message}), status

    def page(default=100, maximum=500):
        offset = int(request.args.get('cursor', '0')); limit = int(request.args.get('limit', str(default)))
        if offset < 0 or not 1 <= limit <= maximum:
            raise ValueError(f'cursor must be nonnegative and limit between 1 and {maximum}')
        return offset, limit

    def body():
        value = request.get_json(silent=True)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        return value

    @app.get('/')
    def index():
        text = (STATIC / 'index.html').read_text(encoding='utf-8')
        return text.replace('__CSRF_TOKEN__', html.escape(csrf)), 200, {'Content-Type': 'text/html; charset=utf-8'}

    @app.get('/static/<path:name>')
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get('/favicon.ico')
    def favicon():
        return send_from_directory(STATIC, 'favicon.ico')

    # ----- history browsing -----
    @app.get('/api/sources')
    def sources():
        return jsonify(service.get_sources())

    @app.get('/api/sessions')
    def sessions():
        offset, limit = page()
        items, total = service.list_sessions(project=request.args.get('project'), source=request.args.get('source'),
                                             query=request.args.get('q', '').strip(), offset=offset, limit=limit)
        return jsonify(items=items, total=total, nextCursor=offset + len(items) if offset + len(items) < total else None)

    @app.get('/api/sessions/<path:sid>/messages')
    def messages(sid):
        offset, limit = page(2000, 2000)
        if request.args.get('branch'):
            raise Problem('ANALYSIS_VIEW', 'Branch replay is not stored in analysis mode', 400)
        value = service.get_messages(sid, offset, limit)
        if value is None:
            raise Problem('NOT_FOUND', 'Unknown session', 404)
        expected = request.args.get('revision')
        if expected is not None and int(expected) != value['sessionRevision']:
            raise Problem('SESSION_CHANGED', 'Session changed; reload before paging', 409)
        return jsonify(value)

    @app.get('/api/messages/<path:mid>/raw')
    def raw(mid):
        raise Problem('ORIGINALS_NOT_STORED', 'The local analysis store keeps no original records', 410)

    @app.get('/api/sessions/<path:sid>/records')
    def records(sid):
        raise Problem('ORIGINALS_NOT_STORED', 'The local analysis store keeps no original records', 410)

    # ----- explicit local re-import (no watcher, no timer) -----
    @app.post('/api/sync/start')
    def start_import():
        return jsonify(service.start_import()), 202

    @app.get('/api/sync/status')
    def import_status():
        return jsonify(service.import_status())

    # ----- cloud console -----
    @app.get('/api/cloud')
    def cloud():
        return jsonify(service.cloud_status())

    @app.post('/api/cloud/test')
    def cloud_test():
        value = body(); return jsonify(service.test_connection(value.get('url'), value.get('key') or None))

    @app.put('/api/cloud/connection')
    def cloud_save():
        value = body(); return jsonify(service.save_connection(value.get('url'), value.get('key') or None))

    @app.delete('/api/cloud/connection')
    def cloud_delete():
        return jsonify(service.delete_connection())

    @app.post('/api/cloud/upload/<action>')
    def cloud_upload(action):
        handlers = {'start': service.start_upload, 'pause': service.pause_upload, 'cancel': service.cancel_upload}
        if action not in handlers:
            raise Problem('NOT_FOUND', 'Unknown upload action', 404)
        return jsonify(handlers[action]()), 202

    return app
