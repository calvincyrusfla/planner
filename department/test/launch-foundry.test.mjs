// Tests for launch.mjs starting Foundry alongside the dashboard.
// Safety: only pure helpers and fake HTTP servers on free loopback ports; the spawn
// function is a stub, so no real Foundry, cmd.exe or shell is ever started.
// Importing launch.mjs does not run it (it only runs when executed directly).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { foundryLaunchSpec, probeFoundry, maybeStartFoundry, foundryPort, FOUNDRY_STARTING_MS } from '../launch.mjs';

process.env.DEPT_HUB = 'off';

const WIN_ROOT = 'C:\\Users\\Ann Lee\\code (2)\\planner';
const winFiles = (root) => new Set([`${root}\\foundry\\start_foundry.bat`, `${root}\\.venv\\Scripts\\python.exe`]);

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

async function serve(handler) {
  const srv = http.createServer(handler);
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  return { srv, port: srv.address().port, close: () => new Promise((r) => { srv.closeAllConnections?.(); srv.close(r); }) };
}

function fakeSpawn() {
  const calls = [];
  const run = (cmd, args, opts) => {
    const child = new EventEmitter();
    const call = { cmd, args, opts, unrefed: false };
    calls.push(call);
    child.unref = () => (call.unrefed = true);
    setImmediate(() => child.emit('exit', 0)); // cmd.exe returns as soon as `start` has run
    return child;
  };
  return { calls, run };
}

test('foundryPort honours FOUNDRY_SERVE_PORT', () => {
  assert.equal(foundryPort({}), 4310);
  assert.equal(foundryPort({ FOUNDRY_SERVE_PORT: '4999' }), 4999);
});

test('win32: minimised "Foundry" window via cmd start, quoted bat path with spaces and parentheses', () => {
  const files = winFiles(WIN_ROOT);
  const { spec } = foundryLaunchSpec({ platform: 'win32', repoRoot: WIN_ROOT, env: { SystemRoot: 'D:\\Win' }, exists: (p) => files.has(p) });
  assert.equal(spec.cmd, 'D:\\Win\\System32\\cmd.exe');
  assert.deepEqual(spec.args, ['/d', '/s', '/c', 'start', '"Foundry"', '/min', `"${WIN_ROOT}\\foundry\\start_foundry.bat"`]);
  assert.equal(spec.args.join(' '), `/d /s /c start "Foundry" /min "${WIN_ROOT}\\foundry\\start_foundry.bat"`);
  assert.equal(spec.opts.cwd, WIN_ROOT);
  // detached would add CREATE_NEW_PROCESS_GROUP, which disables Ctrl+C in the Foundry window.
  assert.equal(spec.opts.detached, undefined);
  assert.equal(spec.opts.windowsHide, true);
  assert.equal(spec.opts.stdio, 'ignore');
  assert.equal(spec.opts.windowsVerbatimArguments, true);
  assert.equal(spec.waitForExit, true);
});

test('win32: defaults SystemRoot to C:\\Windows', () => {
  const files = winFiles(WIN_ROOT);
  const { spec } = foundryLaunchSpec({ platform: 'win32', repoRoot: WIN_ROOT, env: {}, exists: (p) => files.has(p) });
  assert.equal(spec.cmd, 'C:\\Windows\\System32\\cmd.exe');
});

test('win32: missing bat or venv is skipped with a reason', () => {
  const onlyBat = new Set([`${WIN_ROOT}\\foundry\\start_foundry.bat`]);
  let r = foundryLaunchSpec({ platform: 'win32', repoRoot: WIN_ROOT, env: {}, exists: (p) => onlyBat.has(p) });
  assert.equal(r.spec, null);
  assert.match(r.reason, /virtual environment/);
  r = foundryLaunchSpec({ platform: 'win32', repoRoot: WIN_ROOT, env: {}, exists: () => false });
  assert.equal(r.spec, null);
  assert.match(r.reason, /start_foundry\.bat/);
});

test('win32: refuses paths cmd.exe cannot quote safely', () => {
  for (const root of ['C:\\odd"name\\planner', 'C:\\100%PATH%\\planner']) {
    const files = winFiles(root);
    const r = foundryLaunchSpec({ platform: 'win32', repoRoot: root, env: {}, exists: (p) => files.has(p) });
    assert.equal(r.spec, null, root);
    assert.match(r.reason, /not safe/);
  }
});

test('DEPT_FOUNDRY=off disables the launch', () => {
  const files = winFiles(WIN_ROOT);
  const r = foundryLaunchSpec({ platform: 'win32', repoRoot: WIN_ROOT, env: { DEPT_FOUNDRY: 'off' }, exists: (p) => files.has(p) });
  assert.equal(r.spec, null);
  assert.match(r.reason, /DEPT_FOUNDRY=off/);
});

test('posix: start_foundry.sh detached, log next to DEPT_DATA or in department/data', () => {
  const root = '/home/ann/planner';
  const files = new Set([`${root}/foundry/start_foundry.sh`, `${root}/.venv/bin/python`, '/bin/bash']);
  const exists = (p) => files.has(p);
  let { spec } = foundryLaunchSpec({ platform: 'linux', repoRoot: root, env: {}, exists });
  assert.equal(spec.cmd, '/bin/bash');
  assert.deepEqual(spec.args, [`${root}/foundry/start_foundry.sh`]);
  assert.equal(spec.opts.cwd, root);
  assert.equal(spec.opts.detached, true);
  assert.equal(spec.logFile, `${root}/department/data/foundry.log`);
  ({ spec } = foundryLaunchSpec({ platform: 'darwin', repoRoot: root, env: { DEPT_DATA: '/tmp/x/state.json' }, exists }));
  assert.equal(spec.logFile, '/tmp/x/foundry.log');
  assert.equal(spec.opts.detached, true);
  const r = foundryLaunchSpec({ platform: 'linux', repoRoot: root, env: {}, exists: (p) => p !== `${root}/.venv/bin/python` });
  assert.equal(r.spec, null);
  assert.match(r.reason, /virtual environment/);
});

test('probeFoundry: foundry / other (non-JSON, timeout) / free (refused)', async () => {
  const ok = await serve((req, res) => {
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ ok: req.url === '/api/health', repo: 'x' }));
  });
  const html = await serve((req, res) => res.end('<html>hi</html>'));
  const hang = await serve(() => {});
  try {
    assert.equal(await probeFoundry(ok.port), 'foundry');
    assert.equal(await probeFoundry(html.port), 'other');
    assert.equal(await probeFoundry(hang.port, 200), 'other');
    assert.equal(await probeFoundry(await freePort()), 'free');
  } finally {
    await Promise.all([ok.close(), html.close(), hang.close()]);
  }
});

test('maybeStartFoundry: starts only when the probe is refused', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-launch-foundry-'));
  try {
    const root = path.join(dir, 'repo');
    fs.mkdirSync(path.join(root, 'foundry'), { recursive: true });
    fs.mkdirSync(path.join(root, '.venv', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(root, 'foundry', 'start_foundry.sh'), '');
    fs.writeFileSync(path.join(root, '.venv', 'bin', 'python'), '');
    const env = { DEPT_DATA: path.join(dir, 'data', 'state.json'), FOUNDRY_SERVE_PORT: '4999' };
    const lines = [];
    const log = (l) => lines.push(l);

    for (const [state, expected] of [['foundry', 'running'], ['other', 'other']]) {
      const f = fakeSpawn();
      assert.equal(await maybeStartFoundry({ env, platform: 'linux', repoRoot: root, log, probe: async () => state, run: f.run }), expected);
      assert.equal(f.calls.length, 0, state);
    }

    const f = fakeSpawn();
    let probedPort;
    const probe = async (port) => ((probedPort = port), 'free');
    let clock = 1_000_000;
    const now = () => clock;
    assert.equal(await maybeStartFoundry({ env, platform: 'linux', repoRoot: root, log, probe, run: f.run, now }), 'started');
    assert.equal(probedPort, 4999);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].opts.detached, true);
    assert.equal(f.calls[0].opts.stdio[0], 'ignore');
    assert.equal(f.calls[0].unrefed, true);
    assert.ok(fs.readFileSync(path.join(dir, 'data', 'foundry.log'), 'utf8').includes('starting Foundry on port 4999'));

    // A second quick launch: Foundry isn't listening yet, but the marker stops a second start.
    clock += FOUNDRY_STARTING_MS - 1;
    const again = fakeSpawn();
    assert.equal(await maybeStartFoundry({ env, platform: 'linux', repoRoot: root, log, probe, run: again.run, now }), 'starting');
    assert.equal(again.calls.length, 0);
    clock += 2; // the marker is now 30 s old: a refused probe means the first start failed
    assert.equal(await maybeStartFoundry({ env, platform: 'linux', repoRoot: root, log, probe, run: again.run, now }), 'started');
    assert.equal(again.calls.length, 1);
    clock += FOUNDRY_STARTING_MS;

    const off = fakeSpawn();
    let probed = false;
    assert.equal(await maybeStartFoundry({ env: { ...env, DEPT_FOUNDRY: 'off' }, platform: 'linux', repoRoot: root, log, probe: async () => ((probed = true), 'free'), run: off.run }), 'disabled');
    assert.equal(probed, false);
    assert.equal(off.calls.length, 0);

    const none = fakeSpawn();
    const before = lines.length;
    assert.equal(await maybeStartFoundry({ env, platform: 'linux', repoRoot: path.join(dir, 'nope'), log, probe: async () => 'free', run: none.run }), 'skipped');
    assert.equal(none.calls.length, 0);
    assert.equal(lines.length, before + 1);

    clock += FOUNDRY_STARTING_MS;
    const boom = await maybeStartFoundry({ env, platform: 'linux', repoRoot: root, log, probe: async () => 'free', run: () => { throw new Error('ENOENT'); }, now });
    assert.equal(boom, 'error');

    // Every outcome except "already running" and "disabled" leaves a line in launch.log.
    const launchLog = fs.readFileSync(path.join(dir, 'data', 'launch.log'), 'utf8');
    assert.match(launchLog, /answers but not as Foundry/);
    assert.match(launchLog, /Foundry: started on http:\/\/127\.0\.0\.1:4999/);
    assert.match(launchLog, /another launch started it/);
    assert.match(launchLog, /not started \(no .*start_foundry\.sh\)/);
    assert.match(launchLog, /could not start \(ENOENT\)/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('maybeStartFoundry on win32: not detached, waits for cmd.exe to run start, then unrefs', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-launch-foundry-win-'));
  try {
    const files = winFiles(WIN_ROOT);
    const f = fakeSpawn();
    const env = { DEPT_DATA: path.join(dir, 'state.json'), SystemRoot: 'C:\\Windows' };
    const r = await maybeStartFoundry({ env, platform: 'win32', repoRoot: WIN_ROOT, log: () => {}, probe: async () => 'free', run: f.run, exists: (p) => files.has(p) });
    assert.equal(r, 'started');
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].cmd, 'C:\\Windows\\System32\\cmd.exe');
    assert.equal(f.calls[0].opts.detached, undefined);
    assert.equal(f.calls[0].opts.stdio, 'ignore');
    assert.equal(f.calls[0].unrefed, true);
    assert.ok(fs.existsSync(path.join(dir, 'foundry.starting')));
    assert.match(fs.readFileSync(path.join(dir, 'launch.log'), 'utf8'), /minimised "Foundry" window/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
