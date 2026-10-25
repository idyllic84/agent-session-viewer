import { formatJson, escapeHtml } from './utils.js';

export class MessageDetailsView {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.currentMessage = null;
        this.generation = 0;
        this.container.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (button?.dataset.action === 'wrap') {
                button.closest('.content-block').querySelector('pre').classList.toggle('no-wrap');
                button.classList.toggle('active');
            }
        });
    }

    render(message) {
        this.generation++;
        this.currentMessage = message;
        if (!message) { this.clear(); return; }
        const content = message.message?.content || [];
        const blocks = Array.isArray(content) ? content : [{ type: 'text', text: content }];
        // Analysis store: no original records exist; show what the stored message really carries.
        const metadata = { id: message.id, type: message.type, time: Number.isFinite(message.timestamp) ? new Date(message.timestamp).toISOString() : null,
            timeSource: message.timestampSource, toolCallId: message.toolCallId, status: message.status };
        const approximate = message.timestampSource === 'file_mtime'
            ? '<p class="detail-note">The source had no time for this message; the file modification time is shown instead.</p>' : '';
        this.container.innerHTML = `<div class="detail-section">
            <h3>Message Content</h3>${blocks.map(block => this.renderContentBlock(block)).join('')}
            </div><div class="detail-section"><h3>Metadata</h3>
            <pre class="json-view">${escapeHtml(formatJson(metadata))}</pre>${approximate}</div>
            <details class="detail-section"><summary>Stored JSON</summary>
                <pre class="json-view">${escapeHtml(formatJson(message))}</pre></details>`;
    }

    renderContentBlock(block) {
        const kind = String(block?.type || 'unknown');
        let value;
        if (kind === 'text') value = block.text || '';
        else if (kind === 'thinking') value = block.thinking || '';
        else if (kind === 'tool_use') value = formatJson(block.input);
        else if (kind === 'tool_result') value = typeof block.content === 'string' ? block.content : formatJson(block.content);
        else if (['image', 'input_image', 'output_image'].includes(kind)) value = '[Image data retained in original record]';
        else value = formatJson(block);
        return `<div class="content-block ${kind.replace(/[^a-z0-9_-]/gi, '-')}">
            <div class="content-block-header"><span class="content-type">${escapeHtml(kind)}</span>
                <button data-action="wrap" class="wrap-toggle" title="Toggle wrap">⏎</button></div>
            ${kind === 'tool_use' ? `<div class="tool-name">${escapeHtml(block.name || 'Unknown')}</div>` : ''}
            <pre class="content-text">${escapeHtml(value)}</pre></div>`;
    }

    clear() {
        this.generation++;
        this.currentMessage = null;
        this.container.innerHTML = '<div class="empty-state"><p>Click a message to view details</p></div>';
    }
}
