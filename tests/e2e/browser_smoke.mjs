// Optional Node 22+ / Edge check of the running local Docker UI. No data writes.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
const [origin = 'http://localhost:41880', passwordFile, mode = 'empty'] = process.argv.slice(2);
assert.ok(passwordFile, 'Pass a private owner-password file; never pass the password itself as an argument');
assert.ok(/^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(origin), 'Local Docker origin required');
const edge = process.env.EDGE_BIN || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
assert.ok(fs.existsSync(edge), 'Set EDGE_BIN to Edge/Chromium');
const owner = fs.readFileSync(passwordFile, 'utf8').trim();
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'viewer-docker-browser-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser, socket;
try {
    const profile = path.join(root, 'profile');
    browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore', windowsHide: true });
    const portFile = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 200 && !fs.existsSync(portFile); i++) await delay(100);
    assert.ok(fs.existsSync(portFile), 'No browser debugger port');
    const port = fs.readFileSync(portFile, 'utf8').split('\n')[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
    let counter = 0; const pending = new Map(); const errors = []; const loginOrigins = [];
    socket.addEventListener('message', event => {
        const value = JSON.parse(event.data);
        if (value.method === 'Runtime.exceptionThrown') errors.push(value.params.exceptionDetails.text);
        if (value.method === 'Network.requestWillBeSent' && value.params.request.method === 'POST'
            && value.params.request.url === origin + '/auth/login') loginOrigins.push(value.params.request.headers.Origin);
        if (!pending.has(value.id)) return;
        const { resolve, reject, timer } = pending.get(value.id); clearTimeout(timer); pending.delete(value.id);
        if (value.error) reject(new Error('Browser command failed (details withheld)')); else resolve(value.result);
    });
    const command = (method, params = {}) => new Promise((resolve, reject) => {
        const id = ++counter; const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timeout: ${method}`)); }, 15000);
        pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
        const value = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
        if (value.exceptionDetails) throw new Error('Browser evaluation failed (details withheld to protect credentials)');
        return value.result.value;
    };
    const waitFor = async expression => {
        for (let i = 0; i < 150; i++) { if (await evaluate(expression)) return; await delay(100); }
        const diagnostic = await evaluate(`JSON.stringify({path:location.pathname, login:!!document.querySelector('input[name=password]'), cloudMeta:!!document.querySelector('meta[name=cloud-csrf]'), sourceOptions:document.querySelectorAll('#sourceSelect option').length, status:document.getElementById('sessionSearchStatus')?.textContent, cloudError:document.getElementById('cloudError')?.textContent, responseError:(()=>{try{const e=JSON.parse(document.body.textContent).error;return e&&{code:e.code,message:e.message}}catch{return null}})()})`);
        throw new Error(`Browser condition not reached: ${diagnostic}; runtime errors: ${JSON.stringify(errors)}`);
    };
    // Optional visual check: SCREENSHOT_DIR=<folder> saves PNGs of the main states.
    const shoot = async name => {
        if (!process.env.SCREENSHOT_DIR) return;
        await delay(300);
        const shot = await command('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync(path.join(process.env.SCREENSHOT_DIR, `${name}.png`), Buffer.from(shot.data, 'base64'));
    };
    await command('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
    await command('Runtime.enable'); await command('Network.enable'); await command('Page.navigate', { url: origin });
    await waitFor('Boolean(document.querySelector("input[name=password]"))');
    await evaluate(`document.querySelector('input[name=password]').value=${JSON.stringify(owner)};document.querySelector('form').requestSubmit()`);
    await waitFor('Boolean(document.querySelector("meta[name=cloud-csrf]")) && document.querySelectorAll("#sourceSelect option").length===6');
    assert.equal(await evaluate('document.getElementById("reloadBtn").textContent'), 'Refresh');
    assert.ok(await evaluate('Boolean(document.getElementById("createKey"))'));
    assert.ok(await evaluate('Boolean(document.getElementById("changePassword"))'));
    const security = await evaluate(`(async()=>({
        denied:(await fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'must-not-create'})})).status,
        keys:(await (await fetch('/api/keys')).json()).length,
        sessions:(await (await fetch('/api/sessions')).json()).total
    }))()`);
    assert.equal(security.denied, 403);
    if (mode === '--analysis-data') {
        assert.ok(security.keys > 0); assert.ok(security.sessions > 0);
        await waitFor('document.querySelectorAll(".session-item").length > 0');
        // Every cloud session shows which computer (upload key) it came from; the list can be filtered by machine.
        await waitFor('document.querySelectorAll("#machineSelect option").length >= 2');
        assert.ok(await evaluate('[...document.querySelectorAll(".session-item .machine-badge")].some(b => b.textContent === "Docker synthetic smoke")'));
        const machine = await evaluate('[...document.querySelectorAll("#machineSelect option")].find(o => o.textContent.startsWith("Docker synthetic smoke")).value');
        await evaluate(`document.getElementById("machineSelect").value=${JSON.stringify(machine)}; document.getElementById("machineSelect").dispatchEvent(new Event("change"))`);
        await waitFor('document.querySelectorAll(".session-item").length > 0 && [...document.querySelectorAll(".session-item .machine-badge")].every(b => b.textContent === "Docker synthetic smoke")');
        await evaluate('document.querySelector(".session-item").click()');
        await waitFor('document.querySelectorAll(".message-item").length === 5');
        assert.ok(await evaluate('document.getElementById("conversationTitle").textContent.endsWith(" · Docker synthetic smoke")'));
        // Deep link: the opened session is in the URL and survives a reload.
        const linked = await evaluate('location.hash');
        assert.ok(linked.startsWith('#session='));
        await evaluate('location.reload()');
        await waitFor('document.querySelectorAll(".message-item").length === 5 && document.getElementById("conversationTitle").textContent.endsWith(" · Docker synthetic smoke")');
        // Machine chips and date range presets filter the list.
        await waitFor('[...document.querySelectorAll("#machineSummary .machine-chip")].some(c => c.textContent.startsWith("Docker synthetic smoke"))');
        await evaluate('document.getElementById("dateTo").value="2000-01-01"; document.getElementById("dateTo").dispatchEvent(new Event("change"))');
        await waitFor('document.getElementById("sessionSearchStatus").textContent.startsWith("0 sessions")');
        await evaluate('document.getElementById("datePreset").value="1"; document.getElementById("datePreset").dispatchEvent(new Event("change"))');
        await waitFor('document.getElementById("dateFrom").value !== "" && document.getElementById("dateTo").value === "" && document.querySelectorAll(".session-item").length > 0');
        await evaluate('document.getElementById("datePreset").value=""; document.getElementById("datePreset").dispatchEvent(new Event("change"))');
        await waitFor('document.getElementById("dateFrom").value === "" && document.querySelectorAll(".session-item").length > 0');
        assert.equal(await evaluate('document.querySelectorAll(".message-item.tool-use").length'), 1);
        assert.equal(await evaluate('document.querySelectorAll(".message-item.tool-result").length'), 1);
        await evaluate('document.querySelector(".message-item.tool-result").click()');
        await waitFor('document.getElementById("messageDetails").textContent.includes("success")');
        assert.equal(await evaluate('document.querySelectorAll("#messageDetails button[data-action=raw]").length'), 0);
        assert.ok(await evaluate('document.getElementById("messageDetails").textContent.includes("timeSource")'));
        await shoot('desktop-viewer');

        // Phone layout: one pane at a time, Back (button and browser history), collapsible filters, no sideways scrolling.
        await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
        await command('Emulation.setTouchEmulationEnabled', { enabled: true });
        await evaluate('history.replaceState(null, "", location.pathname); location.reload()');
        await waitFor('document.querySelectorAll(".session-item").length > 0');
        const shown = selector => `getComputedStyle(document.querySelector(${JSON.stringify(selector)})).display !== "none"`;
        const fits = 'document.documentElement.scrollWidth <= innerWidth';
        assert.equal(await evaluate('document.body.dataset.view'), 'list');
        assert.ok(await evaluate(`${shown('.left-panel')} && !(${shown('.middle-panel')}) && !(${shown('.right-panel')}) && ${fits}`));
        assert.ok(await evaluate(`!(${shown('#sessionFilterBar')}) && ${shown('#filtersBtn')} && document.getElementById("backBtn").hidden`));
        await evaluate('document.getElementById("filtersBtn").click()');
        assert.ok(await evaluate(`${shown('#sessionFilterBar')} && document.getElementById("filtersBtn").getAttribute("aria-expanded") === "true"`));
        await evaluate(`document.getElementById("machineSelect").value=${JSON.stringify(machine)}; document.getElementById("machineSelect").dispatchEvent(new Event("change"))`);
        await waitFor('document.getElementById("filterCount").textContent === "1" && !document.getElementById("filterCount").hidden');
        await shoot('phone-filters');
        await waitFor('document.querySelectorAll(".session-item").length > 0');
        await evaluate('document.querySelector(".session-item").click()');
        await waitFor(`document.body.dataset.view === "conversation" && document.querySelectorAll(".message-item").length === 5 && ${shown('.middle-panel')} && !(${shown('.left-panel')})`);
        assert.ok(await evaluate(`!document.getElementById("backBtn").hidden && !(${shown('#sessionFilterBar')}) && ${fits}`));
        await waitFor('document.getElementById("appSubtitle").textContent.endsWith(" · Docker synthetic smoke")');
        await shoot('phone-conversation');
        await evaluate('document.querySelector(".message-item.tool-use").click()');
        await waitFor(`document.body.dataset.view === "details" && ${shown('.right-panel')} && !(${shown('.middle-panel')})`);
        assert.ok(await evaluate(fits));
        await shoot('phone-details');
        await evaluate('history.back()');
        await waitFor(`document.body.dataset.view === "conversation" && ${shown('.middle-panel')}`);
        await evaluate('document.getElementById("backBtn").click()');
        await waitFor(`document.body.dataset.view === "list" && ${shown('.left-panel')} && document.getElementById("backBtn").hidden`);
        // A shared deep link opens straight on the conversation pane.
        await evaluate(`location.hash = ${JSON.stringify(linked)}; location.reload()`);
        await waitFor(`document.body.dataset.view === "conversation" && document.querySelectorAll(".message-item").length === 5 && ${shown('.middle-panel')}`);
        await evaluate('document.getElementById("consoleBtn").click()');
        await waitFor('!document.getElementById("consoleSheet").hidden && document.querySelectorAll("#keyList .key-card").length > 0');
        assert.ok(await evaluate('Math.abs(document.querySelector(".sheet-panel").getBoundingClientRect().width - innerWidth) < 1'));
        await shoot('phone-console');
        await evaluate('document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}))');
        await waitFor('document.getElementById("consoleSheet").hidden');
    } else {
        assert.equal(security.keys, 0); assert.equal(security.sessions, 0);
        assert.ok(await evaluate('document.getElementById("consoleSheet").hidden'));
        await evaluate('document.getElementById("consoleBtn").click()');
        await waitFor('!document.getElementById("consoleSheet").hidden && document.activeElement?.id === "consoleClose"');
        await evaluate('document.getElementById("keyName").value="browser created"; document.getElementById("createKey").click()');
        await waitFor('document.getElementById("newKey").textContent.includes("asv_") && !document.getElementById("newKeyBox").hidden && document.querySelectorAll("#keyList .key-card").length === 1');
        const shown = await evaluate('document.getElementById("newKey").textContent.match(/asv_[A-Za-z0-9_-]+/)[0]');
        await shoot('desktop-console');
        await evaluate('document.querySelector(".sheet-backdrop").click()');
        await waitFor('document.getElementById("consoleSheet").hidden');
        await evaluate('window.beforeReload = true; location.reload()');
        await waitFor('!window.beforeReload && document.querySelectorAll("#machineSummary .machine-chip").length === 1');
        await evaluate('document.getElementById("consoleBtn").click()');
        await waitFor('document.querySelectorAll("#keyList .key-card").length === 1 && document.getElementById("newKeyBox").hidden');
        assert.ok(!await evaluate(`document.documentElement.outerHTML.includes(${JSON.stringify(shown)})`), 'key shown only once');
    }
    assert.deepEqual(loginOrigins, [origin]);
    assert.deepEqual(errors, []);
    console.log(mode === '--analysis-data'
        ? 'Docker analysis browser passed: machine badge/filter/title/chips, date range, deep link, timestamped messages, prose, invocation and outcome summary visible; original replay hidden; phone layout (one pane, Back, filters, console sheet); auth/CSRF intact.'
        : 'Docker browser passed: password login, protected viewer/assets, console sheet opens/closes, CSRF rejection, key shown once.');
} finally {
    socket?.close();
    if (browser?.pid) {
        try {
            if (process.platform === 'win32') execFileSync('taskkill', ['/PID', String(browser.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
            else browser.kill('SIGTERM');
        } catch { /* only our headless test process */ }
    }
    await delay(500); fs.rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}
