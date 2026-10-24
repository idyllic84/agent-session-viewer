from server.contracts.models import SessionDocument, blocks
from .base import SourceAdapter, SourceFile, SourceError, read_records


class CopilotCLIAdapter(SourceAdapter):
    source = 'copilot_cli'
    label = 'Copilot CLI'

    def discover(self):
        return (SourceFile(path) for path in self.files(['**/events.jsonl', '*.jsonl']))

    def parse(self, entry):
        records, warnings = read_records(entry.path)
        return self.project_records(entry, records, warnings)

    def project_records(self, entry, records, warnings=()):
        header = next((r.get('data') for _, r in records if r.get('type') == 'session.start'), None)
        if not isinstance(header, dict) or not header.get('sessionId'):
            raise SourceError('Missing Copilot session.start')
        context = header.get('context') or {}
        doc = SessionDocument(self.source, str(header['sessionId']), str(context.get('cwd') or ''),
                              records, warnings=warnings)
        doc.metadata.update(copilotVersion=header.get('copilotVersion'), repository=context.get('repository'))
        model = None
        calls = {}
        lifecycles = {}
        requested = {call.get('toolCallId') for _, row in records if row.get('type') == 'assistant.message'
                     for call in (row.get('data') or {}).get('toolRequests', []) if isinstance(call, dict)}
        for index, row in records:
            kind = row.get('type', 'unknown')
            data = row.get('data') or {}
            if not isinstance(data, dict):
                raise SourceError(f'Invalid Copilot event data at line {index + 1}')
            args = dict(native_id=row.get('id'), parent_id=row.get('parentId'),
                        timestamp=row.get('timestamp'), nativeType=kind, turnId=data.get('turnId'))
            if kind == 'session.model_change':
                model = data.get('newModel', model)
            if kind == 'user.message':
                doc.add(index, 'user', blocks(data.get('content')), **args)
            elif kind == 'assistant.message':
                content = blocks(data.get('content'))
                if data.get('reasoningText'):
                    content.insert(0, {'type': 'thinking', 'thinking': data['reasoningText']})
                for call in data.get('toolRequests') or []:
                    if not isinstance(call, dict):
                        continue
                    content.append({'type': 'tool_use', 'id': call.get('toolCallId'),
                                    'name': call.get('name'), 'input': call.get('arguments', {})})
                m = doc.add(index, 'assistant', content, model=data.get('model', model), **args)
                for b in content:
                    if b.get('type') == 'tool_use' and b.get('id'):
                        calls[b['id']] = m
            elif kind == 'tool.execution_start':
                call_id = data.get('toolCallId')
                lifecycles.setdefault(call_id, []).append((index, 'started'))
                if call_id not in requested and call_id not in calls:
                    m = doc.add(index, 'assistant', [{'type': 'tool_use', 'id': call_id,
                            'name': data.get('toolName'), 'input': data.get('arguments', {})}],
                            toolCallId=call_id, status='started', model=model, **args)
                    if call_id:
                        calls[call_id] = m
            elif kind == 'tool.execution_complete':
                call_id = data.get('toolCallId')
                doc.add(index, 'tool', [{'type': 'tool_result', 'tool_use_id': call_id,
                        'content': data.get('result'), 'is_error': data.get('success') is False}],
                        toolCallId=call_id, status='completed' if data.get('success') else 'error', **args)
                lifecycles.setdefault(call_id, []).append((index, 'completed' if data.get('success') else 'error'))
            elif kind == 'assistant.reasoning':
                doc.add(index, 'assistant', [{'type': 'thinking', 'thinking': data.get('content', '')}],
                        kind='thinking', **args)
            elif not kind.startswith('model.'):
                # Lifecycle/permission/unknown events stay inspectable without replaying snapshots.
                doc.add(index, 'system', data.get('content') or data.get('message') or kind,
                        model=model, **args)
        for call_id, events in lifecycles.items():
            if call_id in calls:
                message = calls[call_id]
                message['relatedRawRecords'] = [index for index, _ in events if index != message['rawRecord']]
                message['status'] = events[-1][1]
                for block in message['message']['content']:
                    if block.get('type') == 'tool_use' and block.get('id') == call_id:
                        block['status'] = events[-1][1]
        return doc.finish()
