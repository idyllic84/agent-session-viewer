"""Console logs must explain unchanged files and expensive steps, not just writes."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from local.service import ClaudeViewerService
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter


class SyncLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'session.jsonl'
        self.path.write_text(json.dumps({'type':'user','sessionId':'s','message':{'content':'PRIVATE_CONTENT_NOT_FOR_LOGS'}})+'\n',encoding='utf-8')
        self.adapter=ClaudeAdapter([self.root]);self.viewer=ClaudeViewerService(self.root/'archive.db',[self.adapter],skip_recent_seconds=0)
        self.viewer.initialize()

    def test_unchanged_file_has_start_end_status_and_duration_without_transcript(self):
        with redirect_stdout(io.StringIO()):self.viewer.sync_sessions()
        output=io.StringIO()
        with redirect_stdout(output):result=self.viewer.sync_sessions()
        log=output.getvalue()
        self.assertEqual(result['unchanged'],1)
        self.assertIn('[sync] file 1 start: Claude Code / session.jsonl',log)
        self.assertIn('[sync] file 1 end: unchanged',log)
        self.assertIn('Sync complete:',log)
        self.assertNotIn('PRIVATE_CONTENT_NOT_FOR_LOGS',log)

    def test_recent_and_failed_files_also_have_final_status(self):
        self.viewer.skip_recent_seconds=3600
        output=io.StringIO()
        with redirect_stdout(output):result=self.viewer.sync_sessions()
        self.assertEqual(result['skippedRecent'],1)
        self.assertIn('end: skipped recent',output.getvalue())
        self.viewer.skip_recent_seconds=0
        self.path.write_bytes(b'invalid JSON\n')
        output=io.StringIO()
        with redirect_stdout(output):result=self.viewer.sync_sessions()
        self.assertEqual(result['failed'],1)
        self.assertIn('[sync] failed:',output.getvalue())
        self.assertIn('end: failed',output.getvalue())

    def test_slow_hash_emits_heartbeat_with_stage_and_file(self):
        observed=Event()
        class Output(io.StringIO):
            def write(self, value):
                result=super().write(value)
                if '[sync] working:' in value and 'step=hash file' in value:observed.set()
                return result
        output=Output();original=SourceFile.fingerprint
        def wait_for_heartbeat(entry):
            if not observed.wait(2):raise AssertionError('Missing heartbeat while hashing')
            return original(entry)
        self.viewer._heartbeat_seconds=.01
        with redirect_stdout(output),patch.object(SourceFile,'fingerprint',side_effect=wait_for_heartbeat,autospec=True):
            result=self.viewer.sync_sessions()
        self.assertEqual(result['failed'],0)
        self.assertIn('step=hash file',output.getvalue())
        self.assertIn('session.jsonl',output.getvalue())
        self.assertNotIn('PRIVATE_CONTENT_NOT_FOR_LOGS',output.getvalue())


if __name__=='__main__':unittest.main()
