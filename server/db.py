"""Viewer-owned archive v2: compressed JSON, shared text, position-free trigram FTS."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from .contracts.session_policy import is_subagent, is_user_message
from .codec import pack_json, unpack_json
from .contracts.models import indexed_message_text

SCHEMA_VERSION = 2
ARCHIVE_APPLICATION_ID = 0x43565732
SEARCH_POLICY_VERSION = 'user-assistant-prose-v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def trigram_query(query):
    """detail=none accepts only three-character tokens, not long phrase queries.

    AND a bounded set of grams to select candidates; literal LIKE verifies exact
    order/adjacency, preserving punctuation, CJK and true substring semantics.
    """
    grams = list(dict.fromkeys(query[i:i + 3] for i in range(len(query) - 2)))
    if len(grams) > 32:
        grams = [grams[i * (len(grams) - 1) // 31] for i in range(32)]
    return ' AND '.join('"' + gram.replace('"', '""') + '"' for gram in grams)


class Database:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or os.environ.get('REMOTE_VIEWER_DB') or Path(__file__).parent / 'state/cloud.db')
        root = Path(__file__).resolve().parents[1]
        if self.db_path.resolve() in (root / 'claude.db', root / 'local/claude.db'):
            raise ValueError('The legacy claude.db is preserved; choose a separate VIEWER_DB')
        self.has_trigram = False

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.create_function('viewer_subagent', 3,
            lambda source, path, meta: int(is_subagent(source, path, json.loads(meta))), deterministic=True)
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            application_id = conn.execute('PRAGMA application_id').fetchone()[0]
            if (tables and application_id != ARCHIVE_APPLICATION_ID) or (
                    not tables and application_id not in (0, ARCHIVE_APPLICATION_ID)):
                raise ValueError('Not a viewer-owned archive; existing databases will not be modified')
            if version == 1:
                raise ValueError('Archive schema v1 requires rebuilding. Stop the viewer and run python rebuild.py')
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f'Unsupported archive schema version: {version}')
            conn.execute(f'PRAGMA application_id = {ARCHIVE_APPLICATION_ID}')
            conn.execute('PRAGMA journal_mode = WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    project TEXT NOT NULL, name TEXT NOT NULL, message_count INTEGER NOT NULL,
                    last_modified INTEGER NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    parser_version INTEGER NOT NULL, metadata TEXT NOT NULL, warnings TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS sessions_modified ON sessions(last_modified DESC, id);
                CREATE INDEX IF NOT EXISTS sessions_source_project ON sessions(source, project);
                CREATE TABLE IF NOT EXISTS raw_records (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    record_index INTEGER NOT NULL, raw_json BLOB NOT NULL,
                    PRIMARY KEY (session_id, record_index)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_pk INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    order_id INTEGER NOT NULL, native_id TEXT, parent_id TEXT,
                    raw_record INTEGER NOT NULL, normalized_json BLOB NOT NULL,
                    UNIQUE (session_id, order_id)
                );
                CREATE TABLE IF NOT EXISTS archive_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS search_content (
                    content_id INTEGER PRIMARY KEY, digest BLOB NOT NULL UNIQUE, search_text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_texts (
                    message_pk INTEGER PRIMARY KEY REFERENCES messages(message_pk) ON DELETE CASCADE,
                    content_id INTEGER NOT NULL REFERENCES search_content(content_id)
                );
                CREATE INDEX IF NOT EXISTS message_texts_content ON message_texts(content_id);
                CREATE TABLE IF NOT EXISTS source_files (
                    path TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, parser_version INTEGER NOT NULL,
                    modified INTEGER NOT NULL, message_count INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS source_files_session ON source_files(session_id);
                CREATE TABLE IF NOT EXISTS sync_checkpoints (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, parser_version INTEGER NOT NULL, state BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_messages (
                    message_pk INTEGER PRIMARY KEY REFERENCES messages(message_pk) ON DELETE CASCADE,
                    scope TEXT NOT NULL, major INTEGER NOT NULL, minor INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS projection_messages_scope ON projection_messages(scope,message_pk);
                CREATE TABLE IF NOT EXISTS projection_raw (
                    session_id TEXT NOT NULL, record_index INTEGER NOT NULL, scope TEXT NOT NULL,
                    PRIMARY KEY(session_id,record_index,scope),
                    FOREIGN KEY(session_id,record_index) REFERENCES raw_records(session_id,record_index) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS projection_raw_scope ON projection_raw(session_id,scope,record_index);
                CREATE TABLE IF NOT EXISTS projection_state (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    scope TEXT NOT NULL, state BLOB NOT NULL, PRIMARY KEY(session_id,scope)
                );
            ''')
            try:
                conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
                    search_text, content='search_content', content_rowid='content_id',
                    tokenize='trigram', detail=none, columnsize=0)""")
                new_index = not conn.execute("SELECT 1 FROM sqlite_master WHERE name='content_search_insert'").fetchone()
                conn.executescript('''
                    CREATE TRIGGER IF NOT EXISTS content_search_insert AFTER INSERT ON search_content BEGIN
                        INSERT INTO message_search(rowid, search_text) VALUES (new.content_id, new.search_text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS content_search_delete AFTER DELETE ON search_content BEGIN
                        INSERT INTO message_search(message_search, rowid, search_text)
                        VALUES ('delete', old.content_id, old.search_text);
                    END;
                ''')
                if new_index:
                    conn.execute("INSERT INTO message_search(message_search) VALUES ('rebuild')")
                self.has_trigram = True
            except sqlite3.OperationalError:
                self.has_trigram = False
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        self.ensure_search_policy()

    def ensure_search_policy(self):
        """Rebuild only search tables atomically when search scope changes."""
        with self.get_connection() as conn:
            setting = conn.execute("SELECT value FROM archive_settings WHERE key='search_policy'").fetchone()
            if setting and setting[0] == SEARCH_POLICY_VERSION:
                return 0
            total = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            if total:
                print(f'[index] rebuilding user/assistant-only search for {total} messages', flush=True)
            # BEGIN explicitly: trigger DDL must roll back together with the
            # index/text mappings. Never use executescript (implicit commit).
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP TRIGGER IF EXISTS content_search_insert')
            conn.execute('DROP TRIGGER IF EXISTS content_search_delete')
            if self.has_trigram:
                conn.execute("INSERT INTO message_search(message_search) VALUES ('delete-all')")
            conn.execute('DELETE FROM message_texts')
            conn.execute('DELETE FROM search_content')
            if self.has_trigram:
                conn.execute('''CREATE TRIGGER content_search_insert AFTER INSERT ON search_content BEGIN
                    INSERT INTO message_search(rowid,search_text) VALUES(new.content_id,new.search_text); END''')
                conn.execute('''CREATE TRIGGER content_search_delete AFTER DELETE ON search_content BEGIN
                    INSERT INTO message_search(message_search,rowid,search_text)
                    VALUES('delete',old.content_id,old.search_text); END''')
            processed = indexed = 0
            last_log = time.monotonic()
            cursor = conn.execute('SELECT message_pk,normalized_json FROM messages ORDER BY message_pk')
            for row in cursor:
                message = unpack_json(row['normalized_json'])
                search = indexed_message_text(message)
                if search.strip():
                    self._store_search_text(conn, row['message_pk'], search)
                    indexed += 1
                processed += 1
                if time.monotonic() - last_log >= 5:
                    print(f'[index] search migration {processed}/{total}; {indexed} prose messages indexed', flush=True)
                    last_log = time.monotonic()
            conn.execute("INSERT INTO archive_settings VALUES('search_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (SEARCH_POLICY_VERSION,))
            if total:
                print(f'[index] user/assistant search ready: {indexed}/{total} messages indexed', flush=True)
            return indexed

    def file_states(self):
        with self.get_connection() as conn:
            return {r['path']: dict(r) for r in conn.execute('SELECT * FROM source_files')}

    def replace_session(self, document, path, fingerprint, modified, parser_version, files):
        """Raw blobs, projection, shared text/FTS and watermarks commit atomically."""
        with self.get_connection() as conn:
            document.metadata['userMessageCount'] = sum(is_user_message(m) for m in document.messages)
            conn.execute('''INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET project=excluded.project, name=excluded.name,
                message_count=excluded.message_count, last_modified=excluded.last_modified,
                path=excluded.path, fingerprint=excluded.fingerprint, parser_version=excluded.parser_version,
                metadata=excluded.metadata, warnings=excluded.warnings, available=1''',
                (document.id, document.source, document.external_id, document.project, document.name,
                 len(document.messages), modified, str(path), fingerprint, parser_version,
                 encode(document.metadata), encode(document.warnings)))
            old_content = [r[0] for r in conn.execute('''SELECT DISTINCT t.content_id FROM message_texts t
                JOIN messages m ON m.message_pk=t.message_pk WHERE m.session_id=?''', (document.id,))]
            conn.execute('DELETE FROM messages WHERE session_id=?', (document.id,))
            # Do not delete shared content still used by another session/message.
            conn.executemany('''DELETE FROM search_content WHERE content_id=? AND NOT EXISTS
                (SELECT 1 FROM message_texts WHERE message_texts.content_id=search_content.content_id)''',
                ((content_id,) for content_id in old_content))
            conn.execute('DELETE FROM raw_records WHERE session_id=?', (document.id,))
            conn.execute('DELETE FROM projection_state WHERE session_id=?', (document.id,))
            conn.execute('DELETE FROM sync_checkpoints WHERE session_id=?', (document.id,))
            self._insert_projection(conn, document)
            self._store_projection_state(conn, document, path, parser_version)
            self._store_files(conn, files)

    def import_legacy_session(self, document, path, fingerprint, modified, parser_version):
        """Insertion-only archive import; never replace a live-source projection.

        Legacy SQLite provenance is not a JSONL file/checkpoint. Keep source_files
        untouched and mark the source unavailable; normal discovery can replace
        this projection if the original transcript reappears later.
        """
        with self.get_connection() as conn:
            document.metadata['userMessageCount'] = sum(is_user_message(m) for m in document.messages)
            cursor = conn.execute('''INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO NOTHING''',
                (document.id, document.source, document.external_id, document.project, document.name,
                 len(document.messages), modified, str(path), fingerprint, parser_version,
                 encode(document.metadata), encode(document.warnings)))
            if cursor.rowcount == 0:
                return False
            self._insert_projection(conn, document)
            return True

    def append_context(self, session_id):
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None:
                return None
            count, last = conn.execute('SELECT COUNT(*),MAX(order_id) FROM messages WHERE session_id=?', (session_id,)).fetchone()
            if count != row['message_count'] or (last is not None and last + 1 != count):
                return None
            return {'session': self._session(row), 'primary': dict(row)}

    def append_session(self, document, path, fingerprint, modified, parser_version, files):
        """Append only new raw/messages/text mappings; old row IDs/blobs are untouched."""
        with self.get_connection() as conn:
            current = conn.execute('SELECT * FROM sessions WHERE id=?', (document.id,)).fetchone()
            base = document.append_base
            if current is None or any((current['fingerprint'] != base['fingerprint'],
                    current['path'] != base['path'], current['path'] != str(path),
                    current['parser_version'] != base['parserVersion'],
                    current['message_count'] != base['messageCount'])):
                raise ValueError('Append base changed; previous archive retained, retry sync')
            actual = conn.execute('SELECT COUNT(*), MAX(order_id) FROM messages WHERE session_id=?', (document.id,)).fetchone()
            if actual[0] != current['message_count'] or (actual[1] is not None and actual[1] + 1 != actual[0]):
                raise ValueError('Append sequence has gaps; full reconciliation required')
            old_users = json.loads(current['metadata']).get('userMessageCount')
            if old_users is None:
                old_users = self._count_users(conn, document.id)
            document.metadata['userMessageCount'] = old_users + sum(is_user_message(m) for m in document.messages)
            self._insert_projection(conn, document, order_start=current['message_count'])
            conn.execute('''UPDATE sessions SET project=?,name=?,message_count=?,last_modified=?,fingerprint=?,
                parser_version=?,metadata=?,warnings=?,available=1 WHERE id=?''',
                (document.project, document.name, current['message_count'] + len(document.messages), modified,
                 fingerprint, parser_version, encode(document.metadata), encode(document.warnings), document.id))
            self._store_projection_state(conn, document, path, parser_version)
            self._store_files(conn, files)

    def projected_message_count(self, document):
        count = document.append_base.get('messageCount', 0) + len(document.messages)
        if document.changed_scopes:
            with self.get_connection() as conn:
                for scope in document.changed_scopes:
                    count -= conn.execute('''SELECT COUNT(*) FROM messages m JOIN projection_messages p
                        ON p.message_pk=m.message_pk WHERE m.session_id=? AND p.scope=?''',
                        (document.id, scope)).fetchone()[0]
        return count

    def get_sync_checkpoint(self, session_id):
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM sync_checkpoints WHERE session_id=?', (session_id,)).fetchone()
            if row is None:
                return None
            missing = conn.execute('''SELECT 1 FROM messages m LEFT JOIN projection_messages p
                ON p.message_pk=m.message_pk WHERE m.session_id=? AND p.message_pk IS NULL LIMIT 1''', (session_id,)).fetchone()
            if missing:
                return None
            missing_raw = conn.execute('''SELECT 1 FROM raw_records r LEFT JOIN projection_raw p
                ON p.session_id=r.session_id AND p.record_index=r.record_index
                WHERE r.session_id=? AND p.record_index IS NULL LIMIT 1''', (session_id,)).fetchone()
            if missing_raw:
                return None
            return {'path': row['path'], 'parserVersion': row['parser_version'], 'state': unpack_json(row['state'])}

    def load_scope_state(self, session_id, scope):
        with self.get_connection() as conn:
            row = conn.execute('SELECT state FROM projection_state WHERE session_id=? AND scope=?', (session_id, scope)).fetchone()
            return unpack_json(row[0]) if row else None

    def load_scope_records(self, session_id, scope):
        with self.get_connection() as conn:
            return [(row[0], unpack_json(row[1])) for row in conn.execute('''
                SELECT r.record_index,r.raw_json FROM projection_raw p JOIN raw_records r
                ON r.session_id=p.session_id AND r.record_index=p.record_index
                WHERE p.session_id=? AND p.scope=? ORDER BY r.record_index''', (session_id, scope))]

    @staticmethod
    def _store_projection_state(conn, document, path, parser_version):
        if document.sync_state:
            conn.execute('''INSERT INTO sync_checkpoints VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                path=excluded.path,parser_version=excluded.parser_version,state=excluded.state''',
                (document.id, str(path), parser_version, pack_json(document.sync_state, streaming=True)))
        for scope, state in document.scope_states.items():
            if state is None:
                conn.execute('DELETE FROM projection_state WHERE session_id=? AND scope=?', (document.id, scope))
            else:
                conn.execute('''INSERT INTO projection_state VALUES (?,?,?) ON CONFLICT(session_id,scope)
                    DO UPDATE SET state=excluded.state''', (document.id, scope, pack_json(state, streaming=True)))
        conn.executemany('INSERT OR IGNORE INTO projection_raw VALUES (?,?,?)',
            ((document.id, index, scope) for index, scopes in document.raw_scopes.items() for scope in scopes))

    def apply_delta(self, document, path, fingerprint, modified, parser_version, files):
        """Reproject only dirty turns/requests. Watermark and every edit are atomic."""
        with self.get_connection() as conn:
            current = conn.execute('SELECT * FROM sessions WHERE id=?', (document.id,)).fetchone()
            base = document.append_base
            if current is None or any((current['fingerprint'] != base['fingerprint'],
                    current['path'] != str(path), current['parser_version'] != base['parserVersion'],
                    current['message_count'] != base['messageCount'])):
                raise ValueError('Incremental base changed; retry sync')
            old_users = json.loads(current['metadata']).get('userMessageCount')
            if old_users is None:
                old_users = self._count_users(conn, document.id)
            removed_users = 0
            obsolete_content = set()
            for scope in document.changed_scopes:
                removed_users += sum(is_user_message(unpack_json(row[0])) for row in conn.execute('''
                    SELECT m.normalized_json FROM messages m JOIN projection_messages p ON p.message_pk=m.message_pk
                    WHERE m.session_id=? AND p.scope=?''', (document.id, scope)))
                obsolete_content.update(row[0] for row in conn.execute('''SELECT DISTINCT t.content_id
                    FROM message_texts t JOIN projection_messages p ON p.message_pk=t.message_pk
                    JOIN messages m ON m.message_pk=p.message_pk WHERE m.session_id=? AND p.scope=?''',
                    (document.id, scope)))
                conn.execute('''DELETE FROM messages WHERE session_id=? AND message_pk IN
                    (SELECT message_pk FROM projection_messages WHERE scope=?)''', (document.id, scope))
            conn.executemany('''DELETE FROM search_content WHERE content_id=? AND NOT EXISTS
                (SELECT 1 FROM message_texts WHERE message_texts.content_id=search_content.content_id)''',
                ((content_id,) for content_id in obsolete_content))
            if document.raw_snapshot_replace:
                # JSON snapshots have one current raw record; mutation logs never take this path.
                conn.execute('DELETE FROM raw_records WHERE session_id=?', (document.id,))
            next_order = conn.execute('SELECT COALESCE(MAX(order_id),-1)+1 FROM messages WHERE session_id=?',
                                      (document.id,)).fetchone()[0]
            self._insert_projection(conn, document, order_start=next_order)
            self._store_projection_state(conn, document, path, parser_version)
            # Only integer ordering metadata is reconciled for untouched messages;
            # their IDs and compressed JSON/raw payloads are not regenerated.
            if document.changed_scopes:
                conn.execute('UPDATE messages SET order_id=-order_id-1 WHERE session_id=?', (document.id,))
                conn.execute('''UPDATE messages SET order_id=ordered.n FROM (SELECT m.message_pk,ROW_NUMBER() OVER(ORDER BY p.major,p.minor,m.message_pk)-1 AS n FROM messages m JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?) AS ordered
                    WHERE messages.message_pk=ordered.message_pk''', (document.id,))
            count = conn.execute('SELECT COUNT(*) FROM messages WHERE session_id=?', (document.id,)).fetchone()[0]
            document.metadata['userMessageCount'] = old_users - removed_users + sum(is_user_message(m) for m in document.messages)
            conn.execute('''UPDATE sessions SET project=?,name=?,message_count=?,last_modified=?,fingerprint=?,
                parser_version=?,metadata=?,warnings=?,available=1 WHERE id=?''',
                (document.project, document.name, count, modified, fingerprint, parser_version,
                 encode(document.metadata), encode(document.warnings), document.id))
            self._store_files(conn, [(p,s,sid,fp,v,mt,count if p==str(path) else n) for p,s,sid,fp,v,mt,n in files])

    def prime_append_checkpoint(self, session_id, path, fingerprint, metadata):
        with self.get_connection() as conn:
            conn.execute('UPDATE sessions SET metadata=? WHERE id=? AND path=? AND fingerprint=?',
                         (encode(metadata), session_id, str(path), fingerprint))

    @staticmethod
    def _insert_projection(conn, document, order_start=0):
        sizes = getattr(document.records, 'record_sizes', {})
        conn.executemany('INSERT INTO raw_records VALUES (?, ?, ?)',
            ((document.id, i, pack_json(r, streaming=sizes.get(i, 0) > 64 * 1024))
             for i, r in document.records))
        first_pk = conn.execute('SELECT COALESCE(MAX(message_pk), 0)+1 FROM messages').fetchone()[0]
        conn.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            ((first_pk + order, m['id'], document.id, order_start + order, m['uuid'], m.get('parentUuid'),
              m['rawRecord'], pack_json(m)) for order, m in enumerate(document.messages)))
        conn.executemany('INSERT INTO projection_messages VALUES (?,?,?,?)',
            ((first_pk + order, document.message_scopes.get(m['id'], f"record:{m['rawRecord']}"),
              *document.message_order.get(m['id'], (m['rawRecord'], int(m['id'].rsplit(':', 1)[-1]))))
             for order, m in enumerate(document.messages)))
        content_cache = {}
        for order, message in enumerate(document.messages):
            search = indexed_message_text(message)
            if not search.strip():
                continue
            Database._store_search_text(conn, first_pk + order, search, content_cache)

    @staticmethod
    def _store_search_text(conn, message_pk, search, cache=None):
        digest = hashlib.sha256(search.encode('utf-8')).digest()
        content_id = cache.get(digest) if cache is not None else None
        if content_id is None:
            row = conn.execute('SELECT content_id FROM search_content WHERE digest=?', (digest,)).fetchone()
            if row:
                content_id = row[0]
            else:
                cursor = conn.execute('INSERT INTO search_content(digest,search_text) VALUES (?,?)', (digest, search))
                content_id = cursor.lastrowid
            if cache is not None:
                cache[digest] = content_id
        conn.execute('INSERT INTO message_texts VALUES (?,?)', (message_pk, content_id))

    @staticmethod
    def _store_files(conn, files):
        conn.executemany('''INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(path) DO UPDATE SET session_id=excluded.session_id, fingerprint=excluded.fingerprint,
            parser_version=excluded.parser_version, modified=excluded.modified,
            message_count=excluded.message_count, available=1''', files)

    def update_files(self, files):
        with self.get_connection() as conn:
            self._store_files(conn, files)
            for f in files:
                conn.execute('UPDATE sessions SET available=1 WHERE id=?', (f[2],))

    @staticmethod
    def _count_users(conn, session_id):
        return sum(is_user_message(unpack_json(row[0])) for row in conn.execute(
            'SELECT normalized_json FROM messages WHERE session_id=?', (session_id,)))

    def backfill_user_counts(self):
        with self.get_connection() as conn:
            rows = conn.execute("SELECT id,metadata FROM sessions WHERE json_extract(metadata,'$.userMessageCount') IS NULL").fetchall()
            for index, row in enumerate(rows, 1):
                metadata = json.loads(row['metadata'])
                metadata['userMessageCount'] = self._count_users(conn, row['id'])
                conn.execute('UPDATE sessions SET metadata=? WHERE id=?', (encode(metadata), row['id']))
                print(f'[index] user-message counts {index}/{len(rows)}', flush=True)
        return len(rows)

    def prune_subagent_sessions(self):
        with self.get_connection() as conn:
            ids = [row[0] for row in conn.execute('''SELECT id FROM sessions
                WHERE viewer_subagent(source,path,metadata)=1''')]
            for sid in ids:
                content = [row[0] for row in conn.execute('''SELECT DISTINCT t.content_id FROM message_texts t
                    JOIN messages m ON m.message_pk=t.message_pk WHERE m.session_id=?''', (sid,))]
                conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
                conn.execute('DELETE FROM source_files WHERE session_id=?', (sid,))
                conn.executemany('''DELETE FROM search_content WHERE content_id=? AND NOT EXISTS
                    (SELECT 1 FROM message_texts WHERE message_texts.content_id=search_content.content_id)''', ((cid,) for cid in content))
            return len(ids)

    def prune_empty_sessions(self):
        """Remove only truly empty viewer sessions, never agent-owned files.

        Keep source_files fingerprints as empty-file cache entries, so unchanged
        empty files are skipped without parsing/recreating sessions next startup.
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''DELETE FROM sessions WHERE message_count=0 AND NOT EXISTS
                (SELECT 1 FROM messages WHERE messages.session_id=sessions.id)''')
            return cursor.rowcount

    def mark_missing(self, paths):
        with self.get_connection() as conn:
            conn.executemany('UPDATE source_files SET available=0 WHERE path=?', ((p,) for p in paths))
            conn.execute('''UPDATE sessions SET available=CASE WHEN EXISTS
                (SELECT 1 FROM source_files f WHERE f.session_id=sessions.id
                 AND f.path=sessions.path AND f.available=1) THEN 1 ELSE 0 END''')

    @staticmethod
    def _session(row):
        return {'id': row['id'], 'source': row['source'], 'externalId': row['external_id'],
                'project': row['project'], 'name': row['name'], 'messageCount': row['message_count'],
                'userMessageCount': json.loads(row['metadata']).get('userMessageCount', 0),
                'lastModified': row['last_modified'], 'timestampUnit': 'ms', 'available': bool(row['available']),
                'metadata': json.loads(row['metadata']), 'warnings': json.loads(row['warnings']),
                'capabilities': {'readOnly': True, 'sourceDelete': False}}

    def get_session(self, session_id):
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            return self._session(row) if row and not is_subagent(row['source'], row['path'], json.loads(row['metadata'])) else None

    def list_sessions(self, project=None, source=None, query='', offset=0, limit=None):
        clauses, params = ['s.message_count>0', "COALESCE(json_extract(s.metadata,'$.userMessageCount'),0)>0",
                           'viewer_subagent(s.source,s.path,s.metadata)=0'], []
        if project:
            clauses.append('s.project=?'); params.append(project)
        if source:
            clauses.append('s.source=?'); params.append(source)
        if query:
            pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            from_text = 'search_content c JOIN message_texts t ON t.content_id=c.content_id JOIN messages m ON m.message_pk=t.message_pk'
            if self.has_trigram and len(query) >= 3 and '\x00' not in query:
                from_text = 'message_search f JOIN search_content c ON c.content_id=f.rowid JOIN message_texts t ON t.content_id=c.content_id JOIN messages m ON m.message_pk=t.message_pk'
                condition = "message_search MATCH ? AND c.search_text LIKE ? ESCAPE '\\'"
                message_params = [trigram_query(query), pattern]
            else:
                condition = "c.search_text LIKE ? ESCAPE '\\'"
                message_params = [pattern]
            message_clause = f's.id IN (SELECT m.session_id FROM {from_text} WHERE {condition})'
            clauses.append(f"({message_clause} OR s.name LIKE ? ESCAPE '\\' OR s.project LIKE ? ESCAPE '\\' OR s.external_id LIKE ? ESCAPE '\\')")
            params.extend(message_params + [pattern, pattern, pattern])
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.get_connection() as conn:
            total = conn.execute('SELECT COUNT(*) FROM sessions s' + where, params).fetchone()[0]
            sql = 'SELECT s.* FROM sessions s' + where + ' ORDER BY s.last_modified DESC, s.id'
            if limit is not None:
                sql += ' LIMIT ? OFFSET ?'; params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
            return [self._session(r) for r in rows], total

    def get_messages(self, session_id, offset=0, limit=2000, branch=None):
        session = self.get_session(session_id)
        if session is None:
            return None
        with self.get_connection() as conn:
            branch_orders = None
            if branch:
                leaf = session['metadata'].get('defaultLeaf') if branch == 'active' else branch
                if not leaf:
                    raise ValueError('This session has no branch metadata')
                nodes = {r['native_id']: r for r in conn.execute(
                    'SELECT native_id, parent_id, order_id FROM messages WHERE session_id=?', (session_id,))}
                if leaf not in nodes:
                    raise ValueError('Unknown branch leaf')
                seen, branch_orders = set(), []
                while leaf and leaf not in seen and leaf in nodes:
                    seen.add(leaf); node = nodes[leaf]
                    branch_orders.append(node['order_id']); leaf = node['parent_id']
            if branch_orders is None:
                rows = conn.execute('''SELECT normalized_json, order_id FROM messages WHERE session_id=?
                    ORDER BY order_id LIMIT ? OFFSET ?''', (session_id, limit, offset)).fetchall()
                total = session['messageCount']
            else:
                branch_orders.sort(); total = len(branch_orders)
                selected = branch_orders[offset:offset + limit]
                rows = [conn.execute('SELECT normalized_json, order_id FROM messages WHERE session_id=? AND order_id=?',
                                     (session_id, order)).fetchone() for order in selected]
            items = []
            for row in rows:
                message = unpack_json(row['normalized_json']); message['orderId'] = row['order_id']
                items.append(message)
            return {'items': items, 'total': total, 'session': session,
                    'nextCursor': offset + len(items) if offset + len(items) < total else None}

    def get_raw_message(self, message_id):
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            if row is None:
                return None
            normalized = unpack_json(row['normalized_json'])
            indices = [row['raw_record'], *normalized.get('relatedRawRecords', [])]
            records = []
            for index in dict.fromkeys(indices):
                raw = conn.execute('SELECT raw_json FROM raw_records WHERE session_id=? AND record_index=?',
                                   (row['session_id'], index)).fetchone()
                if raw:
                    records.append({'recordIndex': index, 'record': unpack_json(raw[0])})
            path = conn.execute('SELECT path FROM sessions WHERE id=?', (row['session_id'],)).fetchone()[0]
            return {'sourcePath': path, 'sourceLocator': normalized.get('sourceLocator'), 'records': records}

    def get_records(self, session_id, offset=0, limit=50):
        if self.get_session(session_id) is None:
            return None
        with self.get_connection() as conn:
            total = conn.execute('SELECT COUNT(*) FROM raw_records WHERE session_id=?', (session_id,)).fetchone()[0]
            rows = conn.execute('''SELECT record_index, raw_json FROM raw_records WHERE session_id=?
                ORDER BY record_index LIMIT ? OFFSET ?''', (session_id, limit, offset)).fetchall()
            return {'items': [{'recordIndex': r[0], 'record': unpack_json(r[1])} for r in rows], 'total': total,
                    'nextCursor': offset + len(rows) if offset + len(rows) < total else None}
