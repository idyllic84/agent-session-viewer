import assert from 'node:assert/strict';
import {
    escapeHtml, formatTime, formatTimestampNumeric, getMessageTypeClass,
    extractMessagePreview, extractFullMessageText,
} from '../static/js/utils.js';

assert.equal(escapeHtml('<img title="x" onerror=\'bad\'>&'),
    '&lt;img title=&quot;x&quot; onerror=&#39;bad&#39;&gt;&amp;');
assert.equal(escapeHtml(null), '');
assert.equal(formatTime('bad'), 'N/A');
assert.equal(formatTimestampNumeric(null), 'N/A');
assert.equal(formatTimestampNumeric(1767323045), formatTimestampNumeric(1767323045000, 'ms'));
const mixed = { type: 'assistant', uuid: 'abc', message: { content: [
    { type: 'thinking', thinking: 'reasoning' },
    { type: 'text', text: 'answer' },
    { type: 'tool_use', name: 'bash', input: { command: 'pwd' } },
] } };
assert.equal(getMessageTypeClass(mixed), 'assistant');
assert.equal(extractMessagePreview(mixed.message), 'answer');
for (const value of ['abc', 'reasoning', 'answer', 'bash', 'pwd']) {
    assert.ok(extractFullMessageText(mixed).includes(value));
}
assert.equal(getMessageTypeClass({ type: 'assistant', message: { content: [
    { type: 'tool_use', name: 'read', input: {} },
] } }), 'tool-use');
assert.ok(extractFullMessageText({ message: { content: [
    { type: 'tool_result', content: [{ type: 'text', text: 'structured result' }] },
] } }).includes('structured result'));
assert.ok(!extractFullMessageText({ message: { content: [
    { type: 'image', data: 'secret-image-data' },
] } }).includes('secret-image-data'));
assert.equal(getMessageTypeClass({ type: '\" onmouseover=bad' }), '--onmouseover-bad');
const referenceSentence = '用 event.item.model_dump() 转发上游 conversation.item.added';
const referenced = { type: 'assistant', message: { content: [{ type: 'text', text: referenceSentence }] } };
assert.equal(extractMessagePreview(referenced.message), referenceSentence);
assert.equal(extractFullMessageText(referenced), referenceSentence);
console.log('Frontend utility assertions passed');
