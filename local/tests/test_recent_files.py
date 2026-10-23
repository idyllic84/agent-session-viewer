"""One-hour quiet period is enforced before body reads and never advances cursors."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local.service import ClaudeViewerService
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter
from local.sources.pi import PiAdapter

NOW = 1800000000_000000000


class RecentFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'session.jsonl'
        self.path.write_text(json.dumps({'type':'user','sessionId':'s','uuid':'u','message':{'content':'original'}})+'\n',encoding='utf-8')
        self.adapter = ClaudeAdapter([self.root])
        self.viewer = ClaudeViewerService(self.root/'archive.db',[self.adapter])
        self.viewer.initialize()

    def age(self, seconds, path=None):
        timestamp = NOW - int(seconds*1_000_000_000)
        os.utime(path or self.path, ns=(timestamp,timestamp))

    def sync(self):
        with patch('local.service.time.time_ns',return_value=NOW):
            return self.viewer.sync_sessions()

    def test_recent_file_skips_before_hash_or_parse_and_progress_is_explicit(self):
        self.age(3599)
        with patch.object(SourceFile,'fingerprint',side_effect=AssertionError('No body/hash read allowed')), \
             patch.object(self.adapter,'parse',side_effect=AssertionError('No parse allowed')):
            result=self.sync()
        self.assertEqual(result['skippedRecent'],1);self.assertEqual(result['failed'],0)
        self.assertEqual(self.viewer.get_all_sessions(),[])
        self.assertEqual(self.viewer._db.file_states(),{})
        self.assertEqual(self.viewer.sync_status()['progress']['skippedRecent'],1)

    def test_exactly_one_hour_old_is_eligible(self):
        self.age(3600)
        self.assertEqual(self.sync()['added'],1)

    def test_future_timestamp_is_also_skipped(self):
        self.age(-60)
        self.assertEqual(self.sync()['skippedRecent'],1)

    def test_skipped_existing_history_and_checkpoint_are_unchanged_then_resume(self):
        self.age(7200);self.sync()
        before=self.viewer._db.file_states()
        metadata=self.viewer._db.get_session('claude:s')['metadata']
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'assistant','sessionId':'s','uuid':'a','message':{'content':'later'}})+'\n')
        self.age(10)
        self.assertEqual(self.sync()['skippedRecent'],1)
        self.assertEqual(self.viewer._db.file_states(),before)
        self.assertEqual(self.viewer._db.get_session('claude:s')['metadata'],metadata)
        self.assertTrue(self.viewer._db.get_session('claude:s')['available'])
        self.assertEqual(self.viewer.get_messages('claude:s')['total'],1)
        self.age(7200)
        result=self.sync()
        self.assertEqual(result['appendedMessages'],1)
        self.assertEqual(self.viewer.get_messages('claude:s')['total'],2)

    def test_file_touched_during_parse_is_not_committed(self):
        self.age(7200)
        original=self.adapter.parse
        def touch(entry):
            document=original(entry)
            self.age(0)
            return document
        with patch.object(self.adapter,'parse',side_effect=touch):result=self.sync()
        self.assertEqual(result['skippedRecent'],1)
        self.assertEqual(self.viewer.get_all_sessions(),[])
        self.assertEqual(self.viewer._db.file_states(),{})

    def test_recent_dependency_defers_transcript_without_reading_either_body(self):
        self.age(7200)
        workspace=self.root/'workspace.json';workspace.write_text('{}',encoding='utf-8');self.age(1,workspace)
        entry=SourceFile(self.path,dependencies=(workspace,))
        with patch.object(self.adapter,'discover',return_value=iter([entry])), \
             patch.object(SourceFile,'fingerprint',side_effect=AssertionError('No body reads')):
            result=self.sync()
        self.assertEqual(result['skippedRecent'],1)

    def test_recent_skip_does_not_prefetch_the_next_path(self):
        self.age(1)
        def discover():
            yield SourceFile(self.path)
            self.assertEqual(self.viewer.sync_status()['progress']['skippedRecent'],1)
        with patch.object(self.adapter,'discover',side_effect=discover):
            self.assertEqual(self.sync()['skippedRecent'],1)

    def test_default_rule_applies_to_every_source(self):
        self.age(1)
        for source in ('claude','pi','codex','copilot_cli','vscode_chat'):
            with self.subTest(source=source):
                adapter=PiAdapter([self.root]);adapter.source=source
                viewer=ClaudeViewerService(self.root/(source+'.db'),[adapter]);viewer.initialize()
                with patch('local.service.time.time_ns',return_value=NOW), \
                     patch.object(adapter,'parse',side_effect=AssertionError('No source parse')):
                    self.assertEqual(viewer.sync_sessions()['skippedRecent'],1)


if __name__=='__main__':unittest.main()
