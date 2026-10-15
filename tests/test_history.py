"""Synthetic fixtures only: never read real history or invoke an agent CLI."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import urllib.request
import tempfile
import time
from threading import Event
from unittest.mock import patch
import unittest

from db import Database
from models import SessionDocument, timestamp_ms
from server import create_app
from service import ClaudeViewerService, SyncBusy
from sources import default_adapters
from sources.base import SourceError, SourceFile, read_records
from sources.claude import ClaudeAdapter
from sources.codex import CodexAdapter
from sources.copilot_cli import CopilotCLIAdapter
from sources.pi import PiAdapter
from sources.vscode_chat import VSCodeChatAdapter, replay_operations, folder_path

TS = '2026-01-02T03:04:05Z'


def write_lines(path, records, tail=b''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode() for r in records) + tail)
    return path


def pi_records(sid='shared', content='hello'):
    return [{'type': 'session', 'version': 3, 'id': sid, 'cwd': 'C:/work/project', 'timestamp': TS},
            {'type': 'message', 'id': 'u', 'parentId': None, 'timestamp': TS,
             'message': {'role': 'user', 'content': content}}]


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def service(self, adapters):
        service = ClaudeViewerService(self.root / 'viewer.db', adapters, skip_recent_seconds=0)
        service.initialize()
        return service


class ParserTests(FixtureCase):
    def test_timestamp_seconds_milliseconds_and_iso(self):
        self.assertEqual(timestamp_ms(TS), 1767323045000)
        self.assertEqual(timestamp_ms(1767323045), timestamp_ms(TS))
        self.assertEqual(timestamp_ms(1767323045000), timestamp_ms(TS))
        self.assertIsNone(timestamp_ms('invalid'))
        self.assertIsNone(timestamp_ms(True))
        self.assertIsNone(timestamp_ms(float('nan')))
        self.assertIsNone(timestamp_ms(float('inf')))
        self.assertEqual(timestamp_ms(1000, unit='ms'), 1000)

    def test_torn_final_line_and_bad_complete_line(self):
        path = write_lines(self.root / 't.jsonl', [{'type': 'one'}], b'{"type":')
        rows, warnings = read_records(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(warnings), 1)
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(SourceError):
            read_records(path)

    def test_snapshot_accepts_append_but_rejects_prefix_rewrite_and_truncation(self):
        path = write_lines(self.root / 'append.jsonl', pi_records())
        entry = SourceFile(path)
        records, _ = read_records(path)
        original = entry.fingerprint()
        with path.open('ab') as stream:
            stream.write(b'{"type":"message","id":"later"}\n')
        self.assertEqual(entry.snapshot_fingerprint(records), original)
        self.assertNotEqual(entry.fingerprint(), original)
        path.write_bytes(path.read_bytes().replace(b'hello', b'other'))
        with self.assertRaises(SourceError):
            entry.snapshot_fingerprint(records)
        path.write_bytes(b'{}\n')
        with self.assertRaises(SourceError):
            entry.snapshot_fingerprint(records)

    def test_pi_tree_mixed_content_and_raw_retained(self):
        rows = pi_records()
        rows += [
            {'type': 'model_change', 'id': 'model', 'parentId': 'u', 'modelId': 'm1', 'provider': 'test'},
            {'type': 'message', 'id': 'a', 'parentId': 'model', 'timestamp': TS,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'answer'}, {'type': 'thinking', 'thinking': 'reason'},
                 {'type': 'toolCall', 'id': 'call', 'name': 'bash', 'arguments': {'command': 'pwd'}}]}},
            {'type': 'message', 'id': 't', 'parentId': 'a', 'message': {'role': 'toolResult',
             'toolCallId': 'call', 'toolName': 'bash', 'isError': False, 'content': [{'type': 'text', 'text': 'result'}]}},
            {'type': 'message', 'id': 'alternate', 'parentId': 'u', 'message': {'role': 'user', 'content': 'another branch'}},
            {'type': 'compaction', 'id': 'c', 'parentId': 'alternate', 'summary': 'summary',
             'retainedTail': [{'role': 'user', 'content': 'do not duplicate me'}]},
            {'type': 'session_info', 'id': 'name', 'parentId': 'c', 'name': 'My task'}]
        path = write_lines(self.root / 'pi.jsonl', rows)
        before = path.read_bytes()
        doc = PiAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(doc.name, 'My task')
        self.assertEqual(doc.metadata['defaultLeaf'], 'name')
        self.assertEqual(doc.messages[2]['type'], 'assistant')
        self.assertEqual([b['type'] for b in doc.messages[2]['message']['content']], ['text', 'thinking', 'tool_use'])
        self.assertEqual(doc.messages[2]['parentUuid'], 'model')
        self.assertEqual(doc.messages[3]['type'], 'tool-result')
        self.assertNotIn('do not duplicate me', '\n'.join(str(m['message']) for m in doc.messages))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(doc.records[-2][1]['retainedTail'], rows[-2]['retainedTail'])

    def test_pi_model_changes_follow_branch_ancestry(self):
        rows = pi_records() + [
            {'type': 'model_change', 'id': 'model', 'parentId': 'u', 'modelId': 'old-model', 'provider': 'test'},
            {'type': 'message', 'id': 'a', 'parentId': 'model', 'message': {'role': 'assistant', 'content': 'old'}},
            {'type': 'message', 'id': 'b', 'parentId': 'u', 'message': {'role': 'assistant', 'content': 'new'}}]
        doc = PiAdapter([self.root]).parse(SourceFile(write_lines(self.root / 'pi.jsonl', rows)))
        self.assertEqual(doc.messages[-2]['model'], 'old-model')
        self.assertIsNone(doc.messages[-1]['model'])

    def test_pi_future_format_refused(self):
        rows = pi_records(); rows[0]['version'] = 99
        path = write_lines(self.root / 'pi.jsonl', rows)
        with self.assertRaises(SourceError):
            PiAdapter([self.root]).parse(SourceFile(path))

    def test_claude_agent_does_not_replace_parent(self):
        row = {'sessionId': 'parent', 'cwd': 'C:/work', 'type': 'assistant', 'uuid': 'a',
               'message': {'content': [{'type': 'text', 'text': 'child answer'}]}}
        path = write_lines(self.root / 'agent-child.jsonl', [row])
        doc = ClaudeAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(doc.id, 'claude:parent:agent-child')
        self.assertEqual(doc.metadata['parentSessionId'], 'claude:parent')

    def test_codex_paginated_and_tool_completion_not_duplicated(self):
        rows = [
            {'type': 'session_meta', 'ordinal': 0, 'payload': {'id': 'child', 'session_id': 'root',
             'cwd': 'C:/work', 'history_mode': 'paginated', 'subagent_history_start_ordinal': 2,
             'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root', 'agent_nickname': 'worker'}}}}},
            {'type': 'response_item', 'ordinal': 1, 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'inherited context'}]}},
            {'type': 'response_item', 'ordinal': 2, 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'question'}]}},
            {'type': 'event_msg', 'ordinal': 3, 'timestamp': TS, 'payload': {'type': 'item_completed', 'turn_id': 'turn',
             'item': {'type': 'UserMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': 'question'}]}}},
            {'type': 'event_msg', 'ordinal': 4, 'payload': {'type': 'item_completed',
             'item': {'type': 'UserMessage', 'id': 'u2', 'content': [{'type': 'text', 'text': 'question'}]}}},
            {'type': 'response_item', 'ordinal': 5, 'payload': {'type': 'function_call', 'call_id': 'call',
             'name': 'shell', 'arguments': '{"command":"pwd"}'}},
            {'type': 'response_item', 'ordinal': 6, 'payload': {'type': 'function_call_output', 'call_id': 'call', 'output': 'out'}},
            {'type': 'event_msg', 'ordinal': 7, 'payload': {'type': 'item_completed',
             'item': {'type': 'CommandExecution', 'id': 'call', 'status': 'completed', 'aggregated_output': 'out'}}},
            {'type': 'response_item', 'ordinal': 8, 'payload': {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'answer'}]}},
            {'type': 'event_msg', 'ordinal': 9, 'payload': {'type': 'item_completed',
             'item': {'type': 'AgentMessage', 'id': 'a', 'content': [{'type': 'Text', 'text': 'answer'}]}}},
            {'type': 'compacted', 'ordinal': 10, 'payload': {'message': 'summary',
             'replacement_history': [{'type': 'message', 'content': 'do not replay'}]}}]
        path = write_lines(self.root / 'codex.jsonl', rows)
        doc = CodexAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(doc.id, 'codex:child')
        self.assertEqual(doc.metadata['rootSessionId'], 'root')
        self.assertEqual(doc.metadata['parentSessionId'], 'codex:root')
        self.assertEqual(doc.metadata['relationship'], 'subagent')
        self.assertEqual(sum(m['type'] == 'user' for m in doc.messages), 2)  # genuine repeat retained
        self.assertEqual(sum(m['type'] == 'tool-use' for m in doc.messages), 1)
        self.assertEqual(sum(m['type'] == 'tool-result' for m in doc.messages), 1)
        self.assertEqual(sum(m['type'] == 'assistant' for m in doc.messages), 1)
        call = next(m for m in doc.messages if m['type'] == 'tool-use')
        self.assertEqual(call['relatedRawRecords'], [7])
        self.assertNotIn('inherited context', str(doc.messages))
        self.assertNotIn('do not replay', str(doc.messages))

    def test_codex_legacy_custom_call_and_opaque_reasoning(self):
        rows = [{'type': 'session_meta', 'payload': {'id': 'old', 'cwd': '/work'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'hi'}]}},
                {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'c', 'name': 'patch', 'input': 'raw patch'}},
                {'type': 'response_item', 'payload': {'type': 'reasoning', 'encrypted_content': 'opaque',
                 'summary': [{'type': 'summary_text', 'text': 'clear summary'}]}}]
        doc = CodexAdapter([self.root]).parse(SourceFile(write_lines(self.root / 'old.jsonl', rows)))
        self.assertEqual(len(doc.messages), 3)
        self.assertNotIn('opaque', str(doc.messages))
        self.assertIn('opaque', str(doc.records))

    def test_copilot_pairs_calls_and_ignores_model_snapshots(self):
        rows = [
            {'type': 'session.start', 'data': {'sessionId': 'shared', 'context': {'cwd': '/work'}}},
            {'type': 'user.message', 'id': 'u', 'data': {'content': 'hello'}},
            {'type': 'assistant.message', 'id': 'a', 'data': {'content': 'working', 'reasoningText': 'thinking',
             'toolRequests': [{'toolCallId': 'c', 'name': 'view', 'arguments': {'path': 'x'}}]}},
            {'type': 'tool.execution_start', 'id': 'start', 'data': {'toolCallId': 'c', 'toolName': 'view'}},
            {'type': 'model.messages_snapshot', 'data': {'messages': [{'content': 'duplicate snapshot'}]}},
            {'type': 'tool.execution_complete', 'id': 'end', 'data': {'toolCallId': 'c', 'success': False,
             'result': {'content': [{'type': 'text', 'text': 'failure'}]}}}]
        doc = CopilotCLIAdapter([self.root]).parse(SourceFile(write_lines(self.root / 'events.jsonl', rows)))
        self.assertEqual(sum(m['role'] == 'assistant' for m in doc.messages), 1)
        call = next(m for m in doc.messages if m['role'] == 'assistant')
        self.assertEqual(call['relatedRawRecords'], [3, 5])
        self.assertEqual(call['status'], 'error')
        self.assertNotIn('duplicate snapshot', str(doc.messages))
        self.assertTrue(doc.messages[-1]['message']['content'][0]['is_error'])

    def test_vscode_push_truncates_and_empty_push_and_deep_delete(self):
        initial = {'requests': [{'response': [{'value': 'old'}, {'value': 'stale'}, {'value': 'stale2'}]}],
                   'deep': {'a': [{'b': {'c': 1, 'remove': 2}}]}}
        ops = [(0, {'kind': 0, 'v': initial}),
               (1, {'kind': 2, 'k': ['requests', 0, 'response'], 'i': 1, 'v': [{'value': 'new'}]}),
               (2, {'kind': 1, 'k': ['deep', 'a', 0, 'b', 'c'], 'v': 3}),
               (3, {'kind': 3, 'k': ['deep', 'a', 0, 'b', 'remove']})]
        before = copy.deepcopy(ops)
        state = replay_operations(ops)
        self.assertEqual([x['value'] for x in state['requests'][0]['response']], ['old', 'new'])
        self.assertEqual(state['deep']['a'][0]['b'], {'c': 3})
        self.assertEqual(ops, before)
        state = replay_operations(ops + [(4, {'kind': 2, 'k': ['requests', 0, 'response'], 'i': 0})])
        self.assertEqual(state['requests'][0]['response'], [])

    def test_vscode_unknown_mutation_fails(self):
        with self.assertRaises(SourceError):
            replay_operations([(0, {'kind': 0, 'v': {}}), (1, {'kind': 4, 'k': ['x']})])

    def test_vscode_many_chunks_have_unique_message_ids(self):
        state = {'sessionId': 'vs', 'customTitle': '<test>', 'creationDate': 1767323045000,
                 'requests': [{'requestId': 'r', 'message': {'text': 'question'},
                               'response': [{'value': f'chunk-{i}'} for i in range(20)] + [
                                   {'kind': 'thinking', 'value': 'reason'},
                                   {'kind': 'toolInvocationSerialized', 'toolId': 'shell', 'toolCallId': 'c',
                                    'toolSpecificData': {'command': 'pwd'}, 'resultDetails': {'output': ['answer']}}]}]}
        path = self.root / 'vs.json'; path.write_text(json.dumps(state), encoding='utf-8')
        doc = VSCodeChatAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(len(doc.messages), len({m['id'] for m in doc.messages}))
        self.assertEqual(doc.messages[0]['timestamp'], 1767323045000)
        self.assertIn('answer', str(doc.messages))
        self.assertEqual(doc.name, '<test>')

    def test_folder_uri_windows_and_remote(self):
        self.assertEqual(folder_path('file:///C:/work/a%20b'), 'C:/work/a b')
        self.assertEqual(folder_path('file://server/share/a'), '//server/share/a')
        self.assertEqual(folder_path('vscode-remote://ssh-remote+host/work'), 'vscode-remote://ssh-remote+host/work')


class ArchiveTests(FixtureCase):
    def test_multisource_identity_idempotence_and_source_unchanged(self):
        pi = write_lines(self.root / 'pi' / 'session.jsonl', pi_records())
        claude = write_lines(self.root / 'claude' / 'project' / 'shared.jsonl', [
            {'type': 'user', 'sessionId': 'shared', 'timestamp': TS, 'message': {'content': 'hello'}}])
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in (pi, claude)}
        service = self.service([PiAdapter([pi.parent]), ClaudeAdapter([claude.parent.parent])])
        first = service.sync_sessions(); second = service.sync_sessions()
        self.assertEqual(first['added'], 2)
        self.assertEqual(second['unchanged'], 2)
        self.assertEqual(second['updated'], 0)
        self.assertEqual({s['id'] for s in service.get_all_sessions()}, {'pi:shared', 'claude:shared'})
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)

    def test_changed_file_replaced_and_same_size_same_mtime_detected(self):
        path = write_lines(self.root / 'session.jsonl', pi_records(content='hello'))
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b'hello', b'world'))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = service.sync_sessions()
        self.assertEqual(result['updated'], 1)
        self.assertEqual(service.get_messages('pi:shared')['items'][0]['message']['content'][0]['text'], 'world')
        path.write_bytes(path.read_bytes() + b'{"bad":\n')
        result = service.sync_sessions()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(service.get_messages('pi:shared')['items'][0]['message']['content'][0]['text'], 'world')

    def test_live_append_during_import_saves_snapshot_then_catches_up(self):
        path = write_lines(self.root / 'session.jsonl', pi_records())
        adapter = PiAdapter([self.root]); original = adapter.parse
        calls = 0
        def append_after_read(entry):
            nonlocal calls
            document = original(entry)
            calls += 1
            if calls == 1:
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'type': 'message', 'id': 'a', 'parentId': 'u',
                        'message': {'role': 'assistant', 'content': 'newly appended'}}) + '\n')
            return document
        service = self.service([adapter])
        with patch.object(adapter, 'parse', side_effect=append_after_read):
            result = service.sync_sessions()
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(calls, 1)  # Winning projection is spooled, not parsed twice.
        self.assertEqual(service.get_messages('pi:shared')['total'], 1)
        self.assertEqual(service.sync_sessions()['updated'], 1)
        self.assertEqual(service.get_messages('pi:shared')['total'], 2)

    def test_rewrite_after_parse_keeps_old_archive(self):
        path = write_lines(self.root / 'session.jsonl', pi_records())
        adapter = PiAdapter([self.root]); service = self.service([adapter]); service.sync_sessions()
        path.write_bytes(path.read_bytes().replace(b'hello', b'world'))
        original = adapter.parse
        def rewrite_after_read(entry):
            document = original(entry)
            path.write_bytes(path.read_bytes().replace(b'world', b'other'))
            return document
        with patch.object(adapter, 'parse', side_effect=rewrite_after_read):
            result = service.sync_sessions()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(service.get_messages('pi:shared')['items'][0]['message']['content'][0]['text'], 'hello')

    def test_torn_tail_then_repaired(self):
        path = write_lines(self.root / 'session.jsonl', pi_records(), b'{"type":"message"')
        service = self.service([PiAdapter([self.root])])
        self.assertEqual(service.sync_sessions()['added'], 1)
        self.assertEqual(len(service.get_messages('pi:shared')['session']['warnings']), 1)
        path.write_bytes(path.read_bytes() + b',"id":"a","parentId":"u","message":{"role":"assistant","content":"ok"}}\n')
        self.assertEqual(service.sync_sessions()['updated'], 1)
        self.assertEqual(service.get_messages('pi:shared')['total'], 2)

    def test_atomic_replace_rolls_back_raw_messages_and_watermarks(self):
        path = write_lines(self.root / 'session.jsonl', pi_records())
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        database = service._db; before = database.file_states()
        doc = PiAdapter([self.root]).parse(SourceFile(path))
        doc.messages.append(copy.deepcopy(doc.messages[0]))  # UNIQUE(id) violation after deletes.
        with self.assertRaises(sqlite3.IntegrityError):
            database.replace_session(doc, path, 'bad-checkpoint', 1, 1,
                                     [(str(path), 'pi', doc.id, 'bad-checkpoint', 1, 1, 2)])
        self.assertEqual(database.file_states(), before)
        self.assertEqual(database.get_messages(doc.id)['total'], 1)
        self.assertEqual(database.get_records(doc.id)['total'], 2)
        self.assertEqual(len(service.search_sessions_by_content('hello')), 1)

    def test_source_missing_retains_archive(self):
        path = write_lines(self.root / 'session.jsonl', pi_records())
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        path.unlink()  # Test fixture only, never a user's source.
        service.sync_sessions()
        session = service.get_all_sessions()[0]
        self.assertFalse(session['available'])
        self.assertEqual(service.get_messages(session['id'])['total'], 1)

    def test_pi_branch_includes_metadata_ancestors_not_other_branch(self):
        rows = pi_records() + [
            {'type': 'model_change', 'id': 'm', 'parentId': 'u', 'modelId': 'model'},
            {'type': 'message', 'id': 'a', 'parentId': 'm', 'message': {'role': 'assistant', 'content': 'old branch'}},
            {'type': 'message', 'id': 'b', 'parentId': 'u', 'message': {'role': 'assistant', 'content': 'new branch'}}]
        write_lines(self.root / 'session.jsonl', rows)
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        active = service.get_messages('pi:shared', branch='active')
        self.assertEqual([m['uuid'] for m in active['items']], ['u', 'b'])
        old = service.get_messages('pi:shared', branch='a')
        self.assertEqual([m['uuid'] for m in old['items']], ['u', 'm', 'a'])
        with self.assertRaises(ValueError):
            service.get_messages('pi:shared', branch='missing')

    def test_vscode_duplicate_storage_prefers_nonempty_and_does_not_oscillate(self):
        root = self.root / 'code'
        state = {'sessionId': 'same', 'requests': [{'message': {'text': 'actual'}, 'response': []}]}
        one = root / 'workspaceStorage/one/chatSessions/one.json'
        two = root / 'workspaceStorage/two/chatSessions/two.json'
        for path, value in [(one, state), (two, {'sessionId': 'same', 'requests': []})]:
            path.parent.mkdir(parents=True); path.write_text(json.dumps(value), encoding='utf-8')
        service = self.service([VSCodeChatAdapter([root])])
        self.assertEqual(service.sync_sessions()['added'], 1)
        self.assertEqual(service.sync_sessions()['unchanged'], 1)
        self.assertEqual(service.get_messages('vscode_chat:same')['total'], 1)
        self.assertEqual(len(service._db.file_states()), 2)
        one.unlink()
        self.assertEqual(service.sync_sessions()['unchanged'], 1)
        self.assertEqual(service.get_messages('vscode_chat:same')['total'], 1)
        self.assertFalse(service.get_all_sessions()[0]['available'])

    def test_literal_search_substrings_cjk_and_special_characters(self):
        write_lines(self.root / 'session.jsonl', pi_records(content='Authentication 中文数据库 100%_done quoted"text'))
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        for query in ['Authent', 'authent', '中文', '数据库', '%_', 'quoted"text', '100%_done']:
            with self.subTest(query=query):
                self.assertEqual(len(service.search_sessions_by_content(query)), 1)
        self.assertEqual(service.search_sessions_by_content('nonexistent'), [])
        self.assertEqual(service.search_sessions_by_content('%wildcard'), [])

    def test_raw_original_not_normalized_and_metadata_retained(self):
        rows = pi_records()
        path = write_lines(self.root / 'session.jsonl', rows)
        service = self.service([PiAdapter([self.root])]); service.sync_sessions()
        message = service.get_messages('pi:shared')['items'][0]
        raw = service.get_raw_message(message['id'])
        self.assertEqual(raw['records'][0]['record'], rows[1])
        self.assertEqual(raw['sourcePath'], str(path.resolve()))
        self.assertEqual(service.get_records('pi:shared')['items'][0]['record'], rows[0])

    def test_all_five_sources_import_in_one_archive(self):
        pi = write_lines(self.root / 'pi' / 's.jsonl', pi_records())
        claude = write_lines(self.root / 'claude' / 'project' / 'shared.jsonl', [
            {'type': 'user', 'sessionId': 'shared', 'message': {'content': 'hello'}}])
        codex = write_lines(self.root / 'codex' / 'rollout.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'shared', 'cwd': '/work'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'hello'}]}}])
        copilot = write_lines(self.root / 'copilot' / 'shared' / 'events.jsonl', [
            {'type': 'session.start', 'data': {'sessionId': 'shared', 'context': {'cwd': '/work'}}},
            {'type': 'user.message', 'data': {'content': 'hello'}}])
        vscode = self.root / 'code' / 'workspaceStorage' / 'ws' / 'chatSessions' / 'shared.json'
        vscode.parent.mkdir(parents=True)
        vscode.write_text(json.dumps({'sessionId': 'shared', 'requests': [
            {'message': {'text': 'hello'}, 'response': []}]}), encoding='utf-8')
        adapters = [PiAdapter([pi.parent]), ClaudeAdapter([claude.parent.parent]), CodexAdapter([codex.parent]),
                    CopilotCLIAdapter([copilot.parent.parent]), VSCodeChatAdapter([self.root / 'code'])]
        service = self.service(adapters)
        result = service.sync_sessions()
        self.assertEqual(result['added'], 5)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(service.search_sessions_by_content('hello')), 5)
        self.assertEqual(service.sync_sessions()['unchanged'], 5)
        service._db.has_trigram = False
        self.assertEqual(len(service.search_sessions_by_content('hello')), 5)

    def test_failed_newer_duplicate_keeps_last_good_projection(self):
        root = self.root / 'code'
        paths = []
        for workspace, content, modified in [('old', 'old text', 1700000000), ('new', 'new text', 1700000001)]:
            path = root / 'workspaceStorage' / workspace / 'chatSessions' / 'same.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'sessionId': 'same', 'requests': [
                {'message': {'text': content}, 'response': []}]}), encoding='utf-8')
            os.utime(path, (modified, modified)); paths.append(path)
        service = self.service([VSCodeChatAdapter([root])]); service.sync_sessions()
        paths[1].write_text('{broken', encoding='utf-8')
        result = service.sync_sessions()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(service.get_messages('vscode_chat:same')['items'][0]['message']['content'][0]['text'], 'new text')

    def test_vscode_workspace_sidecar_change_refreshes_project(self):
        root = self.root / 'code'
        workspace = root / 'workspaceStorage' / 'ws' / 'workspace.json'
        path = workspace.parent / 'chatSessions' / 'same.json'
        path.parent.mkdir(parents=True)
        workspace.write_text(json.dumps({'folder': 'file:///C:/one'}), encoding='utf-8')
        path.write_text(json.dumps({'sessionId': 'same', 'requests': [
            {'message': {'text': 'hello'}, 'response': []}]}), encoding='utf-8')
        service = self.service([VSCodeChatAdapter([root])]); service.sync_sessions()
        self.assertEqual(service.get_all_sessions()[0]['project'], 'C:/one')
        workspace.write_text(json.dumps({'folder': 'file:///C:/two'}), encoding='utf-8')
        self.assertEqual(service.sync_sessions()['updated'], 1)
        self.assertEqual(service.get_all_sessions()[0]['project'], 'C:/two')

    def test_async_sync_has_progress_and_attaches_to_existing_job(self):
        path = write_lines(self.root / 'session.jsonl', pi_records())
        adapter = PiAdapter([self.root])
        service = self.service([adapter])
        entered, release = Event(), Event()
        original = adapter.parse
        def slow_parse(entry):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test barrier timed out')
            return original(entry)
        with patch.object(adapter, 'parse', side_effect=slow_parse):
            state = service.start_sync()
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(service.sync_status()['running'])
                progress = service.sync_status()['progress']
                self.assertEqual(progress['phase'], 'syncing')
                self.assertEqual(progress['filesTotal'], 1)
                self.assertEqual(progress['currentFile'], path.name)
                second = service.start_sync()
                self.assertEqual(second['startedAt'], state['startedAt'])
                self.assertGreaterEqual(second['elapsedSeconds'], 0)
                progress['filesTotal'] = 999
                self.assertEqual(service.sync_status()['progress']['filesTotal'], 1)
            finally:
                release.set()
                for _ in range(100):
                    if not service.sync_status()['running']:
                        break
                    time.sleep(.02)
            done = service.sync_status()
            self.assertFalse(done['running'])
            self.assertEqual(done['progress']['phase'], 'complete')
            self.assertEqual(done['progress']['sessionsDone'], 1)
            self.assertEqual(done['result']['added'], 1)

    def test_sync_lock(self):
        service = self.service([])
        service._sync_lock.acquire()
        try:
            with self.assertRaises(SyncBusy):
                service.sync_sessions()
        finally:
            service._sync_lock.release()

    def test_legacy_database_not_migrated(self):
        path = self.root / 'legacy.db'
        conn = sqlite3.connect(path)
        try:
            conn.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, project TEXT)')
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            Database(path).init_schema()
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(ValueError):
            Database(Path(__file__).resolve().parents[1] / 'claude.db')
        foreign = self.root / 'foreign.db'
        conn = sqlite3.connect(foreign)
        try:
            conn.execute('CREATE TABLE threads(id TEXT PRIMARY KEY)')
            conn.commit()
        finally:
            conn.close()
        before = foreign.read_bytes()
        with self.assertRaises(ValueError):
            Database(foreign).init_schema()
        self.assertEqual(foreign.read_bytes(), before)

    def test_unknown_root_configuration_refused(self):
        with self.assertRaises(ValueError):
            default_adapters({'unknown': []})
        with self.assertRaises(ValueError):
            default_adapters({'pi': 'not-an-array'})


class APITests(FixtureCase):
    def setUp(self):
        super().setUp()
        write_lines(self.root / 'session.jsonl', pi_records() + [
            {'type': 'message', 'id': 'a', 'parentId': 'u', 'message': {'role': 'assistant', 'content': 'answer'}}])
        self.viewer = self.service([PiAdapter([self.root])]); self.viewer.sync_sessions()
        self.app = create_app(self.viewer)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_pagination_validation_and_not_found(self):
        page = self.client.get('/api/sessions/pi:shared/messages?limit=1').json
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['nextCursor'], 1)
        second = self.client.get('/api/sessions/pi:shared/messages?limit=1&cursor=1').json
        self.assertIsNone(second['nextCursor'])
        self.assertEqual(second['items'][0]['orderId'], 1)
        for url in ['/api/sessions?limit=0', '/api/sessions?cursor=-1', '/api/sessions?limit=no']:
            self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.get('/api/sessions/absent/messages').status_code, 404)
        self.assertEqual(self.client.get('/missing-route').status_code, 404)

    def test_message_pages_use_2000_without_expanding_other_endpoints(self):
        with (self.root / 'session.jsonl').open('a', encoding='utf-8') as stream:
            for i in range(1999):
                stream.write(json.dumps({'type': 'message', 'id': f'more-{i}', 'parentId': 'a',
                    'message': {'role': 'assistant', 'content': f'answer {i}'}}) + '\n')
        self.assertEqual(self.viewer.sync_sessions()['failed'], 0)
        response = self.client.get('/api/sessions/pi:shared/messages')
        self.assertEqual(response.status_code, 200)
        page = response.json
        self.assertEqual(page['total'], 2001)
        self.assertEqual(len(page['items']), 2000)
        self.assertEqual(page['nextCursor'], 2000)
        self.assertEqual(page['items'][-1]['orderId'], 1999)
        self.assertEqual(len(self.viewer.get_messages('pi:shared')['items']), 2000)
        second = self.client.get('/api/sessions/pi:shared/messages?limit=2000&cursor=2000')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json['items']), 1)
        self.assertEqual(second.json['items'][0]['orderId'], 2000)
        self.assertIsNone(second.json['nextCursor'])
        for url in ['/api/sessions/pi:shared/messages?limit=2001', '/api/sessions?limit=501',
                    '/api/sessions/pi:shared/records?limit=501']:
            self.assertEqual(self.client.get(url).status_code, 400, url)

    def test_sources_filter_search_and_raw(self):
        sources = self.client.get('/api/sources').json
        self.assertEqual(sources[0]['id'], 'pi')
        self.assertFalse(sources[0]['capabilities']['sourceDelete'])
        response = self.client.get('/api/sources')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/api/sessions?source=codex').json['total'], 0)
        self.assertEqual(self.client.get('/api/sessions?q=hello').json['total'], 1)
        item = self.client.get('/api/sessions/pi:shared/messages').json['items'][0]
        self.assertEqual(self.client.get('/api/messages/' + item['id'] + '/raw').json['records'][0]['record']['type'], 'message')

    def test_cli_syncs_before_listening_by_default(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        env['VIEWER_DB'] = str(self.root / 'cli.db')
        env['VIEWER_SOURCE_ROOTS'] = json.dumps({
            'claude': [], 'codex': [], 'copilot_cli': [], 'vscode_chat': [], 'pi': [str(self.root)]})
        process = subprocess.Popen([sys.executable, '-u', 'server.py', '--port', str(port), '--include-recent'],
                                   cwd=Path(__file__).resolve().parents[1], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            page = None
            for _ in range(100):
                if process.poll() is not None:
                    self.fail(process.stdout.read().decode())
                try:
                    with urllib.request.urlopen(f'http://localhost:{port}/api/sessions', timeout=.5) as response:
                        page = json.load(response)
                    break
                except OSError:
                    time.sleep(.05)
            self.assertIsNotNone(page, 'CLI server failed to start')
            self.assertEqual(page['total'], 1)
            with urllib.request.urlopen(f'http://localhost:{port}/api/sync/status', timeout=2) as response:
                status = json.load(response)
            self.assertFalse(status['running'])
            self.assertEqual(status['result']['added'], 1)
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()

    def test_async_start_endpoint_returns_immediately_and_status_completes(self):
        response = self.client.post('/api/sync/start', headers={'X-Viewer-Request': '1'})
        self.assertEqual(response.status_code, 202)
        for _ in range(100):
            state = self.client.get('/api/sync/status').json
            if not state['running']:
                break
            time.sleep(.02)
        self.assertFalse(state['running'])
        self.assertEqual(state['result']['unchanged'], 1)
        self.assertEqual(state['progress']['phase'], 'complete')

    def test_mutation_origin_host_and_delete_disabled(self):
        self.assertEqual(self.client.post('/api/sync').status_code, 403)
        self.assertEqual(self.client.post('/api/sync', headers={'X-Viewer-Request': '1', 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/sources', headers={'Host': 'evil.example'}).status_code, 403)
        response = self.client.post('/api/sync', headers={'X-Viewer-Request': '1', 'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['unchanged'], 1)
        self.assertEqual(self.client.delete('/session/pi:shared', headers={'X-Viewer-Request': '1'}).status_code, 405)
        self.assertTrue((self.root / 'session.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
