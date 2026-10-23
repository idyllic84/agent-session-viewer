"""Source-independent archive model. Original records are stored separately."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re

_OPAQUE_FIELDS = {'encrypted', 'encrypted_content', 'encryptedContent', 'reasoningOpaque', 'signature'}
_BINARY_TYPES = {'image', 'input_image', 'output_image', 'image_url', 'input_audio', 'audio', 'video', 'Buffer'}
_DATA_URI = re.compile(r'data:[a-zA-Z0-9.+/-]+(?:;[a-zA-Z0-9=.+_-]+)*;base64,[a-zA-Z0-9+/=_-]+')
_ENCODED_JSON_FIELDS = re.compile(r'"(?:mimeType|mime_type|media_type|encrypted|encrypted_content|encryptedContent|reasoningOpaque)"\s*:|"(?:type|encoding)"\s*:\s*"(?:base64|Buffer)"')


def timestamp_ms(value, unit=None):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        try:
            return int(value if unit == 'ms' or (unit is None and abs(value) >= 100_000_000_000) else value * 1000)
        except (ValueError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def text(value):
    if value is None:
        return ''
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def blocks(content):
    """Normalize typed content without discarding unknown or non-text blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        return [{'type': 'unknown', 'value': content}]
    result = []
    for block in content:
        if not isinstance(block, dict):
            result.append({'type': 'unknown', 'value': block})
            continue
        kind = block.get('type')
        if kind in ('text', 'Text', 'input_text', 'output_text'):
            result.append({'type': 'text', 'text': text(block.get('text', ''))})
        elif kind in ('thinking', 'reasoning'):
            result.append({'type': 'thinking', 'thinking': text(block.get('thinking', block.get('text', '')))})
        elif kind in ('toolCall', 'tool_use'):
            result.append({'type': 'tool_use', 'id': block.get('id'), 'name': block.get('name'),
                           'input': block.get('arguments', block.get('input', {}))})
        else:
            result.append(dict(block))
    return result


def compact_value(value):
    """Keep human-readable content; binary/opaque payloads remain only in raw records."""
    if isinstance(value, str):
        rendered = _DATA_URI.sub('[binary data retained in original record]', value)
        # Tool renderers sometimes wrap typed image/result objects in a JSON
        # string. Do not let their nested base64/opaque fields evade filtering.
        if rendered.lstrip().startswith(('{', '[')) and _ENCODED_JSON_FIELDS.search(rendered):
            try:
                structured = json.loads(rendered)
            except (ValueError, RecursionError):
                return rendered
            if isinstance(structured, (dict, list)):
                cleaned = compact_value(structured)
                if cleaned != structured:
                    return json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'))
        return rendered
    if isinstance(value, list):
        return [compact_value(v) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get('_omitted'):
        return dict(value)
    kind = value.get('type', value.get('kind'))
    mime = value.get('mimeType', value.get('mime_type', value.get('media_type', '')))
    encoded = kind == 'base64' or value.get('encoding') == 'base64'
    binary = (isinstance(kind, str) and kind in _BINARY_TYPES) or encoded or (
        isinstance(mime, str) and mime.startswith(('image/', 'audio/', 'video/')))
    result = {}
    for key, child in value.items():
        opaque = key in _OPAQUE_FIELDS and not isinstance(child, (bool, int, float, type(None)))
        binary_payload = binary and key in ('data', 'bytes', 'buffer', 'value', 'url', 'image_url')
        if opaque or binary_payload:
            result[key] = {'_omitted': 'opaque' if opaque else 'binary',
                           'size': len(child) if isinstance(child, (str, bytes, list)) else None,
                           'retainedInRawRecord': True}
        else:
            result[key] = compact_value(child)
    return result


def _search_value(value):
    if isinstance(value, str):
        return _DATA_URI.sub('', value)
    if isinstance(value, list):
        return '\n'.join(_search_value(v) for v in value)
    if isinstance(value, dict):
        if value.get('_omitted'):
            return ''
        parts = []
        for key, child in value.items():
            if key in _OPAQUE_FIELDS or key in ('type', 'kind', '$mid'):
                continue
            rendered = _search_value(child)
            if rendered:
                parts.append(f'{key}: {rendered}')
        return '\n'.join(parts)
    return '' if value is None or isinstance(value, bool) else str(value)


def searchable_text(content):
    parts = []
    for block in compact_value(content):
        kind = block.get('type')
        if kind == 'text':
            parts.append(_search_value(block.get('text')))
        elif kind == 'thinking':
            parts.append(_search_value(block.get('thinking')))
        elif kind == 'tool_use':
            parts.extend([text(block.get('name')), _search_value(block.get('input'))])
        elif kind == 'tool_result':
            parts.append(_search_value(block.get('content')))
        elif kind == 'request_result':
            summary = block.get('value') or {}
            if isinstance(summary, dict):
                parts.extend(_search_value(summary.get(k)) for k in ('errorDetails', 'error'))
        elif kind not in _BINARY_TYPES and kind not in ('encrypted_reasoning', 'request_result'):
            parts.append(_search_value(block))
    return '\n'.join(p for p in parts if p)


def indexed_message_text(message):
    """Session search indexes conversation prose only, not events/tool payloads."""
    if message.get('role') not in ('user', 'assistant'):
        return ''
    content = message.get('message', {}).get('content') or []
    if isinstance(content, str):
        content = blocks(content)
    readable = [block for block in content if isinstance(block, dict) and block.get('type') in ('text', 'thinking')]
    return searchable_text(readable)


@dataclass
class SessionDocument:
    source: str
    external_id: str
    project: str
    records: list
    name: str = ''
    messages: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    append_base: dict = field(default_factory=dict)
    sync_state: dict = field(default_factory=dict)
    scope_states: dict = field(default_factory=dict)
    raw_scopes: dict = field(default_factory=dict)
    message_scopes: dict = field(default_factory=dict)
    message_order: dict = field(default_factory=dict)
    changed_scopes: set = field(default_factory=set)
    raw_snapshot_replace: bool = False
    _subindices: dict = field(default_factory=dict, init=False, repr=False)

    @property
    def id(self):
        return f'{self.source}:{self.external_id}'

    def add(self, record_index, role, content, *, native_id=None, parent_id=None,
            timestamp=None, timestamp_unit=None, kind=None, **metadata):
        content = compact_value(blocks(content))
        if kind is None:
            types = {b.get('type') for b in content}
            kind = 'tool-result' if types == {'tool_result'} else (
                'tool-use' if types == {'tool_use'} else role)
        subindex = self._subindices.get(record_index, 0)
        self._subindices[record_index] = subindex + 1
        message = {
            'id': f'{self.id}:{record_index}:{subindex}', 'sessionId': self.id,
            'source': self.source, 'type': kind, 'role': role,
            'uuid': str(native_id) if native_id is not None else f'record-{record_index}',
            'parentUuid': parent_id, 'timestamp': timestamp_ms(timestamp, timestamp_unit),
            'timestampUnit': 'ms', 'message': {'role': role, 'content': content},
            'rawRecord': record_index, 'relatedRawRecords': [], **metadata,
        }
        self.messages.append(message)
        return message

    def finish(self):
        if not self.name:
            for m in self.messages:
                if m['type'] == 'user':
                    preview = searchable_text(m['message']['content']).strip()
                    if preview:
                        self.name = preview.replace('\n', ' ')[:160]
                        break
        self.name = self.name or self.external_id
        return self
