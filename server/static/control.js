// Cloud console (sheet) and session-list filters that only exist in the cloud:
// API keys, owner password and the machine filter.
// The cloud never commands local computers.
const csrf = document.querySelector('meta[name=cloud-csrf]').content;
const $ = id => document.getElementById(id);
const say = text => { $('cloudError').textContent = text || ''; };
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : '—';
const ago = seconds => {
    if (!seconds) return 'never';
    const minutes = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
    return `${Math.round(minutes / 1440)} days ago`;
};

async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) });
    if (response.status === 401) { location.assign('/auth/login'); throw new Error('Login required'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
    return value;
}

// ----- console sheet -----
const sheet = $('consoleSheet');
let opener = null;
function openConsole() {
    opener = document.activeElement; sheet.hidden = false; document.body.classList.add('sheet-open');
    $('consoleBtn').setAttribute('aria-expanded', 'true'); refreshKeys(); $('consoleClose').focus();
}
function closeConsole() {
    sheet.hidden = true; document.body.classList.remove('sheet-open'); $('consoleBtn').setAttribute('aria-expanded', 'false');
    opener?.focus?.();
}
$('consoleBtn').addEventListener('click', () => (sheet.hidden ? openConsole() : closeConsole()));
sheet.addEventListener('click', event => { if (event.target.closest('[data-close]')) closeConsole(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !sheet.hidden) closeConsole(); });

// ----- API keys -----
const origin = location.origin;
let shownKey = '';
function showKey(value) {
    shownKey = value.key;
    const usage = value.kind === 'agent'
        ? `Agent API: ${origin}/agent/v1/sessions\nHeader: Authorization: Bearer <key>`
        : `Local console → cloud address: ${origin}`;
    $('newKey').textContent = `${value.kind === 'agent' ? 'Agent' : 'Upload'} key “${value.name}” — shown once, copy it now:\n\n${value.key}\n\n${usage}`;
    $('newKeyBox').hidden = false; $('copyKey').textContent = 'Copy key';
}
$('copyKey').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(shownKey); $('copyKey').textContent = 'Copied'; }
    catch { say('Copy is not available here; select the key text instead.'); }
});

function keyCard(key) {
    const card = el('article', `key-card ${key.active ? '' : 'revoked'}`);
    const head = el('div', 'key-head');
    head.append(el('strong', 'key-name', key.name), el('span', `kind-badge ${key.kind}`, key.kind === 'agent' ? 'Agent' : 'Upload'),
        el('span', `state-badge ${key.active ? 'active' : 'revoked'}`, key.active ? 'active' : 'revoked'));
    const meta = el('dl', 'key-meta');
    const row = (label, value) => { meta.append(el('dt', '', label), el('dd', '', value)); };
    row('Key ID', key.keyId);
    if (key.kind === 'agent') row('Last used', key.lastUsed ? `${ago(key.lastUsed)} (${when(key.lastUsed)})` : 'never');
    else {
        row('Computer', key.bound ? 'bound' : 'not used yet');
        row('Uploaded', `${key.sessions} sessions · ${key.uploadedChanges} changes`);
        row('Last upload', key.lastUpload ? `${ago(key.lastUpload)} (${when(key.lastUpload)})` : 'never');
    }
    if (key.expires) row('Expires', when(key.expires));
    const actions = el('div', 'button-row');
    const action = (label, path, confirmText, danger = false) => {
        const button = el('button', danger ? 'danger' : 'secondary', label); button.type = 'button';
        button.addEventListener('click', async () => {
            if (confirmText && !confirm(confirmText)) return;
            button.disabled = true;
            try { const value = await api(`/api/keys/${key.clientId}/${path}`, {}); if (value.key) showKey(value); say(''); await refreshKeys(); }
            catch (e) { say(e.message); button.disabled = false; }
        });
        actions.append(button);
    };
    action('Rotate', 'rotate', 'Issue a new key? The old key stops working immediately' + (key.kind === 'upload' ? '; upload progress is kept.' : '.'));
    if (key.kind === 'upload' && key.bound) action('Allow a new computer', 'unbind', 'Let this key upload a DIFFERENT local dataset? Already uploaded data is kept.');
    if (key.active) action('Revoke', 'revoke', 'Revoke this key? Uploaded data stays in the cloud.', true);
    card.append(head, meta, actions);
    return card;
}
async function refreshKeys() {
    try {
        const keys = await api('/api/keys');
        $('keyList').replaceChildren(...(keys.length ? keys.map(keyCard) : [el('p', 'hint', 'No keys yet.')]));
    } catch (e) { say(e.message); }
}
$('createKey').addEventListener('click', async () => {
    try {
        const days = $('keyExpiry').value ? Number($('keyExpiry').value) : null;
        showKey(await api('/api/keys', { name: $('keyName').value, expiresDays: days, kind: $('keyKind').value }));
        $('keyName').value = ''; say(''); await refreshKeys(); loadMachines();
    } catch (e) { say(e.message); }
});
$('changePassword').addEventListener('click', async () => {
    try {
        await api('/api/account/password', { currentPassword: $('currentPassword').value, newPassword: $('newPassword').value });
        $('currentPassword').value = $('newPassword').value = ''; say('Password changed. Other browser sessions are signed out.');
    } catch (e) { say(e.message); }
});
$('logout').addEventListener('click', async () => { await api('/auth/logout', {}); location.assign('/auth/login'); });

// ----- machines (session-list filter) -----
const STALE_DAYS = 7;
async function loadMachines() {
    try {
        const select = $('machineSelect'); const current = select.value;
        const machines = await api('/api/machines');
        select.replaceChildren(new Option('All machines', ''));
        const summary = $('machineSummary'); summary.replaceChildren();
        for (const machine of machines) {
            select.append(new Option(`${machine.name} (${machine.sessions})${machine.active ? '' : ' · revoked'}`, machine.id));
            const stale = !machine.lastUpload || Date.now() / 1000 - machine.lastUpload > STALE_DAYS * 86400;
            const chip = el('button', 'machine-chip' + (stale ? ' stale' : '') + (machine.id === current ? ' selected' : ''));
            chip.type = 'button'; chip.setAttribute('aria-pressed', String(machine.id === current));
            chip.append(el('span', 'chip-name', machine.name), el('span', 'chip-meta', `${machine.sessions} · ${ago(machine.lastUpload)}`));
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
$('reloadBtn').addEventListener('click', loadMachines);
loadMachines();
