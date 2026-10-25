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
        const params = new URLSearchParams({ source, q: query, cursor, limit: 500 });
        const machine = document.getElementById('machineSelect')?.value;
        if (machine) params.set('machine', machine);
        // Date inputs are local calendar days: [from 00:00, to 23:59:59.999] in this browser's time zone.
        const from = document.getElementById('dateFrom')?.value, to = document.getElementById('dateTo')?.value;
        if (from) params.set('from', new Date(`${from}T00:00:00`).getTime());
        if (to) params.set('to', new Date(`${to}T23:59:59.999`).getTime());
        const page = await requestJson(`/api/sessions?${params}`, { signal });
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
