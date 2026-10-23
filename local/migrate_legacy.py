"""Explicit, offline, insertion-only migration of the old Claude SQLite archive.

Read claude.db only; stage a copy of viewer.db, preserve existing projections and
watermarks, validate raw coverage, then switch with the original kept as backup.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'local'
from .codec import unpack_json
from .db import Database
from .models import SessionDocument
from .rebuild import checkpoint, validate
from .session_policy import is_subagent
from .sources.claude import ClaudeAdapter

MIGRATION_VERSION = 1


@contextmanager
def step(label):
    started = time.monotonic()
    stop = Event()
    def heartbeat():
        while not stop.wait(5):
            print(f'[legacy] {label}: still working ({time.monotonic()-started:.1f}s)', flush=True)
    thread = Thread(target=heartbeat, daemon=True)
    print(f'[legacy] {label}: start', flush=True)
    thread.start()
    try:
        yield
    finally:
        stop.set(); thread.join()
        print(f'[legacy] {label}: end ({time.monotonic()-started:.2f}s)', flush=True)


def assert_offline():
    if sys.platform == 'win32':
        command = ['powershell', '-NoProfile', '-Command',
                   "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '(server[.]py|local[.]server)' } | Select-Object -ExpandProperty ProcessId"]
        result = subprocess.run(command, capture_output=True, text=True, check=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        running = result.stdout.strip()
    else:
        result = subprocess.run(['ps', '-eo', 'args='], capture_output=True, text=True, check=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        running = '\n'.join(line for line in result.stdout.splitlines()
                            if 'python' in line and ('server.py' in line or 'local.server' in line))
    if running:
        raise RuntimeError('Viewer server is running; stop it before migrating. No service was stopped automatically.')


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def signature(path):
    main = (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
    wal = Path(str(path)+'-wal')
    # SQLite read-only connections can create an empty WAL/shm. An empty WAL
    # carries no changes and is equivalent to its absence; nonempty WAL changes
    # remain part of the concurrent-writer guard.
    changes = (wal.stat().st_size, wal.stat().st_mtime_ns) if wal.exists() and wal.stat().st_size else None
    return main, changes


def read_legacy(path):
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {table: {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                   for table in ('sessions', 'messages')}
        if not {'id','project','message_count','last_update_time'} <= columns['sessions'] or not {
                'session_id','order_id','timestamp','type','raw_json'} <= columns['messages']:
            raise ValueError('Not the recognized legacy Claude archive schema')
        conn.execute('BEGIN')  # Stable read snapshot; no source writes/checkpoints.
        return conn
    except Exception:
        conn.close()
        raise


class LegacyRecords(list):
    def __init__(self):
        super().__init__()
        self.record_sizes = {}


def project_legacy(conn, session, source):
    records = LegacyRecords()
    digest = hashlib.sha256()
    legacy_timestamps = {}
    for row in conn.execute('SELECT order_id,timestamp,raw_json FROM messages WHERE session_id=? ORDER BY order_id',
                            (session['id'],)):
        value = json.loads(row['raw_json'])
        if not isinstance(value, dict):
            raise ValueError(f"Legacy record is not an object: {session['id']} / {row['order_id']}")
        index = row['order_id']
        records.append((index, value))
        encoded = row['raw_json'].encode('utf-8')
        records.record_sizes[index] = len(encoded)
        digest.update(str(index).encode()+b'\0'+encoded+b'\0')
        legacy_timestamps[index] = row['timestamp']
    if len(records) != session['message_count']:
        raise ValueError(f"Legacy record count mismatch: {session['id']}")
    cwd = next((row.get('cwd') for _, row in records if row.get('cwd')), None)
    document = SessionDocument('claude', session['id'], str(cwd or session['project']), records)
    document.metadata.update(metadataOnly=True, projectFromCwd=bool(cwd), rawSessionId=session['id'],
                             claudeTitle={'known': True,'custom': None,'slug': None},
                             legacyArchive={'version': MIGRATION_VERSION, 'database': str(source),
                                            'sessionId': session['id'], 'originalProject': session['project'],
                                            'recordCount': len(records), 'lastUpdateTimeSeconds': session['last_update_time'],
                                            'sourceAvailable': False})
    ClaudeAdapter._project(document)
    ClaudeAdapter._titles(document)
    # Preserve the old timestamp column even when the native record omitted it.
    document.metadata['legacyArchive']['timestampColumn'] = [[i,t] for i,t in legacy_timestamps.items()]
    conversation = [row for _,row in records if row.get('type') in ('user','assistant')]
    if conversation and all(row.get('isSidechain') is True for row in conversation):
        document.metadata['relationship'] = 'subagent'
    if any(is_subagent('claude',session['id']+'.jsonl',header=row) for row in conversation):
        document.metadata['relationship'] = 'subagent'
    return document.finish(), digest.hexdigest()


def record_key(record):
    return hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).digest()


def verify_coverage(legacy, database, source):
    result = {'coveredSessions': 0, 'excludedSubagents': 0, 'excludedEmpty': 0,
              'legacyRecordsCovered': 0}
    with database.get_connection() as conn:
        for session in legacy.execute('SELECT * FROM sessions ORDER BY id'):
            if is_subagent('claude',session['id']+'.jsonl'):
                result['excludedSubagents'] += 1; continue
            document, _ = project_legacy(legacy, session, source)
            if is_subagent('claude',session['id']+'.jsonl',document.metadata):
                result['excludedSubagents'] += 1; continue
            if not document.messages:
                result['excludedEmpty'] += 1; continue
            archived = conn.execute('SELECT * FROM sessions WHERE id=?', (document.id,)).fetchone()
            if not archived:
                raise RuntimeError(f'Missing legacy session after migration: {document.id}')
            if json.loads(archived['metadata']).get('legacyArchive'):
                # Newly migrated archives preserve each ordinal and duplicate.
                actual = conn.execute('SELECT record_index,raw_json FROM raw_records WHERE session_id=? ORDER BY record_index',
                                      (document.id,))
                for expected, stored in zip_longest(document.records, actual):
                    if expected is None or stored is None or expected[0] != stored[0] or expected[1] != unpack_json(stored[1]):
                        raise RuntimeError(f'Raw record mismatch: {document.id}')
            else:
                # Existing native projections may include newer records and use
                # different line ordinals. Require every old value to be present.
                native = {record_key(unpack_json(row[0])) for row in conn.execute(
                    'SELECT raw_json FROM raw_records WHERE session_id=?', (document.id,))}
                if any(record_key(value) not in native for _,value in document.records):
                    raise RuntimeError(f'Existing session lacks older legacy records: {document.id}; '
                                       'refusing to overwrite native history')
            result['coveredSessions'] += 1
            result['legacyRecordsCovered'] += len(document.records)
    return result


def verify_existing(original, candidate):
    """Byte-for-byte comparison, streamed; do not decompress existing history."""
    old = sqlite3.connect(original.as_uri()+'?mode=ro', uri=True)
    new = sqlite3.connect(candidate.as_uri()+'?mode=ro', uri=True)
    try:
        ids = [row[0] for row in old.execute('SELECT id FROM sessions ORDER BY id')]
        placeholders = ','.join('?' for _ in ids)
        for table, key in [('sessions','id'), ('raw_records','session_id,record_index'),
                           ('messages','message_pk'), ('source_files','path'),
                           ('sync_checkpoints','session_id'), ('projection_state','session_id,scope'),
                           ('projection_raw','session_id,record_index,scope'), ('projection_messages','message_pk')]:
            old_rows = old.execute(f'SELECT * FROM {table} ORDER BY {key}')
            if table in ('source_files','sync_checkpoints','projection_state'):
                new_rows = new.execute(f'SELECT * FROM {table} ORDER BY {key}')
            elif table == 'projection_messages':
                maximum = old.execute('SELECT COALESCE(MAX(message_pk),0) FROM messages').fetchone()[0]
                new_rows = new.execute(f'SELECT * FROM {table} WHERE message_pk<=? ORDER BY {key}', (maximum,))
            else:
                column = 'id' if table == 'sessions' else 'session_id'
                new_rows = new.execute(f'SELECT * FROM {table} WHERE {column} IN ({placeholders}) ORDER BY {key}', ids)
            for left,right in zip_longest(old_rows,new_rows):
                if left != right:
                    raise RuntimeError(f'Existing archive changed unexpectedly: {table}')
    finally:
        old.close();new.close()


def migrate(source, target, report_path=None):
    source = Path(source).resolve();target = Database(target).db_path.resolve()
    if source == target:
        raise ValueError('Source and destination must be different databases')
    assert_offline()
    legacy = read_legacy(source)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    candidate = target.with_name(f'{target.stem}.legacy-migrate-{stamp}.db')
    backup = target.with_name(f'{target.stem}.before-legacy-{stamp}.db')
    started = time.monotonic()
    try:
        with step('hash read-only legacy archive'):
            source_hash = file_hash(source)
            source_signature = signature(source)
        if target.exists():
            # Validate ownership before any write/checkpoint to the target.
            read = sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
            try:
                from .db import ARCHIVE_APPLICATION_ID, SCHEMA_VERSION
                if read.execute('PRAGMA application_id').fetchone()[0] != ARCHIVE_APPLICATION_ID or read.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError('Destination is not a recognized viewer v2 archive')
            finally:
                read.close()
            checkpoint(target)
        original_signature = signature(target)
        with step('stage viewer archive snapshot'):
            if target.exists():
                original = sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
                dest = sqlite3.connect(candidate)
                try:original.backup(dest,pages=8192,sleep=.01)
                finally:original.close();dest.close()
        database = Database(candidate)
        with step('initialize candidate search policy'):
            database.init_schema()
        report = {'imported': 0,'existingSkipped': 0,'excludedSubagents': 0,'excludedEmpty': 0,
                  'importedRecords': 0,'source': str(source),'sourceSha256': source_hash}
        total = legacy.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        with database.get_connection() as conn:
            before_ids = {row[0] for row in conn.execute('SELECT id FROM sessions')}
        imported_ids = set()
        for number,session in enumerate(legacy.execute('SELECT * FROM sessions ORDER BY id'),1):
            label = f"session {number}/{total}: {session['id']}"
            with step(label):
                if 'claude:'+session['id'] in before_ids:
                    report['existingSkipped'] += 1
                    print('[legacy] existing native/archive session preserved',flush=True);continue
                if is_subagent('claude',session['id']+'.jsonl'):
                    report['excludedSubagents'] += 1;continue
                document,digest = project_legacy(legacy,session,source)
                if is_subagent('claude',session['id']+'.jsonl',document.metadata):
                    report['excludedSubagents'] += 1;continue
                if not document.messages:
                    report['excludedEmpty'] += 1;continue
                path = str(source)+'#session='+session['id']
                if database.import_legacy_session(document,path,digest,int(session['last_update_time'])*1000,
                                                  ClaudeAdapter.parser_version):
                    report['imported'] += 1;report['importedRecords'] += len(document.records)
                    imported_ids.add(document.id)
                    print(f'[legacy] committed {len(document.messages)} records; '
                          f"{document.metadata['userMessageCount']} user messages; source unavailable",flush=True)
        with step('verify all legacy raw records and session coverage'):
            report['coverage'] = verify_coverage(legacy,database,source)
        if target.exists():
            with step('verify existing histories and checkpoints unchanged'):
                verify_existing(target,candidate)
        with step('validate SQLite foreign keys and external-content FTS'):
            report['sessionsAfter'] = validate(database)
            with database.get_connection() as conn:
                after_ids = {row[0] for row in conn.execute('SELECT id FROM sessions')}
                if after_ids != before_ids | imported_ids:
                    raise RuntimeError('Unexpected session coverage after migration')
                report['recordsAfter'] = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
                if conn.execute("SELECT COUNT(*) FROM message_texts t JOIN messages m ON m.message_pk=t.message_pk JOIN sessions s ON s.id=m.session_id WHERE json_extract(s.metadata,'$.legacyArchive') IS NOT NULL AND s.available<>0").fetchone()[0]:
                    raise RuntimeError('Legacy-only sources incorrectly marked available')
            report['visibleSessionsAfter'] = database.list_sessions(limit=0)[1]
        with step('verify legacy source unchanged'):
            if signature(source) != source_signature or file_hash(source) != source_hash:
                raise RuntimeError('Legacy source changed during migration; refusing to switch')
        legacy.close();legacy = None
        assert_offline()
        if signature(target) != original_signature:
            raise RuntimeError('Destination changed during migration; refusing to switch')
        checkpoint(candidate)
        report['beforeBytes'] = target.stat().st_size if target.exists() else 0
        report['afterBytes'] = candidate.stat().st_size
        if target.exists():
            checkpoint(target)
            target.replace(backup)
        try:
            candidate.replace(target)
        except Exception:
            if backup.exists() and not target.exists():backup.replace(target)
            raise
        report['backup'] = str(backup) if backup.exists() else None
        report['elapsedSeconds'] = round(time.monotonic()-started,2)
        rendered = json.dumps(report,ensure_ascii=False,indent=2)
        if report_path:Path(report_path).write_text(rendered+'\n',encoding='utf-8')
        print(rendered,flush=True)
        return report
    except Exception:
        print(f'[legacy] failed; original viewer unchanged; candidate retained at {candidate}',flush=True)
        raise
    finally:
        if legacy is not None:legacy.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=str(Path(__file__).with_name('claude.db')))
    parser.add_argument('--database',default=str(Path(__file__).with_name('viewer.db')))
    parser.add_argument('--report',default=str(Path(__file__).with_name('viewer-legacy-migration-result.log')))
    args = parser.parse_args()
    migrate(args.source,args.database,args.report)
