"""Local importer + store: cursors, change log and analysis-only content.

Synthetic temporary fixtures only; never reads real Agent history or databases.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local.archive import LocalArchive
from local.codec import unpack_json
from local.db import Database
from local.importer import Importer, LocalRepository
from local.sources.base import SourceFile
from local.sources.claude import ClaudeAdapter
from local.sources.copilot_cli import CopilotCLIAdapter
from local.sources.pi import PiAdapter
from local.sources.vscode_chat import VSCodeChatAdapter
from server.contracts.protocol import Problem

TS = '2026-10-23T01:02:03Z'


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')


def append(path, rows):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.source.mkdir()
        self.archive = LocalArchive(self.root / 'state' / 'local.db')

    def run_import(self, adapter, quiet=0, **options):
        return Importer(LocalRepository(self.archive), [adapter], quiet_seconds=quiet, log=lambda *_: None, **options).run()

    def claude(self, text='question'):
        path = self.source / 'c.jsonl'
        write(path, [{'type': 'user', 'sessionId': 'c', 'uuid': 'u', 'timestamp': TS, 'message': {'content': text}}])
        return path, ClaudeAdapter([self.source])

    def log(self):
        last = self.archive.last_seq()
        return self.archive.changes(1, last) if last else []

    def test_initial_incremental_and_unchanged_runs(self):
        path, adapter = self.claude()
        self.assertEqual(self.run_import(adapter)['initialized'], 1)
        append(path, [{'type': 'assistant', 'uuid': 'a', 'timestamp': TS, 'message': {'content': 'readable answer'}},
                      {'type': 'system', 'content': 'SYSTEM_NOT_STORED'}])
        with patch.object(adapter, 'parse', side_effect=AssertionError('Full parse forbidden for an append')):
            self.assertEqual(self.run_import(adapter)['incremental'], 1)
        self.assertEqual(self.archive.db.get_messages('claude:c')['total'], 2)
        self.assertEqual(self.archive.db.list_sessions(query='readable answer')[1], 1)
        before = self.archive.last_seq()
        self.assertEqual(self.run_import(adapter)['unchanged'], 1)
        self.assertEqual(self.archive.last_seq(), before, 'unchanged files emit no upload changes')
        self.assertNotIn('SYSTEM_NOT_STORED', json.dumps(self.log()))

    def test_change_log_is_grouped_session_first_and_immutable(self):
        path, adapter = self.claude(); self.run_import(adapter)
        first = self.log()
        self.assertEqual([c['op'] for c in first], ['session', 'message'])
        self.assertEqual(len({c['group'] for c in first}), 1)
        self.assertNotIn('userMessageCount', first[0]['data']['metadata'])
        self.assertEqual(set(first[0]['data']), {'id', 'source', 'externalId', 'project', 'name', 'lastModified', 'available', 'metadata'})
        append(path, [{'type': 'assistant', 'uuid': 'a', 'timestamp': TS, 'message': {'content': 'second'}}])
        self.run_import(adapter)
        self.assertEqual(self.log()[:2], first, 'old change-log entries never change')
        self.assertEqual(self.archive.group_end(1), 2)

    def test_cursor_messages_and_change_log_commit_atomically(self):
        path, adapter = self.claude()
        original = self.archive.emit_changes
        with patch.object(self.archive, 'emit_changes', side_effect=RuntimeError('crash before commit')):
            with self.assertRaises(RuntimeError):
                Importer(LocalRepository(self.archive), [adapter], quiet_seconds=0, log=lambda *_: None).run()
        self.assertIsNone(self.archive.db.get_session('claude:c'))
        self.assertEqual(self.archive.db.file_states(), {})
        self.assertEqual(self.archive.last_seq(), 0)
        self.archive.emit_changes = original
        self.assertEqual(self.run_import(adapter)['initialized'], 1)
        self.assertEqual(self.archive.last_seq(), 2)

    def test_tool_result_status_update_is_an_upsert_not_a_duplicate(self):
        path = self.source / 'repeat.jsonl'
        write(path, [{'type': 'user', 'sessionId': 'repeat', 'timestamp': TS, 'message': {'content': 'question'}},
                     {'type': 'assistant', 'timestamp': TS, 'message': {'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]}},
                     {'type': 'user', 'timestamp': TS, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': 'OLD_BODY'}]}}])
        adapter = ClaudeAdapter([path]); self.run_import(adapter); before = self.archive.last_seq()
        append(path, [{'type': 'user', 'timestamp': TS, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'is_error': True, 'content': 'NEW_BODY'}]}}])
        self.run_import(adapter)
        changes = self.archive.changes(before + 1, self.archive.last_seq())
        updates = [c for c in changes if c['op'] == 'message']
        self.assertEqual(len(updates), 1); self.assertEqual(updates[0]['data']['message']['status'], 'failure')
        self.assertEqual(self.archive.db.get_messages('claude:repeat')['total'], 3)
        self.assertNotIn('BODY', json.dumps(self.log()))

    def test_duplicate_storage_copies_keep_the_superset_and_do_not_oscillate(self):
        root = self.source / 'vscode'
        older = root / 'workspaceStorage/ws/chatSessions/same.json'
        newer = root / 'globalStorage/emptyWindowChatSessions/same.json'
        state = {'sessionId': 'same', 'requests': [{'timestamp': 1, 'message': {'text': 'question'}, 'response': [{'value': 'answer'}]}]}
        older.parent.mkdir(parents=True); older.write_text(json.dumps(state))
        state['requests'].append({'timestamp': 2, 'message': {'text': 'follow up'}, 'response': [{'value': 'more'}]})
        newer.parent.mkdir(parents=True); newer.write_text(json.dumps(state))
        adapter = VSCodeChatAdapter([root])
        first = self.run_import(adapter)
        self.assertEqual(first['failed'], 0, first)
        self.assertEqual(self.archive.db.get_messages('vscode_chat:same')['total'], 4)
        seq = self.archive.last_seq()
        second = self.run_import(adapter)
        self.assertEqual((second['failed'], second['unchanged']), (0, 2), second)
        self.assertEqual(self.archive.last_seq(), seq, 'no oscillation between copies')
        self.assertEqual(len(self.archive.db.file_states()), 2)

    def test_recent_and_subagent_files_are_not_read(self):
        path, adapter = self.claude(); agent = self.source / 'agent-child.jsonl'
        write(agent, [{'type': 'user', 'message': {'content': 'hidden'}}])
        with patch.object(adapter, 'parse', side_effect=AssertionError('Must not read bodies')), \
                patch('local.importer.header_is_subagent', side_effect=AssertionError('Must not read headers')):
            result = self.run_import(adapter, quiet=3600)
        self.assertEqual(result['skippedRecent'], 1); self.assertEqual(result['skippedSubagents'], 1)
        self.assertEqual(self.archive.last_seq(), 0)

    def test_inline_subagent_transcript_is_skipped(self):
        path = self.source / 'not-named-agent.jsonl'
        write(path, [{'type': 'user', 'sessionId': 'child', 'isSidechain': True, 'message': {'content': 'PRIVATE_CHILD'}},
                     {'type': 'assistant', 'isSidechain': True, 'message': {'content': 'PRIVATE_CHILD_ANSWER'}}])
        self.assertEqual(self.run_import(ClaudeAdapter([path]))['skippedSubagents'], 1)
        self.assertEqual(self.archive.last_seq(), 0)

    def test_divergent_rewrite_keeps_old_history(self):
        path, adapter = self.claude('old history'); self.run_import(adapter)
        write(path, [{'type': 'user', 'sessionId': 'c', 'uuid': 'new', 'message': {'content': 'short replacement'}}])
        result = self.run_import(adapter)
        self.assertEqual(result['failed'], 1); self.assertEqual(result['errors'][0]['code'], 'SOURCE_DIVERGED')
        self.assertIn('old history', str(self.archive.db.get_messages('claude:c')))

    def test_missing_source_marks_unavailable_without_deleting(self):
        paths = []
        for i in range(2):
            path = self.source / f'{i}.jsonl'
            write(path, [{'type': 'user', 'sessionId': str(i), 'timestamp': TS, 'message': {'content': str(i)}}]); paths.append(path)
        adapter = ClaudeAdapter([self.source]); self.run_import(adapter)
        before = self.archive.last_seq(); paths[1].unlink(); self.run_import(adapter)
        self.assertFalse(self.archive.db.get_session('claude:1')['available'])
        self.assertEqual(self.archive.db.get_messages('claude:1')['total'], 1)
        changes = self.archive.changes(before + 1, self.archive.last_seq())
        self.assertEqual([(c['op'], c['data']['available']) for c in changes], [('session', False)])

    def test_dynamic_batches_split_one_file_and_resume(self):
        path = self.source / 'many.jsonl'
        write(path, [{'type': 'user', 'sessionId': 'many', 'uuid': str(i), 'timestamp': TS,
                      'message': {'content': str(i) + ' ' + 'x' * (40 if i < 30 else 1700)}} for i in range(55)])
        adapter = ClaudeAdapter([path])
        result = self.run_import(adapter, batch_bytes=8192)
        self.assertEqual(result['failed'], 0, result)
        groups = {c['group'] for c in self.log()}
        self.assertGreater(len(groups), 2, 'one large file commits in several bounded groups')
        self.assertEqual(self.archive.db.get_messages('claude:many')['total'], 55)

    def test_vscode_mutation_log_keeps_tool_process_out_of_store(self):
        secret = 'PRIVATE_VSCODE_TOOL_PROCESS_99001'; path = self.source / 'private-vs.jsonl'
        state = {'sessionId': 'private-vs', 'requests': [{'message': {'text': 'question'},
                 'response': [{'kind': 'markdown', 'value': 'ordinary answer'},
                              {'kind': 'toolInvocationSerialized', 'toolCallId': 'call', 'toolId': 'read',
                               'toolSpecificData': {'kind': 'terminal', 'command': 'echo allowed', 'terminalCommandOutput': secret},
                               'resultDetails': secret}],
                 'result': {'metadata': {'toolCallResults': {'call': secret}, 'toolCallRounds': [{'response': secret,
                            'toolCalls': [{'id': 'call', 'name': 'read', 'arguments': {'path': 'allowed.txt'}}]}]}}}]}
        write(path, [{'kind': 0, 'v': state}]); adapter = VSCodeChatAdapter([path])
        self.assertEqual(self.run_import(adapter)['initialized'], 1)
        append(path, [{'kind': 1, 'k': ['requests', 0, 'response', 0, 'value'], 'v': 'updated answer'}])
        self.assertEqual(self.run_import(adapter)['incremental'], 1)
        self.assertIn('updated answer', str(self.archive.db.get_messages('vscode_chat:private-vs')))
        self.assertIn('allowed.txt', str(self.archive.db.get_messages('vscode_chat:private-vs')))
        with closing(sqlite3.connect(self.archive.db.db_path)) as conn:
            for table, column in (('messages', 'normalized_json'), ('sync_checkpoints', 'state'),
                                  ('projection_state', 'state'), ('change_log', 'payload')):
                for (blob,) in conn.execute(f'SELECT {column} FROM {table}'):
                    self.assertNotIn(secret, json.dumps(unpack_json(blob)), table)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0], 0)

    def test_copilot_tail_replays_prefix_in_memory_only(self):
        path = self.source / 'events.jsonl'
        write(path, [{'type': 'session.start', 'data': {'sessionId': 'cp'}},
                     {'type': 'user.message', 'id': 'u', 'data': {'content': 'question'}},
                     {'type': 'assistant.message', 'id': 'a', 'data': {'content': 'working', 'toolRequests': [{'toolCallId': 'c', 'name': 'read', 'arguments': {}}]}}])
        adapter = CopilotCLIAdapter([path]); self.run_import(adapter)
        committed = path.stat().st_size; original = adapter.parse
        append(path, [{'type': 'tool.execution_complete', 'data': {'toolCallId': 'c', 'success': True, 'result': 'late output'}}])

        def prefix_only(entry):
            self.assertEqual(entry.path.stat().st_size, committed); return original(entry)
        with patch.object(adapter, 'parse', side_effect=prefix_only):
            self.assertEqual(self.run_import(adapter)['incremental'], 1)
        messages = self.archive.db.get_messages('copilot_cli:cp')['items']
        self.assertEqual(messages[-1]['status'], 'success'); self.assertNotIn('late output', str(messages))

    def test_pi_append_and_query_window_is_metadata_only(self):
        adapter = PiAdapter([self.source]); entries = []
        for i in range(5):
            path = self.source / f'{i}.jsonl'
            write(path, [{'type': 'session', 'version': 3, 'id': f'p{i}'},
                         {'type': 'message', 'id': 'u', 'timestamp': TS, 'message': {'role': 'user', 'content': str(i)}}])
            entries.append(SourceFile(path))
        calls = []; original = self.archive.query_progress
        with patch.object(adapter, 'discover', return_value=iter(entries)), \
                patch.object(self.archive, 'query_progress', side_effect=lambda files, **kw: calls.append(len(files)) or original(files, **kw)):
            self.assertEqual(self.run_import(adapter, progress_query_files=2)['initialized'], 5)
        self.assertEqual(calls, [2, 2, 1])

    def test_store_refuses_old_full_history_databases(self):
        legacy = self.root / 'old.db'
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute('PRAGMA application_id = 1129731890'); conn.execute('CREATE TABLE sessions(id)'); conn.commit()
        with self.assertRaisesRegex(ValueError, 'migrate'):
            LocalArchive(legacy)
        for name in ('viewer.db', 'claude.db', 'viewer.before-legacy-1.db'):
            with self.assertRaises(ValueError):
                Database(Path(__file__).resolve().parents[1] / name)

    def test_commit_validates_analysis_contract(self):
        with self.assertRaises(Problem):
            self.archive.commit({'version': 5, 'contentPolicy': 'analysis-v1', 'file': {}, 'document': {'records': []}, 'changedScopes': []}, 0)
        self.assertEqual(self.archive.last_seq(), 0)


if __name__ == '__main__':
    unittest.main()
