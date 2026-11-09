// claudeLaunchSpec, launchClaude and the POST /api/claude/answer-help route.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { EventEmitter, once } from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { HELP_PROMPT, claudeLaunchSpec, launchClaude } from '../lib/claude-launch.mjs';
import { dispatchBatch, getBatch } from '../lib/foundry-batches.mjs';
import { load, save } from '../lib/store.mjs';

const SERVER = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'server.mjs');
const ROUTE = '/api/claude/answer-help';
const NO_HELP = 'No Foundry subtask needs help';
const UNSAFE_BIN = 'unsafe claude path';
const DISABLED = 'disabled (DEPT_CLAUDE_LAUNCH=off)';
const WIN_ROOT = 'C:\\Work\\planner';
const NIX_ROOT = '/home/me/planner';
const QUOTED_PROMPT = `"${HELP_PROMPT}"`;
const anyTerminal = () => true;
const noTerminal = () => false;
const specFor = (platform, repoRoot, env = {}, which = anyTerminal) => claudeLaunchSpec({ platform, repoRoot, env, which });

test('the prompt is exactly the fixed help request', () => {
  assert.equal(HELP_PROMPT, 'answer the dashboard help request');
});

test('win32: a new window via cmd start, verbatim arguments, no terminal lookup', () => {
  const { spec, reason } = claudeLaunchSpec({
    platform: 'win32',
    repoRoot: WIN_ROOT,
    env: { SystemRoot: 'D:\\Win' },
    which: () => assert.fail('which is not used on Windows'),
  });
  assert.equal(reason, 'starting');
  assert.equal(spec.cmd, 'D:\\Win\\System32\\cmd.exe');
  // The outer quote pair on the last argument is the one cmd /k strips.
  assert.deepEqual(spec.args, ['/d', '/s', '/c', 'start', '"Claude - help request"', '/d', `"${WIN_ROOT}"`, 'cmd', '/k', `""claude" ${QUOTED_PROMPT}"`]);
  assert.deepEqual(spec.opts, { cwd: WIN_ROOT, windowsHide: true, windowsVerbatimArguments: true, stdio: 'ignore' });
});

test('win32: cmd.exe defaults to C:\\Windows', () => {
  assert.equal(specFor('win32', WIN_ROOT).spec.cmd, 'C:\\Windows\\System32\\cmd.exe');
});

test('win32: a repo root cmd.exe cannot quote is refused', () => {
  for (const ch of ['"', '%', '\r', '\n']) {
    const repoRoot = `C:\\Bad${ch}Root`;
    assert.deepEqual(specFor('win32', repoRoot), { spec: null, reason: `path not safe to quote for cmd.exe: ${repoRoot}` });
  }
});

test('darwin: Terminal runs the quoted command through osascript', () => {
  const { spec, reason } = specFor('darwin', '/Users/me/my repo');
  assert.equal(reason, 'starting');
  assert.equal(spec.cmd, 'osascript');
  assert.deepEqual(spec.args, ['-e', `tell application "Terminal" to do script "cd '/Users/me/my repo' && claude \\"${HELP_PROMPT}\\""`]);
  assert.deepEqual(spec.opts, { cwd: '/Users/me/my repo', detached: true, stdio: 'ignore' });
});

test('darwin: quotes and backslashes are escaped for AppleScript', () => {
  const { spec } = specFor('darwin', '/tmp/a"b\\c');
  assert.ok(spec.args[1].includes(`"cd '/tmp/a\\"b\\\\c' && claude`), spec.args[1]);
});

test('darwin and linux: a path that would end the shell quoting is refused', () => {
  for (const platform of ['darwin', 'linux']) {
    for (const ch of ["'", '\r', '\n']) {
      const repoRoot = `/tmp/bad${ch}root`;
      assert.deepEqual(specFor(platform, repoRoot), { spec: null, reason: `path not safe to quote for the shell: ${repoRoot}` });
    }
    assert.deepEqual(specFor(platform, NIX_ROOT, { DEPT_CLAUDE_BIN: "/opt/o'brien/claude" }), { spec: null, reason: UNSAFE_BIN });
  }
});

test('linux: the first terminal found wins, each with its own run flag', () => {
  const order = [['x-terminal-emulator', '-e'], ['gnome-terminal', '--'], ['konsole', '-e'], ['xterm', '-e']];
  for (const [i, [name, flag]] of order.entries()) {
    const installed = new Set(order.slice(i).map(([n]) => n));
    const { spec, reason } = specFor('linux', NIX_ROOT, {}, (n) => installed.has(n));
    assert.equal(reason, 'starting');
    assert.equal(spec.cmd, name);
    assert.deepEqual(spec.args, [flag, 'bash', '-lc', `cd '${NIX_ROOT}' && claude ${QUOTED_PROMPT}; exec bash`]);
    assert.deepEqual(spec.opts, { cwd: NIX_ROOT, detached: true, stdio: 'ignore' });
  }
});

test('linux: no terminal emulator is refused', () => {
  assert.deepEqual(specFor('linux', NIX_ROOT, {}, noTerminal), { spec: null, reason: 'no terminal emulator found' });
});

test('the binary comes from DEPT_CLAUDE_BIN, then FOUNDRY_CLAUDE_BIN, then claude', () => {
  const command = (env) => specFor('linux', NIX_ROOT, env).spec.args.at(-1);
  assert.ok(command({}).includes('&& claude "'));
  assert.ok(command({ FOUNDRY_CLAUDE_BIN: '/opt/foundry-claude' }).includes('&& /opt/foundry-claude "'));
  assert.ok(command({ DEPT_CLAUDE_BIN: '/opt/dept-claude', FOUNDRY_CLAUDE_BIN: '/opt/foundry-claude' }).includes('&& /opt/dept-claude "'));
  assert.ok(command({ DEPT_CLAUDE_BIN: '/opt/my tools/claude' }).includes(`&& '/opt/my tools/claude' "`));
});

test('a claude path with a shell metacharacter is refused on every platform', () => {
  for (const [platform, root] of [['win32', WIN_ROOT], ['darwin', NIX_ROOT], ['linux', NIX_ROOT]]) {
    for (const ch of ['"', '%', '^', '&', '|', '<', '>', '\r', '\n']) {
      const bad = `cla${ch}ude`;
      assert.deepEqual(specFor(platform, root, { DEPT_CLAUDE_BIN: bad }), { spec: null, reason: UNSAFE_BIN });
      assert.deepEqual(specFor(platform, root, { FOUNDRY_CLAUDE_BIN: bad }), { spec: null, reason: UNSAFE_BIN });
    }
  }
});

test('DEPT_CLAUDE_LAUNCH=off refuses on every platform, before any other check', () => {
  for (const [platform, root] of [['win32', WIN_ROOT], ['darwin', NIX_ROOT], ['linux', NIX_ROOT]]) {
    for (const off of ['off', 'OFF']) {
      assert.deepEqual(specFor(platform, root, { DEPT_CLAUDE_LAUNCH: off, DEPT_CLAUDE_BIN: 'bad&bin' }), { spec: null, reason: DISABLED });
    }
  }
});

function fakeChild() {
  const child = new EventEmitter();
  child.unrefs = 0;
  child.unref = () => child.unrefs++;
  return child;
}

test('launchClaude starts the spec, unrefs it and survives a later spawn error', () => {
  const spec = { cmd: 'terminal', args: ['-e', 'claude'], opts: { detached: true } };
  const child = fakeChild();
  const calls = [];
  const started = launchClaude(spec, (...a) => {
    calls.push(a);
    return child;
  });
  assert.deepEqual(started, { ok: true });
  assert.deepEqual(calls, [[spec.cmd, spec.args, spec.opts]]);
  assert.equal(child.unrefs, 1);
  assert.doesNotThrow(() => child.emit('error', new Error('ENOENT')));
});

test('launchClaude reports a failed start instead of throwing', () => {
  const spec = { cmd: 'x', args: [], opts: {} };
  const throwing = (value) => () => {
    throw value;
  };
  assert.deepEqual(launchClaude(spec, throwing(new Error('spawn EPERM'))), { ok: false, error: 'spawn EPERM' });
  assert.deepEqual(launchClaude(spec, throwing('text')), { ok: false, error: 'Could not start Claude' });
  assert.deepEqual(launchClaude(spec, throwing(null)), { ok: false, error: 'Could not start Claude' });
  assert.equal(launchClaude(spec, () => ({})).ok, false); // a child without .on
  assert.equal(launchClaude(null, () => fakeChild()).ok, false);
});

// ---------- the route, on a real server ----------

function request(port, { method = 'POST', route = ROUTE, headers = {}, body = '{}' } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: route, headers, agent: false }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (c) => (raw += c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: JSON.parse(raw) }));
    });
    req.on('error', reject);
    req.end(method === 'POST' ? body : undefined);
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

// Each [status, resultState] pair becomes one batch with one subtask.
function seedState(file, batches) {
  const state = load(file);
  for (const [status, resultState] of batches) {
    const { id } = dispatchBatch(state, { title: 'Batch', repo: 'owner/name', subtasks: [{ key: 'a', title: 'A' }] });
    const batch = getBatch(state, id);
    batch.status = status;
    batch.results = { a: { foundryTask: null, state: resultState, pr: null, error: null, help: { question: 'Which base?', context: '', options: [] } } };
  }
  save(state, file);
}

// PATH is an empty folder, so these tests mean the same on a headless machine as on a desktop:
// no host has a terminal emulator here, and the inherited launch settings are dropped.
function serverEnv(dir, extra) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/^(path|dept_claude_\w+|foundry_claude_bin|dept_hub_url)$/i.test(key)) delete env[key];
  }
  const emptyBin = path.join(dir, 'bin');
  fs.mkdirSync(emptyBin);
  return { ...env, PATH: emptyBin, HOST: '127.0.0.1', DEPT_HUB: 'off', DEPT_DATA: path.join(dir, 'state.json'), DEPT_CLAUDE_BIN: 'fake-claude', DEPT_CLAUDE_LAUNCH_DRYRUN: '1', ...extra };
}

async function startServer(t, { batches = [], env = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-claude-launch-'));
  const childEnv = serverEnv(dir, env);
  seedState(childEnv.DEPT_DATA, batches);
  const port = await freePort();
  const child = spawn(process.execPath, [SERVER, '--port', String(port)], { env: childEnv, stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (c) => (stderr += c));
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const gone = once(child, 'exit');
      child.kill();
      await gone;
    }
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  });
  const origin = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 200; i++) {
    assert.equal(child.exitCode, null, `server exited: ${stderr}`);
    try {
      await request(port, { method: 'GET', route: '/api/state' });
      return { port, origin, answer: (headers = { origin }, body) => request(port, { headers, body }) };
    } catch {
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  return assert.fail(`server did not start: ${stderr}`);
}

const HELP_WAITING = [['running', 'needs_help']];
const capabilityOf = async (srv, headers = {}) =>
  (await request(srv.port, { method: 'GET', route: '/api/state', headers })).body.result.capabilities.claudeLaunch;

test('route: a caller from elsewhere gets 403 and uses up no throttle', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING });
  const foreign = [
    { origin: 'https://evil.example' },
    { origin: 'null' },
    { origin: `http://localhost:${srv.port}` },
    { origin: `http://127.0.0.1:${srv.port + 1}` },
    { host: 'evil.example' },
  ];
  for (const headers of foreign) {
    const res = await srv.answer(headers);
    assert.equal(res.status, 403, JSON.stringify(headers));
    assert.equal(res.body.ok, false);
    assert.equal(res.body.code, 'forbidden');
  }
  assert.equal((await srv.answer()).status, 200);
});

test('route: 403 is checked before the help-request state', async (t) => {
  const srv = await startServer(t);
  assert.equal((await srv.answer({ origin: 'https://evil.example' })).status, 403);
});

test('route: 409 when no open batch has a needs_help result', async (t) => {
  for (const batches of [[], [['running', 'building'], ['claimed', 'answered']], [['done', 'needs_help'], ['failed', 'needs_help'], ['cancelled', 'needs_help']]]) {
    const srv = await startServer(t, { batches });
    const res = await srv.answer();
    assert.equal(res.status, 409, JSON.stringify(batches));
    assert.deepEqual(res.body, { ok: false, error: NO_HELP });
  }
});

test('route: a needs_help result in any open batch status is enough', async (t) => {
  for (const status of ['queued', 'claimed', 'running']) {
    const srv = await startServer(t, { batches: [[status, 'needs_help']] });
    const res = await srv.answer();
    assert.equal(res.status, 200, status);
    assert.deepEqual(res.body, { ok: true });
  }
});

test('route: a second launch within 30 seconds gets 429 with the seconds to wait', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING });
  const first = await srv.answer();
  assert.equal(first.status, 200);
  assert.deepEqual(first.body, { ok: true });
  const second = await srv.answer();
  assert.equal(second.status, 429);
  assert.equal(second.body.ok, false);
  assert.equal(second.body.code, 'throttled');
  assert.ok(second.body.retryAfter >= 1 && second.body.retryAfter <= 30, String(second.body.retryAfter));
  assert.equal(second.headers['retry-after'], String(second.body.retryAfter));
  assert.ok(second.body.error.includes(`${second.body.retryAfter} s`), second.body.error);
});

test('route: the request body is never read and an Origin is not required', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING });
  const res = await srv.answer({}, 'not json; & calc.exe');
  assert.equal(res.status, 200);
  assert.deepEqual(res.body, { ok: true });
});

test('route: only POST is accepted', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING });
  const res = await request(srv.port, { method: 'GET' });
  assert.equal(res.status, 405);
  assert.equal(res.body.ok, false);
});

test('route: 501 with the reason when launching is disabled, and no throttle is used', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING, env: { DEPT_CLAUDE_LAUNCH: 'off' } });
  for (let i = 0; i < 2; i++) {
    const res = await srv.answer();
    assert.equal(res.status, 501);
    assert.deepEqual(res.body, { ok: false, error: DISABLED });
  }
});

test('route: 501 for an unsafe claude path', async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING, env: { DEPT_CLAUDE_BIN: 'fake&claude' } });
  const res = await srv.answer();
  assert.equal(res.status, 501);
  assert.deepEqual(res.body, { ok: false, error: UNSAFE_BIN });
});

test('route: 409 comes before 501', async (t) => {
  const srv = await startServer(t, { env: { DEPT_CLAUDE_LAUNCH: 'off' } });
  assert.equal((await srv.answer()).status, 409);
});

// Not a dry run: with nothing on PATH the spec is null, so nothing can be spawned. Linux only,
// because the other platforms need no lookup and would really open a window.
test('route: 501 on Linux when no terminal emulator is installed', { skip: process.platform !== 'linux' }, async (t) => {
  const srv = await startServer(t, { batches: HELP_WAITING, env: { DEPT_CLAUDE_LAUNCH_DRYRUN: '0' } });
  const res = await srv.answer();
  assert.equal(res.status, 501);
  assert.deepEqual(res.body, { ok: false, error: 'no terminal emulator found' });
});

test('capabilities: claudeLaunch is offered only to local callers when a launch is possible', async (t) => {
  const srv = await startServer(t);
  assert.equal(await capabilityOf(srv), true);
  assert.equal(await capabilityOf(srv, { host: 'evil.example' }), false);
  const off = await startServer(t, { env: { DEPT_CLAUDE_LAUNCH: 'off' } });
  assert.equal(await capabilityOf(off), false);
});
