import { formatTime, extractMessagePreview, extractFullMessageText, getMessageTypeClass, getMessageTypeName, escapeHtml } from './utils.js';

export class ConversationView {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.messages = [];
        this.filteredMessages = [];
        this.onMessageSelect = null;
        this.onFilterChange = null;
    }

    render(messages) {
        this.messages = messages;
        this.filteredMessages = messages;
        this.renderMessages();

        if (this.onFilterChange) {
            this.onFilterChange();
        }
    }

    renderMessages() {
        if (!this.filteredMessages || this.filteredMessages.length === 0) {
            this.container.innerHTML = '<div class="empty-state"><p>No messages match the filter</p></div>';
            return;
        }

        const scrollParent = this.container.parentElement;
        const scrollTop = scrollParent.scrollTop;
        this.container.innerHTML = this.filteredMessages.map((msg, index) => this.renderMessage(msg, index)).join('');
        this.attachEventListeners();
        scrollParent.scrollTop = scrollTop;
    }

    renderMessage(message, index) {
        const typeClass = getMessageTypeClass(message);
        const typeName = getMessageTypeName(message);
        const timestamp = formatTime(message.timestamp, message.timestampUnit);
        const orderId = `#${message.orderId ?? index}`;
        const uuid = message.uuid ? message.uuid.substring(0, 8) : 'N/A';

        // Get preview based on message type
        let preview = '';
        const type = message.type;

        if (type === 'summary') {
            preview = message.summary || extractMessagePreview(message.message) || '';
        } else if (message.message) {
            if (message.message) {
                preview = extractMessagePreview(message.message);
            }
        } else if (type === 'file-history-snapshot') {
            if (message.snapshot?.trackedFileBackups) {
                const files = Object.keys(message.snapshot.trackedFileBackups);
                if (files.length > 0) {
                    preview = files.join(', ');
                }
            }
        } else {
            if (message.content) {
                if (typeof message.content === 'string') {
                    preview = message.content.length > 150 ? message.content.substring(0, 150) + '...' : message.content;
                } else {
                    preview = JSON.stringify(message.content).substring(0, 150) + '...';
                }
            } else if (message.message?.content) {
                if (typeof message.message.content === 'string') {
                    preview = message.message.content.length > 150 ? message.message.content.substring(0, 150) + '...' : message.message.content;
                } else {
                    preview = JSON.stringify(message.message.content).substring(0, 150) + '...';
                }
            }
        }
        // If still empty, preview remains empty string

        return `
            <div class="message-item ${typeClass}" data-message-index="${index}" data-uuid="${escapeHtml(message.uuid || '')}">
                <div class="message-header">
                    <span class="message-order-id">${escapeHtml(orderId)}</span>
                    <span class="message-time">${escapeHtml(timestamp)}</span>
                    <span class="message-type ${typeClass}">${escapeHtml(typeName)}</span>
                    <span class="message-uuid">${escapeHtml(uuid)}</span>
                </div>
                <div class="message-preview">${escapeHtml(preview)}</div>
            </div>
        `;
    }

    attachEventListeners() {
        const items = this.container.querySelectorAll('.message-item');
        items.forEach((item, displayIndex) => {
            item.addEventListener('click', () => {
                const messageIndex = parseInt(item.dataset.messageIndex);
                this.selectMessage(messageIndex, displayIndex);
            });
        });
    }

    selectMessage(messageIndex, displayIndex) {
        // Update UI
        const items = this.container.querySelectorAll('.message-item');
        items.forEach((item, index) => {
            if (index === displayIndex) {
                item.classList.add('active');
                item.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                item.classList.remove('active');
            }
        });

        // Trigger callback with the actual message
        if (this.onMessageSelect && this.filteredMessages[messageIndex]) {
            this.onMessageSelect(this.filteredMessages[messageIndex]);
        }
    }

    getAvailableMessageTypes() {
        const types = new Set();
        this.messages.forEach(msg => {
            const typeClass = getMessageTypeClass(msg);
            types.add(typeClass);
        });
        return Array.from(types);
    }

    filterByTypes(activeFilters) {
        if (activeFilters.size === 0) {
            // No filters active, show all messages
            this.filteredMessages = this.messages;
        } else {
            // Filter messages by active types
            this.filteredMessages = this.messages.filter(msg => {
                const typeClass = getMessageTypeClass(msg);
                return activeFilters.has(typeClass);
            });
        }
        this.renderMessages();
    }

    // Search in UUID and full message text content
    search(searchTerm) {
        if (!searchTerm || searchTerm.trim() === '') {
            return [];
        }

        const term = searchTerm.toLowerCase().trim();
        const matchedIndices = [];

        this.filteredMessages.forEach((msg, index) => {
            // Search in UUID
            if (msg.uuid && msg.uuid.toLowerCase().includes(term)) {
                matchedIndices.push(index);
                return;
            }

            // Search in full message text
            const fullText = extractFullMessageText(msg);
            if (fullText.toLowerCase().includes(term)) {
                matchedIndices.push(index);
            }
        });

        return matchedIndices;
    }

    // Highlight multiple messages with persistent low-key style
    highlightMessages(indices) {
        if (!indices || indices.length === 0) {
            return;
        }

        const items = this.container.querySelectorAll('.message-item');

        // Clear previous search highlights
        items.forEach(item => item.classList.remove('search-highlight'));

        // Add search highlight to matched messages
        indices.forEach(index => {
            if (index >= 0 && index < items.length) {
                items[index].classList.add('search-highlight');
            }
        });

        // Scroll to first match (without selecting)
        if (indices.length > 0 && items[indices[0]]) {
            items[indices[0]].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    // Clear all search highlights
    clearHighlights() {
        const items = this.container.querySelectorAll('.message-item');
        items.forEach(item => item.classList.remove('search-highlight'));
    }

    clear() {
        this.container.innerHTML = '<div class="empty-state"><p>Select a session to view conversation history</p></div>';
        this.messages = [];
        this.filteredMessages = [];
    }
}
