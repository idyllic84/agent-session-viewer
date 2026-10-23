"""Claude append must not decode/delete/reinsert previously archived messages."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest
from local.service import ClaudeViewerService
from local.sources.claude import ClaudeAdapter
from local.sources.vscode_chat import VSCodeChatAdapter


def row(content, uuid='u', **extra):
    return {'type': 'user', 'sessionId': 'session', 'uuid': uuid, 'cwd': '/project',
            'message': {'content': content}, **extra}


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / 'session.jsonl'
        self.path.write_text(json.dumps(row('old message')) + '\n', encoding='utf-8')
        self.adapter = ClaudeAdapter([self.root])
        self.viewer = ClaudeViewerService(self.root/'archive.db', [self.adapter], skip_recent_seconds=0)
        self.viewer.initialize(); self.viewer.sync_sessions()

    def append(self, value):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    def stored(self):
        with self.viewer._db.get_connection() as conn:
            return [tuple(r) for r in conn.execute('select message_pk,id,normalized_json from messages order by order_id')]

    def test_append_parses_only_new_rows_and_preserves_old_blobs_ids_and_order(self):
        before = self.stored()
        self.append(row('new message', 'new'))
        with (patch.object(self.adapter, 'parse', side_effect=AssertionError('Full parse must not be used')),
              patch.object(self.viewer._db, 'replace_session', side_effect=AssertionError('Old projection must not be replaced'))):
            result = self.viewer.sync_sessions()
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['appendedSessions'], 1); self.assertEqual(result['appendedMessages'], 1)
        self.assertEqual(self.stored()[:1], before)
        items = self.viewer.get_messages('claude:session')['items']
        self.assertEqual([m['orderId'] for m in items], [0, 1])
        self.assertEqual(items[1]['rawRecord'], 1)
        self.assertEqual(self.viewer.get_raw_message(items[1]['id'])['records'][0]['record']['uuid'], 'new')
        self.assertEqual(len(self.viewer.search_sessions_by_content('new message')), 1)
        self.assertEqual(self.viewer.sync_sessions()['unchanged'], 1)

    def test_blank_lines_use_physical_line_checkpoint_not_message_count(self):
        self.path.write_text(json.dumps(row('old message')) + '\n\n\n', encoding='utf-8')
        self.viewer.sync_sessions()
        self.append(row('next', 'new'))
        result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedMessages'], 1)
        self.assertEqual(self.viewer.get_messages('claude:session')['items'][-1]['rawRecord'], 3)

    def test_partial_tail_is_deferred_then_repaired_without_old_message_rewrites(self):
        before = self.stored()
        with self.path.open('ab') as stream:stream.write(b'{"type":"user","sessionId":"session"')
        result = self.viewer.sync_sessions()
        self.assertEqual(result['failed'], 0); self.assertEqual(result['appendedMessages'], 0)
        self.assertEqual(self.stored(), before)
        with self.path.open('ab') as stream:stream.write(b',"uuid":"a","message":{"content":"fixed"}}\n')
        result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedMessages'], 1)
        self.assertEqual(self.stored()[:1], before)
        self.assertEqual(self.viewer.get_messages('claude:session')['items'][-1]['rawRecord'], 1)

    def test_valid_unterminated_new_tail_is_not_imported_twice(self):
        with self.path.open('a', encoding='utf-8') as stream:stream.write(json.dumps(row('later', 'a')))
        self.assertEqual(self.viewer.sync_sessions()['appendedMessages'], 0)
        self.assertEqual(self.viewer.get_messages('claude:session')['total'], 1)
        with self.path.open('a', encoding='utf-8') as stream:stream.write('\n')
        self.assertEqual(self.viewer.sync_sessions()['appendedMessages'], 1)
        self.assertEqual(self.viewer.get_messages('claude:session')['total'], 2)

    def test_initial_unterminated_row_uses_full_reconciliation_before_append(self):
        self.path.write_text(json.dumps(row('unterminated')), encoding='utf-8')
        self.viewer.sync_sessions()
        self.assertNotIn('appendCheckpoint', self.viewer._db.get_session('claude:session')['metadata'])
        with self.path.open('a', encoding='utf-8') as stream:stream.write('\n')
        self.append(row('second', 'a'))
        with patch.object(self.adapter, 'parse', wraps=self.adapter.parse) as parse:
            result = self.viewer.sync_sessions()
        self.assertEqual(parse.call_count, 1); self.assertEqual(result['appendedSessions'], 0)
        self.append(row('third', 'b'))
        self.assertEqual(self.viewer.sync_sessions()['appendedMessages'], 1)

    def test_prefix_rewrite_with_growth_and_truncation_force_full_parse(self):
        self.path.write_bytes(self.path.read_bytes().replace(b'old message', b'new message'))
        self.append(row('extra', 'a'))
        with patch.object(self.adapter, 'parse', wraps=self.adapter.parse) as parse:
            result = self.viewer.sync_sessions()
        self.assertEqual(parse.call_count, 1); self.assertEqual(result['appendedSessions'], 0)
        self.assertEqual(self.viewer.get_messages('claude:session')['items'][0]['message']['content'][0]['text'], 'new message')
        self.path.write_text(json.dumps(row('short')) + '\n', encoding='utf-8')
        result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedSessions'], 0)
        self.assertEqual(self.viewer.get_messages('claude:session')['total'], 1)

    def test_existing_archive_is_primed_without_full_parse(self):
        with self.viewer._db.get_connection() as conn:
            metadata = json.loads(conn.execute('select metadata from sessions').fetchone()[0])
            metadata.pop('appendCheckpoint'); metadata.pop('claudeTitle')
            conn.execute('update sessions set metadata=?', (json.dumps(metadata),))
        with patch.object(self.adapter, 'parse', side_effect=AssertionError('Checkpoint priming must not decode history')):
            self.assertEqual(self.viewer.sync_sessions()['unchanged'], 1)
        self.append(row('appended after bootstrap', 'new'))
        with patch.object(self.adapter, 'parse', side_effect=AssertionError('Tail only')):
            result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedMessages'], 1)

    def test_title_precedence_custom_title_beats_appended_slug(self):
        self.append({'type': 'custom-title', 'customTitle': 'My title'})
        self.viewer.sync_sessions()
        self.assertEqual(self.viewer.get_all_sessions()[0]['name'], 'My title')
        self.append(row('another', 'a', slug='auto-title'))
        self.viewer.sync_sessions()
        self.assertEqual(self.viewer.get_all_sessions()[0]['name'], 'My title')
        self.append({'type': 'custom-title', 'customTitle': 'Renamed'})
        self.viewer.sync_sessions()
        self.assertEqual(self.viewer.get_all_sessions()[0]['name'], 'Renamed')

    def test_parser_upgrade_uses_full_parse(self):
        self.append(row('new', 'a')); self.adapter.parser_version += 1
        with patch.object(self.adapter, 'parse', wraps=self.adapter.parse) as parse:
            result = self.viewer.sync_sessions()
        self.assertEqual(parse.call_count, 1); self.assertEqual(result['appendedSessions'], 0)

    def test_append_failure_rolls_back_messages_text_and_watermark(self):
        before = self.stored(); states = self.viewer._db.file_states()
        self.append(row('should not commit', 'new'))
        original = self.adapter.parse_append
        def duplicate_id(*args):
            doc = original(*args); doc.messages[0]['id'] = before[0][1]
            return doc
        with patch.object(self.adapter, 'parse_append', side_effect=duplicate_id):
            result = self.viewer.sync_sessions()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.stored(), before); self.assertEqual(self.viewer._db.file_states(), states)
        self.assertEqual(self.viewer.search_sessions_by_content('should not commit'), [])

    def test_archive_sequence_gap_falls_back_to_full_reconciliation(self):
        with self.viewer._db.get_connection() as conn:
            conn.execute('delete from messages where session_id=?', ('claude:session',))
        self.append(row('new', 'a'))
        with patch.object(self.adapter, 'parse', wraps=self.adapter.parse) as parse:
            result = self.viewer.sync_sessions()
        self.assertEqual(parse.call_count, 1); self.assertEqual(result['appendedSessions'], 0)
        self.assertEqual(self.viewer.get_messages('claude:session')['total'], 2)

    def test_metadata_only_session_gets_real_prompt_title_and_cwd_on_append(self):
        self.path.write_text(json.dumps({'type': 'mode', 'mode': 'agent'}) + '\n', encoding='utf-8')
        self.viewer.sync_sessions()
        self.append(row('first real question', 'u'))
        result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedMessages'], 1)
        session = self.viewer._db.get_session('claude:session')
        self.assertEqual(session['name'], 'first real question')
        self.assertEqual(session['project'], '/project')
        self.assertFalse(session['metadata']['metadataOnly'])

    def test_agent_files_are_not_synced_or_listed(self):
        agent = self.root/'agent-child.jsonl'
        agent.write_text(json.dumps(row('child old')) + '\n', encoding='utf-8')
        self.viewer.sync_sessions()
        sid = 'claude:session:agent-child'
        with agent.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row('child new', 'a')) + '\n')
        result = self.viewer.sync_sessions()
        self.assertEqual(result['appendedMessages'], 0)
        self.assertEqual(result['skippedSubagents'], 1)
        self.assertIsNone(self.viewer._db.get_session(sid))
        self.assertTrue(agent.exists())

    def test_vscode_remains_full_state_replay(self):
        self.assertFalse(hasattr(VSCodeChatAdapter([]), 'parse_append'))


if __name__ == '__main__':
    unittest.main()
