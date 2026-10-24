"""Synthetic local + cloud servers for the optional dependency-free Edge smoke test.

Prints only the local URL. The synthetic cloud origin and its throwaway API key
are written to files in the temporary directory (never printed).
"""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from threading import Thread
from local.app import create_app
from local.archive import LocalArchive
from local.service import LocalService
from server.app import create_app as create_cloud
from server.archive import CloudArchive
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
    cloud = CloudArchive(root / 'cloud' / 'cloud.db'); cloud.set_password('synthetic owner password', require_current=False)
    cloud_server = make_server('127.0.0.1', 0, None, threaded=True)
    origin = f'http://127.0.0.1:{cloud_server.server_port}'
    cloud_server.app = create_cloud(cloud, session_secret='s' * 48, public_origin=origin, allow_http=True)
    Thread(target=cloud_server.serve_forever, daemon=True).start()
    (root / 'cloud-origin.txt').write_text(origin)
    (root / 'cloud-key.txt').write_text(cloud.create_key('browser fixture')['key'])
    service = LocalService(LocalArchive(root / 'state' / 'local.db'), adapters, quiet_seconds=0, credential_backend='file',
                           log=lambda *_: None)
    service.import_history()
    app = create_app(service)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
