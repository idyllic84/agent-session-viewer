"""Local analysis store: import cursors, immutable change log and upload state.

Two independent progress records live here and are never mixed:

* source -> local store: ``source_files`` / ``sync_checkpoints`` (import cursors)
* local store -> cloud:  ``change_log`` sequence + ``cloud_connection`` (upload)

Every write transaction converts the rows it touched into immutable analytical
change-log entries *in the same transaction* (SQLite triggers record what was
touched). A cursor can therefore never advance without its upload changes.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import time
from threading import local

from server.contracts.analysis import POLICY, content_digest
from server.contracts.protocol import Problem, canonical, decode, digest, require, SOURCES
from server.contracts.session_policy import is_user_message
from .codec import pack_json, unpack_json
from .db import Database, encode

CHANGE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS local_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS local_sessions(id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS change_touch(session_id TEXT NOT NULL, message_id TEXT);
CREATE TABLE IF NOT EXISTS change_heads(kind TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT NOT NULL,
    digest TEXT NOT NULL, PRIMARY KEY(kind, id));
CREATE TABLE IF NOT EXISTS change_log(seq INTEGER PRIMARY KEY, group_id TEXT NOT NULL, op TEXT NOT NULL,
    session_id TEXT NOT NULL, entity_id TEXT NOT NULL, payload BLOB NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS change_log_group ON change_log(group_id, seq);
CREATE TABLE IF NOT EXISTS cloud_connection(id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL,
    key_ref TEXT NOT NULL, key_hint TEXT NOT NULL, client_id TEXT, client_name TEXT, cloud_instance TEXT,
    acked_seq INTEGER NOT NULL DEFAULT 0, pending TEXT, last_result TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS upload_receipts(through_seq INTEGER PRIMARY KEY, from_seq INTEGER NOT NULL,
    operation_key TEXT NOT NULL, cloud_instance TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS touch_message_insert AFTER INSERT ON messages BEGIN
    INSERT INTO change_touch VALUES(new.session_id, new.id); END;
CREATE TRIGGER IF NOT EXISTS touch_message_delete AFTER DELETE ON messages BEGIN
    INSERT INTO change_touch VALUES(old.session_id, old.id); END;
CREATE TRIGGER IF NOT EXISTS touch_message_update AFTER UPDATE OF normalized_json ON messages BEGIN
    INSERT INTO change_touch VALUES(new.session_id, new.id); END;
CREATE TRIGGER IF NOT EXISTS touch_session_insert AFTER INSERT ON sessions BEGIN
    INSERT INTO change_touch VALUES(new.id, NULL); END;
CREATE TRIGGER IF NOT EXISTS touch_session_update AFTER UPDATE ON sessions BEGIN
    INSERT INTO change_touch VALUES(new.id, NULL); END;
'''


class AtomicDatabase(Database):
    def __init__(self, path=None):
        super().__init__(path); self._local = local()

    @contextmanager
    def get_connection(self):
        active = getattr(self._local, 'connection', None)
        if active is not None:
            yield active
        else:
            with super().get_connection() as conn:
                yield conn

    @contextmanager
    def transaction(self, *, write=True):
        if getattr(self._local, 'connection', None) is not None:
            raise RuntimeError('Nested outer transaction')
        with super().get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN'); self._local.connection = conn
            try:
                yield conn
            finally:
                del self._local.connection


def session_payload(row):
    metadata = json.loads(row['metadata']); metadata.pop('userMessageCount', None)
    return {'id': row['id'], 'source': row['source'], 'externalId': row['external_id'], 'project': row['project'],
            'name': row['name'], 'lastModified': row['last_modified'], 'available': bool(row['available']),
            'metadata': metadata}


class LocalArchive:
    def __init__(self, path=None):
        self.db = AtomicDatabase(path); self.db.init_schema()
        with self.db.get_connection() as conn:
            conn.executescript(CHANGE_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO local_settings VALUES('content_policy',?)", (POLICY,))
            conn.execute("INSERT OR IGNORE INTO local_settings VALUES('dataset_id',?)", (secrets.token_hex(16),))
            conn.execute("INSERT OR IGNORE INTO local_settings VALUES('last_seq','0')")
            policy = conn.execute("SELECT value FROM local_settings WHERE key='content_policy'").fetchone()[0]
            if policy != POLICY:
                raise ValueError(f'Local store content policy {policy!r} is not supported by this version')
            self.dataset = conn.execute("SELECT value FROM local_settings WHERE key='dataset_id'").fetchone()[0]

    # ----- write transactions always emit their analytical change group -----
    @contextmanager
    def write(self):
        with self.db.transaction() as conn:
            yield conn
            self.emit_changes(conn)

    def emit_changes(self, conn):
        touched = conn.execute('SELECT DISTINCT session_id, message_id FROM change_touch').fetchall()
        if not touched:
            return 0
        conn.execute('DELETE FROM change_touch')
        sessions, messages = [], {}
        for sid, mid in touched:
            if sid not in messages:
                sessions.append(sid); messages[sid] = []
            if mid is not None and mid not in messages[sid]:
                messages[sid].append(mid)
        group = secrets.token_hex(12); now = time.time()
        seq = int(conn.execute("SELECT value FROM local_settings WHERE key='last_seq'").fetchone()[0]); first = seq + 1

        def head(kind, entity, sid, payload):
            nonlocal seq
            value = digest(payload) if payload is not None else None
            old = conn.execute('SELECT digest FROM change_heads WHERE kind=? AND id=?', (kind, entity)).fetchone()
            if payload is None:
                if old is None:
                    return
                conn.execute('DELETE FROM change_heads WHERE kind=? AND id=?', (kind, entity))
                op, payload = 'delete_message', {'id': entity}
            else:
                if old is not None and old[0] == value:
                    return
                conn.execute('INSERT INTO change_heads VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET digest=excluded.digest',
                             (kind, entity, sid, value))
                op = kind
            seq += 1
            conn.execute('INSERT INTO change_log VALUES(?,?,?,?,?,?,?)',
                         (seq, group, op, sid, entity, pack_json(payload), now))

        for sid in sessions:
            row = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if row is not None:
                head('session', sid, sid, session_payload(row))
            # A session must exist in the cloud before its messages arrive.
            for mid in sorted(messages[sid]):
                current = conn.execute('''SELECT m.normalized_json,p.major,p.minor FROM messages m
                    LEFT JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.id=?''', (mid,)).fetchone()
                payload = None if current is None else {
                    'message': unpack_json(current[0]), 'order': [current[1] or 0, current[2] or 0]}
                head('message', mid, sid, payload)
        if seq >= first:
            conn.execute("UPDATE local_settings SET value=? WHERE key='last_seq'", (str(seq),))
        return seq - first + 1

    # ----- import-side API (same semantics the old cloud progress API had) -----
    def revision(self, conn, sid):
        row = conn.execute('SELECT revision FROM local_sessions WHERE id=?', (sid,)).fetchone()
        return row[0] if row else 0

    def snapshot(self, conn, sid, revision, path=None, scope_delta=None):
        row = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone() if sid else None
        state = conn.execute('SELECT * FROM source_files WHERE path=?', (path or (row['path'] if row else ''),)).fetchone()
        checkpoint = conn.execute('SELECT path,parser_version,state FROM sync_checkpoints WHERE session_id=?', (sid,)).fetchone() if row else None
        result = {'state': dict(state) if state else None, 'revision': revision,
                  'context': {'session': self.db._session(row), 'primary': dict(row)} if row else None,
                  'checkpoint': {'path': checkpoint[0], 'parserVersion': checkpoint[1], 'state': unpack_json(checkpoint[2])} if checkpoint else None}
        if scope_delta is None:
            result['scopes'] = {r[0]: unpack_json(r[1]) for r in conn.execute(
                'SELECT scope,state FROM projection_state WHERE session_id=?', (sid,))} if row else {}
        else:
            result['scopesDelta'] = {scope: None for scope in scope_delta}
            for scope in scope_delta:
                saved = conn.execute('SELECT state FROM projection_state WHERE session_id=? AND scope=?', (sid, scope)).fetchone()
                if saved:
                    result['scopesDelta'][scope] = unpack_json(saved[0])
        return result

    def context(self, path=None, sid=None, source=None):
        with self.db.transaction(write=False) as conn:
            state = conn.execute('SELECT * FROM source_files WHERE path=?', (path,)).fetchone() if path else None
            if state:
                require(state['source'] == source, 'Source identity changed', 'SOURCE_CHANGED'); sid = state['session_id']
            return self.snapshot(conn, sid, self.revision(conn, sid) if sid else 0, path)

    def query_progress(self, files, *, include_context=True):
        """Metadata-only progress for a bounded discovery window."""
        require(isinstance(files, list) and 0 < len(files) <= 128, 'Query 1..128 file descriptors', status=400)
        for item in files:
            require(isinstance(item, dict) and item.get('source') in SOURCES and isinstance(item.get('path'), str), 'Invalid file descriptor', status=400)
        paths = list({item['path'] for item in files}); marks = ','.join('?' for _ in paths)
        with self.write() as conn:
            states = {r['path']: dict(r) for r in conn.execute(f'SELECT * FROM source_files WHERE path IN ({marks})', paths)}
            result = []
            for item in files:
                state = states.get(item['path']); entry = {'path': item['path'], 'source': item['source'], 'state': None, 'revision': 0}
                if state and state['source'] != item['source']:
                    entry['error'] = {'code': 'SOURCE_CHANGED', 'message': 'File progress belongs to another source', 'status': 409}
                elif state:
                    conn.execute('UPDATE source_files SET available=1 WHERE path=? AND available=0', (state['path'],))
                    conn.execute('UPDATE sessions SET available=1 WHERE id=? AND path=? AND available=0', (state['session_id'], state['path']))
                    state = {**state, 'available': 1}
                    checkpoint = conn.execute('SELECT state FROM sync_checkpoints WHERE session_id=?', (state['session_id'],)).fetchone()
                    progress = unpack_json(checkpoint[0]) if checkpoint else {}
                    revision = self.revision(conn, state['session_id'])
                    entry.update(state=state, revision=revision)
                    changed = (item.get('modified', state['modified']) != state['modified'] or
                               item.get('parserVersion', state['parser_version']) != state['parser_version'] or
                               progress.get('snapshotBytes') is not None and item.get('size', progress['snapshotBytes']) != progress['snapshotBytes'])
                    if include_context and changed:
                        entry['syncContext'] = self.snapshot(conn, state['session_id'], revision, state['path'])
                result.append(entry)
            return {'files': result}

    def commit(self, value, revision):
        """Validate an analysis-only packet, then store messages+cursor+changes atomically."""
        with self.write() as conn:
            document, info = decode(value); sid = document.id
            current = self.revision(conn, sid)
            require(current == revision, 'Local session revision changed', 'REVISION_CONFLICT', 412)
            file_owner = conn.execute('SELECT session_id FROM source_files WHERE path=?', (info['path'],)).fetchone()
            require(file_owner is None or file_owner['session_id'] == sid, 'Source file now describes another session', 'SOURCE_CHANGED')
            old = self.snapshot(conn, sid, revision, info['path'], scope_delta=set())['context']; base = document.append_base
            if base:
                require(old is not None, 'Increment requires an existing session', 'REVISION_CONFLICT', 412)
                require(old['primary']['path'] == info['path'], 'Source replica path changed', 'SOURCE_CHANGED')
            elif old:
                previous = {content_digest([unpack_json(r[0])]) for r in conn.execute('SELECT normalized_json FROM messages WHERE session_id=?', (sid,))}
                supplied = {content_digest([m]) for m in document.messages}
                require(previous <= supplied, 'Reconciliation would discard existing analytical history', 'SOURCE_DIVERGED')
            extra_scopes = set(); removed_users = 0; obsolete = set()
            if base:
                for message in document.messages:
                    previous = conn.execute('''SELECT m.message_pk,m.normalized_json,p.scope FROM messages m
                        JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.id=? AND m.session_id=?''',
                        (message['id'], sid)).fetchone()
                    if previous and message['type'] == 'tool-result' and message['message']['content'][0]['success'] is None:
                        prior = unpack_json(previous['normalized_json'])
                        if prior.get('type') == 'tool-result':
                            block = prior['message']['content'][0]
                            if isinstance(block.get('success'), bool):
                                message['message']['content'][0] = dict(block); message['status'] = prior['status']
                    if previous and previous['scope'] not in document.changed_scopes:
                        extra_scopes.add(previous['scope'])
                        removed_users += int(is_user_message(unpack_json(previous['normalized_json'])))
                        obsolete.update(r[0] for r in conn.execute('SELECT content_id FROM message_texts WHERE message_pk=?', (previous['message_pk'],)))
                        conn.execute('DELETE FROM messages WHERE message_pk=?', (previous['message_pk'],))
                if removed_users:
                    meta = json.loads(conn.execute('SELECT metadata FROM sessions WHERE id=?', (sid,)).fetchone()[0])
                    meta['userMessageCount'] = meta.get('userMessageCount', 0) - removed_users
                    conn.execute('UPDATE sessions SET metadata=? WHERE id=?', (encode(meta), sid))
                for cid in obsolete:
                    conn.execute('DELETE FROM search_content WHERE content_id=? AND NOT EXISTS (SELECT 1 FROM message_texts WHERE content_id=?)', (cid, cid))
            info_tuple = (info['path'], document.source, sid, info['fingerprint'], info['parserVersion'], info['modified'], len(document.messages))
            method = (self.db.apply_delta if document.sync_state else self.db.append_session) if base else self.db.replace_session
            method(document, info['path'], info['fingerprint'], info['modified'], info['parserVersion'], [info_tuple])
            if extra_scopes:
                conn.execute('UPDATE messages SET order_id=-order_id-1 WHERE session_id=?', (sid,))
                conn.execute('''UPDATE messages SET order_id=ordered.n FROM (SELECT m.message_pk,ROW_NUMBER() OVER (ORDER BY p.major,p.minor,m.message_pk)-1 AS n FROM messages m JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?) AS ordered
                    WHERE messages.message_pk=ordered.message_pk''', (sid,))
            touched = set(document.changed_scopes) | set(document.message_scopes.values()) | extra_scopes
            if not base:
                conn.execute('DELETE FROM projection_state WHERE session_id=?', (sid,))
            self.refresh_scopes(conn, sid, touched)
            conn.execute('INSERT INTO local_sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision', (sid, revision + 1))
            saved = self.db.get_session(sid)
            receipt = {'sessionId': sid, 'revision': revision + 1, 'messageCount': saved['messageCount'] if saved else 0,
                       'userMessageCount': saved['userMessageCount'] if saved else 0, 'fingerprint': info['fingerprint'],
                       'nextContext': self.snapshot(conn, sid, revision + 1, info['path'], scope_delta=touched)}
            receipt['nextContext']['resetScopes'] = not bool(base)
            return receipt

    def remember_duplicate(self, value):
        """Record a non-primary copy whose messages are all already stored."""
        with self.write() as conn:
            document, info = decode(value); sid = document.id
            row = conn.execute('SELECT path FROM sessions WHERE id=?', (sid,)).fetchone()
            if row is None or row[0] == info['path']:
                return False
            previous = {content_digest([unpack_json(r[0])]) for r in conn.execute('SELECT normalized_json FROM messages WHERE session_id=?', (sid,))}
            if not {content_digest([m]) for m in document.messages} <= previous:
                return False
            owner = conn.execute('SELECT session_id FROM source_files WHERE path=?', (info['path'],)).fetchone()
            if owner and owner[0] != sid:
                return False
            self.db._store_files(conn, [(info['path'], document.source, sid, info['fingerprint'], info['parserVersion'],
                                         info['modified'], len(document.messages))])
            return True

    def refresh_scopes(self, conn, sid, scopes):
        for scope in scopes:
            rows = [unpack_json(r[0]) for r in conn.execute('''SELECT m.normalized_json FROM messages m
                JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=? AND p.scope=?
                ORDER BY p.major,p.minor,m.message_pk''', (sid, scope))]
            if rows:
                conn.execute('INSERT INTO projection_state VALUES(?,?,?) ON CONFLICT(session_id,scope) DO UPDATE SET state=excluded.state',
                             (sid, scope, pack_json({'analysisDigest': content_digest(rows), 'count': len(rows)})))
            else:
                conn.execute('DELETE FROM projection_state WHERE session_id=? AND scope=?', (sid, scope))

    def finish_scan(self, sources, seen):
        """Availability only. A missing source never deletes analytical history."""
        require(all(s in SOURCES for s in sources), 'Invalid completed sources')
        present = set(seen)
        with self.write() as conn:
            rows = conn.execute('SELECT path,source FROM source_files WHERE available=1').fetchall()
            missing = [r['path'] for r in rows if r['source'] in sources and r['path'] not in present and not Path(r['path']).exists()]
            conn.executemany('UPDATE source_files SET available=0 WHERE path=?', ((p,) for p in missing))
            if missing:
                conn.execute('''UPDATE sessions SET available=0 WHERE available=1 AND EXISTS
                    (SELECT 1 FROM source_files f WHERE f.session_id=sessions.id) AND NOT EXISTS
                    (SELECT 1 FROM source_files f WHERE f.session_id=sessions.id AND f.path=sessions.path AND f.available=1)''')
            return len(missing)

    def import_legacy(self, document, info, files, available):
        """Insert-only migration of an old full-history session (already filtered)."""
        with self.write() as conn:
            if conn.execute('SELECT 1 FROM sessions WHERE id=?', (document.id,)).fetchone():
                return False
            for path, *_ in files:
                owner = conn.execute('SELECT session_id FROM source_files WHERE path=?', (path,)).fetchone()
                if owner and owner[0] != document.id:
                    raise Problem('SOURCE_CHANGED', 'Legacy source path already belongs to another session')
            self.db.replace_session(document, info['path'], info['fingerprint'], info['modified'], info['parserVersion'], files)
            self.refresh_scopes(conn, document.id, set(document.message_scopes.values()))
            conn.execute('UPDATE sessions SET available=? WHERE id=?', (int(bool(available)), document.id))
            for path, *_ in files:
                exists = Path(path).exists()
                conn.execute('UPDATE source_files SET available=? WHERE path=?', (int(exists), path))
            conn.execute('INSERT OR IGNORE INTO local_sessions VALUES(?,1)', (document.id,))
            return True

    # ----- upload-side API -----
    def last_seq(self):
        with self.db.get_connection() as conn:
            return int(conn.execute("SELECT value FROM local_settings WHERE key='last_seq'").fetchone()[0])

    def changes(self, first, last):
        """Immutable change entries in [first, last], grouped by their commit."""
        with self.db.get_connection() as conn:
            rows = conn.execute('SELECT * FROM change_log WHERE seq BETWEEN ? AND ? ORDER BY seq', (first, last)).fetchall()
        require(len(rows) == max(0, last - first + 1), 'Local change log has a gap; upload refused', 'LOCAL_LOG_GAP', 500)
        return [{'seq': r['seq'], 'group': r['group_id'], 'op': r['op'], 'sessionId': r['session_id'],
                 'data': unpack_json(r['payload'])} for r in rows]

    def group_end(self, seq):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT group_id FROM change_log WHERE seq=?', (seq,)).fetchone()
            require(row is not None, 'Unknown change sequence', 'LOCAL_LOG_GAP', 500)
            return conn.execute('SELECT MAX(seq) FROM change_log WHERE group_id=?', (row[0],)).fetchone()[0]

    def connection(self):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT * FROM cloud_connection WHERE id=1').fetchone()
            if row is None:
                return None
            value = dict(row)
            value['pending'] = json.loads(value['pending']) if value['pending'] else None
            value['last_result'] = json.loads(value['last_result']) if value['last_result'] else None
            return value

    def save_connection(self, url, key_ref, key_hint, client_id, client_name, instance):
        """Saving/replacing a connection never invents upload progress.

        The cloud's committed position is authoritative and is re-read before
        every sync, so a different cloud slot simply starts from its own head.
        """
        with self.db.transaction() as conn:
            old = conn.execute('SELECT * FROM cloud_connection WHERE id=1').fetchone()
            same = old is not None and old['client_id'] == client_id and old['cloud_instance'] == instance
            conn.execute('''INSERT INTO cloud_connection(id,url,key_ref,key_hint,client_id,client_name,cloud_instance,acked_seq,pending,last_result,updated)
                VALUES(1,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,key_ref=excluded.key_ref,
                key_hint=excluded.key_hint,client_id=excluded.client_id,client_name=excluded.client_name,
                cloud_instance=excluded.cloud_instance,acked_seq=excluded.acked_seq,pending=excluded.pending,
                last_result=excluded.last_result,updated=excluded.updated''',
                (url, key_ref, key_hint, client_id, client_name, instance,
                 old['acked_seq'] if same else 0, old['pending'] if same else None,
                 old['last_result'] if same else None, time.time()))
            return old['key_ref'] if old is not None else None

    def delete_connection(self):
        with self.db.transaction() as conn:
            old = conn.execute('SELECT key_ref FROM cloud_connection WHERE id=1').fetchone()
            conn.execute('DELETE FROM cloud_connection WHERE id=1')
            return old[0] if old else None

    def update_connection(self, **values):
        allowed = {'acked_seq', 'pending', 'last_result'}
        require(set(values) <= allowed, 'Invalid connection update', status=500)
        with self.db.transaction() as conn:
            for key, value in values.items():
                if key in ('pending', 'last_result') and value is not None:
                    value = canonical(value)
                conn.execute(f'UPDATE cloud_connection SET {key}=?,updated=? WHERE id=1', (value, time.time()))

    def acknowledge(self, manifest, receipt, instance):
        with self.db.transaction() as conn:
            conn.execute('UPDATE cloud_connection SET acked_seq=?,pending=NULL,updated=? WHERE id=1',
                         (manifest['toSeq'], time.time()))
            conn.execute('INSERT OR REPLACE INTO upload_receipts VALUES(?,?,?,?,?,?)',
                         (manifest['toSeq'], manifest['fromSeq'], manifest['key'], instance, encode(receipt), time.time()))

    def stats(self):
        with self.db.get_connection() as conn:
            sessions = conn.execute('SELECT COUNT(*) FROM sessions WHERE message_count>0').fetchone()[0]
            messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            files = conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]
        return {'sessions': sessions, 'messages': messages, 'sourceFiles': files, 'lastSeq': self.last_seq(),
                'datasetId': self.dataset, 'database': str(self.db.db_path)}

    def session_revision(self, sid):
        with self.db.get_connection() as conn:
            return self.revision(conn, sid)


def payload_hash(body):
    return hashlib.sha256(body).hexdigest()
