"""Local Web Server application service: import, browse, cloud connection, upload."""
from copy import deepcopy
from pathlib import Path
from threading import Lock, Thread
import time

from server.contracts.protocol import Problem, SOURCES
from .archive import LocalArchive
from .cloud import CloudClient, normalize_origin
from .credentials import CredentialStore, key_hint, validate_key
from .importer import Importer, LocalRepository
from .sources import default_adapters
from .uploader import Uploader


class ImportBusy(RuntimeError):
    pass


class LocalService:
    def __init__(self, archive=None, adapters=None, *, state_dir=None, quiet_seconds=3600, credential_backend='auto',
                 client_factory=CloudClient, upload_batch_bytes=1024 * 1024, import_batch_bytes=1024 * 1024, log=print):
        self.archive = archive or LocalArchive()
        self.adapters = default_adapters() if adapters is None else adapters
        self.state_dir = Path(state_dir) if state_dir else self.archive.db.db_path.parent
        self.quiet_seconds = quiet_seconds; self.import_batch_bytes = import_batch_bytes; self.log = log
        self.credentials = CredentialStore(self.state_dir / 'credentials', credential_backend)
        self.client_factory = client_factory
        self.uploader = Uploader(self.archive, self.credentials, client_factory=client_factory,
                                 batch_bytes=upload_batch_bytes, log=log)
        self._import_lock = Lock(); self._status_lock = Lock(); self._importer = None
        self._import = {'running': False, 'result': None, 'progress': None}

    # ----- browsing -----
    def get_sources(self):
        return [adapter.info() for adapter in self.adapters]

    def list_sessions(self, **filters):
        return self.archive.db.list_sessions(**filters)

    def get_messages(self, session_id, offset=0, limit=2000):
        value = self.archive.db.get_messages(session_id, offset, limit)
        if value is not None:
            value['sessionRevision'] = self.archive.session_revision(session_id)
        return value

    # ----- import (startup and explicit console request only) -----
    def import_status(self):
        with self._status_lock:
            status = deepcopy(self._import)
        importer = self._importer
        if status['running'] and importer is not None:
            progress = importer.snapshot()
            status['progress'] = {'phase': 'importing', 'filesDone': progress.get('filesDone', 0),
                                  'added': progress.get('added', 0), 'updated': progress.get('updated', 0),
                                  'skippedRecent': progress.get('skippedRecent', 0), 'failed': progress.get('failed', 0),
                                  'source': progress.get('source', ''), 'currentFile': progress.get('file', ''),
                                  'step': progress.get('step', '')}
            status['elapsedSeconds'] = round(time.monotonic() - status.pop('_started', time.monotonic()), 1)
        status.pop('_started', None)
        return status

    def import_history(self):
        if not self._import_lock.acquire(blocking=False):
            raise ImportBusy('A local import is already running')
        try:
            return self._import_locked()
        finally:
            self._import_lock.release()

    def start_import(self):
        if not self._import_lock.acquire(blocking=False):
            return self.import_status()
        with self._status_lock:
            self._import = {'running': True, 'result': None, 'progress': None, '_started': time.monotonic()}

        def work():
            try:
                self._import_locked()
            except Exception as exc:
                self.log(f'[import] failed: {type(exc).__name__}: {exc}')
            finally:
                self._import_lock.release()
        Thread(target=work, name='local-import', daemon=True).start()
        return self.import_status()

    def _import_locked(self):
        with self._status_lock:
            self._import = {'running': True, 'result': None, 'progress': None, '_started': time.monotonic()}
        importer = Importer(LocalRepository(self.archive), self.adapters, quiet_seconds=self.quiet_seconds,
                            batch_bytes=self.import_batch_bytes, log=self.log)
        self._importer = importer
        try:
            result = importer.run()
            display = {'added': result['initialized'], 'updated': result['incremental'], 'unchanged': result['unchanged'],
                       'failed': result['failed'], 'skippedRecent': result['skippedRecent'],
                       'skippedSubagents': result['skippedSubagents'], 'errors': result['errors'], 'warnings': [],
                       'elapsedSeconds': result.get('elapsedSeconds')}
            with self._status_lock:
                self._import = {'running': False, 'result': display, 'progress': None, 'finishedAt': time.time()}
            return display
        except Exception as exc:
            with self._status_lock:
                self._import = {'running': False, 'result': {'added': 0, 'updated': 0, 'unchanged': 0, 'failed': 1,
                    'errors': [{'source': 'local store', 'path': '', 'error': f'{type(exc).__name__}: {exc}'}]}, 'progress': None}
            raise
        finally:
            self._importer = None

    # ----- cloud connection -----
    def cloud_status(self):
        connection = self.archive.connection()
        value = {'configured': connection is not None, 'upload': self.uploader.status(), 'local': self.archive.stats(),
                 'sources': self.get_sources(), 'import': self.import_status()}
        if connection:
            value['connection'] = {'url': connection['url'], 'keyHint': connection['key_hint'],
                                   'keyStorage': self.credentials.describe(connection['key_ref']),
                                   'clientName': connection['client_name'], 'ackedSeq': connection['acked_seq'],
                                   'pending': connection['pending'], 'lastResult': connection['last_result']}
            value['pendingChanges'] = max(0, value['local']['lastSeq'] - connection['acked_seq'])
            value['pending'] = self.archive.pending_summary(connection['acked_seq'])
        return value

    def _probe(self, url, key):
        client = self.client_factory(url, key)
        try:
            status = client.status(self.archive.dataset)
        finally:
            client.close()
        bound = status.get('boundDataset')
        return status, {'reachable': True, 'clientName': status['client']['name'], 'instance': status['instance'],
                        'boundToThisComputer': bound == self.archive.dataset, 'bound': bound is not None,
                        'conflict': bound is not None and bound != self.archive.dataset,
                        'cloudAckSeq': status.get('ackSeq'), 'localLastSeq': self.archive.last_seq(),
                        'maxRequestBytes': status['maxRequestBytes']}

    def test_connection(self, url=None, key=None):
        connection = self.archive.connection()
        url = normalize_origin(url) if url else (connection or {}).get('url')
        if not url:
            raise ValueError('Cloud address is required')
        if key:
            validate_key(key)
        elif connection:
            key = self.credentials.load(connection['key_ref'])
        else:
            raise ValueError('API key is required')
        return self._probe(url, key)[1]

    def save_connection(self, url, key=None):
        if self.uploader.status().get('running'):
            raise Problem('UPLOAD_RUNNING', 'Pause or cancel the running upload before changing the connection', 409)
        url = normalize_origin(url); connection = self.archive.connection()
        if key:
            validate_key(key)
        elif connection:
            key = self.credentials.load(connection['key_ref'])
        else:
            raise ValueError('API key is required')
        status, summary = self._probe(url, key)   # never saves an unverified connection
        if summary['conflict']:
            raise Problem('DATASET_MISMATCH', 'This API key already uploads another computer\'s data; generate a new key for this computer', 409)
        same_key = False
        if connection:
            try:
                same_key = key == self.credentials.load(connection['key_ref'])
            except LookupError:
                same_key = False
        ref = connection['key_ref'] if same_key else self.credentials.save(key)
        old = self.archive.save_connection(url, ref, key_hint(key), status['client']['id'], status['client']['name'], status['instance'])
        if old and old != ref:
            self.credentials.delete(old)
        return self.cloud_status()

    def delete_connection(self):
        if self.uploader.status().get('running'):
            raise Problem('UPLOAD_RUNNING', 'Pause or cancel the running upload first', 409)
        ref = self.archive.delete_connection()
        if ref:
            self.credentials.delete(ref)
        return self.cloud_status()

    def start_upload(self):
        return self.uploader.start()

    def pause_upload(self):
        return self.uploader.pause()

    def cancel_upload(self):
        return self.uploader.cancel()

    @staticmethod
    def known_sources():
        return dict(SOURCES)
