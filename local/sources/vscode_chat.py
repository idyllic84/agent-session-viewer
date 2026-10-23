"""VS Code state snapshots and official objectMutationLog operations.

Push(i) truncates the entire suffix before appending; it is not replacement of
len(v) elements. The raw operation log is never mutated during reconstruction.
"""
from copy import deepcopy
from urllib.parse import unquote, urlparse
from ..models import SessionDocument, blocks, compact_value, text
from .base import SourceAdapter, SourceFile, SourceError, SnapshotRecords, read_records, read_json


def folder_path(uri):
    if not isinstance(uri, str):
        return ''
    parsed = urlparse(uri)
    if parsed.scheme != 'file':
        return uri
    path = unquote(parsed.path)
    if parsed.netloc:
        return '//' + parsed.netloc + path
    if len(path) > 2 and path[0] == '/' and path[2] == ':':
        path = path[1:]
    return path


def replay_operations(records, initial_state=None):
    state = initial_state
    for index, operation in records:
        kind = operation.get('kind')
        if kind == 0:
            if not isinstance(operation.get('v'), dict):
                raise SourceError(f'Invalid snapshot at line {index + 1}')
            state = deepcopy(operation['v'])
            continue
        if state is None:
            raise SourceError('Mutation precedes Initial snapshot')
        keys = operation.get('k')
        if not isinstance(keys, list) or not keys:
            raise SourceError(f'Invalid mutation path at line {index + 1}')
        try:
            parent = state
            for key in keys[:-1]:
                parent = parent[key]
            key = keys[-1]
            if kind == 1:
                parent[key] = deepcopy(operation.get('v'))
            elif kind == 2:
                array = parent.get(key, []) if isinstance(parent, dict) else parent[key]
                if not isinstance(array, list):
                    raise TypeError('Push target is not an array')
                values = operation.get('v') or []
                if not isinstance(values, list):
                    raise TypeError('Push values must be an array')
                if 'i' in operation:
                    start = operation['i']
                    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                        raise ValueError('Invalid array length')
                    if start > 1_000_000:
                        raise ValueError('Array length exceeds replay limit')
                    del array[start:]
                    if start > len(array):
                        array.extend([None] * (start - len(array)))
                array.extend(deepcopy(values))
                parent[key] = array
            elif kind == 3:
                if isinstance(parent, dict):
                    parent.pop(key, None)
                else:
                    parent[key] = None  # JS undefined; retain array position.
            else:
                raise ValueError(f'Unknown operation kind: {kind}')
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise SourceError(f'Invalid mutation at line {index + 1}: {exc}') from exc
    if state is None:
        raise SourceError('No complete snapshot yet')
    return state


def _markdown_text(chunk):
    value = chunk.get('value', chunk.get('content', ''))
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get('value'), str):
        return value['value']  # Serialized MarkdownString, not its JSON representation.
    return None


def _reference_label(chunk):
    """The visible chip label belongs in assistant prose, not a system event."""
    reference = chunk.get('inlineReference')
    for owner in (chunk, reference if isinstance(reference, dict) else {}):
        for key in ('name', 'label', 'title'):
            value = owner.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(reference, str) and reference.strip():
        return reference
    if isinstance(reference, dict):
        location = reference.get('location')
        uri = reference.get('uri')
        if uri is None and isinstance(location, dict):
            uri = location.get('uri')
        uri = uri if uri is not None else reference
        if isinstance(uri, dict):
            path = uri.get('fsPath') or uri.get('path')
            if isinstance(path, str) and path:
                return unquote(path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or path
            external = uri.get('external')
            if isinstance(external, str) and external:
                return external
        elif isinstance(uri, str) and uri:
            parsed = urlparse(uri)
            if parsed.scheme in ('', 'file'):
                return unquote(parsed.path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or uri
            return uri
    return None


def _native_call_id(key):
    # VS Code qualifies model call IDs with a transport timestamp. Strip only
    # this known namespace, not arbitrary suffixes or text-similar tool names.
    base, separator, suffix = key.rpartition('__vscode-')
    return base if separator and suffix.isdigit() else key


def _tool_definitions(metadata):
    definitions = {}
    for round_data in metadata.get('toolCallRounds') or []:
        if not isinstance(round_data, dict):
            continue
        for call in round_data.get('toolCalls') or []:
            if isinstance(call, dict):
                cid = call.get('id') or call.get('toolCallId')
                if cid:
                    definitions[cid] = call
    return definitions


def _arguments(call, fallback):
    import json
    function = call.get('function')
    value = call.get('arguments', function.get('arguments', fallback) if isinstance(function, dict) else fallback)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _result_content(value):
    if isinstance(value, dict) and isinstance(value.get('content'), list):
        result = []
        for part in value['content']:
            if isinstance(part, dict) and 'type' not in part and isinstance(part.get('value'), str):
                result.append({'type': 'text', 'text': part['value']})
            else:
                result.extend(blocks([part]))
        return result
    return value


def _result_summary(result):
    """Status/usage/error only. Never copy rounds, rendered context or tool results."""
    if not isinstance(result, dict):
        return {'details': text(result)[:4096], 'fullResultInRawRecord': True}
    summary = {'fullResultInRawRecord': True}
    for key in ('details', 'isIncomplete', 'isCancelled', 'isError', 'status'):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)):
            summary[key] = value[:4096] if isinstance(value, str) else value
    for key in ('errorDetails', 'error'):
        value = result.get(key)
        if isinstance(value, dict):
            summary[key] = {k: v[:8192] if isinstance(v, str) else v for k, v in value.items()
                            if k in ('message', 'code', 'name', 'responseIsIncomplete', 'isRateLimited')
                            and isinstance(v, (str, int, float, bool))}
        elif isinstance(value, str):
            summary[key] = value[:8192]
    timings = result.get('timings')
    if isinstance(timings, dict):
        summary['timings'] = {k: v for k, v in timings.items() if isinstance(v, (int, float))}
    metadata = result.get('metadata')
    if isinstance(metadata, dict):
        summary['usage'] = {k: metadata[k] for k in ('promptTokens', 'outputTokens', 'cachedPromptTokens',
                           'totalTokens', 'resolvedModel') if isinstance(metadata.get(k), (str, int, float))}
    return summary


class VSCodeChatAdapter(SourceAdapter):
    source = 'vscode_chat'
    label = 'VS Code Chat'
    parser_version = 3

    def discover(self):
        patterns = ['workspaceStorage/*/chatSessions/*.json', 'workspaceStorage/*/chatSessions/*.jsonl',
                    'globalStorage/emptyWindowChatSessions/*.json', 'globalStorage/emptyWindowChatSessions/*.jsonl',
                    'profiles/*/globalStorage/emptyWindowChatSessions/*.json',
                    'profiles/*/globalStorage/emptyWindowChatSessions/*.jsonl']
        for path in self.files(patterns):
            dependencies = ()
            if path.parent.name == 'chatSessions':
                workspace = path.parent.parent / 'workspace.json'
                # workspace.json is a sidecar inside the same authorized root.
                if workspace.exists() and any(workspace.resolve().is_relative_to(r) for r in self.roots if r.is_dir()):
                    dependencies = (workspace.resolve(),)
            yield SourceFile(path, dependencies=dependencies)

    def parse(self, entry):
        if entry.path.suffix == '.jsonl':
            records, warnings = read_records(entry.path)
            state = replay_operations(records)
        else:
            state = read_json(entry.path)
            records, warnings = SnapshotRecords.from_json(entry.path, state), []
        return self.project_state(entry, records, state, warnings)

    def project_state(self, entry, records, state, warnings=(), request_items=None, raw_overrides=None):
        project = ''
        if entry.dependencies:
            workspace = read_json(entry.dependencies[0], keep_bytes=True)
            records.dependencies[str(entry.dependencies[0])] = workspace.snapshot_bytes
            project = folder_path(workspace.get('folder') or workspace.get('workspace'))
        doc = SessionDocument(self.source, str(state.get('sessionId') or entry.path.stem), project,
                              records, name=str(state.get('customTitle') or ''), warnings=list(warnings))
        doc.metadata['formatVersion'] = state.get('version')
        requests = state.get('requests') or []
        if not isinstance(requests, list):
            raise SourceError('requests is not an array')
        # Keep raw provenance local to a request without copying raw snapshots into every message.
        last_operation = {}
        for index, row in records:
            keys = row.get('k', [])
            if len(keys) >= 2 and keys[0] == 'requests' and isinstance(keys[1], int):
                last_operation[keys[1]] = index
        doc.sync_state = {'kind': 'vscode_chat', 'header': {k: v for k, v in state.items() if k != 'requests'},
                          'requestCount': len(requests)}
        for request_index, request in (request_items if request_items is not None else enumerate(requests)):
            if not isinstance(request, dict):
                raise SourceError('Invalid request')
            raw = last_operation.get(request_index, (raw_overrides or {}).get(request_index, records[0][0] if records else 0))
            doc.scope_states[f'request:{request_index}'] = {'request': request, 'raw': raw}
            rid = str(request.get('requestId') or f'request-{request_index}')
            timestamp = request.get('timestamp', state.get('creationDate'))
            common = dict(timestamp=timestamp, timestamp_unit='ms', model=request.get('modelId'),
                          nativeType='request', sourceLocator={'requestIndex': request_index})
            user = request.get('message') or {}
            content = blocks(user.get('text')) if isinstance(user, dict) else blocks(user)
            if content:
                doc.add(raw, 'user', content, native_id=rid, **common)
            result = request.get('result') or {}
            metadata = result.get('metadata') if isinstance(result, dict) else None
            metadata = metadata if isinstance(metadata, dict) else {}
            mapped_results = metadata.get('toolCallResults') or {}
            mapped_results = mapped_results if isinstance(mapped_results, dict) else {}
            definitions = _tool_definitions(metadata)
            by_native_id = {}
            for key in mapped_results:
                by_native_id.setdefault(_native_call_id(key), []).append(key)
            used_results = set()
            response = request.get('response') or []
            if isinstance(response, str):
                response = [{'value': response}]
            if not isinstance(response, list):
                raise SourceError('Invalid response chunks')
            pending_text = []
            pending_indices = []
            pending_references = []
            unresolved_references = 0

            def flush_text():
                if not pending_indices:
                    return
                # No strip/join separator: these are fragments of one rendered
                # response, so original spaces/newlines are semantically significant.
                prose = ''.join(pending_text)
                if prose:
                    doc.add(raw, 'assistant', prose,
                            native_id=f'{rid}:response-{pending_indices[0]}',
                            sourceLocator={'requestIndex': request_index, 'chunkIndex': pending_indices[0],
                                           'chunkIndices': list(pending_indices)},
                            inlineReferences=list(pending_references), timestamp=timestamp,
                            timestamp_unit='ms', model=request.get('modelId'), nativeType='response_text')
                pending_text.clear()
                pending_indices.clear()
                pending_references.clear()

            for chunk_index, chunk in enumerate(response):
                if not isinstance(chunk, dict):
                    chunk = {'value': chunk}
                kind = chunk.get('kind', 'markdown')
                args = {**common, 'native_id': f'{rid}:response-{chunk_index}', 'nativeType': kind,
                        'sourceLocator': {'requestIndex': request_index, 'chunkIndex': chunk_index}}
                if kind in ('markdown', 'markdownContent') or 'kind' not in chunk:
                    fragment = _markdown_text(chunk)
                    if fragment is None:
                        fragment = '[unresolved markdown content]'
                        doc.warnings.append(f'Unsupported markdown content in request {request_index}, chunk {chunk_index}; retained in raw records')
                    pending_text.append(fragment)
                    pending_indices.append(chunk_index)
                    continue
                if kind == 'inlineReference':
                    label = _reference_label(chunk)
                    if label is None:
                        label = '[unresolved inline reference]'
                        unresolved_references += 1
                    pending_text.append(label)
                    pending_indices.append(chunk_index)
                    pending_references.append(compact_value({'chunkIndex': chunk_index, 'label': label,
                                               'reference': chunk.get('inlineReference'),
                                               'resolveId': chunk.get('resolveId')}))
                    continue
                flush_text()
                if kind == 'thinking':
                    doc.add(raw, 'assistant', [{'type': 'thinking', 'thinking': text(
                        chunk.get('value', chunk.get('text', chunk.get('thinking', ''))))}], kind='thinking', **args)
                elif kind == 'toolInvocationSerialized':
                    call_id = chunk.get('toolCallId')
                    candidates = ([call_id] if call_id in mapped_results else by_native_id.get(call_id, []))
                    result_key = candidates[0] if len(candidates) == 1 else None
                    definition = definitions.get(call_id, {})
                    specific = chunk.get('toolSpecificData')
                    fallback = specific if specific is not None else chunk.get('invocationMessage')
                    if isinstance(specific, dict) and specific.get('kind') == 'terminal':
                        fallback = {k: v for k, v in specific.items() if k in
                                    ('kind', 'command', 'commandLine', 'cwd', 'language', 'isBackground',
                                     'requestUnsandboxedExecution', 'requestAllowNetwork')}
                    doc.add(raw, 'assistant', [{'type': 'tool_use', 'id': call_id,
                            'name': definition.get('name', chunk.get('toolId', chunk.get('toolName', 'tool'))),
                            'input': _arguments(definition, fallback)}],
                            toolCallId=call_id, status=chunk.get('isComplete'), **args)
                    output = None
                    output_args = args
                    if result_key is not None:
                        used_results.add(result_key)
                        output = _result_content(mapped_results[result_key])
                        output_args = {**args, 'sourceLocator': {**args['sourceLocator'],
                                       'metadataToolCallResultKey': result_key}}
                    elif chunk.get('resultDetails') is not None:
                        output = chunk['resultDetails']
                    elif isinstance(specific, dict) and specific.get('terminalCommandOutput') is not None:
                        output = specific['terminalCommandOutput']
                    if output is not None:
                        doc.add(raw, 'tool', [{'type': 'tool_result', 'tool_use_id': call_id,
                                'content': output}], toolCallId=call_id, **output_args)
                else:
                    # Keep references, edits, questions and future chunk kinds as structured blocks.
                    doc.add(raw, 'system', [{'type': kind, 'value': chunk}], **args)
            flush_text()
            if unresolved_references:
                doc.warnings.append(f'{unresolved_references} inline references in request {request_index} have no readable label; placeholders and raw provenance retained')
            # Some UI calls use different IDs than the model. Preserve unpaired
            # outputs as separate, explicitly unpaired records instead of guessing.
            for key, value in mapped_results.items():
                if key in used_results:
                    continue
                native_id = _native_call_id(key)
                doc.add(raw, 'tool', [{'type': 'tool_result', 'tool_use_id': native_id,
                        'content': _result_content(value)}], native_id=f'{rid}:result:{key}',
                        toolCallId=native_id, unpaired=True,
                        sourceLocator={'requestIndex': request_index, 'metadataToolCallResultKey': key},
                        timestamp=timestamp, timestamp_unit='ms', model=request.get('modelId'),
                        nativeType='metadata_tool_result')
            if result:
                doc.add(raw, 'system', [{'type': 'request_result', 'value': _result_summary(result)}],
                        native_id=f'{rid}:result', **common)
        positions = {}
        for message in doc.messages:
            index = message['sourceLocator']['requestIndex']
            scope = f'request:{index}'
            position = positions.get(scope, 0)
            # Request-scoped identity stays collision-free when only one request
            # of a single-record JSON snapshot is regenerated.
            message['id'] = f'{doc.id}:request:{index}:{position}'
            doc.message_scopes[message['id']] = scope
            doc.message_order[message['id']] = (index, position)
            positions[scope] = position + 1
        return doc.finish()
