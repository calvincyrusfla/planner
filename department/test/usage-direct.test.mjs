// Tests for lib/usage-direct.mjs. Everything is fake: credentials live in
// scratch temp dirs or in-memory fs/exec stubs, and Anthropic is a mocked fetch
// or a local HTTP server on 127.0.0.1. Nothing here touches real credentials
// or api.anthropic.com.
import { test, before, after, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { inspect } from 'node:util';
import * as core from '../lib/core.mjs';
import { applyOp } from '../lib/ops.mjs';
import {
  readClaudeToken,
  fetchAnthropicUsage,
  toReport,
  refreshUsageFromAnthropic,
  credentialsPath,
  resolveBase,
  isUsageDirectEnabled,
  defaultSource,
  UsageDirectError,
  CODE_STATUS,
  MESSAGES,
  OAUTH_BETA,
  KEYCHAIN_SERVICE,
} from '../lib/usage-direct.mjs';

const TOKEN = 'sk-ant-oat01-FAKE-test-token-DO-NOT-LEAK-7f3a9c';
const NOW = Date.parse('2026-11-07T12:00:00.000Z');
const FUTURE = NOW + 3600_000;
const PAST = NOW - 1000;
const creds = (over = {}) =>
  JSON.stringify({ claudeAiOauth: { accessToken: TOKEN, refreshToken: 'rt-FAKE-refresh', expiresAt: FUTURE, ...over } });
const GOOD = {
  five_hour: { utilization: 42.34, resets_at: '2026-11-07T15:10:00.000Z' },
  seven_day: { utilization: 18, resets_at: '2026-11-12T00:00:00+00:00' },
  seven_day_opus: null,
  extra_key: { anything: true },
};

// ---- token-leak guard: capture everything printed during the tests ----
const printed = [];
const saved = {};
before(() => {
  for (const k of ['log', 'error', 'warn', 'info', 'debug']) {
    saved[k] = console[k];
    console[k] = (...a) => printed.push(a.map((x) => inspect(x)).join(' '));
  }
});
after(() => {
  for (const k of Object.keys(saved)) console[k] = saved[k];
  assert.ok(!printed.join('\n').includes(TOKEN), 'token appeared in console output');
});

// Everything observable about an error, as text.
const errText = (err) =>
  [String(err), err?.message, err?.stack, err?.code, inspect(err, { depth: 5, showHidden: true }), JSON.stringify(err), String(err?.cause ?? '')].join('\n');
const assertNoToken = (value, what = 'value') => {
  const text = value instanceof Error ? errText(value) : inspect(value, { depth: 10, showHidden: true }) + JSON.stringify(value);
  assert.ok(!text.includes(TOKEN), `token leaked in ${what}`);
  assert.ok(!text.includes('rt-FAKE-refresh'), `refresh token leaked in ${what}`);
};
async function rejectsWith(promise, code, message) {
  let caught;
  try {
    await promise;
  } catch (err) {
    caught = err;
  }
  assert.ok(caught, `expected rejection with ${code}`);
  assert.ok(caught instanceof UsageDirectError, 'is a UsageDirectError');
  assert.ok(caught instanceof core.DeptError, 'is a DeptError');
  assert.equal(caught.code, code);
  if (message) assert.equal(caught.message, message);
  assert.equal(caught.status, CODE_STATUS[code]);
  assert.equal(caught.cause, undefined);
  assertNoToken(caught, `${code} error`);
  return caught;
}

// in-memory fs: {path: text}
const memFs = (files) => ({
  reads: [],
  readFileSync(p) {
    this.reads.push(p);
    if (Object.hasOwn(files, p)) return files[p];
    const e = new Error(`ENOENT: no such file, open '${p}'`);
    e.code = 'ENOENT';
    throw e;
  },
  writeFileSync() {
    throw new Error('must never write credentials');
  },
});
const noExec = () => {
  throw new Error('exec must not be called');
};

// mocked fetch
function mockFetch(respond) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return respond(url, init);
  };
  fn.calls = calls;
  return fn;
}
const jsonRes = (status, body) => ({
  status,
  ok: status >= 200 && status < 300,
  json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
});

let tmp;
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'usage-direct-'));
});
afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

// ---------- credentials path ----------
test('credentialsPath: CLAUDE_CONFIG_DIR wins on every platform', () => {
  assert.equal(credentialsPath({ env: { CLAUDE_CONFIG_DIR: '/x/cfg', HOME: '/h' }, platform: 'linux' }), '/x/cfg/.credentials.json');
  assert.equal(
    credentialsPath({ env: { CLAUDE_CONFIG_DIR: 'D:\\cfg', USERPROFILE: 'C:\\Users\\Ann' }, platform: 'win32' }),
    'D:\\cfg\\.credentials.json',
  );
});

test('credentialsPath: Windows uses %USERPROFILE%\\.claude with backslashes', () => {
  assert.equal(
    credentialsPath({ env: { USERPROFILE: 'C:\\Users\\Ann Lee', HOME: '/ignored' }, platform: 'win32' }),
    'C:\\Users\\Ann Lee\\.claude\\.credentials.json',
  );
  assert.equal(
    credentialsPath({ env: { HOMEDRIVE: 'C:', HOMEPATH: '\\Users\\Bo' }, platform: 'win32' }),
    'C:\\Users\\Bo\\.claude\\.credentials.json',
  );
  assert.equal(credentialsPath({ env: {}, platform: 'win32', homedir: () => 'C:\\Users\\Cy' }), 'C:\\Users\\Cy\\.claude\\.credentials.json');
});

test('credentialsPath: POSIX uses ~/.claude', () => {
  assert.equal(credentialsPath({ env: { HOME: '/home/ann' }, platform: 'linux' }), '/home/ann/.claude/.credentials.json');
  assert.equal(credentialsPath({ env: {}, platform: 'linux', homedir: () => '/home/bo' }), '/home/bo/.claude/.credentials.json');
});

// ---------- readClaudeToken ----------
test('readClaudeToken reads a real (fake) file under CLAUDE_CONFIG_DIR', async () => {
  fs.writeFileSync(path.join(tmp, '.credentials.json'), creds());
  const before = fs.readFileSync(path.join(tmp, '.credentials.json'), 'utf8');
  const r = await readClaudeToken({ env: { CLAUDE_CONFIG_DIR: tmp }, platform: 'linux', execImpl: noExec, now: () => NOW });
  assert.deepEqual(r, { token: TOKEN, expiresAt: FUTURE });
  assert.equal(fs.readFileSync(path.join(tmp, '.credentials.json'), 'utf8'), before, 'file untouched');
});

test('readClaudeToken on Windows reads %USERPROFILE%\\.claude\\.credentials.json', async () => {
  const p = 'C:\\Users\\Ann\\.claude\\.credentials.json';
  const mfs = memFs({ [p]: creds() });
  const r = await readClaudeToken({ env: { USERPROFILE: 'C:\\Users\\Ann' }, platform: 'win32', fsImpl: mfs, execImpl: noExec, now: () => NOW });
  assert.equal(r.token, TOKEN);
  assert.deepEqual(mfs.reads, [p]);
});

test('readClaudeToken: expiresAt in epoch seconds or ISO is understood', async () => {
  const p = '/h/.claude/.credentials.json';
  for (const expiresAt of [Math.floor(FUTURE / 1000), new Date(FUTURE).toISOString()]) {
    const r = await readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: memFs({ [p]: creds({ expiresAt }) }), now: () => NOW });
    assert.equal(r.expiresAt, FUTURE);
  }
  const r = await readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: memFs({ [p]: creds({ expiresAt: undefined }) }), now: () => NOW });
  assert.equal(r.expiresAt, null, 'missing expiresAt is allowed (unknown)');
});

test('readClaudeToken: expired token -> expired, never refreshed or rewritten', async () => {
  const p = '/h/.claude/.credentials.json';
  const mfs = memFs({ [p]: creds({ expiresAt: PAST }) });
  await rejectsWith(readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: mfs, now: () => NOW }), 'expired', MESSAGES.expired);
  await rejectsWith(
    readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: memFs({ [p]: creds({ expiresAt: NOW }) }), now: () => NOW }),
    'expired',
  );
  assert.equal(
    MESSAGES.expired,
    'Your Claude login has expired — open Claude Code once to renew it, then try again.',
  );
});

test('readClaudeToken: missing or empty accessToken -> expired', async () => {
  const p = '/h/.claude/.credentials.json';
  for (const accessToken of [undefined, '', '   ', 42]) {
    await rejectsWith(
      readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: memFs({ [p]: creds({ accessToken }) }), now: () => NOW }),
      'expired',
    );
  }
});

test('readClaudeToken: missing file, garbage file, wrong shape -> no_login', async () => {
  await rejectsWith(readClaudeToken({ env: { CLAUDE_CONFIG_DIR: tmp }, platform: 'linux', now: () => NOW }), 'no_login', MESSAGES.noLogin);
  const p = '/h/.claude/.credentials.json';
  for (const text of ['not json {', 'null', '[]', '{"other":{}}', `{"claudeAiOauth":"${TOKEN}"}`]) {
    const err = await rejectsWith(
      readClaudeToken({ env: { HOME: '/h' }, platform: 'linux', fsImpl: memFs({ [p]: text }), now: () => NOW }),
      'no_login',
    );
    assert.ok(!err.message.includes(p), 'path not needed in message');
  }
});

test('readClaudeToken on macOS reads the Keychain via injected exec', async () => {
  const calls = [];
  const execImpl = async (cmd, args) => {
    calls.push([cmd, ...args]);
    return creds() + '\n';
  };
  const mfs = memFs({});
  const r = await readClaudeToken({ env: { HOME: '/Users/ann' }, platform: 'darwin', fsImpl: mfs, execImpl, now: () => NOW });
  assert.equal(r.token, TOKEN);
  assert.deepEqual(calls, [['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-w']]);
  assert.equal(KEYCHAIN_SERVICE, 'Claude Code-credentials');
  assert.deepEqual(mfs.reads, [], 'no file read when the keychain works');
});

test('readClaudeToken on macOS: keychain fails, falls back to the file', async () => {
  const p = '/Users/ann/.claude/.credentials.json';
  const execImpl = async () => {
    throw new Error(`security: could not be found ${TOKEN}`);
  };
  const r = await readClaudeToken({ env: { HOME: '/Users/ann' }, platform: 'darwin', fsImpl: memFs({ [p]: creds() }), execImpl, now: () => NOW });
  assert.equal(r.token, TOKEN);
});

test('readClaudeToken on macOS: keychain and file both fail -> says the Keychain failed', async () => {
  const execImpl = async () => {
    const e = new Error(`Command failed: security ... output ${TOKEN}`);
    e.stdout = TOKEN;
    throw e;
  };
  await rejectsWith(
    readClaudeToken({ env: { HOME: '/Users/ann' }, platform: 'darwin', fsImpl: memFs({}), execImpl, now: () => NOW }),
    'no_login',
    MESSAGES.keychain,
  );
  // keychain returns garbage that contains the token: still nothing leaks
  await rejectsWith(
    readClaudeToken({ env: { HOME: '/Users/ann' }, platform: 'darwin', fsImpl: memFs({}), execImpl: async () => `oops ${TOKEN}`, now: () => NOW }),
    'no_login',
    MESSAGES.keychain,
  );
  // expired keychain login
  await rejectsWith(
    readClaudeToken({
      env: { HOME: '/Users/ann' },
      platform: 'darwin',
      fsImpl: memFs({}),
      execImpl: async () => creds({ expiresAt: PAST }),
      now: () => NOW,
    }),
    'expired',
  );
});

test('readClaudeToken: non-mac platforms never call exec', async () => {
  for (const platform of ['win32', 'linux']) {
    await rejectsWith(readClaudeToken({ env: { HOME: '/h', USERPROFILE: 'C:\\U' }, platform, fsImpl: memFs({}), execImpl: noExec }), 'no_login');
  }
});

// ---------- fetchAnthropicUsage (mocked fetch) ----------
test('fetchAnthropicUsage sends exactly the documented request', async () => {
  const f = mockFetch(() => jsonRes(200, GOOD));
  const json = await fetchAnthropicUsage({ token: TOKEN, fetchImpl: f });
  assert.deepEqual(json, GOOD);
  assert.equal(f.calls.length, 1);
  const { url, init } = f.calls[0];
  assert.equal(url, 'https://api.anthropic.com/api/oauth/usage');
  assert.equal(init.method, 'GET');
  assert.deepEqual(init.headers, { Authorization: `Bearer ${TOKEN}`, 'anthropic-beta': OAUTH_BETA, Accept: 'application/json' });
  assert.equal(OAUTH_BETA, 'oauth-2025-04-20');
  assert.equal(init.redirect, 'error', 'redirects refused so the header cannot follow one');
  assert.ok(init.signal instanceof AbortSignal);
});

test('fetchAnthropicUsage maps HTTP failures to short coded messages without the body', async () => {
  const echo = { error: { message: `bad token Bearer ${TOKEN}` }, headers: { authorization: `Bearer ${TOKEN}` } };
  const cases = [
    [401, 'expired', MESSAGES.expired],
    [403, 'expired', MESSAGES.expired],
    [429, 'rate_limited', 'Anthropic is rate-limiting usage checks; try again in a minute.'],
    [500, 'bad_response', 'Anthropic returned an unexpected response (HTTP 500).'],
    [404, 'bad_response', 'Anthropic returned an unexpected response (HTTP 404).'],
  ];
  for (const [status, code, message] of cases) {
    const err = await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: mockFetch(() => jsonRes(status, echo)) }), code, message);
    assert.ok(!err.message.includes('bad token'));
  }
});

test('fetchAnthropicUsage: network error -> unreachable, and the error is not passed on', async () => {
  const f = async (url, init) => {
    throw new TypeError(`fetch failed for ${init.headers.Authorization}`);
  };
  await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: f }), 'unreachable', "Couldn't reach Anthropic.");
});

test('fetchAnthropicUsage: timeout aborts -> unreachable', async () => {
  let aborted = false;
  const f = (url, init) =>
    new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => {
        aborted = true;
        reject(new DOMException('This operation was aborted', 'AbortError'));
      });
    });
  const t0 = Date.now();
  await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: f, timeoutMs: 50 }), 'unreachable');
  assert.ok(aborted);
  assert.ok(Date.now() - t0 < 2000);
});

test('fetchAnthropicUsage: garbage body -> bad_response', async () => {
  const bodies = ['<html>nope</html>', 'null', '[1,2]', '"str"'];
  for (const body of bodies) {
    await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: mockFetch(() => jsonRes(200, body)) }), 'bad_response', MESSAGES.noData);
  }
  // a response object that explodes
  await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: async () => null }), 'bad_response');
});

test('fetchAnthropicUsage: no token -> expired, fetch never called', async () => {
  const f = mockFetch(() => jsonRes(200, GOOD));
  await rejectsWith(fetchAnthropicUsage({ token: '', fetchImpl: f }), 'expired');
  assert.equal(f.calls.length, 0);
});

test('resolveBase only allows api.anthropic.com or loopback', () => {
  assert.equal(resolveBase(undefined), 'https://api.anthropic.com');
  assert.equal(resolveBase(''), 'https://api.anthropic.com');
  assert.equal(resolveBase('https://api.anthropic.com/'), 'https://api.anthropic.com');
  assert.equal(resolveBase('http://127.0.0.1:4567'), 'http://127.0.0.1:4567');
  assert.equal(resolveBase('http://localhost:1'), 'http://localhost:1');
  assert.equal(resolveBase('http://[::1]:9'), 'http://[::1]:9');
  for (const bad of ['http://api.anthropic.com', 'https://evil.example', 'https://api.anthropic.com.evil.io', 'ftp://127.0.0.1', 'nonsense', 'http://u:p@127.0.0.1']) {
    assert.throws(() => resolveBase(bad), (e) => e instanceof UsageDirectError && e.code === 'disabled', bad);
  }
});

test('fetchAnthropicUsage refuses a non-allowed base before sending the token', async () => {
  const f = mockFetch(() => jsonRes(200, GOOD));
  await rejectsWith(fetchAnthropicUsage({ token: TOKEN, fetchImpl: f, base: 'https://evil.example' }), 'disabled', MESSAGES.badBase);
  assert.equal(f.calls.length, 0);
});

// ---------- toReport ----------
test('toReport maps both windows into reportUsage args', () => {
  const op = toReport(GOOD, { source: 'ANN-PC · Anthropic', now: () => NOW });
  assert.deepEqual(op, {
    type: 'reportUsage',
    args: {
      fiveHour: { usedPct: 42.3, resetsAt: '2026-11-07T15:10:00.000Z' },
      sevenDay: { usedPct: 18, resetsAt: '2026-11-12T00:00:00.000Z' },
      reportedAt: '2026-11-07T12:00:00.000Z',
      source: 'ANN-PC · Anthropic',
    },
  });
});

test('toReport: null/missing/malformed windows become null; clamps and epoch reset times', () => {
  let op = toReport({ five_hour: null, seven_day: { utilization: 130, resets_at: 1794528000 } }, { source: 's', now: NOW });
  assert.equal(op.args.fiveHour, null);
  assert.deepEqual(op.args.sevenDay, { usedPct: 100, resetsAt: new Date(1794528000 * 1000).toISOString() });
  op = toReport({ five_hour: { utilization: '-5', resets_at: null }, seven_day: { utilization: 'x' } }, { source: 's', now: NOW });
  assert.deepEqual(op.args.fiveHour, { usedPct: 0, resetsAt: null });
  assert.equal(op.args.sevenDay, null);
});

test('toReport: neither window usable -> bad_response with the endpoint-changed message', async () => {
  for (const json of [{}, { five_hour: null, seven_day: null }, { five_hour: { pct: 3 } }, null, [], 'x']) {
    assert.throws(
      () => toReport(json, { now: NOW }),
      (e) => e instanceof UsageDirectError && e.code === 'bad_response' && e.message === "Anthropic didn't return usage data (the unofficial endpoint may have changed)",
    );
  }
});

test('defaultSource is "<hostname> · Anthropic" and fits the 40-char source limit', () => {
  assert.equal(defaultSource(() => 'ANN-PC'), 'ANN-PC · Anthropic');
  const long = defaultSource(() => 'a-very-long-hostname-that-goes-on-and-on.example.lan');
  assert.ok(long.length <= core.USAGE_SOURCE_MAX);
  assert.ok(long.endsWith(' · Anthropic'));
  assert.equal(defaultSource(() => ''), 'PC · Anthropic');
  assert.equal(defaultSource(() => { throw new Error('x'); }), 'PC · Anthropic');
});

test('toReport output is accepted by the real reportUsage op', () => {
  core.setClock(() => NOW);
  try {
    const state = core.createInitialState();
    const op = toReport(GOOD, { source: defaultSource(() => 'ANN-PC'), now: () => NOW });
    applyOp(state, op);
    assert.equal(state.usage.fiveHour.usedPct, 42.3);
    assert.equal(state.usage.sevenDay.usedPct, 18);
    assert.equal(state.usage.source, 'ANN-PC · Anthropic');
    assert.equal(state.usage.reportedAt, '2026-11-07T12:00:00.000Z');
    assertNoToken(state, 'board state');
  } finally {
    core.setClock(null);
  }
});

// ---------- refreshUsageFromAnthropic ----------
const deps = (over = {}) => ({
  env: { CLAUDE_CONFIG_DIR: '/cfg' },
  platform: 'linux',
  fsImpl: memFs({ '/cfg/.credentials.json': creds() }),
  execImpl: noExec,
  fetchImpl: mockFetch(() => jsonRes(200, GOOD)),
  now: () => NOW,
  hostname: () => 'ANN-PC',
  ...over,
});

test('refreshUsageFromAnthropic composes read -> fetch -> op, token only in the header', async () => {
  const d = deps();
  const op = await refreshUsageFromAnthropic(d);
  assert.equal(op.type, 'reportUsage');
  assert.equal(op.args.source, 'ANN-PC · Anthropic');
  assert.equal(op.args.fiveHour.usedPct, 42.3);
  assertNoToken(op, 'returned op');
  assert.equal(d.fetchImpl.calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(!d.fetchImpl.calls[0].url.includes(TOKEN), 'token not in the URL');
});

test('refreshUsageFromAnthropic: DEPT_USAGE_DIRECT=off -> disabled, nothing read or fetched', async () => {
  for (const v of ['off', 'OFF', '0', 'false']) {
    const d = deps({ env: { CLAUDE_CONFIG_DIR: '/cfg', DEPT_USAGE_DIRECT: v } });
    await rejectsWith(refreshUsageFromAnthropic(d), 'disabled', MESSAGES.disabled);
    assert.deepEqual(d.fsImpl.reads, []);
    assert.equal(d.fetchImpl.calls.length, 0);
  }
  assert.equal(isUsageDirectEnabled({}), true);
  assert.equal(isUsageDirectEnabled({ DEPT_USAGE_DIRECT: 'on' }), true);
  assert.equal(isUsageDirectEnabled({ DEPT_USAGE_DIRECT: 'off' }), false);
});

test('refreshUsageFromAnthropic: bad DEPT_ANTHROPIC_BASE refused before the login is read', async () => {
  const d = deps({ env: { CLAUDE_CONFIG_DIR: '/cfg', DEPT_ANTHROPIC_BASE: 'https://evil.example' } });
  await rejectsWith(refreshUsageFromAnthropic(d), 'disabled', MESSAGES.badBase);
  assert.deepEqual(d.fsImpl.reads, []);
  assert.equal(d.fetchImpl.calls.length, 0);
});

test('refreshUsageFromAnthropic: expired login never reaches fetch', async () => {
  const d = deps({ fsImpl: memFs({ '/cfg/.credentials.json': creds({ expiresAt: PAST }) }) });
  await rejectsWith(refreshUsageFromAnthropic(d), 'expired', MESSAGES.expired);
  assert.equal(d.fetchImpl.calls.length, 0);
});

test('refreshUsageFromAnthropic: every failure path keeps the token out of errors', async () => {
  const echo = `Bearer ${TOKEN}`;
  const failing = [
    ['expired', mockFetch(() => jsonRes(401, { echo }))],
    ['rate_limited', mockFetch(() => jsonRes(429, { echo }))],
    ['bad_response', mockFetch(() => jsonRes(500, { echo }))],
    ['bad_response', mockFetch(() => jsonRes(200, echo))],
    ['bad_response', mockFetch(() => jsonRes(200, { five_hour: echo }))],
    ['unreachable', async () => { throw new Error(echo); }],
    // a fetch impl that hands back something odd
    ['bad_response', async () => ({ status: 200, json: () => { throw new Error(echo); } })],
  ];
  for (const [code, fetchImpl] of failing) await rejectsWith(refreshUsageFromAnthropic(deps({ fetchImpl })), code);
  // an injected dependency throwing something unexpected is mapped too
  const weird = { readFileSync() { throw Object.assign(new Error(echo), { token: TOKEN }); } };
  await rejectsWith(refreshUsageFromAnthropic(deps({ fsImpl: weird })), 'no_login');
});

// ---------- end to end against a local fake Anthropic ----------
test('refreshUsageFromAnthropic against a local HTTP server via DEPT_ANTHROPIC_BASE', async (t) => {
  const seen = [];
  let mode = 'ok';
  const server = http.createServer((req, res) => {
    seen.push({ url: req.url, method: req.method, headers: req.headers });
    if (mode === 'slow') return; // never answers
    if (mode === '401') {
      res.writeHead(401, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ error: req.headers.authorization }));
    }
    if (mode === 'redirect') {
      res.writeHead(302, { location: 'http://127.0.0.2:1/steal' });
      return res.end();
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(GOOD));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  t.after(() => {
    server.closeAllConnections?.();
    server.close();
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  fs.writeFileSync(path.join(tmp, '.credentials.json'), creds({ expiresAt: Date.now() + 3600_000 }));
  const env = { CLAUDE_CONFIG_DIR: tmp, DEPT_ANTHROPIC_BASE: base };
  const real = { env, platform: 'linux', execImpl: noExec, hostname: () => 'TEST-PC' };

  const op = await refreshUsageFromAnthropic(real);
  assert.equal(op.args.fiveHour.usedPct, 42.3);
  assert.equal(op.args.source, 'TEST-PC · Anthropic');
  assert.equal(seen[0].url, '/api/oauth/usage');
  assert.equal(seen[0].method, 'GET');
  assert.equal(seen[0].headers.authorization, `Bearer ${TOKEN}`);
  assert.equal(seen[0].headers['anthropic-beta'], 'oauth-2025-04-20');
  assert.equal(seen[0].headers.accept, 'application/json');
  assertNoToken(op);

  mode = '401';
  await rejectsWith(refreshUsageFromAnthropic(real), 'expired');
  mode = 'redirect';
  await rejectsWith(refreshUsageFromAnthropic(real), 'unreachable');
  mode = 'slow';
  await rejectsWith(refreshUsageFromAnthropic({ ...real, timeoutMs: 100 }), 'unreachable');
});
