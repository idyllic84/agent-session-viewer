"""Offline setup/adoption. All original local files remain read-only."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3

from .codec import unpack_json
from .db import ARCHIVE_APPLICATION_ID,SCHEMA_VERSION
from .contracts.models import SessionDocument
from .contracts.session_policy import is_subagent
from .archive import CloudArchive
from .contracts.protocol import packet,decode


def init_keys(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    for name in ('owner.key','session.key'):
        path=directory/name;fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(secrets.token_urlsafe(48)+'\n');stream.flush();os.fsync(stream.fileno())
        print(f'Created private key file: {path} (contents not logged)')


def adopt(source,target):
    """Run LOCALLY: build a NEW analysis archive, never copy a detailed DB.

    In particular, legacy-only sessions survive even if their Agent file has
    disappeared. Raw tool/process records and their projection-state copies do
    not survive into the cloud artifact. Do not upload the original viewer.db.
    """
    source=Path(source).resolve();target=Path(target).resolve()
    root=Path(__file__).resolve().parents[1]
    if target.exists() or source==target or target in (root/'viewer.db',root/'claude.db',root/'local/viewer.db',root/'local/claude.db'):
        raise ValueError('Destination must be a new, different server analysis database')
    candidate=target.with_name(target.name+'.adopting.db')
    if candidate.exists():raise ValueError('Adoption candidate exists; inspect it before retrying')
    original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);original.row_factory=sqlite3.Row
    expected=set();excluded=0
    try:
        if original.execute('PRAGMA application_id').fetchone()[0]!=ARCHIVE_APPLICATION_ID or original.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:
            raise ValueError('Source must be a viewer v2 archive; migrate old claude.db through the local tool first')
        original.execute('BEGIN');archive=CloudArchive(candidate)
        for session in original.execute('SELECT * FROM sessions ORDER BY id'):
            metadata=json.loads(session['metadata'])
            if is_subagent(session['source'],session['path'],metadata):excluded+=1;continue
            print(f"[adopt] filter session {session['id']}",flush=True)
            sid=session['id'];raw=[(r[0],unpack_json(r[1])) for r in original.execute('SELECT record_index,raw_json FROM raw_records WHERE session_id=? ORDER BY record_index',(sid,))]
            doc=SessionDocument(session['source'],session['external_id'],session['project'],raw,name=session['name'],metadata=metadata,warnings=json.loads(session['warnings']))
            doc.messages=[unpack_json(r[0]) for r in original.execute('SELECT normalized_json FROM messages WHERE session_id=? ORDER BY order_id',(sid,))]
            checkpoint=original.execute('SELECT state FROM sync_checkpoints WHERE session_id=?',(sid,)).fetchone()
            doc.sync_state=unpack_json(checkpoint[0]) if checkpoint else {}
            doc.scope_states={r[0]:unpack_json(r[1]) for r in original.execute('SELECT scope,state FROM projection_state WHERE session_id=?',(sid,))}
            for index,scope in original.execute('SELECT record_index,scope FROM projection_raw WHERE session_id=?',(sid,)):doc.raw_scopes.setdefault(index,[]).append(scope)
            for mid,scope,major,minor in original.execute('''SELECT m.id,p.scope,p.major,p.minor FROM messages m
                    JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?''',(sid,)):
                doc.message_scopes[mid]=scope;doc.message_order[mid]=(major,minor)
            info={'path':session['path'],'fingerprint':session['fingerprint'],'modified':session['last_modified'],'parserVersion':session['parser_version']}
            # Wire representation uses JSON keys/arrays, just like remote POST.
            wire=json.loads(json.dumps(packet(doc,info),ensure_ascii=False))
            filtered,info=decode(wire)
            if not filtered.messages:excluded+=1;continue
            files=[(r['path'],r['source'],r['session_id'],r['fingerprint'],r['parser_version'],r['modified'],len(filtered.messages))
                   for r in original.execute('SELECT * FROM source_files WHERE session_id=?',(sid,))]
            with archive.db.transaction() as conn:
                archive.db.replace_session(filtered,info['path'],info['fingerprint'],info['modified'],info['parserVersion'],files)
                archive.refresh_scopes(conn,sid,set(filtered.message_scopes.values()))
                conn.execute('UPDATE sessions SET available=? WHERE id=?',(session['available'],sid))
                for row in original.execute('SELECT path,available FROM source_files WHERE session_id=?',(sid,)):
                    conn.execute('UPDATE source_files SET available=? WHERE path=?',(row['available'],row['path']))
            expected.add(sid)
    finally:original.close()
    with archive.db.transaction() as conn:
        if {r[0] for r in conn.execute('SELECT id FROM sessions')}!=expected:raise RuntimeError('Filtered session coverage mismatch')
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('Archive integrity failure')
        if archive.db.has_trigram:conn.execute("INSERT INTO message_search(message_search,rank) VALUES('integrity-check',1)")
    with archive.db.get_connection() as conn:
        busy,_,_=conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy:raise RuntimeError('Candidate in use; refusing to switch')
    target.parent.mkdir(parents=True,exist_ok=True);candidate.replace(target)
    print(f'Created analysis-only cloud artifact: {target}; retained {len(expected)} sessions, excluded {excluded}; source unchanged.')
    return {'retained':len(expected),'excluded':excluded}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    keys=sub.add_parser('init-keys');keys.add_argument('--directory',default=str(Path(__file__).with_name('state')))
    copy=sub.add_parser('adopt');copy.add_argument('--source',required=True);copy.add_argument('--database',required=True)
    args=parser.parse_args()
    if args.command=='init-keys':init_keys(args.directory)
    else:adopt(args.source,args.database)


if __name__=='__main__':main()
