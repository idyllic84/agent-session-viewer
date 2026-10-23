"""Loopback-only HTTP API for the read-only multi-agent history archive."""
import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, send_from_directory
from flask_compress import Compress
from werkzeug.exceptions import HTTPException
# Support both `python -m local.server` and direct execution from local/.
if not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'local'
from .service import ClaudeViewerService, SyncBusy

BASE_DIR = Path(__file__).parent


def create_app(viewer_service=None):
    app = Flask(__name__, static_folder=str(BASE_DIR / 'static'), static_url_path='/static')
    Compress(app)
    service = viewer_service or ClaudeViewerService()
    service.initialize()
    app.config['VIEWER_SERVICE'] = service
    allowed_hosts = {'localhost', '127.0.0.1', '::1'}
    allowed_hosts.update(h.strip().lower() for h in os.environ.get('VIEWER_ALLOWED_HOSTS', '').split(',') if h.strip())

    @app.before_request
    def protect_local_api():
        host = urlsplit('//' + request.host).hostname
        if not host or host.lower() not in allowed_hosts:
            return jsonify(error='Untrusted Host header'), 403
        if request.method in ('POST', 'DELETE', 'PUT', 'PATCH'):
            if request.headers.get('X-Viewer-Request') != '1':
                return jsonify(error='Missing X-Viewer-Request header'), 403
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Cross-origin mutation is not allowed'), 403

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if response.is_json:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def handle_exception(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, SyncBusy):
            return jsonify(error=str(exc)), 409
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        app.logger.exception('Viewer request failed')
        return jsonify(error='Archive operation failed; see server log'), 500

    def page(default=200, maximum=500):
        offset = int(request.args.get('cursor', '0'))
        limit = int(request.args.get('limit', str(default)))
        if offset < 0 or not 1 <= limit <= maximum:
            raise ValueError(f'cursor must be nonnegative and limit must be between 1 and {maximum}')
        return offset, limit

    def found(result):
        return (jsonify(error='Not found'), 404) if result is None else jsonify(result)

    @app.get('/')
    def index():
        return send_from_directory(BASE_DIR / 'static', 'index.html')

    @app.get('/favicon.ico')
    def favicon():
        return send_from_directory(BASE_DIR / 'static', 'favicon.ico')

    @app.get('/api/sources')
    def sources():
        return jsonify(service.get_sources())

    @app.get('/api/sessions')
    def sessions_page():
        offset, limit = page(100)
        items, total = service.list_sessions(project=request.args.get('project'), source=request.args.get('source'),
                                             query=request.args.get('q', '').strip(), offset=offset, limit=limit)
        return jsonify(items=items, total=total, nextCursor=offset + len(items) if offset + len(items) < total else None)

    @app.get('/sessions')
    def sessions_legacy():
        return jsonify(service.get_all_sessions(request.args.get('project'), request.args.get('source')))

    @app.get('/sessions/search')
    def search_legacy():
        return jsonify(service.search_sessions_by_content(request.args.get('q', '').strip(), request.args.get('source')))

    @app.post('/api/sync')
    @app.post('/sessions/sync')
    def sync():
        return jsonify(service.sync_sessions())

    @app.post('/api/sync/start')
    def start_sync():
        return jsonify(service.start_sync()), 202

    @app.get('/api/sync/status')
    def sync_status():
        return jsonify(service.sync_status())

    @app.get('/api/sessions/<path:session_id>/messages')
    def messages_page(session_id):
        offset, limit = page(2000, maximum=2000)
        return found(service.get_messages(session_id, offset=offset, limit=limit, branch=request.args.get('branch')))

    @app.get('/session/<path:session_id>')
    def messages_legacy(session_id):
        result = service.get_messages(session_id, limit=500)
        if result is None:
            return found(None)
        # Legacy endpoint is bounded. New clients use cursor-based /api/.../messages.
        return jsonify(result['items'])

    @app.delete('/session/<path:session_id>')
    def source_delete_disabled(session_id):
        return jsonify(error='Source deletion is disabled. This viewer only reads agent history.'), 405

    @app.get('/api/messages/<path:message_id>/raw')
    def raw_message(message_id):
        return found(service.get_raw_message(message_id))

    @app.get('/api/sessions/<path:session_id>/records')
    def records(session_id):
        offset, limit = page(50)
        return found(service.get_records(session_id, offset=offset, limit=limit))

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Local read-only agent history viewer')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--include-recent', action='store_true', help='Explicitly include files modified within the last hour')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--no-sync', action='store_true', help='Skip the default startup sync')
    parser.add_argument('--sync', action='store_true', help=argparse.SUPPRESS)  # Old explicit flag remains valid.
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    app = create_app(ClaudeViewerService(skip_recent_seconds=0 if args.include_recent else 3600))
    service = app.config['VIEWER_SERVICE']
    if not args.no_sync:
        print('Syncing configured history sources before startup (read-only)...', flush=True)
        service.sync_sessions()
    print(f'Agent History Viewer: http://localhost:{args.port}\nArchive: {service._db.db_path}', flush=True)
    print(('Startup sync skipped (--no-sync).' if args.no_sync else 'Startup sync completed.') +
          ' Original files and claude.db are never modified.', flush=True)
    try:
        app.run(host='localhost', port=args.port, debug=args.debug, use_reloader=False)
    finally:
        service.shutdown()
