// Cloud-only transport adapter. Shared viewer components remain unchanged.
async function requestJson(path, options = {}) {
    const response = await fetch(path, options);
    if (response.status === 401) { location.assign('/auth/login'); throw new Error('Login required'); }
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error?.message || `HTTP ${response.status}`);
    }
    return response.json();
}
export function fetchSources() { return requestJson('/api/sources'); }
export async function fetchSessions(source = '', query = '', signal) {
    let cursor = 0; const items = [];
    do {
        const machine = document.getElementById('machineSelect')?.value || '';
        const page = await requestJson(`/api/sessions?${new URLSearchParams({ source, q: query, machine, cursor, limit: 500 })}`, { signal });
        items.push(...page.items); cursor = page.nextCursor;
    } while (cursor !== null);
    return items;
}
// The shared Reload button only refreshes cloud reads. Data arrives when a
// local server's console uploads it; the cloud never commands local computers.
export function syncSessions() { return fetchSyncStatus(); }
export function fetchSyncStatus() { return requestJson('/api/sync/status'); }
const revisions = new Map();
export async function fetchSessionMessages(sessionId, cursor = 0, branch = '') {
    const params = new URLSearchParams({ cursor, limit: 2000 });
    if (branch) params.set('branch', branch);
    const key = sessionId + ':' + branch;
    if (cursor !== 0 && revisions.has(key)) params.set('revision', revisions.get(key));
    const page = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages?${params}`);
    revisions.set(key, page.sessionRevision);
    return page;
}
export function fetchRawMessage(id) { return requestJson(`/api/messages/${encodeURIComponent(id)}/raw`); }
