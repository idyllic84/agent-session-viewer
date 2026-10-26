"""User-triggered upload: local change log -> cloud, in ordinary batch POSTs.

* Runs only when the console asks; no timer, watcher, polling or heartbeat.
* The cloud's committed sequence is re-read at the start of every run.
* High-water mark H is fixed per run; changes imported meanwhile wait.
* Each batch = contiguous commit groups, sized by actual JSON bytes (~1 MiB).
  A single group larger than the batch target is split at change boundaries
  (e.g. the first import of a huge session); changes are never truncated.
* The batch range/hash/idempotency key is persisted BEFORE it is sent, and the
  body is rebuilt byte-for-byte from the immutable change log on retry.
"""
from copy import deepcopy
import hashlib
from threading import Event, Lock, Thread
import time

from server.contracts.protocol import Problem, canonical, INGEST_VERSION


class Stop(Exception):
    pass


class Uploader:
    def __init__(self, archive, credentials, *, client_factory=None, batch_bytes=1024 * 1024, log=print):
        if client_factory is None:
            from .cloud import CloudClient as client_factory
        if not isinstance(batch_bytes, int) or batch_bytes < 1024:
            raise ValueError('Upload batch target must be at least 1 KiB')
        self.archive = archive; self.credentials = credentials; self.client_factory = client_factory
        self.batch_bytes = batch_bytes; self.log = log
        self._lock = Lock(); self._thread = None; self._pause = Event(); self._cancel = Event()
        self._status = {'running': False, 'state': 'idle'}

    # ----- console API -----
    def status(self):
        with self._lock:
            return deepcopy(self._status)

    def start(self):
        with self._lock:
            if self._status.get('running'):
                return deepcopy(self._status)
            self._pause.clear(); self._cancel.clear()
            resume = self._status.get('state') == 'paused' and self._status.get('highWater')
            self._status = {'running': True, 'state': 'starting', 'startedAt': time.time(), 'batches': 0, 'bytes': 0,
                            'changes': 0, 'highWater': resume or None, 'error': None}
            self._thread = Thread(target=self._run, name='cloud-upload', daemon=True); self._thread.start()
            return deepcopy(self._status)

    def pause(self):
        self._pause.set(); return self.status()

    def cancel(self):
        self._cancel.set(); return self.status()

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def run_now(self):
        """Synchronous run (tests and CLI)."""
        self.start(); return self.wait()

    # ----- worker -----
    def _set(self, **values):
        with self._lock:
            self._status.update(values)

    def _checkpoint(self):
        if self._cancel.is_set():
            raise Stop('cancelled')
        if self._pause.is_set():
            raise Stop('paused')

    def _run(self):
        client = None; final = {}
        try:
            connection = self.archive.connection()
            if connection is None:
                raise Problem('NOT_CONFIGURED', 'Set the cloud address and API key first', 400)
            key = self.credentials.load(connection['key_ref'])
            client = self.client_factory(connection['url'], key)
            self._set(state='checking cloud progress')
            ack = self._align(client, connection)
            high = self.status().get('highWater') or self.archive.last_seq()
            if high < ack:
                high = self.archive.last_seq()
            self._set(highWater=high, ackSeq=ack, fromSeq=ack, state='uploading' if ack < high else 'up to date')
            while ack < high:
                self._checkpoint()
                connection = self.archive.connection()
                manifest = connection['pending']
                if manifest and manifest['fromSeq'] == ack + 1:
                    body = self._body(manifest['fromSeq'] - 1, manifest['toSeq'])
                    if hashlib.sha256(body).hexdigest() != manifest['hash']:
                        raise Problem('LOCAL_LOG_CHANGED', 'Immutable change log no longer matches the pending batch', 500)
                else:
                    body, through = self._build(ack, high)
                    manifest = {'fromSeq': ack + 1, 'toSeq': through, 'hash': hashlib.sha256(body).hexdigest(),
                                'key': 'b' + hashlib.sha256(b'asv-batch\0' + body).hexdigest()[:63]}
                    self.archive.update_connection(pending=manifest)  # durable BEFORE sending
                if len(body) > self.max_request:
                    raise Problem('TOO_LARGE', 'One indivisible change group exceeds the cloud request limit; it is not truncated', 413)
                try:
                    receipt = client.send(body, manifest['key'])
                except Problem as exc:
                    if exc.code == 'SEQ_CONFLICT':
                        ack = self._align(client, self.archive.connection()); continue
                    raise
                if receipt.get('ackSeq') != manifest['toSeq'] or receipt.get('datasetId') != self.archive.dataset:
                    raise Problem('INVALID_RECEIPT', 'Cloud receipt does not confirm this batch', 502)
                self.archive.acknowledge(manifest, receipt, self.instance)
                ack = manifest['toSeq']
                with self._lock:
                    self._status['batches'] += 1; self._status['bytes'] += len(body)
                    self._status['changes'] += manifest['toSeq'] - manifest['fromSeq'] + 1; self._status['ackSeq'] = ack
                self.log(f"[upload] batch {manifest['fromSeq']}..{manifest['toSeq']} committed ({len(body)} bytes)")
            final = {'state': 'complete', 'ackSeq': ack, 'highWater': high}
        except Stop as stop:
            final = {'state': str(stop), 'highWater': self.status().get('highWater') if str(stop) == 'paused' else None}
        except Problem as exc:
            final = {'state': 'failed', 'error': {'code': exc.code, 'message': str(exc), 'status': exc.status}}
        except LookupError as exc:
            final = {'state': 'failed', 'error': {'code': 'NO_CREDENTIAL', 'message': str(exc), 'status': 400}}
        except Exception as exc:  # pragma: no cover - surfaced in console, never secrets
            final = {'state': 'failed', 'error': {'code': type(exc).__name__, 'message': 'Upload failed; see local server log', 'status': 500}}
            self.log(f'[upload] failed: {type(exc).__name__}: {exc}')
        finally:
            if client is not None:
                client.close()
            with self._lock:
                self._status.update(final, running=False, finishedAt=time.time())
                result = {k: self._status.get(k) for k in ('state', 'error', 'batches', 'bytes', 'changes', 'ackSeq', 'startedAt', 'finishedAt')}
            try:
                if self.archive.connection() is not None:
                    self.archive.update_connection(last_result=result)
            except Exception:
                pass
            self.log(f"[upload] {final.get('state')}" + (f": {final['error']['code']}" if final.get('error') else ''))

    def _align(self, client, connection):
        """Re-read cloud truth; reconcile the persisted pending batch with it."""
        status = client.status(self.archive.dataset)
        self.instance = status['instance']; self.max_request = status['maxRequestBytes']
        if connection.get('cloud_instance') and status['instance'] != connection['cloud_instance']:
            raise Problem('CLOUD_INSTANCE_CHANGED', 'This address now serves a different cloud archive; test and save the connection again', 409)
        if status.get('contentPolicy') != 'analysis-v1':
            raise Problem('CONTENT_POLICY', 'Cloud content policy is not analysis-v1', 409)
        bound = status.get('boundDataset')
        if bound is None:
            status = client.bind(self.archive.dataset); bound = status.get('boundDataset')
        if bound != self.archive.dataset:
            raise Problem('DATASET_MISMATCH', 'This API key is bound to another local dataset; create a separate key for this computer', 409)
        ack = status['ackSeq']; local_last = self.archive.last_seq()
        if not isinstance(ack, int) or ack < 0:
            raise Problem('INVALID_RESPONSE', 'Cloud did not report committed progress', 502)
        if ack > local_last:
            raise Problem('LOCAL_BEHIND_CLOUD', 'Cloud has more changes than this local store (restored backup?). Upload blocked; nothing was overwritten', 409)
        pending = connection.get('pending')
        if pending and (ack >= pending['toSeq'] or ack != pending['fromSeq'] - 1):
            self.archive.update_connection(pending=None)  # committed already, or superseded under CAS
        self.archive.update_connection(acked_seq=ack)
        return ack

    PAGE = 512

    def _pages(self, first, last):
        seq = first
        while seq <= last:
            end = min(last, seq + self.PAGE - 1)
            yield from self.archive.changes(seq, end)
            seq = end + 1

    @staticmethod
    def _change(row):
        return {'seq': row['seq'], 'op': row['op'], 'sessionId': row['sessionId'], 'data': row['data']}

    def _through(self, ack, high):
        """Choose the batch end by actual JSON bytes. Whole commit groups when they
        fit; a first group larger than the target is cut at a change boundary.
        Individual changes are never truncated (one may exceed the target alone)."""
        limit = self.max_request - 64 * 1024; target = min(self.batch_bytes, limit)
        size = 200; seq = ack + 1; through = None
        while seq <= high:
            end = self.archive.group_end(seq); group = 80
            for row in self._pages(seq, end):
                weight = len(canonical(self._change(row)).encode('utf-8')) + 1
                if through is not None and size + group + weight > target:
                    return through                 # next group does not fit: send what we have
                if through is None and group > 80 and group + weight > target:
                    return row['seq'] - 1          # oversized first group: split at the target
                group += weight
            size += group; through = end; seq = end + 1
            if size >= target:
                break
        return through

    def _body(self, ack, through):
        """Deterministic: consecutive changes of one commit form one group entry."""
        groups = []
        for row in self._pages(ack + 1, through):
            if not groups or groups[-1]['id'] != row['group']:
                groups.append({'id': row['group'], 'firstSeq': row['seq'], 'lastSeq': row['seq'], 'changes': []})
            groups[-1]['changes'].append(self._change(row)); groups[-1]['lastSeq'] = row['seq']
        if not groups or groups[-1]['lastSeq'] != through:
            raise Problem('LOCAL_LOG_CHANGED', 'Pending batch range is not available in the change log', 500)
        return canonical({'protocolVersion': INGEST_VERSION, 'datasetId': self.archive.dataset, 'expectedSeq': ack,
                          'throughSeq': through, 'groups': groups}).encode('utf-8')

    def _build(self, ack, high):
        through = self._through(ack, high)
        return self._body(ack, through), through
