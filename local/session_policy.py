"""Session visibility/import policy. Forks are not subagents."""
from pathlib import Path
import json


def is_subagent(source, path, metadata=None, header=None):
    metadata = metadata or {}
    path = Path(path)
    parts = {part.lower() for part in path.parts}
    if metadata.get('relationship') == 'subagent':
        return True
    if 'subagents' in parts or 'subagent' in parts:
        return True
    if source == 'claude' and path.stem.startswith('agent-'):
        return True
    if not isinstance(header, dict):
        return False
    if source == 'codex':
        payload = header.get('payload') or {}
        if not isinstance(payload, dict):
            return False
        origin = payload.get('source')
        return bool(payload.get('parent_thread_id') or
                    isinstance(origin, dict) and 'subagent' in origin or
                    isinstance(origin, str) and origin.lower().startswith('subagent'))
    payload = header.get('data') if source == 'copilot_cli' else header
    payload = payload if isinstance(payload, dict) else {}
    context = payload.get('context') or {}
    return bool(payload.get('isSubagent') is True or payload.get('agentType') == 'subagent'
                or payload.get('parentAgentTaskId')
                or isinstance(context, dict) and context.get('isSubagent') is True)


def header_is_subagent(source, path):
    if is_subagent(source, path):
        return True
    if source not in ('codex', 'copilot_cli', 'pi') or Path(path).suffix != '.jsonl':
        return False
    # Read just the first nonempty header; never inspect the full transcript.
    with Path(path).open('rb') as stream:
        for line in stream:
            if line.strip():
                try:
                    header = json.loads(line.decode('utf-8-sig'))
                except (ValueError, UnicodeError):
                    return False  # The normal parser will report invalid input.
                return is_subagent(source, path, header=header)
    return False


def is_user_message(message):
    # Claude encodes tool results with role=user; normalized type distinguishes
    # them from actual user input. Do not count assistant/tool/system events.
    return message.get('type') == 'user' and message.get('role') == 'user'
