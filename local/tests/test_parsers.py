"""Source adapter/parser tests. Synthetic fixtures only; never reads real history."""
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

from server.contracts.models import SessionDocument, timestamp_ms
from local.sources import default_adapters
from local.sources.base import SourceError, SourceFile, read_records
from local.sources.claude import ClaudeAdapter
from local.sources.codex import CodexAdapter
from local.sources.copilot_cli import CopilotCLIAdapter
from local.sources.pi import PiAdapter
from local.sources.vscode_chat import VSCodeChatAdapter, replay_operations, folder_path

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


if __name__ == '__main__':
    unittest.main()
