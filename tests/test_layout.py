"""Cross-project layout/data-safety checks, not application unit tests."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)


class RepositoryLayoutTests(unittest.TestCase):
    def test_three_projects_and_no_obsolete_runtime_directory(self):
        for name in ('server','deamon','local'):
            self.assertTrue((ROOT/name/'README.md').exists())
            self.assertTrue((ROOT/name/'requirements.txt').exists())
            self.assertTrue((ROOT/name/'tests').is_dir())
        for name in ('remote','distributed','deployment'):
            self.assertFalse((ROOT/name).exists())
        self.assertTrue((ROOT/'server/deploy/Dockerfile').exists())
        self.assertTrue((ROOT/'tests/e2e/local_browser.mjs').exists())

    def test_runtime_data_is_ignored_and_never_tracked(self):
        paths=['local/viewer.db','local/claude.db','local/viewer.db-wal','local/relocation-result.log',
               'server/state/cloud.db','server/deploy/secrets/owner.key','server/deploy/artifacts/report.json']
        result=subprocess.run(['git','check-ignore',*paths],cwd=ROOT,capture_output=True,text=True,creationflags=FLAGS)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(set(result.stdout.splitlines()),set(paths))
        result=subprocess.run(['git','ls-files','*.db','*.db-wal','*.db-shm','*.sqlite','*.sqlite3'],cwd=ROOT,capture_output=True,text=True,creationflags=FLAGS)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'')

    def test_client_contracts_do_not_import_server_or_local_runtime(self):
        code="import deamon.syncer,sys; assert 'sqlite3' not in sys.modules; assert 'flask' not in sys.modules; assert 'local' not in sys.modules; assert 'server.app' not in sys.modules"
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,creationflags=FLAGS)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
