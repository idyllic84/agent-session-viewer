import { formatTimestampNumeric, escapeHtml } from './utils.js';

const LABELS = { claude: 'Claude Code', codex: 'Codex', copilot_cli: 'Copilot CLI', pi: 'pi', vscode_chat: 'VS Code Chat' };

export class SessionListView {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.sessions = [];
        this.selectedId = null;
        this.onSessionSelect = null;
        this.container.addEventListener('click', event => {
            const item = event.target.closest('.session-item');
            if (!item) return;
            this.selectSessionUI(item.dataset.sessionId);
            this.onSessionSelect?.(item.dataset.sessionId);
        });
    }

    render(sessions) {
        this.sessions = sessions.filter(session => (session.userMessageCount ?? session.metadata?.userMessageCount ?? 0) > 0 && session.metadata?.relationship !== 'subagent');
        this.container.innerHTML = this.sessions.length ? this.sessions.map(session => this.renderSessionItem(session)).join('')
            : '<div class="empty-state"><p>No sessions found. Click Reload to import local history.</p></div>';
        this.selectSessionUI(this.selectedId);
    }

    renderSessionItem(session) {
        return `<div class="session-item" data-session-id="${escapeHtml(session.id)}">
            <div class="session-header">
                <span class="source-badge">${escapeHtml(LABELS[session.source] || session.source)}${session.metadata?.relationship ? ` · ${escapeHtml(session.metadata.relationship)}` : ''}</span>
                ${session.machine ? `<span class="machine-badge" title="Uploaded by this computer's key">${escapeHtml(session.machine.name)}</span>` : ''}
                <span class="message-count" title="User messages / total messages (including assistant, tool and system records)">${session.userMessageCount ?? session.metadata?.userMessageCount ?? 0} / ${session.messageCount}</span>
            </div>
            <div class="session-name" title="${escapeHtml(session.name)}">${escapeHtml(session.name)}</div>
            <div class="session-project" title="${escapeHtml(session.project)}">${escapeHtml(session.project || '(no workspace)')}</div>
            <div class="session-uuid" title="${escapeHtml(session.id)}">${escapeHtml(session.externalId)}</div>
            <div class="session-footer">
                <div class="session-time">${escapeHtml(formatTimestampNumeric(session.lastModified, session.timestampUnit))}</div>
                <span class="archive-state">${session.available ? 'Read-only' : 'Archived · source missing'}</span>
            </div>
        </div>`;
    }

    selectSessionUI(sessionId) {
        this.selectedId = sessionId;
        this.container.querySelectorAll('.session-item').forEach(item => {
            item.classList.toggle('active', item.dataset.sessionId === sessionId);
        });
    }
}
