"""Synthetic local server for the optional dependency-free Edge smoke test."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local.server import create_app
from local.service import ClaudeViewerService
from local.sources.pi import PiAdapter
from local.sources.claude import ClaudeAdapter
from local.sources.codex import CodexAdapter
from local.sources.copilot_cli import CopilotCLIAdapter
from local.sources.vscode_chat import VSCodeChatAdapter
from werkzeug.serving import make_server


def main():
    root = Path(sys.argv[1])
    pi = root / 'pi'; pi.mkdir()
    records = [{'type': 'session', 'version': 3, 'id': 'pi-test', 'cwd': 'C:/synthetic/project'},
               {'type': 'message', 'id': 'u', 'parentId': None,
                'message': {'role': 'user', 'content': '<img src=x onerror="window.__xss=1"> synthetic question'}}]
    parent = 'u'
    for i in range(2005):
        content = [{'type': 'text', 'text': f'assistant answer {i}'}]
        if i == 0:
            content += [{'type': 'thinking', 'thinking': 'synthetic reasoning'},
                        {'type': 'toolCall', 'id': 'call', 'name': 'bash', 'arguments': {'command': 'echo test'}}]
        records.append({'type': 'message', 'id': f'a{i}', 'parentId': parent,
                        'message': {'role': 'assistant', 'content': content}})
        parent = f'a{i}'
    records.append({'type': 'message', 'id': 'alternate', 'parentId': 'u',
                    'message': {'role': 'assistant', 'content': 'alternate saved branch'}})
    (pi / 's.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
    claude = root / 'claude'; claude.mkdir()
    (claude / 's.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': 'claude-test',
        'cwd': 'C:/synthetic/other', 'message': {'content': 'claude question'}}) + '\n', encoding='utf-8')
    vscode = root / 'vscode'
    chat = vscode / 'workspaceStorage/ws/chatSessions/inline.json'
    chat.parent.mkdir(parents=True)
    chat.write_text(json.dumps({'sessionId': 'inline-test', 'requests': [{'message': {'text': 'inline reference fixture'},
        'response': [{'kind': 'markdown', 'value': '用 '},
                     {'kind': 'inlineReference', 'inlineReference': {'name': 'event.item.model_dump()'}},
                     {'kind': 'markdown', 'value': ' 转发上游 '},
                     {'kind': 'inlineReference', 'inlineReference': {'name': 'conversation.item.added'}}]}]},
        ensure_ascii=False), encoding='utf-8')
    (claude / 'agent-child.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': 'claude-test',
        'message': {'content': 'subagent should not be indexed'}}) + '\n', encoding='utf-8')
    empty = chat.parent / 'empty.json'
    empty.write_text(json.dumps({'sessionId': 'empty', 'requests': []}), encoding='utf-8')
    adapters = [PiAdapter([pi]), ClaudeAdapter([claude]), CodexAdapter([]), CopilotCLIAdapter([]), VSCodeChatAdapter([vscode])]
    service = ClaudeViewerService(root / 'archive.db', adapters, skip_recent_seconds=0)
    service.initialize(); service.sync_sessions()
    app = create_app(service)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
