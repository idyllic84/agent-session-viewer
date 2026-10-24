"""Local incremental importer: source files -> local analysis store.

Files are processed sequentially. Only metadata of a bounded discovery window
is looked up together; bodies are read, projected and committed one file (and
one bounded byte range) at a time. Every commit stores analytical messages, the
source cursor and the upload change-log entries in one local transaction.
No watcher or periodic scan exists; the importer runs only when invoked.
"""
from contextlib import contextmanager
import json
from threading import Event, Thread, Lock
import time

from . import incremental
from .sources.base import SourceFile
from server.contracts.session_policy import is_subagent, header_is_subagent
from server.contracts.protocol import Problem, packet, encode_batch
from server.contracts.analysis import content_digest
from .replay import Replay
from .batching import PrefixPath, Boundaries


class LocalRepository:
    """Import-side view of the local store. RAM state lasts one file at most."""
    def __init__(self, archive, *, max_request=512 * 1024 * 1024):
        # Local commits are not network requests: one indivisible source record
        # (e.g. a 400 MB VS Code snapshot line) may legitimately project to tens
        # of MiB. The cap only guards against pathological memory use.
        self.archive = archive
        self.caps = {'maxRequestBytes': max_request, 'maxProgressFiles': 128}
        self.reset_run()

    def reset_run(self):
        self.revision = 0; self.progress_window = {}; self.primary = None
        self.adapter = None; self.entry = None; self.current = {}; self.replay = Replay()

    def query_progress(self, files):
        if not files:
            return
        value = self.archive.query_progress(files, include_context=True)
        self.progress_window = {(f['source'], f['path']): f for f in value['files']}

    def file_progress(self, path, source):
        key = (source, path)
        if key not in self.progress_window:
            self.query_progress([{'path': path, 'source': source}])
        value = self.progress_window[key]
        if value.get('error'):
            error = value['error']; raise Problem(error['code'], error['message'], error['status'])
        return value

    def use_context(self, value):
        if 'scopesDelta' in value:
            scopes = {} if value.get('resetScopes') else dict(self.current.get('scopes', {}))
            for scope, state in value['scopesDelta'].items():
                if state is None:
                    scopes.pop(scope, None)
                else:
                    scopes[scope] = state
            value = {**value, 'scopes': scopes}; value.pop('scopesDelta', None)
        self.revision = value['revision']; self.current = value; self.primary = (value['context'] or {}).get('primary')
        return value

    def context(self, path=None, sid=None, source=None):
        return self.use_context(self.archive.context(path, sid, source))

    def load_scope_state(self, sid, scope): return self.replay.load_scope_state(sid, scope)
    def load_scope_records(self, sid, scope): return self.replay.load_scope_records(sid, scope)

    def prepare(self, document, info):
        value = packet(document, info, raw_context=self.replay.records, scope_context=self.replay.scope_states)
        data = value['document']; stored_scopes = self.current.get('scopes', {})
        snapshot = info['path'].endswith('.json') and self.primary is not None
        if document.changed_scopes or snapshot:
            groups = {}
            for message in data['messages']:
                groups.setdefault(data['message_scopes'][message['id']], []).append(message)
            candidates = (set(groups) | set(stored_scopes)) if snapshot else set(document.changed_scopes)
            changed = {scope for scope in candidates if content_digest(groups.get(scope, [])) != stored_scopes.get(scope, {}).get('analysisDigest')}
            value['changedScopes'] = sorted(changed)
            data['messages'] = [m for m in data['messages'] if data['message_scopes'][m['id']] in changed]
            ids = {m['id'] for m in data['messages']}
            data['message_scopes'] = {k: v for k, v in data['message_scopes'].items() if k in ids}
            data['message_order'] = {k: v for k, v in data['message_order'].items() if k in ids}
        if (document.append_base or snapshot) and self.primary:
            p = self.primary
            data['append_base'] = {'path': p['path'], 'fingerprint': p['fingerprint'],
                                   'parserVersion': p['parser_version'], 'messageCount': p['message_count']}
        return encode_batch(value)

    def send(self, document, info, body, verify):
        if len(body) > self.caps['maxRequestBytes']:
            raise Problem('TOO_LARGE', 'One indivisible source record projects beyond the local safety limit; not truncated', 413)
        verify()
        try:
            receipt = self.archive.commit(json.loads(body), self.revision)
        except Problem as exc:
            if exc.code == 'REVISION_CONFLICT':
                context = self.context(sid=document.id)
                self.progress_window[(document.source, info['path'])] = {'state': context['state'], 'syncContext': context}
            raise
        self.replay.advance(document, info)
        context = self.use_context(receipt['nextContext'])
        self.progress_window[(document.source, info['path'])] = {'state': context['state'], 'syncContext': context}
        return receipt

    def finish_scan(self, completed, seen):
        return self.archive.finish_scan(completed, seen)

    def release_file(self):
        self.replay.reset(); self.primary = None
        self.current = {}; self.adapter = None; self.entry = None


class Cancelled(Exception):
    pass


class Importer:
    def __init__(self, repository, adapters, *, quiet_seconds=3600, batch_bytes=1024 * 1024,
                 progress_query_files=64, stop=None, log=print):
        if not isinstance(quiet_seconds, (int, float)) or isinstance(quiet_seconds, bool) or quiet_seconds < 0:
            raise ValueError('Invalid quiet period')
        if not isinstance(batch_bytes, int) or isinstance(batch_bytes, bool) or batch_bytes < 256:
            raise ValueError('Batch target must be >=256 bytes')
        if not isinstance(progress_query_files, int) or isinstance(progress_query_files, bool) or progress_query_files < 1:
            raise ValueError('Invalid progress query window')
        self.repository = repository; self.adapters = adapters; self.quiet_seconds = quiet_seconds
        self.query_files = min(progress_query_files, repository.caps['maxProgressFiles'])
        self.batch_bytes = min(batch_bytes, repository.caps['maxRequestBytes'])
        self.progress = {}; self.progress_lock = Lock(); self.stop = stop or Event(); self.log = log
        self.counts = {'filesDone': 0}

    def recent(self, entry):
        return self.quiet_seconds > 0 and time.time_ns() - max(p.stat().st_mtime_ns for p in (entry.path, *entry.dependencies)) < self.quiet_seconds * 1e9

    def control_point(self):
        if self.stop.is_set():
            raise Cancelled()

    def snapshot(self):
        with self.progress_lock:
            return dict(self.progress)

    def set_progress(self, **values):
        with self.progress_lock:
            self.progress.update(values)

    @contextmanager
    def working_log(self):
        stop = Event()

        def log_step():
            while not stop.wait(5):
                self.log('[import] working ' + json.dumps(self.snapshot(), ensure_ascii=False))
        thread = Thread(target=log_step, daemon=True, name='import-progress-log'); thread.start()
        try:
            yield
        finally:
            stop.set(); thread.join()

    def entries(self, adapter):
        """Batch only file descriptors. Bodies remain strictly sequential."""
        iterator = iter(adapter.discover()); finished = False
        while not finished:
            self.control_point()
            window = []; error = None
            try:
                for _ in range(self.query_files):
                    window.append(next(iterator))
            except StopIteration:
                finished = True
            except OSError as exc:
                error = exc; finished = True
            files = [{'source': adapter.source, 'path': str(e.path), 'modified': e.path.stat().st_mtime_ns // 1_000_000,
                      'size': e.path.stat().st_size, 'parserVersion': adapter.parser_version}
                     for e in window if not is_subagent(adapter.source, e.path)]
            if files:
                self.set_progress(source=adapter.source, step='lookup local cursors', files=len(files))
                self.repository.query_progress(files)
            yield from window
            if error:
                raise error

    def file(self, adapter, entry):
        repo = self.repository; path = str(entry.path); summary = repo.file_progress(path, adapter.source); state = summary['state']
        modified = entry.path.stat().st_mtime_ns // 1_000_000
        if state and state['modified'] == modified and state['parser_version'] == adapter.parser_version and state['fingerprint'] == entry.fingerprint():
            return 'unchanged', False
        if summary.get('syncContext') is not None:
            value = repo.use_context(summary['syncContext'])
        elif state or summary.get('refresh'):
            value = repo.context(path=path, source=adapter.source)
        else:
            value = {'context': None, 'checkpoint': None, 'revision': 0, 'scopes': {}}
            repo.revision = 0; repo.current = value; repo.primary = None
        # Another copy of an already imported session (e.g. VS Code workspace +
        # empty-window storage): never resume THIS file from the primary copy's
        # cursor. Read it whole; it replaces the session only as a superset.
        foreign = bool(value['context']) and value['context']['primary']['path'] != path
        if foreign:
            value = {'context': None, 'checkpoint': None, 'revision': value['revision'], 'scopes': {}}
            repo.revision = value['revision']; repo.current = value; repo.primary = None
        context = value['context']; repo.adapter = adapter; repo.entry = entry
        checkpoint = value.get('checkpoint'); cursor = (checkpoint or {}).get('state', {}).get('cursor')
        repo.replay.seed(adapter, entry, value)
        local_context, local_checkpoint = repo.replay.context()
        start = cursor['offset'] if cursor else 0; bounded = entry; bounds = None
        if entry.path.suffix == '.jsonl' and not foreign and (cursor or context is None):
            bounds = Boundaries(entry.path, min(start, entry.path.stat().st_size)); end = bounds.extend(self.batch_bytes)
            bounded = SourceFile(PrefixPath(entry.path, end), entry.project, entry.dependencies)

        def project(candidate):
            document = None
            if local_context:
                if adapter.source == 'claude':
                    document = adapter.parse_append(candidate, local_context['session'], local_context['primary'])
                else:
                    document = incremental.update(adapter, candidate, local_context['session'], local_context['primary'], local_checkpoint, repo)
            if document is None:
                document = incremental.initialize(adapter, candidate, adapter.parse(candidate))
            conversation = [r for _, r in document.records if r.get('type') in ('user', 'assistant')]
            if conversation and all(r.get('isSidechain') is True or r.get('isSubagent') is True for r in conversation):
                raise Problem('SUBAGENT_EXCLUDED', 'Subagent internal conversations are not imported', 422)
            info = {'path': path, 'fingerprint': candidate.snapshot_fingerprint(document.records),
                    'modified': document.records.modified_ns // 1_000_000, 'parserVersion': adapter.parser_version}
            return document, info, repo.prepare(document, info)
        document, info, body = project(bounded)
        if is_subagent(adapter.source, entry.path, document.metadata):
            return 'skippedSubagents', False
        if bounds:
            while len(body) < self.batch_bytes // 2 and not bounds.eof and (not bounds.ends or bounds.ends[-1] - bounds.start < 8 * self.batch_bytes):
                bounds.extend(max(self.batch_bytes, 2 * (bounds.ends[-1] - bounds.start if bounds.ends else 1)))
                bounded = SourceFile(PrefixPath(entry.path, bounds.ends[-1] if bounds.ends else bounds.start), entry.project, entry.dependencies)
                document, info, body = project(bounded)
            if len(body) > self.batch_bytes and len(bounds.ends) > 1:
                low, high = 1, len(bounds.ends); best = None; first = None
                while low <= high:
                    count = (low + high) // 2
                    candidate = SourceFile(PrefixPath(entry.path, bounds.ends[count - 1]), entry.project, entry.dependencies)
                    doc, meta, encoded = project(candidate)
                    if first is None or count < first[0]:
                        first = (count, candidate, doc, meta, encoded)
                    if len(encoded) <= self.batch_bytes:
                        best = (candidate, doc, meta, encoded); low = count + 1
                    else:
                        high = count - 1
                if best:
                    bounded, document, info, body = best
                elif first:
                    _, bounded, document, info, body = first

        def verify():
            self.control_point()
            if bounded.snapshot_fingerprint(document.records) != info['fingerprint']:
                raise Problem('SOURCE_CHANGED', 'Source snapshot changed before commit')
            if self.recent(entry):
                raise Problem('SOURCE_RECENT', 'Source changed within the quiet period')
        try:
            receipt = repo.send(document, info, body, verify)
        except Problem as exc:
            if exc.code == 'SOURCE_DIVERGED' and foreign and repo.archive.remember_duplicate(json.loads(body)):
                return 'unchanged', False   # older/equal duplicate copy; history kept
            raise
        self.set_progress(batchBytes=len(body), revision=receipt['revision'], messageCount=receipt['messageCount'])
        more = document.records.byte_count < entry.path.stat().st_size and bounds is not None
        return ('incremental' if json.loads(body)['document']['append_base'] else 'initialized'), more

    def run(self, sources=None):
        self.repository.reset_run()
        result = {key: 0 for key in ('initialized', 'incremental', 'unchanged', 'skippedRecent', 'skippedSubagents', 'failed')}
        result['errors'] = []; seen = []; completed = []; started_run = time.monotonic()
        self.log(f'[import] start: sequential files; quiet-period={self.quiet_seconds:g}s')
        try:
            with self.working_log():
                for adapter in self.adapters:
                    if sources and adapter.source not in sources:
                        continue
                    complete = bool(adapter.roots) and all(p.exists() for p in adapter.roots)
                    try:
                        for entry in self.entries(adapter):
                            self.control_point(); path = str(entry.path); seen.append(path); started = time.monotonic(); outcome = 'failed'
                            self.set_progress(source=adapter.source, file=entry.path.name, step='stat/recent-check')
                            try:
                                if is_subagent(adapter.source, entry.path):
                                    outcome = 'skippedSubagents'; result[outcome] += 1
                                elif self.recent(entry):
                                    outcome = 'skippedRecent'; result[outcome] += 1
                                elif header_is_subagent(adapter.source, entry.path):
                                    outcome = 'skippedSubagents'; result[outcome] += 1
                                else:
                                    more = True; counted = None
                                    while more:
                                        self.control_point(); self.set_progress(step='parse/filter/commit')
                                        for attempt in range(3):
                                            try:
                                                outcome, more = self.file(adapter, entry); break
                                            except Problem as exc:
                                                if exc.code == 'REVISION_CONFLICT' and attempt < 2:
                                                    if not self.repository.progress_window.get((adapter.source, path), {}).get('syncContext'):
                                                        self.repository.progress_window[(adapter.source, path)] = {'refresh': True, 'state': None}
                                                    continue
                                                raise
                                        if counted is None or outcome == 'initialized':
                                            counted = outcome
                                    outcome = counted; result[outcome] += 1
                            except Problem as exc:
                                if exc.code == 'SUBAGENT_EXCLUDED':
                                    outcome = 'skippedSubagents'; result[outcome] += 1
                                else:
                                    result['failed'] += 1
                                    result['errors'].append({'source': adapter.source, 'path': path, 'code': exc.code, 'error': str(exc)})
                            except (ValueError, OSError) as exc:
                                result['failed'] += 1
                                result['errors'].append({'source': adapter.source, 'path': path, 'code': type(exc).__name__, 'error': str(exc)})
                            finally:
                                self.repository.release_file(); self.counts['filesDone'] += 1
                                self.set_progress(filesDone=self.counts['filesDone'])
                                if outcome not in ('unchanged',):
                                    self.log(f'[import] {outcome} / {adapter.source} / {entry.path.name} ({time.monotonic()-started:.2f}s)')
                    except OSError as exc:
                        complete = False; result['failed'] += 1
                        result['errors'].append({'source': adapter.source, 'path': '<discovery>', 'code': 'DISCOVERY_FAILED', 'error': str(exc)})
                    if complete:
                        completed.append(adapter.source)
                self.set_progress(step='finalize availability', file='')
                self.repository.finish_scan(completed, seen)
        except Cancelled:
            result['cancelled'] = True
        finally:
            self.repository.reset_run()
        result['elapsedSeconds'] = round(time.monotonic() - started_run, 1)
        self.log('[import] complete: ' + json.dumps({k: v for k, v in result.items() if k != 'errors'}))
        return result
