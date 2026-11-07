// Tests for POST /api/usage/refresh (server.mjs), capabilities.usageRefresh on the
// state payloads, and `dept.mjs usage refresh`.
// Safety: no real credentials and no real Anthropic. Each test writes FAKE
// credentials into a scratch CLAUDE_CONFIG_DIR (HOME/USERPROFILE point at the
// scratch dir too), DEPT_ANTHROPIC_BASE points at a local fake HTTP server,
// the shared board is off (DEPT_HUB=off, DEPT_HUB_URL empty), DEPT_DATA is a
// scratch file and every port is a free one. Only child processes started here
// are killed.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(here, '..', 'server.mjs');
const CLI = path.join(here, '..', 'dept.mjs');
const FAKE_TOKEN = 'fake-test-token-NOT-REAL-7f3a9c';
const FAKE_REFRESH = 'fake-refresh-token-NOT-REAL-11b2';

const scratchDirs = [];
const children = new Set();
const fakes = new Set();
const scratch = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-refresh-'));
  scratchDirs.push(dir);
  return dir;
};
test.after(async () => {
  for (const c of children) c.kill();
  for (const f of fakes) f.close();
  for (const dir of scratchDirs) fs.rmSync(dir, { recursive: true, force: true });
});

async function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function writeCreds(dir, { expiresAt = Date.now() + 3600e3, accessToken = FAKE_TOKEN } = {}) {
  const config = path.join(dir, 'claude-config');
  fs.mkdirSync(config, { recursive: true });
  fs.writeFileSync(
    path.join(config, '.credentials.json'),
    JSON.stringify({ claudeAiOauth: { accessToken, refreshToken: FAKE_REFRESH, expiresAt, scopes: ['user:inference'] } }),
  );
  return config;
}

// Fake Anthropic usage endpoint. `mode` picks the answer; requests are recorded.
const FIVE_RESET = new Date(Date.now() + 2 * 3600e3).toISOString();
const WEEK_RESET = new Date(Date.now() + 4 * 86400e3).toISOString();
async function fakeAnthropic(mode = 'ok') {
  const fake = { mode, requests: [] };
  const srv = http.createServer((req, res) => {
    fake.requests.push({ method: req.method, url: req.url, headers: { ...req.headers } });
    switch (fake.mode) {
      case 'ok':
        res.writeHead(200, { 'content-type': 'application/json' });
        return res.end(JSON.stringify({
          five_hour: { utilization: 37.5, resets_at: FIVE_RESET },
          seven_day: { utilization: 12, resets_at: WEEK_RESET },
          seven_day_opus: null,
          extra_unknown: { whatever: true },
        }));
      case '401':
        res.writeHead(401, { 'content-type': 'application/json' });
        // An echo of the header, which must never reach the client.
        return res.end(JSON.stringify({ error: `invalid token ${req.headers.authorization}` }));
      case '500':
        res.writeHead(500, { 'content-type': 'text/plain' });
        return res.end(`boom ${req.headers.authorization}`);
      case '429':
        res.writeHead(429, { 'content-type': 'application/json' });
        return res.end('{"error":"rate"}');
      case 'garbage':
        res.writeHead(200, { 'content-type': 'application/json' });
        return res.end(`<html>not json ${req.headers.authorization}</html>`);
      default:
        res.writeHead(404).end();
    }
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  fakes.add(srv);
  fake.base = `http://127.0.0.1:${srv.address().port}`;
  fake.close = () => new Promise((r) => { fakes.delete(srv); srv.close(r); });
  return fake;
}

function childEnv(dir, port, extra = {}) {
  const e = { ...process.env };
  for (const k of Object.keys(e)) {
    if (/^(HTTPS?_PROXY|https?_proxy|NODE_USE_ENV_PROXY|DEPT_|CLAUDE_|ANTHROPIC_)/.test(k)) delete e[k];
  }
  return {
    ...e,
    HOME: dir,
    USERPROFILE: dir,
    DEPT_DATA: path.join(dir, 'state.json'),
    DEPT_HUB: 'off',
    DEPT_HUB_URL: '',
    PORT: String(port),
    ...extra,
  };
}

// Start server.mjs; resolves once it listens. Output is collected for the
// token-leak checks.
async function startServer(dir, extra = {}) {
  const port = await freePort();
  const child = spawn(process.execPath, [SERVER, '--port', String(port)], { env: childEnv(dir, port, extra) });
  children.add(child);
  const out = { text: '' };
  child.stdout.on('data', (d) => (out.text += d));
  child.stderr.on('data', (d) => (out.text += d));
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`server did not start:\n${out.text}`)), 8000);
    child.stdout.on('data', () => {
      if (out.text.includes('Agent Department dashboard')) {
        clearTimeout(timer);
        resolve();
      }
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`server exited (${code}):\n${out.text}`));
    });
  });
  const stop = () =>
    new Promise((r) => {
      if (child.exitCode !== null) return r();
      child.once('exit', () => r());
      child.kill();
    }).then(() => children.delete(child));
  return { port, url: `http://127.0.0.1:${port}`, out, stop, child };
}

async function post(url, headers = {}) {
  const res = await fetch(`${url}/api/usage/refresh`, { method: 'POST', headers });
  const text = await res.text();
  return { status: res.status, text, json: JSON.parse(text), headers: res.headers };
}

// First `state` event of the SSE stream.
async function firstEvent(url) {
  const res = await fetch(`${url}/api/events`);
  const reader = res.body.getReader();
  let text = '';
  while (!text.includes('\n\n')) text += new TextDecoder().decode((await reader.read()).value);
  await reader.cancel();
  return JSON.parse(text.split('\n').find((l) => l.startsWith('data: ')).slice(6));
}

const getState = async (url) => (await (await fetch(`${url}/api/state`)).json()).result;

function assertNoToken(...texts) {
  for (const t of texts) {
    assert.ok(!String(t).includes(FAKE_TOKEN), 'access token leaked');
    assert.ok(!String(t).includes(FAKE_REFRESH), 'refresh token leaked');
  }
}

test('success: stores usage from Anthropic, source ends with "· Anthropic", then throttles', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const config = writeCreds(dir);
  const credsBefore = fs.readFileSync(path.join(config, '.credentials.json'), 'utf8');
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: config, DEPT_ANTHROPIC_BASE: fake.base });
  try {
    const before = await getState(srv.url);
    assert.equal(before.usage, null);
    assert.deepEqual(before.capabilities, { usageRefresh: true });

    const r = await post(srv.url);
    assert.equal(r.status, 200, r.text);
    assert.equal(r.json.ok, true);
    const u = r.json.result.usage;
    assert.equal(u.fiveHour.usedPct, 37.5);
    assert.equal(Date.parse(u.fiveHour.resetsAt), Date.parse(FIVE_RESET));
    assert.equal(u.sevenDay.usedPct, 12);
    assert.ok(u.source.endsWith('· Anthropic'), u.source);

    // The fake endpoint got the documented request.
    assert.equal(fake.requests.length, 1);
    const q = fake.requests[0];
    assert.equal(q.method, 'GET');
    assert.equal(q.url, '/api/oauth/usage');
    assert.equal(q.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    assert.equal(q.headers['anthropic-beta'], 'oauth-2025-04-20');

    // Board updated (live payload and file), capabilities not saved, no token anywhere.
    const after = await getState(srv.url);
    assert.equal(after.usage.fiveHour.usedPct, 37.5);
    assert.ok(after.usage.source.endsWith('· Anthropic'));
    const file = fs.readFileSync(path.join(dir, 'state.json'), 'utf8');
    assert.equal(JSON.parse(file).usage.sevenDay.usedPct, 12);
    assert.ok(!('capabilities' in JSON.parse(file)), 'capabilities must not be saved');
    assertNoToken(r.text, JSON.stringify(after), file);

    // Second click within 30 s: 429 with retryAfter, Anthropic not called again.
    const t = await post(srv.url);
    assert.equal(t.status, 429);
    assert.equal(t.json.ok, false);
    assert.equal(t.json.code, 'throttled');
    assert.ok(Number.isInteger(t.json.retryAfter) && t.json.retryAfter >= 1 && t.json.retryAfter <= 30, t.text);
    assert.match(t.json.error, /^Wait \d+ s before refreshing again$/);
    assert.equal(t.headers.get('retry-after'), String(t.json.retryAfter));
    assert.equal(fake.requests.length, 1);

    // Only POST.
    const g = await fetch(`${srv.url}/api/usage/refresh`);
    assert.equal(g.status, 405);

    assertNoToken(srv.out.text);
    assert.equal(fs.readFileSync(path.join(config, '.credentials.json'), 'utf8'), credsBefore, 'credentials file untouched');
  } finally {
    await srv.stop();
    await fake.close();
  }
});

test('capabilities.usageRefresh is on the SSE payload too', async () => {
  const dir = scratch();
  const srv = await startServer(dir);
  try {
    const data = await firstEvent(srv.url);
    assert.deepEqual(data.capabilities, { usageRefresh: true });
  } finally {
    await srv.stop();
  }
});

test('DEPT_USAGE_DIRECT=off: capability false and refresh refused', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: fake.base, DEPT_USAGE_DIRECT: 'off' });
  try {
    assert.deepEqual((await getState(srv.url)).capabilities, { usageRefresh: false });
    const r = await post(srv.url);
    assert.equal(r.status, 403);
    assert.equal(r.json.code, 'disabled');
    assert.equal(fake.requests.length, 0);
    assert.equal((await getState(srv.url)).usage, null);
  } finally {
    await srv.stop();
    await fake.close();
  }
});

test('refused unless the request comes from this machine', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: fake.base, HOST: '0.0.0.0' });
  try {
    // Loopback socket but a foreign Host header (DNS rebinding) or a foreign Origin (another website).
    const rebinding = await new Promise((resolve, reject) => {
      const req = http.request({ host: '127.0.0.1', port: srv.port, method: 'POST', path: '/api/usage/refresh', headers: { host: `evil.example:${srv.port}` } }, (res) => {
        let b = '';
        res.on('data', (d) => (b += d));
        res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(b) }));
      });
      req.on('error', reject);
      req.end();
    });
    assert.equal(rebinding.status, 403);
    assert.equal(rebinding.json.code, 'forbidden');
    const cross = await post(srv.url, { origin: 'https://evil.example' });
    assert.equal(cross.status, 403);
    assert.equal(cross.json.code, 'forbidden');

    // capabilities.usageRefresh is per request: true for this computer, false for a foreign Host.
    assert.equal((await getState(srv.url)).capabilities.usageRefresh, true);
    const foreignState = await new Promise((resolve, reject) => {
      http.get({ host: '127.0.0.1', port: srv.port, path: '/api/state', headers: { host: `evil.example:${srv.port}` } }, (res) => {
        let b = '';
        res.on('data', (d) => (b += d));
        res.on('end', () => resolve(JSON.parse(b).result));
      }).on('error', reject);
    });
    assert.equal(foreignState.capabilities.usageRefresh, false);

    // A real non-loopback address of this machine, when it has one.
    const external = Object.values(os.networkInterfaces()).flat().find((i) => i && i.family === 'IPv4' && !i.internal);
    if (external) {
      const remote = `http://${external.address}:${srv.port}`;
      const r = await post(remote);
      assert.equal(r.status, 403);
      assert.equal(r.json.code, 'forbidden');
      // A phone on the LAN is told there is no refresh, on GET /api/state and on the SSE stream.
      assert.equal((await getState(remote)).capabilities.usageRefresh, false);
      assert.equal((await firstEvent(remote)).capabilities.usageRefresh, false);
      assert.equal((await firstEvent(srv.url)).capabilities.usageRefresh, true);
    } else {
      test.diagnostic('no non-loopback IPv4 interface; remote-address case skipped');
    }
    assert.equal(fake.requests.length, 0, 'Anthropic never called for refused requests');

    // Refusals don't use up the throttle: this machine can still refresh, with a same-origin Origin.
    const ok = await post(srv.url, { origin: srv.url });
    assert.equal(ok.status, 200, ok.text);
    assert.equal(fake.requests.length, 1);
  } finally {
    await srv.stop();
    await fake.close();
  }
});

test('expired login: short message, Anthropic not called, throttle not used', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const config = writeCreds(dir, { expiresAt: Date.now() - 60e3 });
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: config, DEPT_ANTHROPIC_BASE: fake.base });
  try {
    for (let i = 0; i < 2; i++) {
      const r = await post(srv.url);
      assert.equal(r.status, 401, r.text);
      assert.equal(r.json.code, 'expired');
      assert.match(r.json.error, /Claude login has expired/);
      assertNoToken(r.text);
    }
    assert.equal(fake.requests.length, 0);
    // Missing credentials file entirely.
    fs.rmSync(path.join(config, '.credentials.json'));
    const m = await post(srv.url);
    assert.equal(m.status, 401);
    assert.equal(m.json.code, 'no_login');
    assert.equal((await getState(srv.url)).usage, null);
    assertNoToken(srv.out.text);
  } finally {
    await srv.stop();
    await fake.close();
  }
});

for (const [mode, code, status, pattern] of [
  ['401', 'expired', 401, /Claude login has expired/],
  ['429', 'rate_limited', 429, /rate-limiting/],
  ['500', 'bad_response', 502, /unexpected response/],
  ['garbage', 'bad_response', 502, /didn't return usage data/],
]) {
  test(`endpoint answers ${mode}: ${code}, short message, no token, board unchanged`, async () => {
    const dir = scratch();
    const fake = await fakeAnthropic(mode);
    const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: fake.base });
    try {
      const r = await post(srv.url);
      assert.equal(r.status, status, r.text);
      assert.equal(r.json.ok, false);
      assert.equal(r.json.code, code);
      assert.match(r.json.error, pattern);
      assert.ok(r.json.error.length < 200);
      assert.equal(fake.requests.length, 1);
      assertNoToken(r.text, srv.out.text);
      assert.equal((await getState(srv.url)).usage, null);
      // The request reached Anthropic, so the throttle applies.
      assert.equal((await post(srv.url)).json.code, 'throttled');
    } finally {
      await srv.stop();
      await fake.close();
    }
  });
}

test('endpoint unreachable: "Couldn\'t reach Anthropic."', async () => {
  const dir = scratch();
  const dead = `http://127.0.0.1:${await freePort()}`;
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: dead });
  try {
    const r = await post(srv.url);
    assert.equal(r.status, 502);
    assert.equal(r.json.code, 'unreachable');
    assert.equal(r.json.error, "Couldn't reach Anthropic.");
  } finally {
    await srv.stop();
  }
});

function runCli(args, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLI, ...args], { env });
    children.add(child);
    let out = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (out += d));
    child.on('exit', (code) => {
      children.delete(child);
      resolve({ code, out });
    });
  });
}

test('dept.mjs usage refresh: without a dashboard it stores to the local board', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const e = childEnv(dir, await freePort(), { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: fake.base });
  const r = await runCli(['usage', 'refresh'], e);
  assert.equal(r.code, 0, r.out);
  assert.match(r.out, /Usage updated from Anthropic/);
  assert.match(r.out, /37\.5%/);
  assertNoToken(r.out);
  const st = JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'));
  assert.equal(st.usage.fiveHour.usedPct, 37.5);
  assert.ok(st.usage.source.endsWith('· Anthropic'));

  // Same 30 s throttle as the dashboard, kept next to the state file.
  const lastFile = path.join(dir, 'usage-refresh-last.json');
  assert.ok(fs.existsSync(lastFile), 'throttle file written');
  assertNoToken(fs.readFileSync(lastFile, 'utf8'));
  const again = await runCli(['usage', 'refresh'], e);
  assert.equal(again.code, 1);
  assert.match(again.out, /Error: Wait \d+ s before refreshing again/);
  assert.equal(fake.requests.length, 1, 'throttled run did not call Anthropic');

  // Once 30 s have passed: errors are the short message, exit code 1, and count for the throttle.
  fs.writeFileSync(lastFile, JSON.stringify({ at: Date.now() - 31e3 }));
  fake.mode = '401';
  const bad = await runCli(['usage', 'refresh'], e);
  assert.equal(bad.code, 1);
  assert.match(bad.out, /Error: Your Claude login has expired/);
  assertNoToken(bad.out);
  assert.equal(fake.requests.length, 2);
  assert.ok(JSON.parse(fs.readFileSync(lastFile, 'utf8')).at > Date.now() - 10e3, 'attempt that reached Anthropic marks the throttle');

  const off = await runCli(['usage', 'refresh'], { ...e, DEPT_USAGE_DIRECT: 'off' });
  assert.equal(off.code, 1);
  assert.match(off.out, /turned off/);
  await fake.close();
});

test('dept.mjs usage refresh: goes through the running dashboard (and its throttle)', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const config = writeCreds(dir);
  const srv = await startServer(dir, { CLAUDE_CONFIG_DIR: config, DEPT_ANTHROPIC_BASE: fake.base });
  try {
    const e = childEnv(dir, srv.port, { CLAUDE_CONFIG_DIR: config, DEPT_ANTHROPIC_BASE: fake.base });
    const r = await runCli(['usage', 'refresh'], e);
    assert.equal(r.code, 0, r.out);
    assert.match(r.out, /Usage updated from Anthropic/);
    assert.equal((await getState(srv.url)).usage.fiveHour.usedPct, 37.5);
    const again = await runCli(['usage', 'refresh'], e);
    assert.equal(again.code, 1);
    assert.match(again.out, /Error: Wait \d+ s before refreshing again/);
    assert.equal(fake.requests.length, 1);
    assertNoToken(r.out, again.out, srv.out.text);
  } finally {
    await srv.stop();
    await fake.close();
  }
});

test('dept.mjs usage refresh: an expired login does not use up the direct throttle', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  const config = writeCreds(dir, { expiresAt: Date.now() - 60e3 });
  const e = childEnv(dir, await freePort(), { CLAUDE_CONFIG_DIR: config, DEPT_ANTHROPIC_BASE: fake.base });
  const bad = await runCli(['usage', 'refresh'], e);
  assert.equal(bad.code, 1);
  assert.match(bad.out, /Claude login has expired/);
  assert.equal(fake.requests.length, 0);
  assert.ok(!fs.existsSync(path.join(dir, 'usage-refresh-last.json')));
  writeCreds(dir); // "renewed" the login
  const ok = await runCli(['usage', 'refresh'], e);
  assert.equal(ok.code, 0, ok.out);
  assert.equal(fake.requests.length, 1);
  await fake.close();
});

test('dept.mjs usage refresh: a dashboard running older code gets a restart hint, no direct fallback', async () => {
  const dir = scratch();
  const fake = await fakeAnthropic('ok');
  // Stand-in for a pre-refresh dashboard: unknown /api routes answer 404 {"error":"Not found"}.
  const old = http.createServer((req, res) => {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'Not found' }));
  });
  await new Promise((r) => old.listen(0, '127.0.0.1', r));
  fakes.add(old);
  try {
    const e = childEnv(dir, old.address().port, { CLAUDE_CONFIG_DIR: writeCreds(dir), DEPT_ANTHROPIC_BASE: fake.base });
    const r = await runCli(['usage', 'refresh'], e);
    assert.equal(r.code, 1);
    assert.match(r.out, /Error: Your dashboard is running older code — restart it \(close it, stop the server, reopen the shortcut\) to enable usage refresh\./);
    assert.equal(fake.requests.length, 0, 'no fallback to the direct path');
    assert.ok(!fs.existsSync(path.join(dir, 'state.json')), 'local board untouched');
  } finally {
    fakes.delete(old);
    await new Promise((r) => old.close(r));
    await fake.close();
  }
});
