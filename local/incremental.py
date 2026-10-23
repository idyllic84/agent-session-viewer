"""Incremental source projections, independent of batch discovery/commit scheduling.

JSONL is read from a verified byte/line cursor. pi projects new entries directly;
Codex/Copilot reproject only dirty turns; VS Code replays new mutations against
per-request checkpoints and reprojects only dirty requests. Closed units and
unrelated messages are never decoded or normalized on these incremental paths.
"""
from copy import deepcopy
import hashlib
import json
from .models import SessionDocument, searchable_text
from .sources.base import SourceError, SnapshotRecords, read_json, read_records
from .sources.vscode_chat import replay_operations

ENGINE_VERSION = 1


class CheckpointInvalid(SourceError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()


def _cursor(records):
    if records.has_unterminated_value:
        return None
    return {'offset': records.safe_offset, 'nextLine': records.safe_next_line,
            'fingerprint': records.safe_digest.hex()}


def _read_tail(entry, cursor):
    if not cursor or entry.path.stat().st_size < cursor['offset']:
        return None
    digest = hashlib.sha256(str(entry.path).encode('utf-8'))
    remaining = cursor['offset']
    with entry.path.open('rb') as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                return None
            digest.update(chunk); remaining -= len(chunk)
    if digest.hexdigest() != cursor['fingerprint']:
        return None
    return read_records(entry.path, offset=cursor['offset'], next_line=cursor['nextLine'],
                        prefix_hasher=digest, require_newline=True)


def _base(primary, session):
    return {'path': primary['path'], 'fingerprint': primary['fingerprint'],
            'parserVersion': primary['parser_version'], 'messageCount': session['messageCount']}


def _set_cursor(state, entry, records):
    state['engineVersion'] = ENGINE_VERSION
    state['format'] = 'json' if entry.path.suffix == '.json' else 'jsonl'
    state['cursor'] = _cursor(records) if state['format'] == 'jsonl' else None


def _scope_messages(document, raw_scopes):
    for message in document.messages:
        index = message['rawRecord']
        scope = raw_scopes.get(index, ['root'])[0]
        document.message_scopes[message['id']] = scope
        document.message_order[message['id']] = (index, int(message['id'].rsplit(':', 1)[-1]))


def _policy(records, state):
    previous = dict(state.get('policy', {}))
    policy = dict(previous)
    paginated = (state['header'][1].get('payload') or {}).get('history_mode') == 'paginated'
    for _, row in records:
        payload = row.get('payload') or {}
        if row.get('type') == 'compacted':
            policy['compactions'] = True
        if paginated and isinstance(payload, dict) and payload.get('type') == 'item_completed':
            thread = payload.get('thread_id')
            header = state['header'][1]['payload']
            boundary = header.get('subagent_history_start_ordinal')
            if thread is not None and thread != header['id']:
                continue
            if thread is None and boundary is not None and row.get('ordinal', -1) < boundary:
                continue
            item = payload.get('item') or {}
            if isinstance(item, dict):
                if item.get('type') in ('UserMessage', 'AgentMessage'):
                    policy['messages'] = True
                elif item.get('type') == 'Reasoning':
                    policy['reasoning'] = True
    state['policy'] = policy
    return policy != previous


def _partition(records, state):
    """Map exact native turn/call identities, not text similarity, to stable units."""
    result, seeds = {}, {}
    source = state['kind']
    calls = state.setdefault('calls', {})
    turns = state.setdefault('turns', {})
    definitions = state.setdefault('units', {})
    for index, row in records:
        payload = row.get('payload' if source == 'codex' else 'data') or {}
        if not isinstance(payload, dict):
            raise SourceError(f'Invalid event data at line {index + 1}')
        kind = row.get('type')
        event = payload.get('type') if source == 'codex' else kind
        turn = payload.get('turn_id' if source == 'codex' else 'turnId')
        old_carry = state.get('carry')
        if source == 'codex':
            if kind == 'turn_context' or (kind == 'event_msg' and event == 'task_started'):
                if turn:
                    state['open'] = turns.setdefault(turn, f'turn:{turn}')
            scope = turns.get(turn, f'turn:{turn}') if turn else state.get('open', 'root')
            if turn:
                turns.setdefault(turn, scope)
            call = payload.get('call_id')
            if kind == 'event_msg' and event == 'item_completed':
                item = payload.get('item') or {}
                call = item.get('id') if isinstance(item, dict) else None
            if call and call in calls:
                scope = calls[call]
            if kind == 'response_item' and event in ('function_call', 'custom_tool_call') and call:
                calls[call] = scope
            if kind == 'turn_context':
                state['carry'] = [index, row]
        else:
            interaction = payload.get('interactionId')
            if kind == 'user.message':
                state['open'] = f'interaction:{interaction}' if interaction else (f'turn:{turn}' if turn else f'user:{index}')
            if turn and turn not in turns:
                turns[turn] = state.get('open', f'turn:{turn}')
            scope = (f'interaction:{interaction}' if interaction else turns.get(turn)) or state.get('open', 'root')
            call = payload.get('toolCallId')
            if call and call in calls:
                scope = calls[call]
            if kind == 'assistant.message':
                for request in payload.get('toolRequests') or []:
                    if isinstance(request, dict) and request.get('toolCallId'):
                        calls[request['toolCallId']] = scope
            if kind == 'tool.execution_start' and call:
                calls[call] = scope
            if kind == 'session.model_change':
                state['carry'] = [index, row]
        if scope not in definitions:
            definitions[scope] = {'carry': old_carry}
            seeds[scope] = definitions[scope]
        result[index] = [scope]
    return result, seeds


def initialize(adapter, entry, document):
    """Install state for a full parse. Existing unchanged archives may stay lazy."""
    source = adapter.source
    if source == 'claude':
        return document
    if source == 'vscode_chat':
        state = document.sync_state
        state['requestDigests'] = {scope: _digest(value['request']) for scope, value in document.scope_states.items()}
        state['firstPrompt'] = next((m['message']['content'][0].get('text', '')[:160]
                                    for m in document.messages if m['type']=='user'), '')
        document.raw_scopes = {index: ['header'] for index, _ in document.records}
    elif source == 'pi':
        state = document.sync_state
        document.raw_scopes = {index: [f'entry:{index}'] for index, _ in document.records}
        _scope_messages(document, document.raw_scopes)
    else:
        header = next(([i, row] for i, row in document.records if row.get('type') ==
                       ('session_meta' if source == 'codex' else 'session.start')), None)
        if header is None:
            raise SourceError('Missing source header for incremental initialization')
        state = {'kind': source, 'header': header, 'open': 'root', 'units': {}, 'calls': {}, 'turns': {}}
        document.raw_scopes, document.scope_states = _partition(document.records, state)
        _scope_messages(document, document.raw_scopes)
        if source == 'codex':
            state['policy'] = dict(document.sync_state.get('policy', {}))
        document.sync_state = state
    _set_cursor(state, entry, document.records)
    return document


def update(adapter, entry, session, primary, checkpoint, database):
    if not checkpoint or checkpoint['path'] != str(entry.path) or checkpoint['parserVersion'] != adapter.parser_version:
        return None
    state = checkpoint['state']
    if state.get('engineVersion') != ENGINE_VERSION:
        return None
    if adapter.source == 'vscode_chat':
        try:
            return _update_vscode(adapter, entry, session, primary, state, database)
        except CheckpointInvalid:
            return None
    if state.get('format') != 'jsonl':
        return None
    tail = _read_tail(entry, state.get('cursor'))
    if tail is None:
        return None
    records, warnings = tail
    if any(row.get('type') == ('session' if adapter.source == 'pi' else 'session_meta' if adapter.source == 'codex' else 'session.start')
           for _, row in records):
        return None  # Header/identity replacement is an explicit full-reconciliation boundary.
    if adapter.source == 'pi':
        document = adapter.project_records(entry, records, warnings, seed=state)
        document.raw_scopes = {index: [f'entry:{index}'] for index, _ in records}
        _scope_messages(document, document.raw_scopes)
        document.metadata = {**session['metadata'], **document.metadata}
    else:
        state = deepcopy(state)
        mapping, seeds = _partition(records, state)
        dirty = {scope for scopes in mapping.values() for scope in scopes}
        if adapter.source == 'codex' and _policy(records, state):
            # A canonical-format transition changes the interpretation of older
            # units too. Reconcile those units from archive, never reread the log.
            dirty.update(state['units'])
        document = SessionDocument(adapter.source, session['externalId'], session['project'], records,
                                   name=session['name'], metadata=deepcopy(session['metadata']), warnings=list(warnings))
        document.raw_scopes = mapping
        document.changed_scopes = dirty
        document.scope_states = seeds
        for scope in sorted(dirty):
            retained = database.load_scope_records(session['id'], scope)
            added = [(index, row) for index, row in records if scope in mapping.get(index, [])]
            unit = {index: row for index, row in retained + added}
            seed = state['units'].get(scope, {})
            context = {state['header'][0]: state['header'][1]}
            if seed.get('carry'):
                index, row = seed['carry']; context[index] = row
            context.update(unit)
            projected = (adapter.project_records(entry, sorted(context.items()), policy=state.get('policy'))
                         if adapter.source == 'codex' else adapter.project_records(entry, sorted(context.items())))
            for message in projected.messages:
                if message['rawRecord'] not in unit:
                    continue
                document.messages.append(message)
                document.message_scopes[message['id']] = scope
                document.message_order[message['id']] = (message['rawRecord'], int(message['id'].rsplit(':', 1)[-1]))
            document.metadata.update({key: value for key, value in projected.metadata.items() if key != 'latestUsageRecord'})
        if adapter.source == 'codex':
            for _, row in records:
                if row.get('type') == 'token_usage_record' and isinstance(row.get('payload'), dict):
                    payload = row['payload']
                    document.metadata['latestUsageRecord'] = {key: payload.get(key) for key in
                        ('usage','turn_token_usage','thread_token_usage','turn_id','response_id')}
        document.sync_state = state
        document.messages.sort(key=lambda m: document.message_order[m['id']])
    _set_cursor(document.sync_state, entry, records)
    document.append_base = _base(primary, session)
    return document


def _update_vscode(adapter, entry, session, primary, state, database):
    state = deepcopy(state)
    header = state['header']
    count = state['requestCount']
    digests = state.get('requestDigests', {})
    working, raw_indices, dirty, deleted = {}, {}, set(), set()

    def load(index):
        if index not in working:
            stored = database.load_scope_state(session['id'], f'request:{index}')
            if stored is None:
                raise CheckpointInvalid(f'Missing VS Code request checkpoint {index}; full reconciliation required')
            working[index] = stored['request']; raw_indices[index] = stored['raw']
        return working[index]

    def replace_requests(values, raw_index, start=0, truncate=True):
        nonlocal count
        values = [] if values is None else values
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise SourceError('Invalid replacement request array')
        old_count = count
        new_count = start + len(values)
        if truncate:
            for index in range(new_count, old_count):
                deleted.add(index); dirty.discard(index); working.pop(index, None)
                digests.pop(f'request:{index}', None)
            count = new_count
        for index, value in enumerate(values, start):
            digest = _digest(value)
            if digests.get(f'request:{index}') != digest:
                working[index] = value; raw_indices[index] = raw_index
                dirty.add(index); deleted.discard(index)
            else:
                # Earlier operations in this same tail may already have touched
                # the request; restoring it to its original state cancels work.
                dirty.discard(index); working.pop(index, None)

    if entry.path.suffix == '.json':
        snapshot = read_json(entry.path)
        if str(snapshot.get('sessionId') or entry.path.stem) != session['externalId']:
            return None
        records, warnings = SnapshotRecords.from_json(entry.path, snapshot), []
        header = {key: value for key, value in snapshot.items() if key != 'requests'}
        replace_requests(snapshot.get('requests') or [], 0)
        raw_snapshot_replace = True
    else:
        tail = _read_tail(entry, state.get('cursor'))
        if tail is None:
            return None
        records, warnings = tail
        raw_snapshot_replace = False
        for raw_index, operation in records:
            kind = operation.get('kind')
            keys = operation.get('k') or []
            if kind == 0:
                snapshot = operation.get('v')
                if not isinstance(snapshot, dict) or str(snapshot.get('sessionId') or entry.path.stem) != session['externalId']:
                    return None
                header = {key: value for key, value in snapshot.items() if key != 'requests'}
                replace_requests(snapshot.get('requests') or [], raw_index)
            elif keys and keys[0] == 'requests':
                if len(keys) == 1:
                    if kind == 1:
                        replace_requests(operation.get('v'), raw_index)
                    elif kind == 2:
                        start = operation.get('i', count)
                        if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= count:
                            raise SourceError('Invalid request-array splice index')
                        replace_requests(operation.get('v') or [], raw_index, start=start)
                    elif kind == 3:
                        replace_requests([], raw_index)
                    else:
                        raise SourceError('Unknown request-array mutation')
                else:
                    index = keys[1]
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
                        raise SourceError('Invalid request checkpoint index')
                    # Only the mutated request is restored. Other request slots
                    # stay holes and are never loaded/decoded from archive.
                    proxy = {'requests': [None] * (index + 1)}
                    proxy['requests'][index] = load(index)
                    replay_operations([(raw_index, operation)], initial_state=proxy)
                    working[index] = proxy['requests'][index]
                    raw_indices[index] = raw_index; dirty.add(index)
            else:
                if not keys:
                    raise SourceError('Invalid global VS Code mutation')
                replay_operations([(raw_index, operation)], initial_state=header)
    if str(header.get('sessionId') or entry.path.stem) != session['externalId']:
        return None
    for index in list(dirty):
        if not isinstance(working[index], dict):
            raise SourceError('Request checkpoint is not an object')
        digest = _digest(working[index])
        if digest == digests.get(f'request:{index}'):
            dirty.discard(index)
        else:
            digests[f'request:{index}'] = digest
    items = [(index, working[index]) for index in sorted(dirty)]
    document = adapter.project_state(entry, records, {**header, 'requests': []}, warnings,
                                     request_items=items, raw_overrides=raw_indices)
    if 0 in dirty:
        user = working[0].get('message') or {}
        state['firstPrompt'] = str(user.get('text') or '')[:160] if isinstance(user, dict) else ''
    if count == 0:
        state['firstPrompt'] = ''
    document.name = str(header.get('customTitle') or state.get('firstPrompt') or session['externalId'])
    document.changed_scopes = {f'request:{index}' for index in dirty | deleted}
    document.scope_states.update({f'request:{index}': None for index in deleted})
    document.raw_scopes = {index: ['header'] for index, _ in records}
    document.raw_snapshot_replace = raw_snapshot_replace
    document.metadata = {**session['metadata'], **document.metadata}
    state.update(header=header, requestCount=count, requestDigests=digests)
    document.sync_state = state
    _set_cursor(state, entry, records)
    document.append_base = _base(primary, session)
    return document
