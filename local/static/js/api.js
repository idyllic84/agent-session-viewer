async function requestJson(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${response.status}`);
    }
    return response.json();
}

export function fetchSources() {
    return requestJson('/api/sources');
}

export async function fetchSessions(source = '', query = '', signal = undefined) {
    const sessions = [];
    let cursor = 0;
    do {
        const params = new URLSearchParams({ source, q: query, cursor, limit: 500 });
        const page = await requestJson(`/api/sessions?${params}`, { signal });
        sessions.push(...page.items);
        cursor = page.nextCursor;
    } while (cursor !== null);
    return sessions;
}

export function syncSessions() {
    return requestJson('/api/sync/start', { method: 'POST', headers: { 'X-Viewer-Request': '1' } });
}

export async function fetchSyncStatus() {
    const status = await requestJson('/api/sync/status');
    // Attach to an in-flight job from the previous server version without restarting it.
    if (status.running && !status.progress) {
        const page = await requestJson('/api/sessions?limit=1');
        status.indexedSessions = page.total;
    }
    return status;
}

export function fetchSessionMessages(sessionId, cursor = 0, branch = '') {
    const params = new URLSearchParams({ cursor, limit: 2000 });
    if (branch) params.set('branch', branch);
    return requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages?${params}`);
}

export function fetchRawMessage(messageId) {
    return requestJson(`/api/messages/${encodeURIComponent(messageId)}/raw`);
}
