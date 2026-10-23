"""Codex rollout projection: completed paginated items, legacy response items.

Execution completions are joined by call ID, never by text similarity. Context
snapshots remain in raw_records, not duplicated in the conversation projection.
"""
import json
from server.contracts.models import SessionDocument, blocks, text
from .base import SourceAdapter, SourceError, read_records


class CodexAdapter(SourceAdapter):
    source = 'codex'
    label = 'Codex'
    parser_version = 2

    def parse(self, entry):
        records, warnings = read_records(entry.path)
        return self.project_records(entry, records, warnings)

    def project_records(self, entry, records, warnings=(), policy=None):
        header = next((r.get('payload') for _, r in records if r.get('type') == 'session_meta'), None)
        if not isinstance(header, dict) or not header.get('id'):
            raise SourceError('Missing Codex thread metadata')
        doc = SessionDocument(self.source, str(header['id']), str(header.get('cwd') or ''), records,
                              warnings=warnings)
        mode = header.get('history_mode', 'legacy')
        if mode not in ('legacy', 'paginated'):
            raise SourceError(f'Unsupported Codex history mode: {mode}')
        doc.metadata.update(historyMode=mode, cliVersion=header.get('cli_version'),
                            rootSessionId=header.get('session_id'), historyBase=header.get('history_base'))
        source = header.get('source')
        subagent = source.get('subagent') if isinstance(source, dict) else None
        spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
        spawn = spawn if isinstance(spawn, dict) else {}
        agent_parent = header.get('parent_thread_id') or spawn.get('parent_thread_id')
        parent = agent_parent or header.get('forked_from_id')
        if parent:
            doc.metadata.update(parentSessionId=f'codex:{parent}',
                                relationship='subagent' if agent_parent else 'fork',
                                agentNickname=header.get('agent_nickname', spawn.get('agent_nickname')),
                                agentRole=header.get('agent_role', spawn.get('agent_role')))
        if header.get('history_base'):
            doc.warnings.append('Inherited history_base prefix is not expanded; showing locally retained records')
        start = header.get('subagent_history_start_ordinal')
        if start is not None:
            doc.metadata['inheritedContextBoundary'] = start
        # Migrated historical child logs may set the boundary to EOF even though
        # their completed events belong to this child. Explicit thread/turn
        # ownership is stronger evidence than that model-context boundary.
        owned_turns = {p.get('turn_id') for _, r in records
                       if r.get('type') == 'event_msg'
                       and isinstance((p := r.get('payload')), dict)
                       and p.get('type') == 'item_completed'
                       and p.get('thread_id') == header['id'] and p.get('turn_id')}
        local = []
        current_turn = None
        recovered = 0
        for index, row in records:
            payload = row.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            outer, event = row.get('type'), payload.get('type')
            if outer == 'turn_context' or (outer == 'event_msg' and event == 'task_started'):
                current_turn = payload.get('turn_id')
            explicit_thread = payload.get('thread_id') if outer == 'event_msg' else None
            foreign = explicit_thread is not None and explicit_thread != header['id']
            own = explicit_thread == header['id'] or payload.get('turn_id') in owned_turns
            in_owned_turn = current_turn in owned_turns
            beyond_boundary = start is None or row.get('ordinal', index) >= start
            if not foreign and (beyond_boundary or own or in_owned_turn):
                local.append((index, row))
                recovered += not beyond_boundary
            if outer == 'event_msg' and event in ('task_complete', 'turn_aborted'):
                current_turn = None
        if recovered:
            doc.metadata['recoveredPreBoundaryRecords'] = recovered
        completed = []
        calls = {}
        for index, row in local:
            payload = row.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            if row.get('type') == 'event_msg' and payload.get('type') == 'item_completed':
                completed.append((index, row, payload.get('item') or {}))
            if row.get('type') == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call'):
                if payload.get('call_id'):
                    calls[payload['call_id']] = index
        canonical_messages = mode == 'paginated' and any(
            item.get('type') in ('UserMessage', 'AgentMessage') for _, _, item in completed)
        canonical_reasoning = mode == 'paginated' and any(item.get('type') == 'Reasoning' for _, _, item in completed)
        has_compactions = any(r.get('type') == 'compacted' for _, r in local)
        if policy is not None:
            canonical_messages = policy.get('messages', canonical_messages)
            canonical_reasoning = policy.get('reasoning', canonical_reasoning)
            has_compactions = policy.get('compactions', has_compactions)
        doc.sync_state['policy'] = {'messages': canonical_messages, 'reasoning': canonical_reasoning,
                                    'compactions': has_compactions}
        raw_messages = any(r.get('type') == 'response_item' and
                           (r.get('payload') or {}).get('type') == 'message' for _, r in local)
        model = None
        call_messages = {}
        pending_completions = {}
        for index, row in local:
            outer = row.get('type')
            payload = row.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            kind = payload.get('type')
            ts = row.get('timestamp')
            if outer == 'turn_context':
                model = payload.get('model', model)
                continue
            if outer == 'response_item':
                if kind == 'message' and not canonical_messages:
                    role = payload.get('role', 'system')
                    # developer/system instructions are raw context, not user conversation.
                    if role in ('user', 'assistant'):
                        doc.add(index, role, blocks(payload.get('content')), native_id=payload.get('id'),
                                timestamp=ts, model=model, nativeType=kind)
                elif kind in ('function_call', 'custom_tool_call'):
                    arguments = payload.get('arguments', payload.get('input', {}))
                    if kind == 'function_call' and isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            pass
                    call_id = payload.get('call_id')
                    m = doc.add(index, 'assistant', [{'type': 'tool_use', 'id': call_id,
                                'name': payload.get('name'), 'input': arguments}],
                                native_id=payload.get('id') or call_id, timestamp=ts, model=model,
                                toolCallId=call_id, nativeType=kind)
                    if call_id:
                        call_messages[call_id] = m
                elif kind in ('function_call_output', 'custom_tool_call_output'):
                    call_id = payload.get('call_id')
                    doc.add(index, 'tool', [{'type': 'tool_result', 'tool_use_id': call_id,
                            'content': payload.get('output')}], timestamp=ts, native_id=payload.get('id'),
                            toolCallId=call_id, nativeType=kind)
                elif kind == 'reasoning' and not canonical_reasoning:
                    clear = []
                    for b in (payload.get('summary') or []) + (payload.get('content') or []):
                        if isinstance(b, dict) and b.get('text'):
                            clear.append(text(b['text']))
                    if clear:
                        doc.add(index, 'assistant', [{'type': 'thinking', 'thinking': '\n'.join(clear)}],
                                timestamp=ts, kind='thinking', native_id=payload.get('id'), nativeType=kind)
            elif outer == 'event_msg' and kind == 'item_completed':
                item = payload.get('item') or {}
                if not isinstance(item, dict):
                    raise SourceError(f'Invalid completed item at line {index + 1}')
                itype, eid = item.get('type'), item.get('id')
                args = dict(native_id=eid, timestamp=ts or payload.get('completed_at_ms'), timestamp_unit='ms',
                            turnId=payload.get('turn_id'),
                            model=model, nativeType=itype)
                if itype in ('UserMessage', 'AgentMessage') and (canonical_messages or not raw_messages):
                    doc.add(index, 'user' if itype == 'UserMessage' else 'assistant',
                            blocks(item.get('content')), **args)
                elif itype == 'Reasoning' and canonical_reasoning:
                    clear = item.get('summary_text') or item.get('raw_content') or []
                    doc.add(index, 'assistant', [{'type': 'thinking', 'thinking':
                            '\n'.join(text(v) for v in clear) if isinstance(clear, list) else text(clear)}],
                            kind='thinking', **args)
                elif itype in ('CommandExecution', 'FileChange', 'McpToolCall', 'DynamicToolCall'):
                    if eid in calls:
                        pending_completions[eid] = (index, item.get('status'))
                    else:
                        name = item.get('tool') or ('shell' if itype == 'CommandExecution' else itype)
                        doc.add(index, 'assistant', [{'type': 'tool_use', 'id': eid, 'name': name,
                                'input': item.get('arguments', item.get('changes', {'command': item.get('command')}))}],
                                toolCallId=eid, **args)
                        output = item.get('aggregated_output', item.get('stdout', item.get('result')))
                        doc.add(index, 'tool', [{'type': 'tool_result', 'tool_use_id': eid,
                                'content': output, 'exitCode': item.get('exit_code')}],
                                status=item.get('status'), toolCallId=eid, **args)
                elif itype == 'ContextCompaction':
                    if not has_compactions:
                        doc.add(index, 'system', 'Context compacted (original history retained)', kind='summary', **args)
                elif itype not in ('UserMessage', 'AgentMessage', 'Reasoning'):
                    doc.add(index, 'system', f'Completed item: {itype}', **args)
            elif outer == 'compacted':
                doc.add(index, 'system', payload.get('message', 'Context compacted'), kind='summary', timestamp=ts,
                        nativeType='compacted')
            elif outer == 'event_msg' and kind in ('task_started', 'task_complete', 'turn_aborted'):
                doc.add(index, 'system', kind, timestamp=ts, nativeType=kind, turnId=payload.get('turn_id'))
            elif outer == 'token_usage_record':
                doc.metadata['latestUsageRecord'] = {k: payload.get(k) for k in
                    ('usage', 'turn_token_usage', 'thread_token_usage', 'turn_id', 'response_id')}
        for call_id, (index, status) in pending_completions.items():
            if call_id in call_messages:
                call_messages[call_id]['relatedRawRecords'].append(index)
                call_messages[call_id]['status'] = status
        return doc.finish()
