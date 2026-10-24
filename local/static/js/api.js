// Local transport adapter. Mutations carry the per-process CSRF token from the page.
const csrf = document.querySelector('meta[name=local-csrf]').content;
export async function requestJson(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') {
        headers['X-CSRF-Token'] = csrf;
        if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(path, { ...options, headers });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error?.message || value.error || `HTTP ${response.status}`);
    return value;
}
export function fetchSources() { return requestJson('/api/sources'); }
export async function fetchSessions(source = '', query = '', signal) {
    let cursor = 0; const items = [];
    do {
        const page = await requestJson(`/api/sessions?${new URLSearchParams({ source, q: query, cursor, limit: 500 })}`, { signal });
        items.push(...page.items); cursor = page.nextCursor;
    } while (cursor !== null);
    return items;
}
// "Reload" explicitly re-imports local history (no watcher/timer exists).
export function syncSessions() { return requestJson('/api/sync/start', { method: 'POST' }); }
export function fetchSyncStatus() { return requestJson('/api/sync/status'); }
const revisions = new Map();
export async function fetchSessionMessages(sessionId, cursor = 0, branch = '') {
    const params = new URLSearchParams({ cursor, limit: 2000 });
    const key = sessionId;
    if (cursor !== 0 && revisions.has(key)) params.set('revision', revisions.get(key));
    const page = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages?${params}`);
    revisions.set(key, page.sessionRevision);
    return page;
}
export function fetchRawMessage(id) { return requestJson(`/api/messages/${encodeURIComponent(id)}/raw`); }
