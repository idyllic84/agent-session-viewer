import { formatJson, escapeHtml } from './utils.js';
import { fetchRawMessage } from './api.js';

export class MessageDetailsView {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.currentMessage = null;
        this.generation = 0;
        this.container.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (button?.dataset.action === 'raw') this.loadRaw(button);
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
        const metadata = { source: message.source, nativeType: message.nativeType, uuid: message.uuid,
            parentUuid: message.parentUuid, model: message.model, provider: message.provider,
            usage: message.usage, status: message.status, sourceLocator: message.sourceLocator };
        this.container.innerHTML = `<div class="detail-section">
            <h3>Message Content</h3>${blocks.map(block => this.renderContentBlock(block)).join('')}
            </div><div class="detail-section"><h3>Metadata</h3>
            <pre class="json-view">${escapeHtml(formatJson(metadata))}</pre></div>
            <div class="detail-section"><h3>Original Record</h3>
                <p class="detail-note">Original source files are read-only. VS Code records are state operations; the source locator identifies the reconstructed request/chunk.</p>
                <button data-action="raw">Load original record</button><div id="rawRecord"></div>
            </div><details class="detail-section"><summary>Normalized JSON</summary>
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

    async loadRaw(button) {
        const generation = this.generation;
        const message = this.currentMessage;
        if (!message) return;
        button.disabled = true;
        try {
            const result = await fetchRawMessage(message.id);
            if (generation !== this.generation) return;
            const target = document.getElementById('rawRecord');
            target.innerHTML = `<pre class="json-view">${escapeHtml(formatJson(result))}</pre>`;
            button.hidden = true;
        } catch (error) {
            if (generation !== this.generation) return;
            document.getElementById('rawRecord').textContent = error.message;
            button.disabled = false;
        }
    }

    clear() {
        this.generation++;
        this.currentMessage = null;
        this.container.innerHTML = '<div class="empty-state"><p>Click a message to view details</p></div>';
    }
}
