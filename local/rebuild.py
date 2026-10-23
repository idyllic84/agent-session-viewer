"""Explicit, side-by-side archive rebuild. Agent files and claude.db are untouched."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
if not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'local'
from .db import Database, ARCHIVE_APPLICATION_ID
from .service import ClaudeViewerService
from .session_policy import is_subagent


def archive_ids(path):
    if not path.exists():
        return set()
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if conn.execute('PRAGMA application_id').fetchone()[0] != ARCHIVE_APPLICATION_ID:
            raise ValueError('Refusing to replace a database not owned by this viewer')
        return {row[0] for row in conn.execute('SELECT id,source,path,metadata FROM sessions WHERE message_count>0')
                if not is_subagent(row[1], row[2], json.loads(row[3]))}
    finally:
        conn.close()


def checkpoint(path):
    conn = sqlite3.connect(path, timeout=5)
    try:
        busy, _, _ = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy:
            raise RuntimeError('Archive is in use. Stop the viewer before rebuilding/replacing it')
    finally:
        conn.close()


def validate(database):
    with database.get_connection() as conn:
        check = conn.execute('PRAGMA quick_check').fetchone()[0]
        if check != 'ok':
            raise RuntimeError(f'Archive check failed: {check}')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Archive foreign-key check failed')
        if database.has_trigram:
            conn.execute("INSERT INTO message_search(message_search,rank) VALUES ('integrity-check',1)")
        return conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]


def rebuild(target, *, adapters=None, discard_backup=False, allow_missing=False, skip_recent_seconds=3600):
    target = Database(target).db_path.resolve()  # Includes the legacy claude.db safeguard.
    before_ids = archive_ids(target)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    candidate = target.with_name(f'{target.stem}.rebuild-{stamp}.db')
    backup = target.with_name(f'{target.stem}.backup-{stamp}.db')
    started = time.perf_counter()
    print(f'Building {candidate.name}; current archive remains untouched', flush=True)
    service = ClaudeViewerService(candidate, adapters, skip_recent_seconds=skip_recent_seconds)
    service.initialize()
    result = service.sync_sessions()
    if result['failed']:
        print(json.dumps(result['errors'], ensure_ascii=False, indent=2), flush=True)
        raise RuntimeError(f"{result['failed']} sources failed. Candidate retained at {candidate}; original archive unchanged")
    print('Validating SQLite, foreign keys and the search index...', flush=True)
    session_count = validate(service._db)
    print('Checking archived session coverage and switching databases...', flush=True)
    with service._db.get_connection() as conn:
        after_ids = {row[0] for row in conn.execute('SELECT id FROM sessions')}
    missing = before_ids - after_ids
    if missing and not allow_missing:
        raise RuntimeError(f'{len(missing)} previously archived sessions are absent. Original archive unchanged; '
                           f'candidate at {candidate}. Review sources or explicitly use --allow-missing')
    checkpoint(candidate)
    old_size = target.stat().st_size if target.exists() else 0
    if target.exists():
        # Checkpoint only our own archive. Never discard a live/uncheckpointed WAL.
        checkpoint(target)
        target.replace(backup)
    try:
        candidate.replace(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise
    if backup.exists() and discard_backup:
        backup.unlink()
    report = {'sessions': session_count, 'oldBytes': old_size, 'newBytes': target.stat().st_size,
              'elapsedSeconds': round(time.perf_counter() - started, 2), 'sync': result,
              'missingPreviousSessions': len(missing), 'backup': str(backup) if backup.exists() else None}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Rebuild the viewer index without changing agent history')
    parser.add_argument('--database', default=os.environ.get('VIEWER_DB') or str(Path(__file__).parent / 'viewer.db'))
    parser.add_argument('--discard-backup', action='store_true', help='Remove the old viewer index only after validation/switch')
    parser.add_argument('--allow-missing', action='store_true', help='Allow sessions absent from current source files')
    parser.add_argument('--include-recent', action='store_true', help='Explicitly include files modified within the last hour')
    args = parser.parse_args()
    rebuild(Path(args.database), discard_backup=args.discard_backup, allow_missing=args.allow_missing,
            skip_recent_seconds=0 if args.include_recent else 3600)
