"""Discover one file, resume its cursor, commit, then request the next file."""
from pathlib import Path
from threading import Event, Lock, Thread
from copy import deepcopy
import time
from db import Database
from sources import default_adapters
from sources.base import SourceError
from session_policy import is_subagent, header_is_subagent
import incremental


class SyncBusy(RuntimeError):
    pass


class ClaudeViewerService:
    def __init__(self, db_path=None, adapters=None, skip_recent_seconds=3600):
        if isinstance(skip_recent_seconds, bool) or not isinstance(skip_recent_seconds, (int, float)) or skip_recent_seconds < 0:
            raise ValueError('skip_recent_seconds must be a nonnegative number')
        self._db = Database(db_path)
        self.adapters = default_adapters() if adapters is None else adapters
        self.skip_recent_seconds = skip_recent_seconds
        self._sync_lock = Lock()
        self._status_lock = Lock()
        self._started_monotonic = None
        self._file_started = None
        self._heartbeat_seconds = 5
        self._status = {'running': False, 'result': None, 'progress': None}

    def initialize(self, sync=False):
        self._db.init_schema()
        self._db.prune_subagent_sessions()
        self._db.prune_empty_sessions()
        self._db.backfill_user_counts()
        if sync:
            return self.sync_sessions()

    def shutdown(self):
        pass

    def get_sources(self):
        return [adapter.info() for adapter in self.adapters]

    def get_all_sessions(self, project_filter=None, source=None):
        return self._db.list_sessions(project=project_filter, source=source)[0]

    def list_sessions(self, **filters):
        return self._db.list_sessions(**filters)

    def search_sessions_by_content(self, search_term, source=None):
        return self._db.list_sessions(query=search_term, source=source)[0]

    def get_messages(self, session_id, **options):
        return self._db.get_messages(session_id, **options)

    def get_raw_message(self, message_id):
        return self._db.get_raw_message(message_id)

    def get_records(self, session_id, **options):
        return self._db.get_records(session_id, **options)

    def sync_status(self):
        with self._status_lock:
            status = deepcopy(self._status)
            if status['running'] and self._started_monotonic is not None:
                status['elapsedSeconds'] = round(time.monotonic() - self._started_monotonic, 1)
                if self._file_started is not None:
                    status['fileElapsedSeconds'] = round(time.monotonic() - self._file_started, 1)
            return status

    def _prepare_sync(self):
        with self._status_lock:
            self._started_monotonic = time.monotonic()
            self._file_started = None
            self._status = {'running': True, 'result': None, 'startedAt': int(time.time() * 1000),
                            'progress': {'phase': 'discovering', 'filesDone': 0, 'filesTotal': 0,
                                         'sessionsDone': 0, 'sessionsTotal': 0, 'source': '', 'currentFile': '',
                                         'skippedRecent': 0, 'step': '', 'discoveryComplete': False}}

    def _progress(self, **values):
        with self._status_lock:
            self._status['progress'].update(values)

    def _heartbeat(self, stop):
        while not stop.wait(self._heartbeat_seconds):
            state = self.sync_status()
            if not state['running']:
                return
            progress = state.get('progress') or {}
            print(f"[sync] working: {progress.get('source','')} / {progress.get('currentFile','')} "
                  f"step={progress.get('step') or progress.get('phase')} "
                  f"file={state.get('fileElapsedSeconds',0):.1f}s total={state.get('elapsedSeconds',0):.1f}s "
                  f"done={progress.get('filesDone',0)} skipped={progress.get('skippedRecent',0)}", flush=True)

    def start_sync(self):
        if not self._sync_lock.acquire(blocking=False):
            return self.sync_status()
        self._prepare_sync()
        worker = Thread(target=self._background_sync, name='history-sync', daemon=True)
        try:
            worker.start()
        except Exception:
            self._sync_lock.release()
            with self._status_lock:
                self._status['running'] = False
            raise
        return self.sync_status()

    def _background_sync(self):
        try:
            self._sync_sessions_locked()
        except Exception as exc:
            print(f'Sync failed: {exc}', flush=True)

    def sync_sessions(self):
        if not self._sync_lock.acquire(blocking=False):
            raise SyncBusy('A sync is already running')
        self._prepare_sync()
        return self._sync_sessions_locked()

    def _is_recent(self, entry):
        if self.skip_recent_seconds == 0:
            return False
        # Sidecars can change project/session interpretation too. Do not read
        # a stable transcript against a still-changing workspace descriptor.
        modified_ns = max(path.stat().st_mtime_ns for path in (entry.path, *entry.dependencies))
        return time.time_ns() - modified_ns < self.skip_recent_seconds * 1_000_000_000

    def _sync_sessions_locked(self):
        result = {'added': 0, 'updated': 0, 'unchanged': 0, 'failed': 0, 'skippedRecent': 0,
                  'skippedEmpty': 0, 'skippedSubagents': 0, 'removedEmpty': 0, 'removedSubagents': 0,
                  'appendedSessions': 0, 'appendedMessages': 0, 'incrementalSessions': 0,
                  'reprojectedScopes': 0, 'errors': [], 'warnings': []}
        outcomes, incremental_ids, appended_ids = {}, set(), set()
        discovered = set()
        files_done = files_total = 0
        heartbeat_stop = Event()
        heartbeat = Thread(target=self._heartbeat, args=(heartbeat_stop,), name='sync-progress-log', daemon=True)

        def failure(source, path, exc):
            result['failed'] += 1
            result['errors'].append({'source': source, 'path': str(path), 'error': str(exc)})
            print(f'[sync] failed: {source} / {Path(path).name}: {type(exc).__name__}: {exc}', flush=True)

        def outcome(sid, kind):
            previous = outcomes.get(sid)
            # Copies of the same session are counted once. A better copy found
            # later may replace it, without precollecting/sorting all files.
            if previous == 'added' or previous == 'updated' and kind == 'unchanged':
                return
            if previous == kind:
                return
            if previous:
                result[previous] -= 1
            outcomes[sid] = kind; result[kind] += 1

        try:
            heartbeat.start()
            print(f'[sync] start: sequential files; quiet-period={self.skip_recent_seconds:g}s', flush=True)
            result['removedSubagents'] = self._db.prune_subagent_sessions()
            result['removedEmpty'] = self._db.prune_empty_sessions()
            states = self._db.file_states()
            for adapter in self.adapters:
                self._progress(phase='discovering', source=adapter.label, currentFile='', step='discover')
                print(f'[sync] source: {adapter.label}', flush=True)
                try:
                    # No prefetch/list/batch: the generator is advanced only
                    # after this file has committed, failed, or been skipped.
                    for entry in adapter.discover():
                        path = str(entry.path); discovered.add(path); files_total += 1
                        file_started = time.monotonic()
                        file_result = 'failed'
                        with self._status_lock:
                            self._file_started = file_started
                        self._progress(phase='syncing', currentFile=entry.path.name, filesTotal=files_total, step='stat/recent-check')
                        print(f'[sync] file {files_total} start: {adapter.label} / {entry.path.name}', flush=True)
                        try:
                            if is_subagent(adapter.source, entry.path):
                                result['skippedSubagents'] += 1
                                file_result = 'skipped subagent'
                                continue
                            if self._is_recent(entry):
                                result['skippedRecent'] += 1
                                file_result = 'skipped recent'
                                print(f'Sync skipped recent: {adapter.label} / {entry.path.name}', flush=True)
                                continue
                            if header_is_subagent(adapter.source, entry.path):
                                result['skippedSubagents'] += 1
                                file_result = 'skipped subagent'
                                continue
                            file_state = states.get(path)
                            self._progress(step='lookup checkpoint')
                            context = self._db.append_context(file_state['session_id']) if file_state else None
                            self._progress(step='hash file')
                            fingerprint = entry.fingerprint()
                            modified = entry.path.stat().st_mtime_ns // 1_000_000
                            cached = file_state and file_state['source'] == adapter.source and (
                                file_state['fingerprint'] == fingerprint
                                and file_state['parser_version'] == adapter.parser_version
                                and file_state['modified'] == modified)
                            document = None
                            if not cached:
                                self._progress(step='read incremental data')
                                if context:
                                    if adapter.source == 'claude':
                                        document = adapter.parse_append(entry, context['session'], context['primary'])
                                    else:
                                        checkpoint = self._db.get_sync_checkpoint(context['session']['id'])
                                        document = incremental.update(adapter, entry, context['session'],
                                                                      context['primary'], checkpoint, self._db)
                                if document is None:
                                    self._progress(step='initialize/reconcile file')
                                    document = incremental.initialize(adapter, entry, adapter.parse(entry))
                                if is_subagent(adapter.source, entry.path, document.metadata):
                                    result['skippedSubagents'] += 1
                                    file_result = 'skipped subagent'
                                    continue
                                self._progress(step='verify snapshot prefix')
                                fingerprint = entry.snapshot_fingerprint(document.records)
                                modified = document.records.modified_ns // 1_000_000
                            # A writer may have touched it after the first stat.
                            # Discard this attempt; no archive/checkpoint is advanced.
                            if self._is_recent(entry):
                                result['skippedRecent'] += 1
                                file_result = 'skipped recent (changed during read)'
                                continue
                            sid = file_state['session_id'] if cached else document.id
                            existing = self._db.get_session(sid)
                            with self._db.get_connection() as conn:
                                row = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                                primary = dict(row) if row else None
                            count = (file_state['message_count'] if cached else self._db.projected_message_count(document)
                                     if document.append_base else len(document.messages))
                            file_info = (path, adapter.source, sid, fingerprint, adapter.parser_version, modified, count)
                            if count == 0 and existing is None:
                                # Cache the empty file's fingerprint, not an empty session.
                                self._db.update_files([file_info])
                                states[path] = self._file_state(file_info)
                                result['skippedEmpty'] += 1
                                file_result = 'skipped empty session'
                                continue
                            if primary and primary['path'] != path:
                                current_path = Path(primary['path'])
                                current_modified = (current_path.stat().st_mtime_ns // 1_000_000
                                                    if current_path.exists() else primary['last_modified'])
                                rank = (count > 0, modified, count, path)
                                previous_rank = (primary['message_count'] > 0, current_modified,
                                                 primary['message_count'], primary['path'])
                                if current_path.exists() and rank <= previous_rank:
                                    self._db.update_files([file_info]); outcome(sid, 'unchanged')
                                    file_result = 'unchanged (older duplicate)'
                                    states[path] = self._file_state(file_info)
                                    continue
                            if existing and existing['messageCount'] > 0 and count == 0 and not (document and document.append_base):
                                self._db.update_files([file_info]); outcome(sid, 'unchanged')
                                file_result = 'unchanged (empty copy ignored)'
                                result['warnings'].append({'source': adapter.source, 'sessionId': sid,
                                    'warning': 'Empty projection ignored; previous history retained'})
                                states[path] = self._file_state(file_info)
                                continue
                            unchanged = primary and primary['path'] == path and (
                                primary['fingerprint'] == fingerprint and primary['parser_version'] == adapter.parser_version
                                and primary['last_modified'] == modified)
                            if unchanged:
                                self._progress(step='reuse unchanged index')
                                self._db.update_files([file_info])
                                if hasattr(adapter, 'prime_checkpoint'):
                                    metadata = adapter.prime_checkpoint(entry, existing, fingerprint)
                                    if metadata is not None:
                                        self._db.prime_append_checkpoint(sid, entry.path, fingerprint, metadata)
                                print(f"[sync] session records: {existing['messageCount']} unchanged; "
                                      f"user messages={existing['userMessageCount']} / {entry.path.name}", flush=True)
                                outcome(sid, 'unchanged')
                                file_result = 'unchanged'
                            else:
                                if document is None:
                                    document = incremental.initialize(adapter, entry, adapter.parse(entry))
                                    fingerprint = entry.snapshot_fingerprint(document.records)
                                    modified = document.records.modified_ns // 1_000_000
                                    count = len(document.messages)
                                    file_info = (path,adapter.source,sid,fingerprint,adapter.parser_version,modified,count)
                                    if self._is_recent(entry):
                                        result['skippedRecent'] += 1
                                        file_result = 'skipped recent (changed during read)'
                                        continue
                                if document.id != sid:
                                    raise SourceError('Source session identity changed; retry sync')
                                self._progress(step='commit incremental delta' if document.append_base else 'commit initialization')
                                if document.append_base:
                                    if document.sync_state:
                                        self._db.apply_delta(document, entry.path, fingerprint, modified,
                                                             adapter.parser_version, [file_info])
                                        result['reprojectedScopes'] += len(document.changed_scopes)
                                    else:
                                        self._db.append_session(document, entry.path, fingerprint, modified,
                                                                adapter.parser_version, [file_info])
                                    if sid not in incremental_ids:
                                        result['incrementalSessions'] += 1; incremental_ids.add(sid)
                                    if not document.changed_scopes:
                                        if sid not in appended_ids:
                                            result['appendedSessions'] += 1; appended_ids.add(sid)
                                        result['appendedMessages'] += len(document.messages)
                                else:
                                    self._db.replace_session(document, entry.path, fingerprint, modified,
                                                             adapter.parser_version, [file_info])
                                saved = self._db.get_session(sid)
                                self._progress(sessionRecords=saved['messageCount'], userMessages=saved['userMessageCount'])
                                print(f"[sync] session records: {existing['messageCount'] if existing else 0} -> "
                                      f"{saved['messageCount']}; user messages={saved['userMessageCount']} / {entry.path.name}", flush=True)
                                outcome(sid, 'updated' if existing else 'added')
                                file_result = 'incremental update' if document.append_base else 'full initialization'
                                for warning in document.warnings:
                                    result['warnings'].append({'source': adapter.source, 'sessionId': sid, 'warning': warning})
                                print(f'Sync committed: {adapter.label} / {entry.path.name} '
                                      f"({'incremental' if document.append_base else 'full initialization'})", flush=True)
                            states[path] = self._file_state(file_info)
                        except Exception as exc:
                            failure(adapter.source, path, exc)
                        finally:
                            files_done += 1
                            print(f'[sync] file {files_total} end: {file_result} / {adapter.label} / {entry.path.name} '
                                  f'({time.monotonic()-file_started:.2f}s)', flush=True)
                            with self._status_lock:
                                self._file_started = None
                            self._progress(filesDone=files_done, sessionsDone=len(outcomes), sessionsTotal=len(outcomes),
                                skippedRecent=result['skippedRecent'],
                                counts={key: result[key] for key in ('added','updated','unchanged','failed','incrementalSessions','skippedRecent')})
                except Exception as exc:
                    failure(adapter.source, '<discovery>', exc)
            self._progress(phase='finishing', discoveryComplete=True, currentFile='', step='finalize availability')
            missing = [path for path in states if path not in discovered and not Path(path).exists()]
            self._db.mark_missing(missing)
            result['removedEmpty'] += self._db.prune_empty_sessions()
            with self._status_lock:
                self._status.update(running=False, result=result,
                                    elapsedSeconds=round(time.monotonic() - self._started_monotonic, 1))
                self._status['progress']['phase'] = 'complete'
            print(f"Sync complete: {result['added']} added, {result['updated']} updated, "
                  f"{result['unchanged']} unchanged, {result['failed']} failed, "
                  f"{result['skippedRecent']} recent files skipped, {result['incrementalSessions']} incremental", flush=True)
            return result
        except Exception as exc:
            failure('archive', str(self._db.db_path), exc)
            with self._status_lock:
                self._status.update(running=False, result=result,
                                    elapsedSeconds=round(time.monotonic() - self._started_monotonic, 1))
                self._status['progress']['phase'] = 'failed'
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat.ident is not None:
                heartbeat.join(timeout=1)
            with self._status_lock:
                self._status['running'] = False
                self._file_started = None
            self._sync_lock.release()

    @staticmethod
    def _file_state(info):
        path,source,sid,fingerprint,version,modified,count = info
        return {'path':path,'source':source,'session_id':sid,'fingerprint':fingerprint,
                'parser_version':version,'modified':modified,'message_count':count,'available':1}

    def sync_sessions_from_claude_files(self):
        return self.sync_sessions()
