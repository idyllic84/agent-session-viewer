// Local cloud-sync console. Talks only to this local server; the server talks to the cloud.
import { requestJson } from './js/api.js';

const $ = id => document.getElementById(id);
const message = text => { $('consoleMessage').textContent = text || ''; };
const line = (label, value) => { const div = document.createElement('div'); div.textContent = `${label}: ${value}`; return div; };
const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : '—';
let timer = null;

function render(state) {
    const local = state.local;
    $('localStats').replaceChildren(line('Sessions', local.sessions), line('Analytical messages', local.messages),
        line('Tracked source files', local.sourceFiles), line('Local change sequence', local.lastSeq),
        line('Dataset ID (identifies this computer to the cloud)', local.datasetId),
        line('Database', local.database));
    const box = $('cloudConnection');
    if (!state.configured) {
        box.replaceChildren(line('Status', 'not configured — enter the cloud address and an API key'));
        $('consoleSummary').textContent = '· not connected';
    } else {
        const c = state.connection;
        if (!$('cloudUrl').value) $('cloudUrl').value = c.url;
        const last = c.lastResult;
        box.replaceChildren(line('Cloud', c.url), line('API key', `${c.keyHint} (stored in ${c.keyStorage}, never shown again)`),
            line('Cloud client', c.clientName || '—'), line('Confirmed by cloud', `${c.ackedSeq} / ${local.lastSeq}`),
            line('Waiting to upload', `${state.pendingChanges} changes`),
            line('Last sync', last ? `${last.state}${last.error ? ' · ' + last.error.code + ': ' + last.error.message : ''} · ${when(last.finishedAt)}` : 'never'));
        $('consoleSummary').textContent = state.pendingChanges ? `· ${state.pendingChanges} changes waiting` : '· up to date';
    }
    const upload = state.upload;
    const parts = [line('State', upload.state)];
    if (upload.highWater) parts.push(line('This run uploads through', upload.highWater));
    if (upload.batches !== undefined) parts.push(line('Batches / changes / bytes', `${upload.batches} / ${upload.changes} / ${upload.bytes}`));
    if (upload.error) parts.push(line('Error', `${upload.error.code}: ${upload.error.message}`));
    $('uploadState').replaceChildren(...parts);
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
        : `Connection OK · cloud client "${r.clientName}" · cloud has ${r.cloudAckSeq ?? 0} of ${r.localLastSeq} local changes.`;
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
