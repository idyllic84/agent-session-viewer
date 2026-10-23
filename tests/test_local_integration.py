"""Real local-server lifecycle integration belongs at repository root."""
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


class LocalServerIntegrationTests(unittest.TestCase):
    def test_cli_syncs_before_listening_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[{'type':'session','version':3,'id':'startup','cwd':'/synthetic'},
                  {'type':'message','id':'u','message':{'role':'user','content':'synthetic question'}}]
            (root/'s.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env={**os.environ,'VIEWER_DB':str(root/'cli.db'),'VIEWER_SOURCE_ROOTS':json.dumps({
                'claude':[],'codex':[],'copilot_cli':[],'vscode_chat':[],'pi':[str(root)]})}
            process=subprocess.Popen([sys.executable,'-u','-m','local.server','--port',str(port),'--include-recent'],
                cwd=Path(__file__).resolve().parents[1],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                page=None
                for _ in range(100):
                    if process.poll() is not None:self.fail(process.stdout.read().decode())
                    try:
                        with urllib.request.urlopen(f'http://localhost:{port}/api/sessions',timeout=.5) as response:page=json.load(response)
                        break
                    except OSError:time.sleep(.05)
                self.assertIsNotNone(page,'Local CLI failed to start');self.assertEqual(page['total'],1)
                with urllib.request.urlopen(f'http://localhost:{port}/api/sync/status',timeout=2) as response:status=json.load(response)
                self.assertFalse(status['running']);self.assertEqual(status['result']['added'],1)
            finally:
                process.terminate();process.wait(timeout=10);process.stdout.close()


if __name__=='__main__':unittest.main()
