"""Start the local Web Server: import history incrementally FIRST, then listen.

    python -m local [--port 8000] [--include-recent] [--no-import]

There is no watcher or periodic task: history is imported at startup (and when
explicitly requested in the page); cloud upload runs only from the console.
"""
import argparse
import json
import os
from pathlib import Path
import sys

if not __package__:  # allow `python local/__main__.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1])); __package__ = 'local'

from .archive import LocalArchive
from .sources import default_adapters
from server.contracts.protocol import SOURCES


class InstanceLock:
    """OS-level single-instance lock beside the database (released on exit)."""
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0); msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise SystemExit(f'Another local server/migration is using {self.path.parent}; stop it first.') from None

    def close(self):
        self.handle.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--database', help='Local analysis store (default local/state/local.db)')
    parser.add_argument('--roots-file', help='JSON object mapping source IDs to approved history directories')
    parser.add_argument('--include-recent', action='store_true', help='Also import files modified within the quiet period')
    parser.add_argument('--quiet-seconds', type=float, default=3600)
    parser.add_argument('--no-import', action='store_true', help='Browse only: skip the startup import')
    parser.add_argument('--credential-store', choices=('auto', 'keyring', 'file'), default='auto')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if args.quiet_seconds < 0:
        parser.error('--quiet-seconds must be nonnegative')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    roots = None
    if args.roots_file:
        roots = json.loads(Path(args.roots_file).read_text(encoding='utf-8'))
        if not isinstance(roots, dict) or set(roots) - set(SOURCES):
            parser.error('roots-file must map known source IDs to arrays of local paths')
        roots = {source: roots.get(source, []) for source in SOURCES}
    database = Path(args.database or os.environ.get('LOCAL_VIEWER_DB') or Path(__file__).with_name('state') / 'local.db')
    lock = InstanceLock(database.with_name(database.name + '.lock'))
    try:
        from .service import LocalService
        from .app import create_app
        archive = LocalArchive(database)
        service = LocalService(archive, default_adapters(roots), quiet_seconds=0 if args.include_recent else args.quiet_seconds,
                               credential_backend=args.credential_store)
        print(f'[local] store: {archive.db.db_path}', flush=True)
        if args.no_import:
            print('[local] startup import skipped (--no-import)', flush=True)
        else:
            print('[local] importing history incrementally before starting the web server (sources are read-only)...', flush=True)
            result = service.import_history()
            print(f"[local] import finished: {result['added']} new, {result['updated']} updated, "
                  f"{result['unchanged']} unchanged, {result['skippedRecent']} recent deferred, {result['failed']} failed", flush=True)
        app = create_app(service)
        from werkzeug.serving import make_server
        server = make_server('127.0.0.1', args.port, app, threaded=True)
        print(f'[local] Agent Session Viewer (local): http://localhost:{args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        lock.close()


if __name__ == '__main__':
    main()
