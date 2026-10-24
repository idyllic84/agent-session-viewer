"""Synthetic Docker smoke test: cloud container + a real local Web Server process.

Use only with an isolated local Compose project (e.g. claudeviewer-smoke). May
restart only explicitly named claudeviewer-smoke* projects. Never discovers
real Agent directories; never prints passwords or API keys.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid

import requests

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'server/deploy/docker-compose.local.yml'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def wait(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (requests.RequestException, AssertionError, KeyError, ValueError, IndexError):
            pass
        time.sleep(.5)
    raise RuntimeError('Smoke check timed out (no credentials or payloads logged)')


def run(origin, password_file, *, restart_project=None):
    parsed = urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('Smoke is restricted to an isolated loopback HTTP cloud')
    if restart_project and not restart_project.startswith('claudeviewer-smoke'):
        raise ValueError('Restart is allowed only for explicitly named claudeviewer-smoke* test projects')
    password = Path(password_file).read_text().strip(); origin = origin.rstrip('/')
    web = requests.Session(); web.trust_env = False

    def call(method, path, **kwargs):
        response = web.request(method, origin + path, timeout=10, **kwargs); response.raise_for_status(); return response

    def login():
        page = call('GET', '/auth/login')
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        response = call('POST', '/auth/login', data={'nonce': nonce, 'password': password}, headers={'Origin': origin}, allow_redirects=False)
        assert response.status_code == 302
        csrf = re.search(r'name="cloud-csrf" content="([^"]+)"', call('GET', '/').text).group(1)
        return {'Origin': origin, 'X-CSRF-Token': csrf}
    wait(lambda: call('GET', '/healthz').json().get('ok'))
    headers = login(); checks = ['health', 'owner password login']
    assert web.get(origin + '/ingest/v1/status', timeout=10).status_code == 401
    key = call('POST', '/api/keys', headers=headers, json={'name': 'Docker synthetic smoke'}).json()['key']
    checks.append('API key generated in the cloud console')
    sid = 'docker-smoke-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='viewer-docker-smoke-') as temp:
        temp = Path(temp); source = temp / 'claude'; source.mkdir(); history = source / 'history.jsonl'
        marker = 'SYNTHETIC_PROCESS_OMITTED_' + uuid.uuid4().hex
        rows = [{'type': 'user', 'sessionId': sid, 'uuid': 'u', 'timestamp': '2026-10-23T01:02:03Z', 'message': {'content': 'Docker synthetic question'}},
                {'type': 'assistant', 'uuid': 'a', 'timestamp': '2026-10-23T01:02:04Z', 'message': {'content': [{'type': 'text', 'text': 'Docker synthetic answer'},
                 {'type': 'tool_use', 'id': 'call', 'name': 'Agent', 'input': {'prompt': 'Allowed synthetic task'}}]}},
                {'type': 'progress', 'content': marker},
                {'type': 'user', 'uuid': 'result', 'timestamp': '2026-10-23T01:02:05Z', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': marker}]}}]
        history.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
        (source / 'agent-child.jsonl').write_text(json.dumps({'type': 'user', 'sessionId': 'child', 'message': {'content': marker}}) + '\n')
        roots = temp / 'roots.json'
        roots.write_text(json.dumps({'claude': [str(source)], 'codex': [], 'copilot_cli': [], 'pi': [], 'vscode_chat': []}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        local = f'http://localhost:{port}'; log = temp / 'local.log'
        command = [sys.executable, '-u', '-m', 'local', '--port', str(port), '--database', str(temp / 'state/local.db'),
                   '--roots-file', str(roots), '--quiet-seconds', '0', '--credential-store', 'file']
        lan = requests.Session(); lan.trust_env = False

        def start():
            output = log.open('ab')
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=FLAGS)
            output.close()
            page = wait(lambda: lan.get(local + '/', timeout=5))
            return process, {'X-CSRF-Token': re.search(r'name="local-csrf" content="([^"]+)"', page.text).group(1), 'Origin': local}

        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=10)

        def upload(local_headers):
            lan.post(local + '/api/cloud/upload/start', headers=local_headers, timeout=10).raise_for_status()
            state = wait(lambda: (lambda s: s if not s['upload']['running'] and s['upload']['state'] != 'idle' else None)(
                lan.get(local + '/api/cloud', timeout=10).json()))
            assert state['upload']['state'] == 'complete', state['upload'].get('error')
            return state
        process, local_headers = start()
        try:
            assert lan.get(local + '/api/sessions', timeout=10).json()['total'] == 1
            checks.append('local server imported history before listening (no cloud contact)')
            assert call('GET', '/api/sessions', params={'q': sid}).json()['total'] == 0
            response = lan.put(local + '/api/cloud/connection', headers=local_headers, json={'url': origin, 'key': key}, timeout=30)
            assert response.status_code == 200, response.text
            assert key not in lan.get(local + '/api/cloud', timeout=10).text
            assert call('GET', '/api/sessions', params={'q': sid}).json()['total'] == 0
            checks.append('console saves address + key (key never echoed); saving does not upload')
            upload(local_headers)
            session = call('GET', '/api/sessions', params={'q': sid}).json()['items'][0]
            assert session['messageCount'] == 4 and session['userMessageCount'] == 1 and session['uploadedBy'] == 'Docker synthetic smoke'
            cloud_sid = quote(session['id'], safe='')
            assert web.get(origin + f'/api/sessions/{cloud_sid}/records', timeout=10).status_code == 410
            messages = call('GET', f'/api/sessions/{cloud_sid}/messages').json()
            assert 'Allowed synthetic task' in json.dumps(messages) and marker not in json.dumps(messages)
            assert all(m['timestampSource'] == 'source' for m in messages['items'])
            checks.append('explicit upload: analysis only (prose, call, outcome, timestamps); no process/subagent bodies')
            if restart_project:
                env = {**os.environ, 'VIEWER_LOCAL_PORT': str(parsed.port or 80)}
                result = subprocess.run(['docker', 'compose', '-p', restart_project, '-f', str(COMPOSE), 'restart', 'viewer'],
                                        env=env, capture_output=True, text=True, creationflags=FLAGS)
                if result.returncode:
                    raise RuntimeError('Restart of isolated smoke project failed')
                wait(lambda: call('GET', '/healthz').json().get('ok'))
                assert call('GET', '/api/sources').status_code == 200
                checks.append('container restart preserves password, browser session, key and data')
            stop(process)
            with history.open('a') as stream:
                stream.write(json.dumps({'type': 'user', 'sessionId': sid, 'uuid': 'later', 'timestamp': '2026-10-23T01:03:00Z',
                                         'message': {'content': 'Synthetic incremental followup'}}) + '\n')
            process, local_headers = start()
            state = upload(local_headers)
            assert call('GET', f'/api/sessions/{cloud_sid}/messages').json()['total'] == 5
            assert state['upload']['batches'] == 1
            checks.append('local restart: incremental import then incremental upload, no duplicates')
        finally:
            stop(process)
        assert key.split('_', 2)[2] not in log.read_text(errors='replace')
        checks.append('no API key in local logs')
    return {'ok': True, 'origin': origin, 'checks': checks, 'syntheticOnly': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--origin', default='http://localhost:41881')
    parser.add_argument('--password-file', required=True, help='Private file holding the cloud owner password')
    parser.add_argument('--restart-project')
    parser.add_argument('--report')
    args = parser.parse_args(); result = run(args.origin, args.password_file, restart_project=args.restart_project)
    rendered = json.dumps(result, indent=2)
    if args.report:
        path = Path(args.report); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(rendered + '\n')
    print(rendered)


if __name__ == '__main__':
    main()
