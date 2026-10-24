"""Cross-project layout/data-safety checks, not application unit tests."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class RepositoryLayoutTests(unittest.TestCase):
    def test_two_web_servers_and_no_retired_runtime_directories(self):
        for name in ('server', 'local'):
            self.assertTrue((ROOT / name / 'README.md').exists())
            self.assertTrue((ROOT / name / 'requirements.txt').exists())
            self.assertTrue((ROOT / name / 'tests').is_dir())
        for name in ('deamon', 'remote', 'distributed', 'deployment'):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertTrue((ROOT / 'server/deploy/docker-compose.yml').exists())
        self.assertTrue((ROOT / 'server/deploy/docker-compose.local.yml').exists())
        self.assertFalse(any((ROOT / 'local').glob('Dockerfile*')), 'the local server is not containerised')
        self.assertTrue((ROOT / 'docs/two-web-servers-design.md').exists())

    def test_runtime_data_is_ignored_and_never_tracked(self):
        paths = ['local/viewer.db', 'local/claude.db', 'local/viewer.db-wal', 'local/relocation-result.log',
                 'local/state/local.db', 'local/state/credentials/cloud-api-key-x.key', 'local/state/local.db.lock',
                 'server/state/cloud.db', 'server/deploy/secrets/admin.password', 'server/deploy/artifacts/report.json']
        result = subprocess.run(['git', 'check-ignore', *paths], cwd=ROOT, capture_output=True, text=True, creationflags=FLAGS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), set(paths))
        result = subprocess.run(['git', 'ls-files', '*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.sqlite3', '*.key'],
                                cwd=ROOT, capture_output=True, text=True, creationflags=FLAGS)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout.strip(), '')

    def test_contracts_and_local_server_do_not_import_cloud_runtime(self):
        code = ("import server.contracts.protocol,sys; assert 'sqlite3' not in sys.modules and 'flask' not in sys.modules; "
                "import local.service,local.app; assert 'server.app' not in sys.modules and 'server.archive' not in sys.modules "
                "and 'server.db' not in sys.modules")
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, creationflags=FLAGS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cloud_image_does_not_include_local_server(self):
        dockerfile = (ROOT / 'server/deploy/Dockerfile').read_text()
        self.assertNotIn('COPY local', dockerfile)


if __name__ == '__main__':
    unittest.main()
