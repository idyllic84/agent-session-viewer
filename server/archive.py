"""Cloud archive: password owner, upload API keys and the ingest change stream.

The cloud keeps no source-file paths, cursors or raw records. Each upload key
is a *client slot* bound to exactly one local dataset. A batch is applied only
if its ``expectedSeq`` equals the slot's committed sequence (compare-and-set);
messages, search index, counts, sequence and the idempotency receipt commit in
ONE transaction.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from threading import local
import time

from .codec import pack_json, unpack_json
from .db import Database, encode
from .contracts.analysis import POLICY
from .contracts.protocol import Problem, digest, require, check_ingest, INGEST_VERSION
from .contracts.session_policy import is_user_message
from .contracts.models import indexed_message_text

SCRYPT = {'n': 2 ** 15, 'r': 8, 'p': 1}
KEY_KINDS = ('upload', 'agent')   # upload: one computer pushes data; agent: read-only analysis API
MIN_PASSWORD = 12


def hash_password(password):
    require(isinstance(password, str) and MIN_PASSWORD <= len(password) <= 1024,
            f'Password must contain at least {MIN_PASSWORD} characters', 'WEAK_PASSWORD', 400)
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode('utf-8'), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **SCRYPT)
    return 'scrypt$%d$%d$%d$%s$%s' % (SCRYPT['n'], SCRYPT['r'], SCRYPT['p'],
                                     base64.b64encode(salt).decode(), base64.b64encode(key).decode())


def verify_password(password, stored):
    try:
        kind, n, r, p, salt, key = stored.split('$')
        if kind != 'scrypt' or not isinstance(password, str) or len(password) > 1024:
            return False
        actual = hashlib.scrypt(password.encode('utf-8'), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, base64.b64decode(key))
    except (ValueError, AttributeError):
        return False


class AtomicDatabase(Database):
    def __init__(self, path):
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


class CloudArchive:
    def __init__(self, path, *, max_request=16 * 1024 * 1024):
        path = Path(path).resolve()
        if path.exists() and path.stat().st_size:
            probe = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                probe.close()
            if 'dist_settings' in tables:
                raise ValueError('This database belongs to the retired task-queue cloud (protocol 5). It is left untouched; '
                                 'start the cloud with a NEW database file and re-upload from the local servers.')
            if tables and 'cloud_settings' not in tables:
                raise ValueError('Not a cloud ingest database; existing databases will not be modified')
        self.db = AtomicDatabase(path); self.max_request = max_request; self.db.init_schema()
        with self.db.get_connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS cloud_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cloud_clients(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    key_id TEXT UNIQUE NOT NULL, key_hash TEXT NOT NULL, active INTEGER NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
                    dataset_id TEXT, ack_seq INTEGER NOT NULL DEFAULT 0, last_upload REAL NOT NULL DEFAULT 0,
                    uploaded_changes INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS cloud_clients_dataset ON cloud_clients(dataset_id) WHERE dataset_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS cloud_sessions(id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL, local_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(dataset_id, local_id));
                CREATE TABLE IF NOT EXISTS cloud_receipts(client_id TEXT NOT NULL, operation_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(client_id, operation_key));
            ''')
            columns = {r[1] for r in conn.execute('PRAGMA table_info(cloud_clients)')}
            if 'kind' not in columns:   # additive upgrade: existing keys are upload keys
                conn.execute("ALTER TABLE cloud_clients ADD COLUMN kind TEXT NOT NULL DEFAULT 'upload'")
            conn.execute("UPDATE cloud_clients SET kind='agent' WHERE kind='query'")   # earlier name of the same key type
            conn.execute("INSERT OR IGNORE INTO cloud_settings VALUES('content_policy',?)", (POLICY,))
            conn.execute("INSERT OR IGNORE INTO cloud_settings VALUES('instance',?)", (secrets.token_hex(16),))
            conn.execute("INSERT OR IGNORE INTO cloud_settings VALUES('password_version','0')")
            self.instance = conn.execute("SELECT value FROM cloud_settings WHERE key='instance'").fetchone()[0]

    # ----- owner password -----
    def setting(self, key):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT value FROM cloud_settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def has_password(self):
        return self.setting('password_hash') is not None

    def password_version(self):
        return int(self.setting('password_version') or 0)

    def set_password(self, password, *, current=None, require_current=True):
        hashed = hash_password(password)
        with self.db.transaction() as conn:
            old = conn.execute("SELECT value FROM cloud_settings WHERE key='password_hash'").fetchone()
            if old is not None and require_current:
                require(verify_password(current or '', old[0]), 'Current password is incorrect', 'UNAUTHORIZED', 403)
            conn.execute("INSERT INTO cloud_settings VALUES('password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (hashed,))
            # Every existing browser session is invalidated by the version bump.
            conn.execute("UPDATE cloud_settings SET value=CAST(value AS INTEGER)+1 WHERE key='password_version'")
        return self.password_version()

    def check_password(self, password):
        stored = self.setting('password_hash')
        return stored is not None and verify_password(password, stored)

    # ----- API keys: upload (client slots) and agent (read-only) -----
    def create_key(self, name, *, expires_days=None, kind='upload'):
        require(kind in KEY_KINDS, 'Key type must be upload or agent', status=400)
        require(isinstance(name, str) and 0 < len(name.strip()) <= 160, 'Key name is required (max 160 characters)', status=400)
        require(expires_days is None or isinstance(expires_days, int) and not isinstance(expires_days, bool) and 1 <= expires_days <= 3650,
                'Expiry must be 1..3650 days', status=400)
        client = secrets.token_hex(12); key_id = secrets.token_hex(8); secret = secrets.token_urlsafe(32)
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO cloud_clients(id,name,key_id,key_hash,active,created,expires,kind) VALUES(?,?,?,?,1,?,?,?)',
                         (client, name.strip(), key_id, hashlib.sha256(secret.encode()).hexdigest(), time.time(),
                          time.time() + expires_days * 86400 if expires_days else 0, kind))
        return {'clientId': client, 'name': name.strip(), 'kind': kind, 'key': f'asv_{key_id}_{secret}', 'keyId': f'asv_{key_id}'}

    def rotate_key(self, client):
        key_id = secrets.token_hex(8); secret = secrets.token_urlsafe(32)
        with self.db.transaction() as conn:
            row = conn.execute('SELECT name,kind FROM cloud_clients WHERE id=?', (client,)).fetchone()
            require(row is not None, 'Unknown key', 'NOT_FOUND', 404)
            conn.execute('UPDATE cloud_clients SET key_id=?,key_hash=?,active=1 WHERE id=?',
                         (key_id, hashlib.sha256(secret.encode()).hexdigest(), client))
        return {'clientId': client, 'name': row[0], 'kind': row[1], 'key': f'asv_{key_id}_{secret}', 'keyId': f'asv_{key_id}'}

    def revoke_key(self, client):
        with self.db.transaction() as conn:
            require(conn.execute('UPDATE cloud_clients SET active=0 WHERE id=?', (client,)).rowcount == 1, 'Unknown key', 'NOT_FOUND', 404)

    def unbind_key(self, client):
        """Explicit owner action: let this slot accept a NEW local dataset. Data is kept."""
        with self.db.transaction() as conn:
            require(conn.execute("UPDATE cloud_clients SET dataset_id=NULL,ack_seq=0 WHERE id=? AND kind='upload'", (client,)).rowcount == 1,
                    'Unknown upload key', 'NOT_FOUND', 404)

    def authenticate(self, key, kind='upload'):
        parts = key.split('_', 2) if isinstance(key, str) else []
        if len(parts) != 3 or parts[0] != 'asv' or len(parts[1]) != 16:
            raise Problem('UNAUTHORIZED', 'Invalid API key', 401)
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT * FROM cloud_clients WHERE key_id=?', (parts[1],)).fetchone()
            expected = row['key_hash'] if row else '0' * 64
            valid = hmac.compare_digest(hashlib.sha256(parts[2].encode()).hexdigest(), expected)
            if row is None or not valid or not row['active'] or (row['expires'] and row['expires'] < time.time()):
                raise Problem('UNAUTHORIZED', 'Invalid, revoked or expired API key', 401)
            if row['kind'] != kind:
                raise Problem('WRONG_KEY_TYPE', f"This is a {row['kind']} key; use a {kind} key for this API", 403)
            if time.time() - row['last_used'] > 60:
                conn.execute('UPDATE cloud_clients SET last_used=? WHERE id=?', (time.time(), row['id']))
            return dict(row)

    def keys(self):
        with self.db.get_connection() as conn:
            rows = conn.execute('''SELECT c.id,c.name,c.kind,c.key_id,c.active,c.created,c.expires,c.last_used,c.dataset_id,
                c.ack_seq,c.last_upload,c.uploaded_changes,(SELECT COUNT(*) FROM cloud_sessions s WHERE s.client_id=c.id) AS sessions
                FROM cloud_clients c ORDER BY c.created''').fetchall()
        return [{'clientId': r['id'], 'name': r['name'], 'kind': r['kind'], 'keyId': 'asv_' + r['key_id'], 'active': bool(r['active']),
                 'created': r['created'], 'expires': r['expires'] or None, 'lastUsed': r['last_used'] or None,
                 'bound': r['dataset_id'] is not None, 'ackSeq': r['ack_seq'], 'lastUpload': r['last_upload'] or None,
                 'uploadedChanges': r['uploaded_changes'], 'sessions': r['sessions']} for r in rows]

    def machines(self):
        """Upload clients as seen by readers: names and activity only, no key data."""
        with self.db.get_connection() as conn:
            rows = conn.execute('''SELECT c.id,c.name,c.active,c.last_upload,
                (SELECT COUNT(*) FROM cloud_sessions s JOIN sessions x ON x.id=s.id WHERE s.client_id=c.id AND x.message_count>0) AS sessions
                FROM cloud_clients c WHERE c.kind='upload' ORDER BY c.name,c.created''').fetchall()
        return [{'id': r['id'], 'name': r['name'], 'active': bool(r['active']), 'sessions': r['sessions'],
                 'lastUpload': r['last_upload'] or None} for r in rows]

    # ----- agent API: list sessions, session metadata, paged messages -----
    AGENT_SESSION_SQL = '''SELECT s.*,c.id AS machine_id,c.name AS machine_name,cs.revision FROM sessions s
        JOIN cloud_sessions cs ON cs.id=s.id JOIN cloud_clients c ON c.id=cs.client_id
        WHERE s.message_count>0 AND viewer_subagent(s.source,s.path,s.metadata)=0'''

    @staticmethod
    def agent_summary(row, *, detail=False):
        metadata = json.loads(row['metadata'])
        value = {'id': row['id'], 'machine': {'id': row['machine_id'], 'name': row['machine_name']},
                 'source': row['source'], 'externalId': row['external_id'], 'project': row['project'], 'name': row['name'],
                 'messageCount': row['message_count'], 'userMessageCount': metadata.get('userMessageCount', 0),
                 'lastModified': row['last_modified'], 'available': bool(row['available']), 'revision': row['revision']}
        if detail:
            value['metadata'] = {k: v for k, v in metadata.items() if k != 'userMessageCount'}
        return value

    def agent_sessions(self, after=None, limit=100):
        """Every non-empty session, ordered by ID; keyset cursor is stable under uploads."""
        with self.db.get_connection() as conn:
            total = conn.execute(f'SELECT COUNT(*) FROM ({self.AGENT_SESSION_SQL})').fetchone()[0]
            sql = self.AGENT_SESSION_SQL + (' AND s.id>?' if after else '') + ' ORDER BY s.id LIMIT ?'
            rows = conn.execute(sql, ((after,) if after else ()) + (limit + 1,)).fetchall()
        items = [self.agent_summary(r) for r in rows[:limit]]
        return items, total, items[-1]['id'] if len(rows) > limit else None

    def agent_session(self, sid):
        with self.db.get_connection() as conn:
            row = conn.execute(self.AGENT_SESSION_SQL + ' AND s.id=?', (sid,)).fetchone()
        return self.agent_summary(row, detail=True) if row else None

    def agent_messages(self, sid, offset=0, limit=500, revision=None):
        with self.db.transaction(write=False) as conn:
            row = conn.execute(self.AGENT_SESSION_SQL + ' AND s.id=?', (sid,)).fetchone()
            require(row is not None, 'Unknown session', 'NOT_FOUND', 404)
            require(revision is None or revision == row['revision'], 'Session changed; restart paging from cursor 0', 'SESSION_CHANGED', 409)
            items = []
            for r in conn.execute('SELECT normalized_json,order_id FROM messages WHERE session_id=? ORDER BY order_id LIMIT ? OFFSET ?',
                                  (sid, limit, offset)):
                m = unpack_json(r[0])
                item = {'id': m['id'], 'order': r[1], 'type': m['type'], 'role': m['role'], 'timestamp': m['timestamp'],
                        'timestampSource': m['timestampSource'], 'content': m['message']['content']}
                for key in ('toolCallId', 'status'):
                    if key in m:
                        item[key] = m[key]
                items.append(item)
            total = row['message_count']
        nxt = offset + len(items)
        return {'sessionId': sid, 'revision': row['revision'], 'total': total, 'items': items,
                'nextCursor': str(nxt) if nxt < total else None}

    # ----- ingest -----
    def status(self, client, dataset=None):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT * FROM cloud_clients WHERE id=?', (client['id'],)).fetchone()
        bound = row['dataset_id']
        return {'protocolVersion': INGEST_VERSION, 'instance': self.instance, 'contentPolicy': POLICY,
                'maxRequestBytes': self.max_request, 'client': {'id': row['id'], 'name': row['name']},
                'boundDataset': bound, 'ackSeq': row['ack_seq'] if bound is not None and bound == dataset else None}

    def bind(self, client, dataset):
        require(isinstance(dataset, str) and len(dataset) == 32 and all(c in '0123456789abcdef' for c in dataset), 'Invalid dataset ID', status=400)
        with self.db.transaction() as conn:
            row = conn.execute('SELECT dataset_id FROM cloud_clients WHERE id=?', (client['id'],)).fetchone()
            if row['dataset_id'] is None:
                taken = conn.execute('SELECT 1 FROM cloud_clients WHERE dataset_id=?', (dataset,)).fetchone()
                require(taken is None, 'This local dataset already uploads through another key', 'DATASET_MISMATCH', 409)
                conn.execute('UPDATE cloud_clients SET dataset_id=?,ack_seq=0 WHERE id=?', (dataset, client['id']))
            else:
                require(row['dataset_id'] == dataset, 'This key is bound to another local dataset', 'DATASET_MISMATCH', 409)
        return self.status(client, dataset)

    def commit(self, client, key, value):
        require(isinstance(key, str) and 0 < len(key) <= 160, 'Idempotency-Key header required', status=400)
        payload_hash = digest(value)
        with self.db.transaction() as conn:
            previous = conn.execute('SELECT * FROM cloud_receipts WHERE client_id=? AND operation_key=?', (client['id'], key)).fetchone()
            if previous:
                require(previous['payload_hash'] == payload_hash, 'Same idempotency key with a different batch', 'IDEMPOTENCY_MISMATCH')
                return json.loads(previous['receipt'])
            row = conn.execute('SELECT * FROM cloud_clients WHERE id=?', (client['id'],)).fetchone()
            require(row['dataset_id'] is not None, 'Bind the dataset before uploading', 'NOT_BOUND', 409)
            changes = check_ingest(value, row['dataset_id'])
            if value['expectedSeq'] != row['ack_seq']:
                problem = Problem('SEQ_CONFLICT', f"Cloud has committed through {row['ack_seq']}", 409)
                raise problem
            dataset = row['dataset_id']; touched = {}
            for change in changes:
                cloud_sid = self.cloud_session(conn, client['id'], dataset, change['sessionId'], create=change['op'] == 'session')
                touched.setdefault(cloud_sid, {'inserted': [], 'deleted': False})
                if change['op'] == 'session':
                    self.upsert_session(conn, cloud_sid, change['data'])
                elif change['op'] == 'message':
                    self.upsert_message(conn, cloud_sid, dataset, change['data'], touched[cloud_sid])
                else:
                    self.delete_message(conn, cloud_sid, dataset + '/' + change['data']['id'], touched[cloud_sid])
            for cloud_sid, info in touched.items():
                self.reorder(conn, cloud_sid, info)
                conn.execute('UPDATE cloud_sessions SET revision=revision+1 WHERE id=?', (cloud_sid,))
            count = len(changes)
            conn.execute('UPDATE cloud_clients SET ack_seq=?,last_upload=?,uploaded_changes=uploaded_changes+? WHERE id=?',
                         (value['throughSeq'], time.time(), count, client['id']))
            receipt = {'operationId': key, 'datasetId': dataset, 'ackSeq': value['throughSeq'], 'instance': self.instance,
                       'changes': count, 'sessions': len(touched)}
            conn.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?,?)', (client['id'], key, payload_hash, encode(receipt), time.time()))
            return receipt

    @staticmethod
    def cloud_session(conn, client, dataset, sid, *, create):
        cloud_sid = dataset + '/' + sid
        row = conn.execute('SELECT client_id FROM cloud_sessions WHERE id=?', (cloud_sid,)).fetchone()
        if row is None:
            require(create, 'Message for a session that was never uploaded', 'UNKNOWN_SESSION')
            conn.execute('INSERT INTO cloud_sessions VALUES(?,?,?,?,0)', (cloud_sid, client, dataset, sid))
        else:
            require(row[0] == client, 'Session belongs to another upload key', 'REPLICA_CONFLICT')
        return cloud_sid

    @staticmethod
    def upsert_session(conn, cloud_sid, data):
        old = conn.execute('SELECT metadata FROM sessions WHERE id=?', (cloud_sid,)).fetchone()
        metadata = dict(data['metadata'])
        metadata['userMessageCount'] = json.loads(old[0]).get('userMessageCount', 0) if old else 0
        conn.execute('''INSERT INTO sessions VALUES (?,?,?,?,?,0,?,'','',0,?,'[]',?)
            ON CONFLICT(id) DO UPDATE SET project=excluded.project,name=excluded.name,last_modified=excluded.last_modified,
            metadata=excluded.metadata,available=excluded.available''',
            (cloud_sid, data['source'], data['externalId'], data['project'], data['name'], data['lastModified'],
             encode(metadata), int(data['available'])))

    @staticmethod
    def adjust_counts(conn, cloud_sid, messages=0, users=0):
        if not messages and not users:
            return
        meta = json.loads(conn.execute('SELECT metadata FROM sessions WHERE id=?', (cloud_sid,)).fetchone()[0])
        meta['userMessageCount'] = meta.get('userMessageCount', 0) + users
        conn.execute('UPDATE sessions SET message_count=message_count+?,metadata=? WHERE id=?', (messages, encode(meta), cloud_sid))

    @staticmethod
    def drop_text(conn, message_pk):
        content = [r[0] for r in conn.execute('SELECT content_id FROM message_texts WHERE message_pk=?', (message_pk,))]
        conn.execute('DELETE FROM message_texts WHERE message_pk=?', (message_pk,))
        for cid in content:
            conn.execute('DELETE FROM search_content WHERE content_id=? AND NOT EXISTS (SELECT 1 FROM message_texts WHERE content_id=?)', (cid, cid))

    def upsert_message(self, conn, cloud_sid, dataset, data, info):
        message = dict(data['message']); local_id = message['id']
        message['id'] = dataset + '/' + local_id; message['sessionId'] = cloud_sid
        major, minor = data['order']
        old = conn.execute('''SELECT m.message_pk,m.normalized_json,p.major,p.minor FROM messages m
            JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.id=?''', (message['id'],)).fetchone()
        require(old is None or conn.execute('SELECT session_id FROM messages WHERE message_pk=?', (old['message_pk'],)).fetchone()[0] == cloud_sid,
                'Message belongs to another session', 'REPLICA_CONFLICT')
        search = indexed_message_text(message)
        if old:
            prior = unpack_json(old['normalized_json'])
            self.adjust_counts(conn, cloud_sid, 0, int(is_user_message(message)) - int(is_user_message(prior)))
            conn.execute('UPDATE messages SET normalized_json=?,native_id=?,raw_record=? WHERE message_pk=?',
                         (pack_json(message), message.get('uuid'), message['rawRecord'], old['message_pk']))
            conn.execute('UPDATE projection_messages SET major=?,minor=? WHERE message_pk=?', (major, minor, old['message_pk']))
            self.drop_text(conn, old['message_pk']); pk = old['message_pk']
            if (old['major'], old['minor']) != (major, minor):
                info['deleted'] = True  # order key changed: renumber this session
        else:
            pk = conn.execute('SELECT COALESCE(MAX(message_pk),0)+1 FROM messages').fetchone()[0]
            conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)',
                         (pk, message['id'], cloud_sid, -pk, message.get('uuid'), None, message['rawRecord'], pack_json(message)))
            conn.execute('INSERT INTO projection_messages VALUES(?,?,?,?)', (pk, '', major, minor))
            self.adjust_counts(conn, cloud_sid, 1, int(is_user_message(message)))
            info['inserted'].append((major, minor, message['id'], pk))
        if search.strip():
            Database._store_search_text(conn, pk, search)

    def delete_message(self, conn, cloud_sid, mid, info):
        old = conn.execute('SELECT message_pk,normalized_json FROM messages WHERE id=? AND session_id=?', (mid, cloud_sid)).fetchone()
        if old is None:
            return
        self.drop_text(conn, old['message_pk'])
        conn.execute('DELETE FROM messages WHERE message_pk=?', (old['message_pk'],))
        self.adjust_counts(conn, cloud_sid, -1, -int(is_user_message(unpack_json(old['normalized_json']))))
        info['deleted'] = True

    @staticmethod
    def reorder(conn, cloud_sid, info):
        """Order by (source position, id). Pure appends avoid renumbering the session."""
        if not info['inserted'] and not info['deleted']:
            return
        if not info['deleted']:
            last = conn.execute('''SELECT p.major,p.minor,m.id,m.order_id FROM messages m JOIN projection_messages p
                ON p.message_pk=m.message_pk WHERE m.session_id=? AND m.order_id>=0 ORDER BY m.order_id DESC LIMIT 1''', (cloud_sid,)).fetchone()
            ordered = sorted(info['inserted'])
            if last is None or (last[0], last[1], last[2]) < ordered[0][:3]:
                start = -1 if last is None else last[3]
                for offset, (*_, pk) in enumerate(ordered, 1):
                    conn.execute('UPDATE messages SET order_id=? WHERE message_pk=?', (start + offset, pk))
                return
        conn.execute('UPDATE messages SET order_id=-message_pk-1000000000 WHERE session_id=?', (cloud_sid,))
        conn.execute('''UPDATE messages SET order_id=ordered.n FROM (SELECT m.message_pk,ROW_NUMBER() OVER (ORDER BY p.major,p.minor,m.id)-1 AS n FROM messages m JOIN projection_messages p ON p.message_pk=m.message_pk WHERE m.session_id=?) AS ordered
            WHERE messages.message_pk=ordered.message_pk''', (cloud_sid,))

    def receipt(self, client, key):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT receipt FROM cloud_receipts WHERE client_id=? AND operation_key=?', (client['id'], key)).fetchone()
            return json.loads(row[0]) if row else None

    def session_revision(self, sid):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT revision FROM cloud_sessions WHERE id=?', (sid,)).fetchone()
            return row[0] if row else 0

    def session_clients(self, ids):
        if not ids:
            return {}
        with self.db.get_connection() as conn:
            marks = ','.join('?' for _ in ids)
            return {r[0]: {'id': r[1], 'name': r[2]} for r in conn.execute(f'''SELECT s.id,c.id,c.name FROM cloud_sessions s
                JOIN cloud_clients c ON c.id=s.client_id WHERE s.id IN ({marks})''', list(ids))}
