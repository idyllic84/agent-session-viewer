"""Import an old full-history viewer.db (archive v2) into the local analysis store.

    python -m local.migrate --source local/viewer.db [--database local/state/local.db]

* The source database is opened READ-ONLY and never modified.
* Insert-only: sessions already present in the local store are skipped, so
  sessions imported from live Agent files are never replaced by older copies.
* Each session is projected through the same analysis contract as the live
  importer: no thinking, system events, attachments, raw records, tool output
  or subagent conversations are copied. Legacy-only sessions (whose Agent file
  no longer exists) are preserved with available=false.
* Imported sessions enter the change log, so a later cloud sync uploads them.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from server.contracts.models import SessionDocument
from server.contracts.protocol import packet, decode
from server.contracts.session_policy import is_subagent
from .archive import LocalArchive
from .codec import unpack_json
from .db import LEGACY_APPLICATION_ID

LEGACY_SCHEMA = 2


def migrate(source, database=None, log=print):
    source = Path(source).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    archive = LocalArchive(database)
    if archive.db.db_path.resolve() == source:
        raise ValueError('Source and destination must be different databases')
    original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True); original.row_factory = sqlite3.Row
    counts = {'imported': 0, 'present': 0, 'excluded': 0, 'empty': 0}; eligible = set()
    try:
        if (original.execute('PRAGMA application_id').fetchone()[0] != LEGACY_APPLICATION_ID or
                original.execute('PRAGMA user_version').fetchone()[0] != LEGACY_SCHEMA):
            raise ValueError('Source must be an old viewer v2 archive (viewer.db)')
        original.execute('BEGIN')  # one consistent read snapshot
        for session in original.execute('SELECT * FROM sessions ORDER BY id').fetchall():
            sid = session['id']; metadata = json.loads(session['metadata'])
            if is_subagent(session['source'], session['path'], metadata):
                counts['excluded'] += 1; continue
            with archive.db.get_connection() as conn:
                if conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
                    counts['present'] += 1; eligible.add(sid); continue
            raw = [(r[0], unpack_json(r[1])) for r in original.execute(
                'SELECT record_index,raw_json FROM raw_records WHERE session_id=? ORDER BY record_index', (sid,))]
            doc = SessionDocument(session['source'], session['external_id'], session['project'], raw, name=session['name'],
                                  metadata=metadata, warnings=json.loads(session['warnings']))
            doc.messages = [unpack_json(r[0]) for r in original.execute(
                'SELECT normalized_json FROM messages WHERE session_id=? ORDER BY order_id', (sid,))]
            checkpoint = original.execute('SELECT state FROM sync_checkpoints WHERE session_id=?', (sid,)).fetchone()
            doc.sync_state = unpack_json(checkpoint[0]) if checkpoint else {}
            doc.scope_states = {r[0]: unpack_json(r[1]) for r in original.execute(
                'SELECT scope,state FROM projection_state WHERE session_id=?', (sid,))}
            for index, scope in original.execute('SELECT record_index,scope FROM projection_raw WHERE session_id=?', (sid,)):
                doc.raw_scopes.setdefault(index, []).append(scope)
            for mid, scope, major, minor in original.execute('''SELECT m.id,p.scope,p.major,p.minor FROM messages m
                    JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?''', (sid,)):
                doc.message_scopes[mid] = scope; doc.message_order[mid] = (major, minor)
            info = {'path': session['path'], 'fingerprint': session['fingerprint'], 'modified': session['last_modified'],
                    'parserVersion': session['parser_version']}
            wire = json.loads(json.dumps(packet(doc, info), ensure_ascii=False))
            filtered, info = decode(wire)
            if not filtered.messages:
                counts['empty'] += 1; continue
            files = [(r['path'], r['source'], r['session_id'], r['fingerprint'], r['parser_version'], r['modified'], len(filtered.messages))
                     for r in original.execute('SELECT * FROM source_files WHERE session_id=?', (sid,))]
            available = bool(session['available']) and Path(session['path']).exists()
            if archive.import_legacy(filtered, info, files, available):
                counts['imported'] += 1
                log(f"[migrate] imported {sid} ({len(filtered.messages)} analytical messages)")
            else:
                counts['present'] += 1
            eligible.add(sid)
    finally:
        original.close()
    with archive.db.transaction(write=False) as conn:
        present = {r[0] for r in conn.execute('SELECT id FROM sessions')}
        if not eligible <= present:
            raise RuntimeError('Migration coverage check failed; nothing was deleted, inspect the log')
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Local store integrity check failed')
    counts['database'] = str(archive.db.db_path)
    log('[migrate] complete: ' + json.dumps(counts))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', required=True, help='Old full-history viewer.db (opened read-only)')
    parser.add_argument('--database', help='Local analysis store (default local/state/local.db)')
    args = parser.parse_args(argv)
    from .__main__ import InstanceLock
    database = Path(args.database) if args.database else Path(__file__).with_name('state') / 'local.db'
    lock = InstanceLock(database.with_name(database.name + '.lock'))
    try:
        migrate(args.source, database)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
