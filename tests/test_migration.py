"""Old full-history viewer.db (archive v2) -> local analysis store. Synthetic only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local import incremental
from local.archive import LocalArchive
from local.importer import Importer, LocalRepository
from local.migrate import migrate
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter
from server.db import Database as FullHistoryFormat  # same v2 schema as the old local viewer.db

TS = '2026-10-23T01:02:03Z'


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'history'

    def old_archive(self, paths):
        old = FullHistoryFormat(self.root / 'old' / 'viewer.db'); old.init_schema()
        adapter = ClaudeAdapter([self.source])
        for path in paths:
            entry = SourceFile(path); doc = incremental.initialize(adapter, entry, adapter.parse(entry))
            fingerprint = entry.snapshot_fingerprint(doc.records)
            old.replace_session(doc, path, fingerprint, path.stat().st_mtime_ns // 1_000_000, adapter.parser_version,
                                [(str(path), 'claude', doc.id, fingerprint, adapter.parser_version, path.stat().st_mtime_ns // 1_000_000, len(doc.messages))])
        return old.db_path

    def test_legacy_only_sessions_are_imported_filtered_and_source_is_untouched(self):
        secret = 'OLD_THINKING_AND_TOOL_OUTPUT_712'
        gone = self.source / 'gone.jsonl'; live = self.source / 'live.jsonl'
        write(gone, [{'type': 'user', 'sessionId': 'gone', 'timestamp': TS, 'message': {'content': 'retained question'}},
                     {'type': 'assistant', 'timestamp': TS, 'message': {'content': [{'type': 'thinking', 'thinking': secret},
                      {'type': 'tool_use', 'id': 'x', 'name': 'Agent', 'input': {'prompt': 'retained task'}}]}},
                     {'type': 'user', 'timestamp': TS, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': secret}]}},
                     {'type': 'progress', 'content': secret}])
        write(live, [{'type': 'user', 'sessionId': 'live', 'timestamp': TS, 'message': {'content': 'live question'}}])
        old = self.old_archive([gone, live]); gone.unlink()
        before = hashlib.sha256(old.read_bytes()).hexdigest()
        archive = LocalArchive(self.root / 'state/local.db')
        Importer(LocalRepository(archive), [ClaudeAdapter([self.source])], quiet_seconds=0, log=lambda *_: None).run()
        live_seq = archive.last_seq()
        counts = migrate(old, self.root / 'state/local.db', log=lambda *_: None)
        self.assertEqual((counts['imported'], counts['present']), (1, 1))
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), before)
        archive = LocalArchive(self.root / 'state/local.db')
        session = archive.db.get_session('claude:gone')
        self.assertFalse(session['available'])
        messages = archive.db.get_messages('claude:gone')['items']
        self.assertEqual([m['type'] for m in messages], ['user', 'tool-use', 'tool-result'])
        self.assertIn('retained task', str(messages)); self.assertNotIn(secret, str(messages))
        changes = archive.changes(live_seq + 1, archive.last_seq())
        self.assertEqual(changes[0]['op'], 'session'); self.assertNotIn(secret, json.dumps(changes))
        self.assertEqual(migrate(old, self.root / 'state/local.db', log=lambda *_: None)['imported'], 0, 'insert-only and repeatable')

    def test_refuses_non_legacy_sources_and_preserves_original_names(self):
        archive = LocalArchive(self.root / 'state/local.db')
        with self.assertRaises(ValueError):
            migrate(archive.db.db_path, self.root / 'state/other.db', log=lambda *_: None)
        with self.assertRaises(ValueError):
            LocalArchive(Path(__file__).resolve().parents[1] / 'local' / 'viewer.db')


if __name__ == '__main__':
    unittest.main()
