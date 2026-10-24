"""Real local-server lifecycle: import completes BEFORE the port is opened."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class LocalServerIntegrationTests(unittest.TestCase):
    def test_cli_imports_before_listening_and_holds_a_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); history = root / 'history'; history.mkdir()
            rows = [{'type': 'session', 'version': 3, 'id': 'startup', 'cwd': '/synthetic'},
                    {'type': 'message', 'id': 'u', 'timestamp': '2026-10-23T01:02:03Z', 'message': {'role': 'user', 'content': 'synthetic question'}}]
            (history / 's.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            roots = root / 'roots.json'
            roots.write_text(json.dumps({'claude': [], 'codex': [], 'copilot_cli': [], 'vscode_chat': [], 'pi': [str(history)]}))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            database = root / 'state' / 'local.db'
            command = [sys.executable, '-u', '-m', 'local', '--port', str(port), '--include-recent', '--database', str(database),
                       '--roots-file', str(roots), '--credential-store', 'file']
            process = subprocess.Popen(command, cwd=ROOT, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=FLAGS)
            try:
                page = None
                for _ in range(200):
                    if process.poll() is not None:
                        self.fail(process.stdout.read().decode())
                    try:
                        with urllib.request.urlopen(f'http://localhost:{port}/api/sessions', timeout=.5) as response:
                            page = json.load(response)
                        break
                    except OSError:
                        time.sleep(.05)
                self.assertIsNotNone(page, 'Local server failed to start')
                self.assertEqual(page['total'], 1, 'first response already contains the startup import')
                with urllib.request.urlopen(f'http://localhost:{port}/api/cloud', timeout=2) as response:
                    console = json.load(response)
                self.assertFalse(console['configured']); self.assertEqual(console['upload']['state'], 'idle')
                second = subprocess.run(command[:5] + [str(port + 1)] + command[6:], cwd=ROOT, capture_output=True, text=True,
                                        timeout=30, creationflags=FLAGS)
                self.assertNotEqual(second.returncode, 0); self.assertIn('Another local server', second.stdout + second.stderr)
            finally:
                process.terminate(); process.wait(timeout=10); output = process.stdout.read().decode(errors='replace'); process.stdout.close()
            self.assertLess(output.index('import finished'), output.index('Agent Session Viewer (local)'))


if __name__ == '__main__':
    unittest.main()
