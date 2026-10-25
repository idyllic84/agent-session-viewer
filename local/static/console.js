// Local cloud-sync console. Talks only to this local server; the server talks to the cloud.
import { requestJson } from './js/api.js';

const $ = id => document.getElementById(id);
const message = text => { $('consoleMessage').textContent = text || ''; };
const line = (label, value) => { const div = document.createElement('div'); div.textContent = `${label}: ${value}`; return div; };
const number = value => Number(value || 0).toLocaleString();
const bytes = value => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : value >= 1024 ? `${Math.round(value / 1024)} KB` : `${value || 0} B`;
const ago = seconds => {
    if (!seconds) return 'never';
    const minutes = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    const text = minutes < 1 ? 'just now' : minutes < 60 ? `${minutes} min ago` : minutes < 2880 ? `${Math.round(minutes / 60)} h ago` : `${Math.round(minutes / 1440)} days ago`;
    return `${text} (${new Date(seconds * 1000).toLocaleString()})`;
};
const duration = seconds => seconds < 90 ? `${Math.max(1, Math.round(seconds))} s` : `${Math.round(seconds / 60)} min`;
// Actionable explanations for errors the user can fix.
const HINTS = {
    NETWORK_UNAVAILABLE: 'The cloud could not be reached. Nothing local was lost; click Sync again later.',
    UNAUTHORIZED: 'The cloud rejected the API key (revoked, rotated or expired). Paste a new upload key and Save.',
    WRONG_KEY_TYPE: 'This is not an upload key. Generate an "Upload" key in the cloud console.',
    DATASET_MISMATCH: 'This key already belongs to another computer. Generate a separate upload key for this computer.',
    LOCAL_BEHIND_CLOUD: 'The cloud has more data from this computer than the local store (restored backup?). Upload stopped; nothing was overwritten.',
    CLOUD_INSTANCE_CHANGED: 'This address now serves a different cloud. Test and Save the connection again.',
    TLS_FAILED: 'The HTTPS certificate of the cloud address could not be verified.',
    TOO_LARGE: 'One change is larger than the cloud request limit. Raise VIEWER_MAX_REQUEST_MIB on the cloud.',
    NO_CREDENTIAL: 'The stored API key is missing. Paste the key again and Save.',
};
const explain = error => error ? `${HINTS[error.code] || error.message} [${error.code}]` : '';
let timer = null;

function render(state) {
    const local = state.local;
    $('localStats').replaceChildren(
        line('History', `${number(local.sessions)} sessions · ${number(local.messages)} messages · ${number(local.sourceFiles)} source files`),
        line('Last import', state.import?.running ? 'running…' : state.import?.result
            ? `${state.import.result.added} new · ${state.import.result.updated} updated · ${state.import.result.unchanged} unchanged · `
              + `${state.import.result.skippedRecent || 0} deferred · ${state.import.result.failed} failed` : 'at startup'),
        line('Dataset ID (identifies this computer to the cloud)', local.datasetId),
        line('Database', local.database));
    $('localSources').replaceChildren(...(state.sources || []).map(source => {
        const div = document.createElement('div');
        div.textContent = `${source.available ? '✓' : '–'} ${source.label}: ${source.available ? 'found' : 'not found'}`;
        div.title = source.roots.join('\n'); div.className = source.available ? 'found' : 'missing';
        return div;
    }));
    const upload = state.upload;
    const box = $('cloudConnection');
    let summary;
    if (!state.configured) {
        box.replaceChildren(line('Status', 'not configured — enter the cloud address and an upload API key'));
        summary = '· not connected';
    } else {
        const c = state.connection, last = c.lastResult, pending = state.pending || {};
        if (!$('cloudUrl').value) $('cloudUrl').value = c.url;
        box.replaceChildren(line('Cloud', c.url),
            line('This computer appears in the cloud as', c.clientName || '—'),
            line('API key', `${c.keyHint} (stored in ${c.keyStorage}; never shown again)`),
            line('Waiting to upload', pending.changes ? `${number(pending.sessions)} sessions · ${number(pending.messages)} messages (${number(pending.changes)} changes)` : 'nothing — up to date'),
            line('Last sync', !last ? 'never' : last.state === 'complete'
                ? `succeeded ${ago(last.finishedAt)} · ${number(last.changes)} changes in ${number(last.batches)} batches (${bytes(last.bytes)})`
                : `${last.state} ${ago(last.finishedAt)}${last.error ? ' · ' + explain(last.error) : ''}`));
        summary = pending.changes ? `· ${number(pending.sessions)} sessions waiting to upload` : '· up to date';
    }
    const bar = $('uploadProgress'); const parts = [];
    const total = (upload.highWater || 0) - (upload.fromSeq || 0), done = (upload.ackSeq || 0) - (upload.fromSeq || 0);
    if (upload.running && total > 0) {
        const fraction = Math.min(1, Math.max(0, done / total)); const elapsed = Date.now() / 1000 - upload.startedAt;
        bar.hidden = false; bar.value = fraction;
        const eta = done > 0 ? ` · about ${duration(elapsed * (total - done) / done)} left` : '';
        parts.push(line('Uploading', `${Math.floor(fraction * 100)}% · ${number(done)} / ${number(total)} changes · ${number(upload.batches)} batches · ${bytes(upload.bytes)}${eta}`));
        summary = `· uploading ${Math.floor(fraction * 100)}%`;
    } else {
        bar.hidden = true;
        const labels = { idle: 'idle', starting: 'starting…', 'checking cloud progress': 'checking the cloud…', complete: 'complete', paused: 'paused — click Resume sync to continue', cancelled: 'cancelled', failed: 'failed', 'up to date': 'up to date' };
        parts.push(line('State', labels[upload.state] || upload.state));
        if (upload.state === 'complete' && upload.batches !== undefined)
            parts.push(line('This run', `${number(upload.changes)} changes in ${number(upload.batches)} batches (${bytes(upload.bytes)})`));
    }
    if (upload.error) parts.push(line('Problem', explain(upload.error)));
    $('uploadState').replaceChildren(...parts);
    $('consoleSummary').textContent = summary;
    $('uploadStart').textContent = upload.state === 'paused' ? 'Resume sync' : 'Sync to cloud';
    $('uploadStart').disabled = upload.running || !state.configured;
    $('uploadPause').disabled = $('uploadCancel').disabled = !upload.running;
    for (const id of ['cloudSave', 'cloudForget']) $(id).disabled = upload.running;
}

async function refresh() {
    try {
        const state = await requestJson('/api/cloud');
        render(state);
        clearTimeout(timer);
        // Poll THIS local server only while an upload runs; nothing polls the cloud.
        if (state.upload.running) timer = setTimeout(refresh, 1500);
        return state;
    } catch (error) { message(error.message); }
}

async function act(button, work) {
    button.disabled = true;
    let text;
    try { text = await work() || ''; } catch (error) { text = error.message; }
    button.disabled = false;
    await refresh();      // update buttons/state first, then report the outcome
    message(text);
}

const credentials = () => ({ url: $('cloudUrl').value.trim(), key: $('cloudKey').value.trim() });
$('cloudTest').addEventListener('click', e => act(e.target, async () => {
    const r = await requestJson('/api/cloud/test', { method: 'POST', body: JSON.stringify(credentials()) });
    return r.conflict ? `Reachable, but this key already belongs to another computer's data (${r.clientName}).`
        : `Connection OK · this computer will appear as "${r.clientName}" · the cloud has ${number(r.cloudAckSeq ?? 0)} of ${number(r.localLastSeq)} local changes.`;
}));
$('cloudSave').addEventListener('click', e => act(e.target, async () => {
    await requestJson('/api/cloud/connection', { method: 'PUT', body: JSON.stringify(credentials()) });
    $('cloudKey').value = '';
    return 'Saved. Nothing was uploaded yet — click "Sync to cloud" when ready.';
}));
$('cloudForget').addEventListener('click', e => act(e.target, async () => {
    if (!confirm('Forget the cloud address and the stored API key? Local and cloud data are kept.')) return '';
    await requestJson('/api/cloud/connection', { method: 'DELETE' });
    $('cloudUrl').value = '';
    return 'Connection removed.';
}));
for (const [id, action] of [['uploadStart', 'start'], ['uploadPause', 'pause'], ['uploadCancel', 'cancel']]) {
    $(id).addEventListener('click', e => act(e.target, async () => {
        await requestJson(`/api/cloud/upload/${action}`, { method: 'POST' });
        return action === 'start' ? 'Upload started.' : `Will ${action} after the current batch.`;
    }));
}
refresh();
