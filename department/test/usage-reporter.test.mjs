// Tests for the usage meter reporter: usage/statusline.mjs and usage/report.mjs.
// Everything runs against scratch data dirs (DEPT_DATA), with the shared board
// off (DEPT_HUB=off, DEPT_HUB_URL empty) and on ports nothing else uses.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { formatLine, readUsage, toIso, shouldSend, usageKey, RESEND_MS, MIN_GAP_MS } from '../usage/statusline.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const STATUSLINE = path.join(here, '..', 'usage', 'statusline.mjs');
const REPORT = path.join(here, '..', 'usage', 'report.mjs');
const SERVER = path.join(here, '..', 'server.mjs');

// Each test's scratch dir is removed when the test file finishes.
const scratchDirs = [];
const scratch = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-usage-'));
  scratchDirs.push(dir);
  return dir;
};
test.after(() => {
  for (const dir of scratchDirs) fs.rmSync(dir, { recursive: true, force: true });
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

// Env for child processes: scratch data, no shared board, no proxy, a dead port.
function env(dir, port, extra = {}) {
  const e = { ...process.env };
  for (const k of ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NODE_USE_ENV_PROXY', 'DEPT_URL', 'DEPT_PORT', 'DEPT_USAGE_PAYLOAD', 'DEPT_USAGE_SOURCE', 'DEPT_USAGE_SEND']) delete e[k];
  return { ...e, DEPT_DATA: path.join(dir, 'state.json'), DEPT_HUB: 'off', DEPT_HUB_URL: '', PORT: String(port), DEPT_PORT: String(port), ...extra };
}

async function waitFor(fn, ms = 8000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = fn();
    if (v) return v;
    await sleep(50);
  }
  return fn();
}

const readState = (dir) => {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'));
  } catch {
    return null;
  }
};

const inOneHour = () => Math.floor(Date.now() / 1000) + 3600;
// Fixed resets (one epoch-seconds, one ISO) so repeated samples compare equal.
const FIVE_RESET = inOneHour();
const WEEK_RESET = new Date(Date.now() + 3 * 86400e3).toISOString();
const sample = (over = {}) => ({
  model: { id: 'claude-opus', display_name: 'Opus' },
  rate_limits: {
    five_hour: { used_percentage: 42.37, resets_at: FIVE_RESET },
    seven_day: { used_percentage: 18, resets_at: WEEK_RESET },
  },
  ...over,
});

function runStatusline(input, e) {
  const t0 = Date.now();
  const r = spawnSync(process.execPath, [STATUSLINE], {
    input: typeof input === 'string' ? input : JSON.stringify(input),
    env: e,
    encoding: 'utf8',
    timeout: 5000,
  });
  return { ...r, ms: Date.now() - t0 };
}

// ---------- pure helpers ----------

test('toIso accepts epoch seconds, epoch ms, numeric strings and ISO', () => {
  assert.equal(toIso(1762520400), '2025-11-07T13:00:00.000Z');
  assert.equal(toIso(1762520400000), '2025-11-07T13:00:00.000Z');
  // 1e11 cutoff (as core's USAGE_EPOCH_MS_CUTOFF): just below is seconds, from it on ms
  assert.equal(toIso(99_999_999_999), new Date(99_999_999_999 * 1000).toISOString());
  assert.equal(toIso(100_000_000_000), new Date(100_000_000_000).toISOString());
  assert.equal(toIso('1762520400'), '2025-11-07T13:00:00.000Z');
  assert.equal(toIso('2025-11-07T13:00:00Z'), '2025-11-07T13:00:00.000Z');
  assert.equal(toIso('2025-11-07T14:00:00+01:00'), '2025-11-07T13:00:00.000Z');
  for (const bad of [null, undefined, '', 'soon', 0, -5, {}, NaN]) assert.equal(toIso(bad), null);
});

test('readUsage: both windows, one window, none, junk', () => {
  const u = readUsage(sample());
  assert.equal(u.fiveHour.usedPct, 42.4);
  assert.match(u.fiveHour.resetsAt, /^\d{4}-\d\d-\d\dT/);
  assert.equal(u.sevenDay.usedPct, 18);

  const five = readUsage({ rate_limits: { five_hour: { used_percentage: 7 } } });
  assert.deepEqual(five, { fiveHour: { usedPct: 7, resetsAt: null }, sevenDay: null });
  const week = readUsage({ rate_limits: { seven_day: { used_percentage: '55.55', resets_at: 'x' } } });
  assert.deepEqual(week, { fiveHour: null, sevenDay: { usedPct: 55.6, resetsAt: null } });

  assert.equal(readUsage({ model: { display_name: 'Opus' } }), null);
  assert.equal(readUsage({ rate_limits: {} }), null);
  assert.equal(readUsage({ rate_limits: { five_hour: {}, seven_day: { used_percentage: null } } }), null);
  assert.equal(readUsage({ rate_limits: { five_hour: { used_percentage: 'lots' } } }), null);
  assert.equal(readUsage(null), null);
  assert.equal(readUsage('text'), null);
  // clamped to 0-100
  assert.equal(readUsage({ rate_limits: { five_hour: { used_percentage: 130 } } }).fiveHour.usedPct, 100);
  assert.equal(readUsage({ rate_limits: { five_hour: { used_percentage: -3 } } }).fiveHour.usedPct, 0);
});

test('formatLine: short line with both windows, model name, fallbacks', () => {
  const now = Date.parse('2026-11-07T12:00:00Z');
  const reset = new Date(now + 90 * 60e3);
  const line = formatLine({
    model: { display_name: 'Opus' },
    rate_limits: { five_hour: { used_percentage: 42.4, resets_at: Math.floor(reset / 1000) }, seven_day: { used_percentage: 18.2 } },
  }, now);
  assert.match(line, /^5h 42% \(resets \d{1,2}:\d\d[ap]m\) · week 18% · Opus$/);
  assert.ok(line.length < 80);
  // the reset clock is local time
  const h = reset.getHours() % 12 || 12;
  assert.ok(line.includes(`${h}:${String(reset.getMinutes()).padStart(2, '0')}`));

  // a reset in the past is not shown
  assert.equal(formatLine({ rate_limits: { five_hour: { used_percentage: 10, resets_at: '2020-01-01T00:00:00Z' } } }, now), '5h 10%');
  assert.equal(formatLine({ rate_limits: { seven_day: { used_percentage: 3 } }, model: { display_name: 'Sonnet' } }, now), 'week 3% · Sonnet');
  assert.equal(formatLine({ model: { display_name: 'Opus' } }, now), 'Opus');
  assert.equal(formatLine(null, now), 'Claude');
  assert.equal(formatLine({}, now), 'Claude');
});

test('shouldSend: first time, changed values, 60 s resend, unchanged within 60 s', () => {
  const u = readUsage(sample());
  const now = 1_000_000_000_000;
  assert.equal(shouldSend(null, null, now), false);
  assert.equal(shouldSend(u, null, now), true);
  assert.equal(shouldSend(u, { key: usageKey(u), sentAt: now - 5000 }, now), false);
  assert.equal(shouldSend(u, { key: usageKey(u), sentAt: now - RESEND_MS }, now), true);
  const changed = { ...u, fiveHour: { ...u.fiveHour, usedPct: 43 } };
  // changed values: not within the 15 s floor, at once after it
  assert.equal(MIN_GAP_MS, 15_000);
  assert.equal(shouldSend(changed, { key: usageKey(u), sentAt: now - 1000 }, now), false);
  assert.equal(shouldSend(changed, { key: usageKey(u), sentAt: now - MIN_GAP_MS + 1 }, now), false);
  assert.equal(shouldSend(changed, { key: usageKey(u), sentAt: now - MIN_GAP_MS }, now), true);
  // unchanged values after the floor but before 60 s: no
  assert.equal(shouldSend(u, { key: usageKey(u), sentAt: now - 30_000 }, now), false);
  assert.equal(shouldSend(u, { key: usageKey(u), sentAt: now + 60_000 }, now), true, 'clock went back');
  assert.equal(shouldSend(u, { garbage: true }, now), true);
});

// ---------- statusline process ----------

test('statusline prints one line and exits 0 for every kind of input', async () => {
  const dir = scratch();
  const e = env(dir, await freePort(), { DEPT_USAGE_SEND: 'off' });
  const cases = [
    [sample(), /^5h 42% \(resets .+\) · week 18% · Opus\n$/],
    [{ ...sample(), rate_limits: { five_hour: { used_percentage: 5, resets_at: new Date(Date.now() + 3600e3).toISOString() } } }, /^5h 5% \(resets .+\) · Opus\n$/],
    [{ ...sample(), rate_limits: { seven_day: { used_percentage: 91 } } }, /^week 91% · Opus\n$/],
    [{ model: { display_name: 'Opus' } }, /^Opus\n$/],
    ['{not json', /^Claude\n$/],
    ['', /^Claude\n$/],
    ['[1,2,3]', /^Claude\n$/],
  ];
  for (const [input, re] of cases) {
    const r = runStatusline(input, e);
    assert.equal(r.status, 0, `exit code for ${JSON.stringify(input).slice(0, 60)}`);
    assert.match(r.stdout, re);
    assert.equal(r.stderr, '');
  }
  assert.equal(fs.existsSync(path.join(dir, 'usage-last.json')), false, 'DEPT_USAGE_SEND=off sends nothing');
});

test('statusline does not wait on a stdin that never closes', async () => {
  const dir = scratch();
  const child = spawn(process.execPath, [STATUSLINE], { env: env(dir, await freePort(), { DEPT_USAGE_SEND: 'off' }) });
  const t0 = Date.now();
  let out = '';
  child.stdout.on('data', (c) => { out += c; });
  child.stdin.write(JSON.stringify(sample()));
  const code = await new Promise((r) => child.on('exit', r));
  assert.equal(code, 0);
  assert.match(out, /^5h 42%/);
  assert.ok(Date.now() - t0 < 4000);
  child.stdin.destroy();
});

test('statusline sends in the background, throttles via the cache, and returns fast', async () => {
  const dir = scratch();
  const port = await freePort(); // nothing listens: report.mjs falls back to the scratch state file
  const e = env(dir, port, { DEPT_USAGE_SOURCE: 'test-pc' });
  const cache = path.join(dir, 'usage-last.json');

  // No rate_limits: no send, no cache.
  runStatusline({ model: { display_name: 'Opus' } }, e);
  assert.equal(fs.existsSync(cache), false);

  // First report: cache written, report.mjs stores it in the scratch file.
  const first = runStatusline(sample(), e);
  assert.equal(first.status, 0);
  assert.ok(first.ms < 1500, `statusline took ${first.ms} ms`);
  const c1 = JSON.parse(fs.readFileSync(cache, 'utf8'));
  assert.ok(Number.isFinite(c1.sentAt));
  const st = await waitFor(() => readState(dir)?.usage);
  assert.ok(st, 'state.usage written by the background report');
  assert.equal(st.fiveHour.usedPct, 42.4);
  assert.equal(st.sevenDay.usedPct, 18);
  assert.match(st.fiveHour.resetsAt, /Z$/);
  assert.equal(st.source, 'test-pc');

  // Same values within 60 s: no new send (cache unchanged).
  runStatusline(sample(), e);
  assert.deepEqual(JSON.parse(fs.readFileSync(cache, 'utf8')), c1);

  // Changed values within 15 s of the last send: held back by the floor.
  const more = sample();
  more.rate_limits.five_hour.used_percentage = 50;
  runStatusline(more, e);
  assert.deepEqual(JSON.parse(fs.readFileSync(cache, 'utf8')), c1, '15 s floor');

  // Changed values once 15 s have passed: sends.
  fs.writeFileSync(cache, JSON.stringify({ ...c1, sentAt: Date.now() - MIN_GAP_MS - 500 }));
  runStatusline(more, e);
  const c2 = JSON.parse(fs.readFileSync(cache, 'utf8'));
  assert.notEqual(c2.key, c1.key);
  assert.ok(await waitFor(() => readState(dir)?.usage?.fiveHour?.usedPct === 50), 'changed report stored');

  // Same values but the last send is over 60 s old: sends again.
  fs.writeFileSync(cache, JSON.stringify({ ...c2, sentAt: Date.now() - RESEND_MS - 1000 }));
  runStatusline(more, e);
  const c3 = JSON.parse(fs.readFileSync(cache, 'utf8'));
  assert.ok(c3.sentAt > Date.now() - 10_000);
  assert.equal(c3.key, c2.key);
  assert.ok(await waitFor(() => readState(dir)?.usage?.reportedAt === c3.sentAtIso), 'resend stored');

  // A broken cache file never breaks the status line.
  fs.writeFileSync(cache, '{oops');
  const r = runStatusline(more, e);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /^5h 50%/);
  // ...and it re-sent (a broken cache counts as "never sent"). Wait for that
  // background report so it doesn't outlive the scratch dir.
  const last = JSON.parse(fs.readFileSync(cache, 'utf8'));
  assert.ok(await waitFor(() => readState(dir)?.usage?.reportedAt === last.sentAtIso), 'last report stored');
});

// ---------- report.mjs ----------

function runReport(dir, port, payload, extra = {}) {
  return spawnSync(process.execPath, [REPORT], {
    env: env(dir, port, { DEPT_USAGE_PAYLOAD: JSON.stringify(payload), ...extra }),
    encoding: 'utf8',
    timeout: 15000,
  });
}

test('report.mjs writes to the local state file when no server runs', async () => {
  const dir = scratch();
  const port = await freePort();
  const at = new Date().toISOString();
  const r = runReport(dir, port, { fiveHour: { usedPct: 12.34, resetsAt: inOneHour() }, sevenDay: null, reportedAt: at, source: 'scratch' });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout, '');
  const u = readState(dir).usage;
  assert.equal(u.fiveHour.usedPct, 12.3);
  assert.match(u.fiveHour.resetsAt, /Z$/);
  assert.equal(u.sevenDay, null);
  assert.equal(u.reportedAt, at);
  assert.equal(u.source, 'scratch');
  assert.ok(u.receivedAt);
  assert.equal(fs.existsSync(path.join(dir, 'usage-report.log')), false);
});

test('report.mjs posts to a running local dashboard server', async (t) => {
  const dir = scratch();
  const port = await freePort();
  const srv = spawn(process.execPath, [SERVER, '--port', String(port)], { env: env(dir, port), stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => srv.kill());
  await new Promise((resolve, reject) => {
    srv.stdout.on('data', (c) => { if (String(c).includes('dashboard')) resolve(); });
    srv.on('exit', () => reject(new Error('server exited')));
    setTimeout(() => reject(new Error('server did not start')), 8000);
  });
  const at = new Date().toISOString();
  const r = runReport(dir, port, { fiveHour: { usedPct: 77, resetsAt: '2030-01-01T00:00:00Z' }, sevenDay: { usedPct: 33.33, resetsAt: null }, reportedAt: at });
  assert.equal(r.status, 0, r.stderr);
  const res = await fetch(`http://127.0.0.1:${port}/api/state`);
  const { result } = await res.json();
  assert.equal(result.usage.fiveHour.usedPct, 77);
  assert.equal(result.usage.sevenDay.usedPct, 33.3);
  assert.equal(result.usage.reportedAt, at);
  assert.ok(result.usage.source.length > 0 && result.usage.source.length <= 40);
  // The server persisted it to its (scratch) state file too.
  assert.equal(readState(dir).usage.fiveHour.usedPct, 77);
});

test('report.mjs: an older report does not replace a newer one', async () => {
  const dir = scratch();
  const port = await freePort();
  const now = Date.now();
  runReport(dir, port, { fiveHour: { usedPct: 60, resetsAt: null }, reportedAt: new Date(now).toISOString(), source: 'a' });
  const r = runReport(dir, port, { fiveHour: { usedPct: 10, resetsAt: null }, reportedAt: new Date(now - 60e3).toISOString(), source: 'a' });
  assert.equal(r.status, 0);
  assert.equal(readState(dir).usage.fiveHour.usedPct, 60);
});

test('report.mjs is silent on errors and keeps a capped log', async () => {
  const dir = scratch();
  const port = await freePort();
  const log = path.join(dir, 'usage-report.log');
  const bad = spawnSync(process.execPath, [REPORT], { env: env(dir, port, { DEPT_USAGE_PAYLOAD: '{nope' }), encoding: 'utf8', timeout: 15000 });
  assert.equal(bad.stdout, '');
  assert.equal(bad.stderr, '');
  assert.notEqual(bad.status, 0);
  const lines = fs.readFileSync(log, 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^\d{4}-\d\d-\d\dT\S+ report failed: /);

  // Pre-fill with 250 lines; one more failure trims it to 200.
  fs.writeFileSync(log, Array.from({ length: 250 }, (_, i) => `old ${i}`).join('\n') + '\n');
  spawnSync(process.execPath, [REPORT], { env: env(dir, port, { DEPT_USAGE_PAYLOAD: '' }), encoding: 'utf8', timeout: 15000 });
  const after = fs.readFileSync(log, 'utf8').trim().split('\n');
  assert.equal(after.length, 200);
  assert.match(after.at(-1), /no payload/);
  assert.equal(readState(dir), null, 'nothing written to the board on failure');
});

test('report.mjs uses the shared board when no local server runs and a hub is configured', async (t) => {
  const dir = scratch();
  const deadPort = await freePort();
  // A stand-in for the Netlify hub function: applies ops to an in-memory board.
  const { createInitialState } = await import('../lib/core.mjs');
  const { applyOp } = await import('../lib/ops.mjs');
  const board = createInitialState();
  const seen = [];
  const http = await import('node:http');
  const hub = http.createServer(async (req, res) => {
    let raw = '';
    for await (const c of req) raw += c;
    seen.push({ method: req.method, url: req.url, key: req.headers['x-edit-key'], body: raw && JSON.parse(raw) });
    const result = applyOp(board, JSON.parse(raw));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true, result, state: board, rev: 1 }));
  });
  await new Promise((r) => hub.listen(0, '127.0.0.1', r));
  t.after(() => hub.close());
  const e = env(dir, deadPort, {
    DEPT_HUB: '',
    DEPT_HUB_URL: `http://127.0.0.1:${hub.address().port}`,
    DEPT_HUB_KEY: 'k',
    DEPT_USAGE_PAYLOAD: JSON.stringify({ fiveHour: { usedPct: 88, resetsAt: inOneHour() }, sevenDay: { usedPct: 40, resetsAt: null }, reportedAt: new Date().toISOString() }),
  });
  const child = spawn(process.execPath, [REPORT], { env: e, stdio: 'ignore' });
  const code = await new Promise((r) => child.on('exit', r));
  assert.equal(code, 0);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].method, 'POST');
  assert.equal(seen[0].url, '/api/department');
  assert.equal(seen[0].key, 'k');
  assert.equal(seen[0].body.type, 'reportUsage');
  assert.equal(board.usage.fiveHour.usedPct, 88);
  assert.equal(readState(dir), null, 'the local file is left alone in hub mode');
});

test('report.mjs never overwrites a state file it could not read or parse', async () => {
  const dir = scratch();
  const port = await freePort();
  const file = path.join(dir, 'state.json');
  const payload = { fiveHour: { usedPct: 5, resetsAt: null }, reportedAt: new Date().toISOString() };

  // Corrupt JSON (e.g. caught half-written): left as is, one log line.
  fs.writeFileSync(file, '{"tasks": [ half');
  const r = runReport(dir, port, payload);
  assert.notEqual(r.status, 0);
  assert.equal(fs.readFileSync(file, 'utf8'), '{"tasks": [ half');
  assert.match(fs.readFileSync(path.join(dir, 'usage-report.log'), 'utf8'), /could not parse .*not writing/);

  // Unreadable (a directory stands in for EPERM/EBUSY on Windows): not replaced.
  const dir2 = scratch();
  fs.mkdirSync(path.join(dir2, 'state.json'));
  const r2 = runReport(dir2, port, payload);
  assert.notEqual(r2.status, 0);
  assert.ok(fs.statSync(path.join(dir2, 'state.json')).isDirectory());
  assert.match(fs.readFileSync(path.join(dir2, 'usage-report.log'), 'utf8'), /could not read .*not writing/);

  // A good board keeps its tasks and gains the usage.
  const { createInitialState, addTask } = await import('../lib/core.mjs');
  const st = createInitialState();
  addTask(st, { title: 'Keep me' });
  fs.writeFileSync(file, JSON.stringify(st));
  assert.equal(runReport(dir, port, payload).status, 0);
  const after = readState(dir);
  assert.equal(after.tasks[0].title, 'Keep me');
  assert.equal(after.usage.fiveHour.usedPct, 5);
});

test('report.mjs ignores a stray PORT and falls through past a foreign server', async (t) => {
  const http = await import('node:http');
  // Some other app on the port: answers HTML, or JSON that isn't ours.
  const answers = ['<html>hello</html>', JSON.stringify({ status: 'fine' })];
  let hits = 0;
  const other = http.createServer((req, res) => {
    const body = answers[hits++ % answers.length];
    res.writeHead(hits === 1 ? 404 : 200, { 'content-type': hits === 1 ? 'text/html' : 'application/json' });
    res.end(body);
  });
  await new Promise((r) => other.listen(0, '127.0.0.1', r));
  t.after(() => other.close());
  const otherPort = other.address().port;
  const deadPort = await freePort();
  const run = (dir, extra) => new Promise((resolve) => {
    const child = spawn(process.execPath, [REPORT], {
      env: env(dir, deadPort, { DEPT_USAGE_PAYLOAD: JSON.stringify({ fiveHour: { usedPct: 21, resetsAt: null }, reportedAt: new Date().toISOString() }), ...extra }),
      stdio: 'ignore',
    });
    child.on('exit', resolve);
  });

  // PORT points at the other app: never used, only DEPT_URL / DEPT_PORT are.
  // (With neither set the default is 4300; not exercised here so a real
  // dashboard on this machine is never touched.)
  const d1 = scratch();
  const e1 = await run(d1, { PORT: String(otherPort), DEPT_PORT: String(deadPort) });
  assert.equal(e1, 0);
  assert.equal(hits, 0, 'PORT alone is not used');
  assert.equal(readState(d1).usage.fiveHour.usedPct, 21);

  // DEPT_PORT / DEPT_URL at the other app (HTML, then foreign JSON): falls through to the file.
  const d2 = scratch();
  assert.equal(await run(d2, { DEPT_PORT: String(otherPort) }), 0);
  const d3 = scratch();
  assert.equal(await run(d3, { DEPT_URL: `http://127.0.0.1:${otherPort}` }), 0);
  assert.equal(hits, 2);
  assert.equal(readState(d2).usage.fiveHour.usedPct, 21);
  assert.equal(readState(d3).usage.fiveHour.usedPct, 21);
});

test('report.mjs stops (no fallback) on a genuine {ok:false} from our server', async (t) => {
  const http = await import('node:http');
  const ours = http.createServer((req, res) => {
    res.writeHead(400, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'Unknown operation reportUsage' }));
  });
  await new Promise((r) => ours.listen(0, '127.0.0.1', r));
  t.after(() => ours.close());
  const dir = scratch();
  const code = await new Promise((resolve) => {
    spawn(process.execPath, [REPORT], {
      env: env(dir, ours.address().port, { DEPT_USAGE_PAYLOAD: JSON.stringify({ fiveHour: { usedPct: 1, resetsAt: null } }) }),
      stdio: 'ignore',
    }).on('exit', resolve);
  });
  assert.notEqual(code, 0);
  assert.equal(readState(dir), null, 'no fallback write to the file');
  assert.match(fs.readFileSync(path.join(dir, 'usage-report.log'), 'utf8'), /Unknown operation reportUsage/);
});
