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
    $('newKey').textContent = `Key for "${value.name}" (shown once — copy it now):\n${value.key}\n\nIn the local console use cloud address: ${origin}`;
}
async function refresh() {
    try {
        const keys = await api('/api/keys'); const list = $('keyList'); list.replaceChildren();
        for (const key of keys) {
            const row = node('div', `${key.name} · ${key.keyId} · ${key.active ? 'active' : 'revoked'} · `
                + `${key.bound ? 'bound to one computer' : 'not used yet'} · ${key.uploadedChanges} changes · `
                + `${key.sessions} sessions · last upload ${when(key.lastUpload)}${key.expires ? ' · expires ' + when(key.expires) : ''}`);
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
            button('Rotate', 'rotate', 'Issue a new key for this computer? The old key stops working immediately; upload progress is kept.');
            if (key.active) button('Revoke', 'revoke', 'Revoke this key? Uploaded data stays in the cloud.');
            if (key.bound) button('Allow a new computer', 'unbind', 'Let this key upload a DIFFERENT local dataset? Already uploaded data is kept.');
            list.append(row);
        }
    } catch (e) { say(e.message); }
}
$('createKey').addEventListener('click', async () => {
    try {
        const days = $('keyExpiry').value ? Number($('keyExpiry').value) : null;
        showKey(await api('/api/keys', { name: $('keyName').value, expiresDays: days }));
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
refresh();
