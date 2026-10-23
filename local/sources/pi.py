from ..models import SessionDocument, blocks, searchable_text
from .base import SourceAdapter, SourceError, read_records


class PiAdapter(SourceAdapter):
    source = 'pi'
    label = 'pi'

    def parse(self, entry):
        records, warnings = read_records(entry.path)
        return self.project_records(entry, records, warnings)

    def project_records(self, entry, records, warnings=(), seed=None):
        if seed is None and (not records or records[0][1].get('type') != 'session'):
            raise SourceError('Missing pi session header')
        header = seed['header'] if seed else records[0][1]
        version = header.get('version', 1)
        if version not in (1, 2, 3):
            raise SourceError(f'Unsupported pi format version: {version}')
        doc = SessionDocument(self.source, str(header.get('id') or entry.path.stem),
                              str(header.get('cwd') or ''), records, warnings=list(warnings),
                              name=seed.get('name', '') if seed else '')
        doc.metadata.update(formatVersion=version, parentSessionPath=header.get('parentSession'))
        contexts = dict(seed.get('contexts', {})) if seed else {}
        previous = seed.get('previous') if seed else None
        fallback_name = seed.get('fallbackName', '') if seed else ''
        for index, row in (records if seed else records[1:]):
            kind = row.get('type', 'unknown')
            eid = row.get('id') or f'legacy-{index}'
            parent = row.get('parentId', previous if version == 1 else None)
            previous = eid
            model, provider = contexts.get(parent, (None, None))
            if kind == 'model_change':
                model, provider = row.get('modelId'), row.get('provider')
            if kind == 'session_info':
                doc.name = str(row.get('name') or '')
            if kind == 'message':
                native = row.get('message')
                if not isinstance(native, dict):
                    raise SourceError(f'Invalid pi message at line {index + 1}')
                role = native.get('role', 'unknown')
                content = blocks(native.get('content'))
                if role == 'toolResult':
                    role = 'tool'
                    content = [{'type': 'tool_result', 'tool_use_id': native.get('toolCallId'),
                                'name': native.get('toolName'), 'content': content,
                                'is_error': native.get('isError'), 'details': native.get('details')}]
                elif role == 'bashExecution':
                    role = 'system'
                    content = [{'type': 'tool_use', 'name': 'bash', 'input': {'command': native.get('command')}},
                               {'type': 'tool_result', 'content': native.get('output'),
                                'exitCode': native.get('exitCode')}]
                model, provider = native.get('model', model), native.get('provider', provider)
                doc.add(index, role, content, native_id=eid, parent_id=parent,
                        timestamp=row.get('timestamp', native.get('timestamp')),
                        timestamp_unit=None if 'timestamp' in row else 'ms',
                        model=model, provider=provider,
                        usage=native.get('usage'), status=native.get('stopReason'), nativeType=kind)
            else:
                content = row.get('summary', row.get('content', ''))
                if not content and kind in ('model_change', 'thinking_level_change', 'session_info', 'label'):
                    content = {k: v for k, v in row.items() if k not in ('id', 'parentId', 'timestamp', 'type')}
                doc.add(index, 'system', content, kind='summary' if kind in ('compaction', 'branch_summary') else 'system',
                        native_id=eid, parent_id=parent, timestamp=row.get('timestamp'), nativeType=kind,
                        model=model, provider=provider)
            contexts[eid] = (model, provider)
        doc.metadata['defaultLeaf'] = previous
        if not fallback_name:
            fallback_name = next((searchable_text(m['message']['content']).replace('\n', ' ')[:160]
                                  for m in doc.messages if m['type']=='user'), doc.external_id)
        if not doc.name:
            doc.name = fallback_name
        doc.finish()
        doc.sync_state = {'kind': 'pi', 'header': header, 'contexts': contexts, 'previous': previous,
                          'name': doc.name, 'fallbackName': fallback_name}
        return doc
