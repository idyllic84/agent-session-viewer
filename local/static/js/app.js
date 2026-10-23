import { fetchSources, fetchSessions, fetchSessionMessages, syncSessions, fetchSyncStatus } from './api.js';
import { SessionListView } from './sessionList.js';
import { ConversationView } from './conversationView.js';
import { MessageDetailsView } from './messageDetails.js';
import { PanelResizer } from './resizer.js';
import { escapeHtml } from './utils.js';

class App {
    constructor() {
        this.sessionListView = new SessionListView('sessionList');
        this.conversationView = new ConversationView('conversationHistory');
        this.messageDetailsView = new MessageDetailsView('messageDetails');
        this.panelResizer = new PanelResizer();
        this.activeFilters = new Set(['user', 'assistant']);
        this.currentSessionId = null;
        this.listGeneration = 0;
        this.messageGeneration = 0;
        this.nextCursor = null;
        this.loadingMore = false;
        this.searchTimer = null;
        this.sessionSearchController = null;
        this.lastSearchQuery = '';
        this.sessionListView.onSessionSelect = id => this.loadSession(id);
        this.conversationView.onMessageSelect = message => this.messageDetailsView.render(message);
        document.getElementById('reloadBtn').addEventListener('click', () => this.reloadSessions());
        document.getElementById('sourceSelect').addEventListener('change', () => {
            document.getElementById('projectSelect').value = '';
            this.loadSessions();
        });
        document.getElementById('projectSelect').addEventListener('change', () => this.renderSessions());
        const searchInput = document.getElementById('sessionSearch');
        const scheduleSearch = () => {
            clearTimeout(this.searchTimer);
            this.searchTimer = setTimeout(() => this.loadSessions(), 350);
        };
        searchInput.addEventListener('input', e => { if (!e.isComposing) scheduleSearch(); });
        searchInput.addEventListener('compositionend', scheduleSearch);
        searchInput.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.isComposing) { clearTimeout(this.searchTimer); this.loadSessions(); }
        });
        document.getElementById('sessionSearchBtn').addEventListener('click', () => {
            clearTimeout(this.searchTimer); this.loadSessions();
        });
        document.getElementById('resetSearchScopeBtn').addEventListener('click', () => {
            document.getElementById('sourceSelect').value = '';
            document.getElementById('projectSelect').value = '';
            this.loadSessions();
        });
        document.getElementById('clearSessionSearchBtn').addEventListener('click', () => {
            document.getElementById('sessionSearch').value = '';
            clearTimeout(this.searchTimer);
            this.loadSessions();
        });
        document.getElementById('uuidSearch').addEventListener('keydown', e => {
            if (e.key === 'Enter') this.searchMessages();
        });
        document.getElementById('uuidSearch').addEventListener('input', () => this.conversationView.clearHighlights());
        document.getElementById('clearUuidBtn').addEventListener('click', () => {
            document.getElementById('uuidSearch').value = '';
            this.conversationView.clearHighlights();
        });
        document.getElementById('filterTags').addEventListener('click', e => {
            const tag = e.target.closest('.filter-tag');
            if (!tag) return;
            if (this.activeFilters.has(tag.dataset.type)) this.activeFilters.delete(tag.dataset.type);
            else this.activeFilters.add(tag.dataset.type);
            this.applyFilters();
        });
        document.getElementById('loadMoreBtn').addEventListener('click', () => this.loadMore());
        document.getElementById('branchSelect').addEventListener('change', () => {
            if (this.currentSessionId) this.loadSession(this.currentSessionId, false);
        });
        this.syncMonitor = null;
        this.loadSources();
        this.loadSessions();
        this.restoreSyncStatus();
    }

    async loadSources() {
        try {
            const sources = await fetchSources();
            const select = document.getElementById('sourceSelect');
            for (const source of sources) {
                const option = document.createElement('option');
                option.value = source.id;
                option.textContent = source.label + (source.available ? '' : ' (not found)');
                select.append(option);
            }
        } catch (error) {
            this.status(`Source discovery failed: ${error.message}`);
        }
    }

    status(message, issues = []) {
        document.getElementById('syncStatus').textContent = message;
        const details = document.getElementById('syncIssues');
        details.hidden = issues.length === 0;
        document.getElementById('syncIssueText').textContent = issues.map(issue =>
            `${issue.source}: ${issue.path || issue.sessionId || ''}\n${issue.error || issue.warning}`).join('\n\n');
    }

    async loadSessions() {
        const generation = ++this.listGeneration;
        this.sessionSearchController?.abort();
        this.sessionSearchController = new AbortController();
        const query = document.getElementById('sessionSearch').value.trim();
        if (query !== this.lastSearchQuery) document.getElementById('projectSelect').value = '';
        this.lastSearchQuery = query;
        document.getElementById('sessionSearchStatus').textContent = query ? 'Searching user/assistant content…' : 'Loading sessions…';
        const container = document.getElementById('sessionList');
        container.innerHTML = '<div class="loading">Loading sessions</div>';
        try {
            const sessions = await fetchSessions(document.getElementById('sourceSelect').value,
                query, this.sessionSearchController.signal);
            if (generation !== this.listGeneration) return;
            this.sessions = sessions.filter(session => (session.userMessageCount ?? session.metadata?.userMessageCount ?? 0) > 0 && session.metadata?.relationship !== 'subagent');
            const projects = document.getElementById('projectSelect');
            const selected = projects.value;
            projects.replaceChildren(new Option('All projects', ''));
            const paths = new Set(this.sessions.map(session => session.project).filter(Boolean));
            if (selected) paths.add(selected);
            for (const path of [...paths].sort()) projects.append(new Option(path, path));
            projects.value = selected;
            this.renderSessions();
        } catch (error) {
            if (generation !== this.listGeneration) return;
            if (error.name === 'AbortError') return;
            document.getElementById('sessionSearchStatus').textContent = `Search failed: ${error.message}`;
            container.innerHTML = `<div class="empty-state"><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    renderSessions() {
        const project = document.getElementById('projectSelect').value;
        const visible = (this.sessions || []).filter(session => !project || session.project === project);
        this.sessionListView.render(visible);
        const source = document.getElementById('sourceSelect');
        const scope = source.value ? source.options[source.selectedIndex].textContent : 'all sources';
        document.getElementById('sessionSearchStatus').textContent = `${visible.length} sessions · ${scope}${project ? ' · project filter active' : ''}${this.lastSearchQuery ? ' · user/assistant content' : ''}`;
        document.getElementById('resetSearchScopeBtn').hidden = !source.value && !project;
    }

    async restoreSyncStatus() {
        try {
            const state = await fetchSyncStatus();
            if (state.running) await this.monitorSync();
            else if (state.result) this.showSyncResult(state.result);
        } catch (error) {
            this.status(`Cannot read sync status: ${error.message}`);
        }
    }

    showSyncResult(result) {
        const append = result.incrementalSessions
            ? ` · ${result.incrementalSessions} incremental sessions · ${result.reprojectedScopes || 0} changed turns/requests`
            : result.appendedSessions ? ` · ${result.appendedMessages} messages appended` : '';
        this.status(`${result.added} added · ${result.updated} updated · ${result.unchanged} unchanged · ${result.skippedRecent || 0} recent files skipped · ${result.failed} failed${append}`,
            [...(result.errors || []), ...(result.warnings || [])]);
    }

    showSyncProgress(state) {
        const progress = state.progress;
        if (!progress) {
            this.status(`Sync running · ${state.indexedSessions ?? '?'} sessions already indexed. You can browse committed history.`);
            return;
        }
        const stage = progress.phase === 'syncing'
            ? `${progress.filesDone} files processed · ${progress.sessionsDone} sessions synced · ${progress.skippedRecent || 0} recent files skipped`
            : progress.phase === 'scanning' ? `Scanning ${progress.filesDone}/${progress.filesTotal} files`
                : progress.phase === 'importing' ? `Importing ${progress.sessionsDone}/${progress.sessionsTotal} sessions` : progress.phase;
        const elapsed = state.elapsedSeconds ?? 0;
        const time = `${Math.floor(elapsed / 60)}m ${Math.floor(elapsed % 60)}s`;
        const step = progress.step ? ` · ${progress.step}` : '';
        this.status(`${stage} · ${progress.source || ''} · ${progress.currentFile || ''}${step} · ${time}`);
    }

    monitorSync() {
        if (this.syncMonitor) return this.syncMonitor;
        this.syncMonitor = this.watchSync().finally(() => {
            this.syncMonitor = null;
            document.getElementById('reloadBtn').disabled = false;
        });
        return this.syncMonitor;
    }

    async watchSync() {
        document.getElementById('reloadBtn').disabled = true;
        let polls = 0;
        while (true) {
            const state = await fetchSyncStatus();
            if (!state.running) {
                if (state.result) this.showSyncResult(state.result);
                await this.loadSessions();
                if (this.currentSessionId) await this.loadSession(this.currentSessionId, false);
                return;
            }
            this.showSyncProgress(state);
            // Show newly committed sessions without waiting for the entire archive.
            if (++polls % 5 === 0) await this.loadSessions();
            await new Promise(resolve => setTimeout(resolve, 1500));
        }
    }

    async reloadSessions() {
        const button = document.getElementById('reloadBtn');
        button.disabled = true;
        this.status('Starting read-only history sync…');
        try {
            const state = await fetchSyncStatus();
            if (!state.running) await syncSessions();
            await this.monitorSync();
        } catch (error) {
            this.status(`Sync request/status failed: ${error.message}. Refresh to reconnect; the server job is not cancelled.`);
        } finally {
            button.disabled = false;
        }
    }

    async loadSession(sessionId, resetBranch = true) {
        const generation = ++this.messageGeneration;
        this.currentSessionId = sessionId;
        this.nextCursor = null;
        this.loadingMore = false;
        const branchSelect = document.getElementById('branchSelect');
        if (resetBranch) branchSelect.value = '';
        this.messageDetailsView.clear();
        document.getElementById('loadMoreBtn').hidden = true;
        document.getElementById('conversationHistory').innerHTML = '<div class="loading">Loading messages</div>';
        try {
            const page = await fetchSessionMessages(sessionId, 0, branchSelect.value);
            if (generation !== this.messageGeneration) return;
            branchSelect.disabled = !page.session.metadata.defaultLeaf;
            document.getElementById('conversationTitle').textContent = page.session.name;
            this.activeFilters = new Set(['user', 'assistant']);
            document.getElementById('uuidSearch').value = '';
            this.conversationView.render(page.items);
            this.applyFilters();
            this.updatePage(page);
            const warnings = page.session.warnings || [];
            document.getElementById('sessionWarnings').textContent = warnings.join(' · ');
        } catch (error) {
            if (generation !== this.messageGeneration) return;
            document.getElementById('conversationHistory').innerHTML =
                `<div class="empty-state"><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    updatePage(page) {
        this.nextCursor = page.nextCursor;
        const button = document.getElementById('loadMoreBtn');
        button.hidden = this.nextCursor === null;
        button.disabled = false;
        document.getElementById('loadedCount').textContent =
            `${this.conversationView.messages.length} / ${page.total} loaded`;
    }

    async loadMore() {
        if (this.loadingMore || this.nextCursor === null) return;
        this.loadingMore = true;
        const generation = this.messageGeneration;
        const button = document.getElementById('loadMoreBtn');
        button.disabled = true;
        try {
            const page = await fetchSessionMessages(this.currentSessionId, this.nextCursor,
                document.getElementById('branchSelect').value);
            if (generation !== this.messageGeneration) return;
            this.conversationView.render([...this.conversationView.messages, ...page.items]);
            this.applyFilters();
            this.updatePage(page);
            if (document.getElementById('uuidSearch').value) this.searchMessages();
        } catch (error) {
            if (generation === this.messageGeneration) this.status(`Loading next page failed: ${error.message}`);
        } finally {
            if (generation === this.messageGeneration) {
                this.loadingMore = false;
                button.disabled = false;
            }
        }
    }

    applyFilters() {
        this.conversationView.filterByTypes(this.activeFilters);
        document.getElementById('filterTags').innerHTML = this.conversationView.getAvailableMessageTypes().map(type =>
            `<div class="filter-tag ${escapeHtml(type)} ${this.activeFilters.has(type) ? 'active' : ''}" data-type="${escapeHtml(type)}">${escapeHtml(type)}</div>`).join('');
        this.conversationView.clearHighlights();
    }

    searchMessages() {
        this.conversationView.clearHighlights();
        this.conversationView.highlightMessages(this.conversationView.search(document.getElementById('uuidSearch').value));
    }
}

document.addEventListener('DOMContentLoaded', () => new App());
