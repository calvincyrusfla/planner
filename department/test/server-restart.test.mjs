import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const server = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'server.mjs');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
    s.on('error', reject);
  });
}

async function health(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`, { signal: AbortSignal.timeout(1000) });
    return await res.json();
  } catch {
    return null;
  }
}

async function waitFor(fn, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(100);
  }
  return null;
}

async function start(extraEnv = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-restart-'));
  const port = await freePort();
  const child = spawn(process.execPath, [server, '--port', String(port), '--hub-off'], {
    env: { ...process.env, DEPT_DATA: path.join(dir, 'state.json'), DEPT_HUB: 'off', HOST: '127.0.0.1', ...extraEnv },
    stdio: 'ignore',
  });
  const first = await waitFor(() => health(port));
  assert.ok(first, 'server did not start');
  return { port, child, first, dir };
}

const post = (port, p, headers = {}) =>
  fetch(`http://127.0.0.1:${port}${p}`, { method: 'POST', headers, signal: AbortSignal.timeout(5000) });

function cleanup(ctx, pids = []) {
  for (const pid of [ctx.child.pid, ...pids]) {
    try {
      process.kill(pid);
    } catch {}
  }
}

test('health and state report commit and start time', async () => {
  const ctx = await start();
  try {
    assert.equal(ctx.first.ok, true);
    assert.equal(ctx.first.pid, ctx.child.pid);
    assert.ok(!Number.isNaN(Date.parse(ctx.first.startedAt)));
    assert.ok(ctx.first.commit === null || /^[0-9a-f]{7,64}$/i.test(ctx.first.commit));
    const st = await (await fetch(`http://127.0.0.1:${ctx.port}/api/state`)).json();
    assert.equal(st.result.dashboard.startedAt, ctx.first.startedAt);
    assert.equal(st.result.dashboard.commit, ctx.first.commit);
  } finally {
    cleanup(ctx);
  }
});

test('restart is refused for a non-local request', async () => {
  const ctx = await start();
  try {
    const status = await new Promise((resolve, reject) => {
      const r = http.request(
        { host: '127.0.0.1', port: ctx.port, path: '/api/dashboard/restart', method: 'POST', headers: { host: 'evil.example.com' } },
        (rs) => {
          rs.resume();
          resolve(rs.statusCode);
        },
      );
      r.on('error', reject);
      r.end();
    });
    assert.equal(status, 403);
    await sleep(500);
    assert.equal((await health(ctx.port))?.pid, ctx.child.pid);
  } finally {
    cleanup(ctx);
  }
});

test('restart replaces the process on the same port', async () => {
  const ctx = await start();
  let newPid = null;
  try {
    const res = await post(ctx.port, '/api/dashboard/restart');
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, restarting: true });
    const next = await waitFor(async () => {
      const h = await health(ctx.port);
      return h && h.pid !== ctx.child.pid ? h : null;
    });
    assert.ok(next, 'no new dashboard answered');
    newPid = next.pid;
    assert.notEqual(next.startedAt, ctx.first.startedAt);
  } finally {
    cleanup(ctx, newPid ? [newPid] : []);
  }
});

test('a failed respawn leaves the old dashboard running', async () => {
  const ctx = await start({ DEPT_RESTART_FAIL: '1' });
  try {
    const res = await post(ctx.port, '/api/dashboard/restart');
    assert.equal(res.status, 200);
    await sleep(1000);
    const h = await waitFor(() => health(ctx.port));
    assert.equal(h?.pid, ctx.child.pid);
  } finally {
    cleanup(ctx);
  }
});
