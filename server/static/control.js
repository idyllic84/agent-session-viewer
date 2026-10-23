const csrf = document.querySelector('meta[name=cloud-csrf]').content;
const error = document.getElementById('cloudError');
const devices = document.getElementById('cloudDevices');
const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
const showToken = value => { document.getElementById('newDeviceToken').textContent = `Device: ${value.deviceId}\nSave this token to a private *.token file. Shown once:\n${value.token}`; };
async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf,
            'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body)
    });
    if (response.status === 401) { location.assign('/auth/login'); throw new Error('Login required'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
    return value;
}
async function refresh() {
    try {
        const list = await api('/api/devices'); devices.replaceChildren();
        for (const device of list) {
            const row = node('div', `${device.name} · ${!device.active ? 'revoked' : device.online ? 'online' : 'offline'} · ${device.id}`);
            const button = (label, path, body = {}, allowRevoked = false) => {
                const el = node('button', label); el.disabled = !device.active && !allowRevoked;
                el.addEventListener('click', async () => {
                    el.disabled = true;
                    try { const value = await api(path, body); if (value.token) showToken(value); error.textContent = ''; await refresh(); }
                    catch (e) { error.textContent = e.message; el.disabled = false; }
                }); row.append(el);
            };
            if (device.active && device.control) {
                button('Sync now (expires in 10 min)', `/api/devices/${device.id}/jobs`, { sources: [] });
                if (device.job && ['QUEUED', 'RUNNING', 'PAUSED'].includes(device.job.state)) {
                    for (const [label, action] of [['Pause', 'pause'], ['Resume', 'run'], ['Cancel after current file', 'cancel']])
                        button(label, `/api/devices/${device.id}/jobs/${device.job.id}/${action}`);
                }
            }
            if (device.job) {
                const status = node('pre', JSON.stringify({ state: device.job.state, desired: device.job.desired,
                    progress: JSON.parse(device.job.progress), result: device.job.result && JSON.parse(device.job.result) }, null, 2));
                row.append(status);
            }
            button('Rotate credential', `/api/devices/${device.id}/rotate`, {}, true);
            button('Revoke device', `/api/devices/${device.id}/revoke`);
            devices.append(row);
        }
    } catch (e) { error.textContent = e.message; }
}
document.getElementById('createDevice').addEventListener('click', async () => {
    try {
        const value = await api('/api/devices', { name: document.getElementById('deviceName').value,
            control: document.getElementById('deviceControl').checked, adopt: document.getElementById('deviceAdopt').checked });
        showToken(value);
        error.textContent = ''; await refresh();
    } catch (e) { error.textContent = e.message; }
});
document.getElementById('reloadBtn').textContent = 'Refresh';
refresh(); setInterval(refresh, 5000);
