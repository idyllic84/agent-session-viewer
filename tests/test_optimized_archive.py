"""Regression tests for archive v2; synthetic data, no real user history."""
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from codec import pack_json, unpack_json
from db import Database, ARCHIVE_APPLICATION_ID
from models import compact_value, searchable_text
from service import ClaudeViewerService
from sources.base import SourceFile, read_records
from sources.codex import CodexAdapter
from sources.pi import PiAdapter
from sources.vscode_chat import VSCodeChatAdapter


def lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    return path


def pi(sid, value):
    return [{'type': 'session', 'version': 3, 'id': sid, 'cwd': '/project'},
            {'type': 'message', 'id': 'u', 'parentId': None, 'message': {'role': 'user', 'content': value}}]


class OptimizedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def viewer(self, adapters):
        viewer = ClaudeViewerService(self.root / 'archive.db', adapters, skip_recent_seconds=0)
        viewer.initialize()
        return viewer

    def test_lossless_json_codec_small_large_streaming_and_unicode(self):
        for value in [{'text': '你好 🌍'}, {'data': 'A' * 100000, 'parts': [None, True, 42, {'emoji': '🦊'}]}]:
            for streaming in (False, True):
                packed = pack_json(value, streaming=streaming)
                self.assertEqual(unpack_json(packed), value)
        self.assertLess(len(pack_json({'text': 'test ' * 10000})), 2000)
        with self.assertRaises(ValueError):
            unpack_json(b'unknown')

    def test_binary_and_opaque_removed_recursively_but_plain_text_preserved(self):
        original = [{'type': 'tool_result', 'content': {'output': [
            {'type': 'text', 'text': 'error details remain searchable'},
            {'type': 'image', 'mimeType': 'image/png', 'data': 'IMAGEPAYLOAD'},
            {'type': 'document', 'name': 'report.pdf', 'source':
             {'type': 'base64', 'media_type': 'application/pdf', 'data': 'PDFPAYLOAD'}},
            {'thinking': {'text': 'clear reasoning', 'encrypted': 'CIPHERPAYLOAD', 'signature': 'SIGNATUREPAYLOAD'}}]}}]
        before = copy.deepcopy(original)
        compact = compact_value(original)
        self.assertEqual(original, before)
        rendered = json.dumps(compact)
        indexed = searchable_text(original)
        for payload in ('IMAGEPAYLOAD', 'PDFPAYLOAD', 'CIPHERPAYLOAD', 'SIGNATUREPAYLOAD'):
            self.assertNotIn(payload, rendered)
            self.assertNotIn(payload, indexed)
        for phrase in ('error details remain searchable', 'clear reasoning', 'report.pdf'):
            self.assertIn(phrase, indexed)
        self.assertIn('retainedInRawRecord', rendered)

    def test_embedded_data_uri_not_indexed_and_adjacent_text_preserved(self):
        content = [{'type': 'text', 'text': 'before ![](data:image/png;base64,QUJDREVGR0g=) after'}]
        self.assertNotIn('QUJDREVGR0g', searchable_text(content))
        self.assertIn('before', searchable_text(content)); self.assertIn('after', searchable_text(content))

    def test_json_encoded_tool_output_does_not_hide_nested_binary(self):
        output = json.dumps({'content': [{'type': 'image', 'mimeType': 'image/png', 'data': 'HIDDENIMAGE'},
                                         {'type': 'text', 'text': 'meaningful JSON tool output'}]})
        blocks = [{'type': 'tool_result', 'content': {'output': [{'type': 'embed', 'isText': True, 'value': output}]}}]
        self.assertNotIn('HIDDENIMAGE', json.dumps(compact_value(blocks)))
        self.assertNotIn('HIDDENIMAGE', searchable_text(blocks))
        self.assertIn('meaningful JSON tool output', searchable_text(blocks))

    def test_raw_binary_and_cipher_losslessly_retained_in_compressed_archive(self):
        rows = pi('binary', [{'type': 'text', 'text': 'question'},
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'A' * 100000}},
                {'type': 'thinking', 'thinking': 'clear', 'signature': 'opaque-signature'}])
        path = lines(self.root / 's.jsonl', rows)
        viewer = self.viewer([PiAdapter([self.root])]); result = viewer.sync_sessions()
        self.assertEqual(result['added'], 1)
        item = viewer.get_messages('pi:binary')['items'][0]
        self.assertNotIn('A' * 100, json.dumps(item))
        self.assertEqual(viewer.get_raw_message(item['id'])['records'][0]['record'], rows[1])
        with viewer._db.get_connection() as conn:
            blob = conn.execute('select raw_json from raw_records where record_index=1').fetchone()[0]
            self.assertIsInstance(blob, bytes)
            self.assertLess(len(blob), 2000)

    def test_shared_text_keeps_distinct_messages_and_sessions(self):
        lines(self.root / 'a.jsonl', pi('a', 'identical searchable text'))
        lines(self.root / 'b.jsonl', pi('b', 'identical searchable text'))
        viewer = self.viewer([PiAdapter([self.root])]); viewer.sync_sessions()
        with viewer._db.get_connection() as conn:
            self.assertEqual(conn.execute('select count(*) from messages').fetchone()[0], 2)
            self.assertEqual(conn.execute('select count(*) from search_content').fetchone()[0], 1)
            self.assertEqual(conn.execute('select count(*) from message_texts').fetchone()[0], 2)
        self.assertEqual(len(viewer.search_sessions_by_content('searchable')), 2)
        lines(self.root / 'a.jsonl', pi('a', 'different text'))
        viewer.sync_sessions()
        self.assertEqual([s['id'] for s in viewer.search_sessions_by_content('identical')], ['pi:b'])
        lines(self.root / 'b.jsonl', pi('b', 'different text'))
        viewer.sync_sessions()
        self.assertEqual(viewer.search_sessions_by_content('identical'), [])
        with viewer._db.get_connection() as conn:
            self.assertEqual(conn.execute('select count(*) from search_content').fetchone()[0], 1)
            self.assertEqual(conn.execute('pragma foreign_key_check').fetchall(), [])

    def test_compact_trigram_matches_exact_substrings_not_just_unordered_grams(self):
        lines(self.root / 'a.jsonl', pi('a', 'Authentication 中文数据库 100%_done quoted"text a b abc---bcd long ' + 'unique' * 20))
        viewer = self.viewer([PiAdapter([self.root])]); viewer.sync_sessions()
        self.assertTrue(viewer._db.has_trigram)
        for query in ['authent', '中文', '数据库', '100%_done', 'quoted"text', 'a b', 'unique' * 20]:
            self.assertEqual(len(viewer.search_sessions_by_content(query)), 1, query)
        self.assertEqual(viewer.search_sessions_by_content('abcd'), [])
        with viewer._db.get_connection() as conn:
            sql = conn.execute("select sql from sqlite_master where name='message_search'").fetchone()[0]
            self.assertIn('detail=none', sql)
            self.assertIsNone(conn.execute("select name from sqlite_master where name='message_search_docsize'").fetchone())
            conn.execute("INSERT INTO message_search(message_search,rank) VALUES ('integrity-check',1)")

    def test_v1_requires_explicit_rebuild_and_is_not_modified(self):
        path = self.root / 'v1.db'
        conn = sqlite3.connect(path)
        conn.execute('create table sessions(id TEXT, source TEXT)')
        conn.execute(f'pragma application_id={ARCHIVE_APPLICATION_ID}')
        conn.execute('pragma user_version=1'); conn.commit(); conn.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            Database(path).init_schema()
        self.assertEqual(path.read_bytes(), before)

    def test_codex_child_ownership_recovers_historical_turns_before_eof_boundary(self):
        records = [
            {'type': 'session_meta', 'ordinal': 0, 'payload': {'id': 'child', 'history_mode': 'paginated',
             'subagent_history_start_ordinal': 100, 'parent_thread_id': 'parent'}},
            {'type': 'turn_context', 'ordinal': 1, 'payload': {'turn_id': 'foreign'}},
            {'type': 'response_item', 'ordinal': 2, 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'inherited parent context'}]}},
            {'type': 'event_msg', 'ordinal': 3, 'payload': {'type': 'item_completed', 'thread_id': 'parent',
             'turn_id': 'foreign', 'item': {'type': 'AgentMessage', 'id': 'parent-msg', 'content': [{'type': 'Text', 'text': 'parent answer'}]}}},
            {'type': 'event_msg', 'ordinal': 4, 'payload': {'type': 'task_started', 'turn_id': 'owned'}},
            {'type': 'turn_context', 'ordinal': 5, 'payload': {'turn_id': 'owned', 'model': 'test'}},
            {'type': 'event_msg', 'ordinal': 6, 'payload': {'type': 'item_completed', 'thread_id': 'child',
             'turn_id': 'owned', 'item': {'type': 'UserMessage', 'id': 'u', 'content': [{'type': 'text', 'text': 'child task'}]}}},
            {'type': 'response_item', 'ordinal': 7, 'payload': {'type': 'function_call', 'call_id': 'c', 'name': 'shell', 'arguments': '{}'}},
            {'type': 'response_item', 'ordinal': 8, 'payload': {'type': 'function_call_output', 'call_id': 'c', 'output': 'child result'}},
            {'type': 'event_msg', 'ordinal': 9, 'payload': {'type': 'item_completed', 'thread_id': 'child',
             'turn_id': 'owned', 'item': {'type': 'AgentMessage', 'id': 'a', 'content': [{'type': 'Text', 'text': 'child answer'}]}}},
            {'type': 'event_msg', 'ordinal': 10, 'payload': {'type': 'task_complete', 'turn_id': 'owned'}}]
        path = lines(self.root / 'child.jsonl', records)
        doc = CodexAdapter([self.root]).parse(SourceFile(path))
        self.assertGreater(doc.metadata['recoveredPreBoundaryRecords'], 0)
        self.assertEqual(sum(m['type']=='user' for m in doc.messages), 1)
        self.assertEqual(sum(m['type']=='assistant' for m in doc.messages), 1)
        self.assertEqual(sum(m['type']=='tool-use' for m in doc.messages), 1)
        self.assertEqual(sum(m['type']=='tool-result' for m in doc.messages), 1)
        self.assertNotIn('parent answer', str(doc.messages))
        self.assertNotIn('inherited parent context', str(doc.messages))

    def test_codex_no_ownership_proof_does_not_disable_boundary(self):
        records = [{'type': 'session_meta', 'ordinal': 0, 'payload': {'id': 'child', 'history_mode': 'paginated',
                    'subagent_history_start_ordinal': 100}},
                   {'type': 'response_item', 'ordinal': 1, 'payload': {'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'inherited only'}]}}]
        doc = CodexAdapter([self.root]).parse(SourceFile(lines(self.root / 's.jsonl', records)))
        self.assertEqual(doc.messages, [])

    def test_vscode_summary_does_not_copy_rounds_or_rendered_context_and_pairs_output(self):
        result = {'timings': {'totalElapsed': 100}, 'errorDetails': {'message': 'useful error'}, 'metadata': {
            'promptTokens': 1000, 'outputTokens': 50, 'resolvedModel': 'm', 'renderedGlobalContext': 'REPEATEDCONTEXT' * 1000,
            'toolCallRounds': [{'thinking': {'encrypted': 'OPAQUE' * 1000},
                               'toolCalls': [{'id': 'call', 'name': 'read', 'arguments': '{"path":"file.py"}'}]}],
            'toolCallResults': {'call__vscode-1234': {'content': [{'$mid': 23, 'value': 'actual tool output'}]}}}}
        state = {'sessionId': 'vs', 'requests': [{'requestId': 'r', 'message': {'text': 'question'}, 'result': result,
            'response': [{'kind': 'toolInvocationSerialized', 'toolCallId': 'call', 'toolId': 'read'}, {'value': 'answer'}]}]}
        path = self.root / 's.json'; path.write_text(json.dumps(state), encoding='utf-8')
        doc = VSCodeChatAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(sum(m['type']=='tool-result' for m in doc.messages), 1)
        tool = next(m for m in doc.messages if m['type']=='tool-use')
        self.assertEqual(tool['message']['content'][0]['input'], {'path': 'file.py'})
        self.assertNotIn('REPEATEDCONTEXT', str(doc.messages))
        self.assertNotIn('OPAQUE', str(doc.messages))
        self.assertIn('actual tool output', str(doc.messages))
        summary = doc.messages[-1]['message']['content'][0]['value']
        self.assertEqual(summary['usage']['promptTokens'], 1000)
        self.assertEqual(summary['errorDetails']['message'], 'useful error')
        self.assertNotIn('toolCallResults', summary)
        self.assertEqual(doc.records[0][1]['requests'][0]['result'], result)

    def test_vscode_unpaired_results_preserved_without_guessing(self):
        state = {'sessionId': 'vs', 'requests': [{'message': {'text': 'question'},
            'response': [{'kind': 'toolInvocationSerialized', 'toolCallId': 'ui-id', 'toolId': 'read'}],
            'result': {'metadata': {'toolCallResults': {'model-id__vscode-123': {'content': [{'$mid': 23, 'value': 'output'}]}}}}}]}
        path = self.root / 's.json'; path.write_text(json.dumps(state), encoding='utf-8')
        doc = VSCodeChatAdapter([self.root]).parse(SourceFile(path))
        results = [m for m in doc.messages if m['type']=='tool-result']
        self.assertEqual(len(results), 1); self.assertTrue(results[0]['unpaired'])
        self.assertEqual(results[0]['toolCallId'], 'model-id')

    def test_rebuild_validates_switches_and_retains_backup(self):
        from functools import partial
        from rebuild import rebuild as rebuild_archive
        rebuild = partial(rebuild_archive, skip_recent_seconds=0)
        path = lines(self.root / 's.jsonl', pi('rebuild', 'hello'))
        target = self.root / 'rebuilt.db'
        first = rebuild(target, adapters=[PiAdapter([self.root])])
        self.assertIsNone(first['backup'])
        second = rebuild(target, adapters=[PiAdapter([self.root])])
        self.assertEqual(second['sessions'], 1)
        self.assertTrue(Path(second['backup']).exists())
        self.assertTrue(path.exists())
        self.assertEqual(Database(target).get_messages('pi:rebuild')['total'], 1)

    def test_rebuild_failure_and_missing_history_do_not_replace_original(self):
        from functools import partial
        from rebuild import rebuild as rebuild_archive
        rebuild = partial(rebuild_archive, skip_recent_seconds=0)
        path = lines(self.root / 's.jsonl', pi('keep', 'hello'))
        target = self.root / 'rebuilt.db'
        rebuild(target, adapters=[PiAdapter([self.root])])
        before = target.read_bytes()
        path.write_bytes(b'broken JSON\n')
        with self.assertRaisesRegex(RuntimeError, 'sources failed'):
            rebuild(target, adapters=[PiAdapter([self.root])])
        self.assertEqual(target.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, 'previously archived sessions'):
            rebuild(target, adapters=[])
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
