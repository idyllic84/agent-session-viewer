"""Inline chips must survive the assistant-only transcript/search projection."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from local.models import searchable_text
from local.service import ClaudeViewerService
from local.sources.base import SourceFile
from local.sources.vscode_chat import VSCodeChatAdapter


class InlineReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def document(self, response, sid='inline', jsonl=False):
        state = {'sessionId': sid, 'requests': [{'requestId': 'r', 'message': {'text': 'question'}, 'response': response}]}
        path = self.root / (sid + ('.jsonl' if jsonl else '.json'))
        data = {'kind': 0, 'v': state} if jsonl else state
        path.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
        return VSCodeChatAdapter([self.root]).parse(SourceFile(path)), path, state

    def assistant_text(self, doc):
        return [m['message']['content'][0]['text'] for m in doc.messages if m['type'] == 'assistant']

    def test_reported_sentence_is_complete_in_assistant_content_and_search(self):
        response = [
            {'kind': 'markdown', 'value': '用 '},
            {'kind': 'inlineReference', 'inlineReference': 'event.item.model_dump()'},
            {'kind': 'markdown', 'value': ' 转发上游 '},
            {'kind': 'inlineReference', 'inlineReference': 'conversation.item.added'}]
        original = copy.deepcopy(response)
        expected = '用 event.item.model_dump() 转发上游 conversation.item.added'
        for jsonl in (False, True):
            with self.subTest(jsonl=jsonl):
                doc, path, state = self.document(response, jsonl=jsonl)
                self.assertEqual(self.assistant_text(doc), [expected])
                message = next(m for m in doc.messages if m['type']=='assistant')
                self.assertEqual(message['sourceLocator']['chunkIndices'], [0, 1, 2, 3])
                self.assertEqual([r['label'] for r in message['inlineReferences']],
                                 ['event.item.model_dump()', 'conversation.item.added'])
                self.assertEqual(searchable_text(message['message']['content']), expected)
                raw = doc.records[0][1]
                self.assertEqual((raw['v'] if jsonl else raw)['requests'][0]['response'], original)
        self.assertEqual(response, original)

    def test_visible_names_and_symbol_locations_and_uri_fallback(self):
        response = [
            {'value': 'A '},
            {'kind': 'inlineReference', 'name': 'display.name()', 'inlineReference': {'name': 'other', 'path': '/wrong'}},
            {'value': ' B '},
            {'kind': 'inlineReference', 'inlineReference': {'name': 'symbol()', 'containerName': 'SomeClass',
             'location': {'uri': {'scheme': 'file', 'path': '/C:/code/file.py'}, 'range': {'startLineNumber': 2}}}},
            {'value': ' C '},
            {'kind': 'inlineReference', 'inlineReference': {'$mid': 1, 'scheme': 'file', 'fsPath': 'C:\\code\\file name.py'}},
            {'value': ' D '},
            {'kind': 'inlineReference', 'inlineReference': {'uri': 'file:///C:/code/a%20b.py'}}]
        doc, _, _ = self.document(response)
        self.assertEqual(self.assistant_text(doc), ['A display.name() B symbol() C file name.py D a b.py'])
        message = next(m for m in doc.messages if m['type']=='assistant')
        self.assertEqual(message['inlineReferences'][1]['reference']['location']['range']['startLineNumber'], 2)

    def test_markdown_string_unicode_whitespace_and_no_inserted_newlines(self):
        response = [
            {'kind': 'markdown', 'value': {'value': '\n  中文🌍：', 'isTrusted': False}},
            {'kind': 'inlineReference', 'inlineReference': {'name': '处理()'}},
            {'kind': 'markdownContent', 'content': {'value': '  \n下一行'}},
            {'kind': 'markdown', 'value': '。'}]
        doc, _, _ = self.document(response)
        self.assertEqual(self.assistant_text(doc), ['\n  中文🌍：处理()  \n下一行。'])

    def test_text_does_not_merge_across_tools_thinking_or_requests(self):
        response = [
            {'value': 'before '}, {'kind': 'inlineReference', 'inlineReference': 'ref'},
            {'kind': 'toolInvocationSerialized', 'toolCallId': 'call', 'toolId': 'read'},
            {'value': 'after '}, {'kind': 'inlineReference', 'inlineReference': {'name': 'next'}},
            {'kind': 'thinking', 'value': 'reasoning'}, {'value': 'final'}]
        doc, _, _ = self.document(response)
        self.assertEqual(self.assistant_text(doc), ['before ref', 'after next', 'final'])
        self.assertEqual([m['type'] for m in doc.messages], ['user', 'assistant', 'tool-use', 'assistant', 'thinking', 'assistant'])

    def test_text_runs_never_merge_across_requests(self):
        state = {'sessionId': 'requests', 'requests': [
            {'requestId': 'r1', 'message': {'text': 'first question'}, 'response': [
                {'value': 'first '}, {'kind': 'inlineReference', 'inlineReference': 'ref1'}]},
            {'requestId': 'r2', 'message': {'text': 'second question'}, 'response': [
                {'value': 'second '}, {'kind': 'inlineReference', 'inlineReference': 'ref2'}]}]}
        path = self.root / 'requests.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        doc = VSCodeChatAdapter([self.root]).parse(SourceFile(path))
        self.assertEqual(self.assistant_text(doc), ['first ref1', 'second ref2'])
        self.assertEqual([m['sourceLocator']['requestIndex'] for m in doc.messages if m['type']=='assistant'], [0, 1])

    def test_unresolved_reference_is_explicit_not_silently_empty(self):
        doc, _, _ = self.document([{'value': 'before '}, {'kind': 'inlineReference', 'resolveId': 'unknown',
                                  'inlineReference': {'unsupported': True}}, {'value': ' after'}])
        self.assertEqual(self.assistant_text(doc), ['before [unresolved inline reference] after'])
        self.assertTrue(doc.warnings)
        ref = doc.messages[1]['inlineReferences'][0]
        self.assertEqual(ref['resolveId'], 'unknown')
        self.assertEqual(ref['reference'], {'unsupported': True})

    def test_reference_only_response_and_empty_markdown(self):
        doc, _, _ = self.document([{'value': ''}, {'kind': 'inlineReference', 'inlineReference': {'name': 'only.ref'}}])
        self.assertEqual(self.assistant_text(doc), ['only.ref'])
        doc, _, _ = self.document([{'value': ''}, {'kind': 'markdown', 'value': None}], sid='empty')
        self.assertEqual(self.assistant_text(doc), [])

    def test_archive_resync_refreshes_old_projection_and_preserves_raw(self):
        response = [{'value': '用 '}, {'kind': 'inlineReference', 'name': 'event.item.model_dump()',
                      'inlineReference': {'scheme': 'file', 'path': '/code/event.py'}}, {'value': ' 转发上游 '},
                    {'kind': 'inlineReference', 'inlineReference': {'name': 'conversation.item.added'}}]
        _, path, state = self.document(response, jsonl=True)
        adapter = VSCodeChatAdapter([path])
        viewer = ClaudeViewerService(self.root/'archive.db', [adapter], skip_recent_seconds=0); viewer.initialize()
        viewer.sync_sessions()
        # Emulate a v2 parser watermark on the same file: v3 must force refresh.
        with viewer._db.get_connection() as conn:
            conn.execute('update source_files set parser_version=2')
            conn.execute('update sessions set parser_version=2')
        result = viewer.sync_sessions()
        self.assertEqual(result['updated'], 1)
        page = viewer.get_messages('vscode_chat:inline')
        assistant = next(m for m in page['items'] if m['type']=='assistant')
        expected = '用 event.item.model_dump() 转发上游 conversation.item.added'
        self.assertEqual(assistant['message']['content'], [{'type': 'text', 'text': expected}])
        self.assertEqual(len(viewer.search_sessions_by_content(expected)), 1)
        self.assertEqual(viewer.get_raw_message(assistant['id'])['records'][0]['record']['v'], state)
        self.assertEqual(viewer.sync_sessions()['unchanged'], 1)


if __name__ == '__main__':
    unittest.main()
