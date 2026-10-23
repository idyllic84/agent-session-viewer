function asDate(timestamp, unit = 's') {
    if (timestamp === null || timestamp === undefined || timestamp === '') return null;
    const date = new Date(typeof timestamp === 'number' && unit !== 'ms' ? timestamp * 1000 : timestamp);
    return Number.isNaN(date.getTime()) ? null : date;
}

export function formatTime(timestamp, unit = 's') {
    return asDate(timestamp, unit)?.toLocaleTimeString('en-US', { hour12: false }) || 'N/A';
}

export function formatTimestampNumeric(timestamp, unit = 's') {
    const date = asDate(timestamp, unit);
    if (!date) return 'N/A';
    const pad = value => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[char]));
}

export function formatJson(value) {
    return JSON.stringify(value, null, 2) ?? '';
}

function contentText(content) {
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return formatJson(content);
    return content.map(block => {
        if (!block || typeof block !== 'object') return formatJson(block);
        if (block.type === 'text') return block.text || '';
        if (block.type === 'thinking') return block.thinking || '';
        if (block.type === 'tool_use') return `${block.name || ''} ${formatJson(block.input)}`;
        if (block.type === 'tool_result') return contentText(block.content);
        if (['image', 'input_image', 'output_image', 'encrypted_reasoning'].includes(block.type)) return '';
        return formatJson(block);
    }).join('\n');
}

export function extractMessagePreview(message) {
    const content = message?.content;
    const firstText = Array.isArray(content) ? content.find(b => b?.type === 'text' && b.text) : null;
    const preview = firstText?.text || contentText(content);
    return preview.length > 150 ? preview.slice(0, 150) + '…' : preview;
}

export function detectMessageContentType(message) {
    const content = message?.content;
    if (!Array.isArray(content) || !content.length) return null;
    if (content.every(b => b?.type === 'tool_use')) return 'tool_use';
    if (content.every(b => b?.type === 'tool_result')) return 'tool_result';
    if (content.some(b => b?.type === 'text')) return 'text';
    return null;
}

export function getMessageTypeClass(message) {
    if (message.type === 'user' || message.type === 'assistant') {
        const contentType = detectMessageContentType(message.message);
        if (contentType === 'tool_use') return 'tool-use';
        if (contentType === 'tool_result') return 'tool-result';
    }
    return String(message.type || 'unknown').toLowerCase().replace(/[^a-z0-9-]/g, '-');
}

export function getMessageTypeName(message) {
    return getMessageTypeClass(message).split('-').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}

export function extractFullMessageText(message) {
    return [message.uuid, message.summary, contentText(message.message?.content), contentText(message.content)]
        .filter(Boolean).join('\n');
}
