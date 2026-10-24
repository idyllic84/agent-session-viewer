// Optional real-browser smoke test. No npm packages; Node 22+ and Edge required.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const edgeBin = process.env.EDGE_BIN || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
if (!fs.existsSync(edgeBin)) {
    console.log('Browser smoke test skipped: set EDGE_BIN to an Edge/Chromium executable.');
    process.exit(0);
}
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'viewer-browser-test-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, browser, socket;
const stop = child => {
    if (!child?.pid) return;
    try {
        if (process.platform === 'win32') execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
        else child.kill('SIGTERM');
    } catch { /* Process may already have exited. */ }
};
try {
    server = spawn(process.env.PYTHON || 'python', ['-u', fileURLToPath(new URL('./local_fixture.py', import.meta.url)), root],
        { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    let output = '';
    server.stdout.on('data', chunk => { output += chunk; });
    let serverErrors = '';
    server.stderr.on('data', chunk => { serverErrors += chunk; });
    let url;
    for (let i = 0; i < 150; i++) {
        url = output.split(/\r?\n/).find(line => /^http:\/\/127\.0\.0\.1:\d+$/.test(line));
        if (url) break;
        if (server.exitCode !== null) throw new Error(serverErrors);
        await delay(100);
    }
    assert.ok(url?.startsWith('http://127.0.0.1:'), serverErrors || 'Fixture server failed to start');
    const profile = path.join(root, 'browser-profile');
    browser = spawn(edgeBin, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore', windowsHide: true });
    const activePort = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 200 && !fs.existsSync(activePort); i++) await delay(100);
    assert.ok(fs.existsSync(activePort), 'Browser DevTools port did not appear');
    const port = fs.readFileSync(activePort, 'utf8').split('\n')[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
        socket.addEventListener('open', resolve, { once: true });
        socket.addEventListener('error', reject, { once: true });
    });
    let id = 0;
    const pending = new Map();
    const runtimeErrors = [];
    socket.addEventListener('message', event => {
        const message = JSON.parse(event.data);
        if (message.method === 'Runtime.exceptionThrown') runtimeErrors.push(message.params.exceptionDetails.text);
        if (!pending.has(message.id)) return;
        const { resolve, reject, timer } = pending.get(message.id);
        clearTimeout(timer); pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
    });
    const command = (method, params = {}) => new Promise((resolve, reject) => {
        const key = ++id;
        const timer = setTimeout(() => { pending.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
        pending.set(key, { resolve, reject, timer });
        socket.send(JSON.stringify({ id: key, method, params }));
    });
    const evaluate = async expression => {
        const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
        if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
        return result.result.value;
    };
    const waitFor = async expression => {
        for (let i = 0; i < 150; i++) {
            if (await evaluate(expression)) return;
            await delay(100);
        }
        throw new Error(`Browser condition not reached: ${expression}`);
    };
    await command('Runtime.enable');
    await command('Page.navigate', { url });
    await waitFor('document.querySelectorAll(".session-item").length === 3 && document.querySelectorAll("#sourceSelect option").length === 6');
    await evaluate('document.getElementById("sourceSelect").value="pi"; document.getElementById("sourceSelect").dispatchEvent(new Event("change"))');
    await waitFor('document.querySelectorAll(".session-item").length === 1');
    await evaluate('document.querySelector(".session-item").click()');
    await waitFor('document.querySelectorAll(".message-item").length === 2000');
    assert.equal(await evaluate('document.getElementById("loadMoreBtn").textContent.trim()'), 'Load next 2000 records');
    assert.equal(await evaluate('document.querySelector(".session-item .message-count").textContent.trim()'), '1 / 2008');
    await evaluate('document.querySelectorAll(".message-item")[1].click()');
    await waitFor('document.querySelectorAll("#messageDetails .content-block").length === 1');
    assert.ok(!await evaluate('document.getElementById("messageDetails").textContent.includes("synthetic reasoning")'), 'thinking is not stored');
    assert.equal(await evaluate('getComputedStyle(document.querySelector("#messageDetails button[data-action=raw]")).display'), 'none');
    await evaluate('document.getElementById("loadMoreBtn").click()');
    await waitFor('document.querySelectorAll(".message-item").length === 2008');
    assert.equal(await evaluate('document.getElementById("loadedCount").textContent'), '2008 / 2008 loaded');
    assert.equal(await evaluate('document.querySelectorAll(".message-item.tool-use").length'), 1);
    await evaluate('document.querySelector(".message-item").click()');
    assert.equal(await evaluate('document.querySelectorAll("#messageDetails img").length'), 0);
    assert.equal(await evaluate('Boolean(window.__xss)'), false);
    await evaluate('document.getElementById("sourceSelect").value="vscode_chat"; document.getElementById("sourceSelect").dispatchEvent(new Event("change"))');
    await waitFor('document.querySelectorAll(".session-item").length === 1 && document.querySelector(".session-item").dataset.sessionId === "vscode_chat:inline-test"');
    await evaluate('document.querySelector(".session-item").click()');
    await waitFor('document.getElementById("conversationHistory").textContent.includes("event.item.model_dump()")');
    const completeSentence = '用 event.item.model_dump() 转发上游 conversation.item.added';
    assert.ok(await evaluate(`document.getElementById("conversationHistory").textContent.includes(${JSON.stringify(completeSentence)})`));
    await evaluate('document.querySelectorAll(".message-item")[1].click()');
    assert.ok(await evaluate(`document.getElementById("messageDetails").textContent.includes(${JSON.stringify(completeSentence)})`));
    await evaluate('document.getElementById("resetSearchScopeBtn").click()');
    await waitFor('document.querySelectorAll(".session-item").length === 3');
    await evaluate('document.getElementById("projectSelect").value="C:/synthetic/project"; document.getElementById("projectSelect").dispatchEvent(new Event("change"))');
    await waitFor('document.querySelectorAll(".session-item").length === 1');
    // Input only, no Enter: search must run and stale project scope must not hide the hit.
    await evaluate('document.getElementById("sessionSearch").value="conversation.item.added"; document.getElementById("sessionSearch").dispatchEvent(new Event("input", {bubbles:true}))');
    await waitFor('document.querySelectorAll(".session-item").length === 1 && document.querySelector(".session-item").dataset.sessionId === "vscode_chat:inline-test"');
    assert.equal(await evaluate('document.getElementById("projectSelect").value'), '');
    assert.ok(await evaluate('document.getElementById("sessionSearchStatus").textContent.includes("1 sessions")'));
    assert.equal(await evaluate('document.querySelectorAll(".session-item[data-session-id*=empty], .session-item[data-session-id*=agent-child]").length'), 0);
    await evaluate('document.getElementById("reloadBtn").click()');
    await waitFor('/0 added · 0 updated · [0-9]+ unchanged .* 0 failed/.test(document.getElementById("syncStatus").textContent)');
    // Cloud console: no login locally; address + key entered by the user; upload only on click.
    const cloudOrigin = fs.readFileSync(path.join(root, 'cloud-origin.txt'), 'utf8').trim();
    const cloudKey = fs.readFileSync(path.join(root, 'cloud-key.txt'), 'utf8').trim();
    await evaluate('document.getElementById("cloudConsole").open = true');
    await waitFor('document.getElementById("cloudConnection").textContent.includes("not configured")');
    const forged = await evaluate(`fetch('/api/cloud/upload/start',{method:'POST'}).then(r=>r.status)`);
    assert.equal(forged, 403, 'mutations without the page CSRF token are refused');
    await evaluate(`document.getElementById("cloudUrl").value=${JSON.stringify(cloudOrigin)}; document.getElementById("cloudKey").value=${JSON.stringify(cloudKey)}; document.getElementById("cloudTest").click()`);
    await waitFor('document.getElementById("consoleMessage").textContent.includes("Connection OK")');
    await evaluate('document.getElementById("cloudSave").click()');
    await waitFor('document.getElementById("consoleMessage").textContent.includes("Nothing was uploaded yet")');
    assert.equal(await evaluate('document.getElementById("cloudKey").value'), '');
    assert.ok(!await evaluate(`document.body.textContent.includes(${JSON.stringify(cloudKey)})`), 'key is never displayed again');
    await evaluate('document.getElementById("uploadStart").click()');
    await waitFor('document.getElementById("uploadState").textContent.includes("complete") && document.getElementById("consoleSummary").textContent.includes("up to date")');
    assert.deepEqual(runtimeErrors, []);
    console.log('Real browser smoke test passed: local viewer (analysis only), inline references, live search, user counts, hidden empty/subagent sessions, pagination, XSS escaping, explicit re-import, console connect/test/save/upload with CSRF.');
} finally {
    socket?.close();
    stop(browser);
    stop(server);
    await delay(500);
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}
