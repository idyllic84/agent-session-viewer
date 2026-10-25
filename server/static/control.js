// Cloud console: upload API keys and owner password. The cloud never commands local computers.
const csrf = document.querySelector('meta[name=cloud-csrf]').content;
const $ = id => document.getElementById(id);
const say = text => { $('cloudError').textContent = text || ''; };
const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : '—';
async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) });
    if (response.status === 401) { location.assign('/auth/login'); throw new Error('Login required'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
    return value;
}
const origin = location.origin;
function showKey(value) {
    const usage = value.kind === 'agent'
        ? `Agent API: ${origin}/agent/v1/sessions  (header  Authorization: Bearer <key>)`
        : `In the local console use cloud address: ${origin}`;
    $('newKey').textContent = `${value.kind === 'agent' ? 'Agent' : 'Upload'} key "${value.name}" (shown once — copy it now):\n${value.key}\n\n${usage}`;
}
async function refresh() {
    try {
        const keys = await api('/api/keys'); const list = $('keyList'); list.replaceChildren();
        for (const key of keys) {
            const detail = key.kind === 'agent'
                ? `agent (read-only API) · last used ${when(key.lastUsed)}`
                : `upload · ${key.bound ? 'bound to one computer' : 'not used yet'} · ${key.uploadedChanges} changes · `
                  + `${key.sessions} sessions · last upload ${when(key.lastUpload)}`;
            const row = node('div', `${key.name} · ${key.keyId} · ${key.active ? 'active' : 'revoked'} · ${detail}`
                + `${key.expires ? ' · expires ' + when(key.expires) : ''}`);
            const button = (label, action, confirmText) => {
                const el = node('button', label); el.className = 'secondary';
                el.addEventListener('click', async () => {
                    if (confirmText && !confirm(confirmText)) return;
                    el.disabled = true;
                    try { const value = await api(`/api/keys/${key.clientId}/${action}`, {}); if (value.key) showKey(value); say(''); await refresh(); }
                    catch (e) { say(e.message); el.disabled = false; }
                });
                row.append(' ', el);
            };
            button('Rotate', 'rotate', 'Issue a new key? The old key stops working immediately' + (key.kind === 'upload' ? '; upload progress is kept.' : '.'));
            if (key.active) button('Revoke', 'revoke', 'Revoke this key? Uploaded data stays in the cloud.');
            if (key.kind === 'upload' && key.bound) button('Allow a new computer', 'unbind', 'Let this key upload a DIFFERENT local dataset? Already uploaded data is kept.');
            list.append(row);
        }
    } catch (e) { say(e.message); }
}
$('createKey').addEventListener('click', async () => {
    try {
        const days = $('keyExpiry').value ? Number($('keyExpiry').value) : null;
        showKey(await api('/api/keys', { name: $('keyName').value, expiresDays: days, kind: $('keyKind').value }));
        $('keyName').value = ''; say(''); await refresh();
    } catch (e) { say(e.message); }
});
$('changePassword').addEventListener('click', async () => {
    try {
        await api('/api/account/password', { currentPassword: $('currentPassword').value, newPassword: $('newPassword').value });
        $('currentPassword').value = $('newPassword').value = ''; say('Password changed. Other browser sessions are signed out.');
    } catch (e) { say(e.message); }
});
$('logout').addEventListener('click', async () => { await api('/auth/logout', {}); location.assign('/auth/login'); });
$('reloadBtn').textContent = 'Refresh';

// Machines: every session belongs to the upload key (computer) that sent it.
const STALE_DAYS = 7;
const ago = seconds => {
    if (!seconds) return 'never uploaded';
    const minutes = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
    return `${Math.round(minutes / 1440)} days ago`;
};
async function loadMachines() {
    try {
        const select = $('machineSelect'); const current = select.value;
        const machines = await api('/api/machines');
        select.replaceChildren(new Option('All machines', ''));
        const summary = $('machineSummary'); summary.replaceChildren();
        for (const machine of machines) {
            select.append(new Option(`${machine.name} (${machine.sessions})${machine.active ? '' : ' · revoked'}`, machine.id));
            const stale = !machine.lastUpload || Date.now() / 1000 - machine.lastUpload > STALE_DAYS * 86400;
            const chip = node('button', `${machine.name} · ${machine.sessions} sessions · ${ago(machine.lastUpload)}`);
            chip.className = 'machine-chip' + (stale ? ' stale' : '') + (machine.id === current ? ' selected' : '');
            chip.title = stale ? `No upload in the last ${STALE_DAYS} days — open that computer's local console and click Sync to cloud`
                : 'Show only this machine';
            chip.addEventListener('click', () => { select.value = select.value === machine.id ? '' : machine.id; select.dispatchEvent(new Event('change')); });
            summary.append(chip);
        }
        select.value = current;
    } catch (e) { say(e.message); }
}
const reloadList = () => $('sourceSelect').dispatchEvent(new Event('change'));
$('machineSelect').addEventListener('change', () => { reloadList(); loadMachines(); });
const isoDay = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
$('datePreset').addEventListener('change', () => {
    const days = Number($('datePreset').value);
    $('dateFrom').value = days ? isoDay(new Date(Date.now() - (days - 1) * 86400000)) : '';
    $('dateTo').value = '';
    reloadList();
});
for (const id of ['dateFrom', 'dateTo']) $(id).addEventListener('change', () => { $('datePreset').value = ''; reloadList(); });
$('reloadBtn').addEventListener('click', loadMachines);
loadMachines();
refresh();
