// Tests for the dashboard's /api/foundry/* proxy (server.mjs) to the Foundry service.
// Safety: a fake Foundry HTTP server on a free loopback port (FOUNDRY_SERVE_PORT),
// the shared board off (DEPT_HUB=off), a scratch DEPT_DATA and free ports only.
// Only child processes and servers started here are stopped.
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

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-foundry-'));
let child;
let fake;
let dash;
let foundryPort;

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

// Fake Foundry: records requests; behaviour per path.
const seen = [];
function startFake(port) {
  const srv = http.createServer(async (req, res) => {
    let raw = '';
    for await (const c of req) raw += c;
    seen.push({ method: req.method, url: req.url, host: req.headers.host, body: raw });
    const json = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    if (req.url === '/api/health') return json(200, { ok: true, repo: '/repo', model: 'gemma', gemma_ok: true, claude_ok: true, version: '2' });
    if (req.url === '/api/status') return json(200, { ok: true, tasks: [], foreman: {}, paused_until: null, last_account_error: null });
    if (req.url === '/api/orders') {
      const b = JSON.parse(raw || '{}');
      if (!b.title) return json(400, { ok: false, error: 'title is required' });
      return json(200, { ok: true, task_id: 'T-900', echo: b });
    }
    if (req.url === '/api/ask') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
      res.write(`data: ${JSON.stringify({ text: 'Hello ' })}\n\n`);
      // The second chunk only comes once the test says the first one arrived.
      fake.release = () => {
        res.write(`data: ${JSON.stringify({ text: 'world' })}\n\n`);
        res.end('event: done\ndata: {}\n\n');
      };
      return;
    }
    json(404, { ok: false, error: 'nope' });
  });
  return new Promise((r) => srv.listen(port, '127.0.0.1', () => r(srv)));
}

function request(method, p, { body, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body);
    const req = http.request(
      { host: '127.0.0.1', port: dash, method, path: p, headers: { ...(data ? { 'content-type': 'application/json' } : {}), ...headers } },
      (res) => {
        let text = '';
        res.setEncoding('utf8');
        res.on('data', (c) => (text += c));
        res.on('end', () => {
          let json = null;
          try {
            json = JSON.parse(text);
          } catch {}
          resolve({ status: res.statusCode, headers: res.headers, text, json });
        });
      },
    );
    req.on('error', reject);
    req.end(data);
  });
}

test.before(async () => {
  foundryPort = await freePort();
  dash = await freePort();
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (/^(HTTPS?_PROXY|https?_proxy|NODE_USE_ENV_PROXY|DEPT_|FOUNDRY_)/.test(k)) delete env[k];
  Object.assign(env, { HOME: dir, USERPROFILE: dir, DEPT_DATA: path.join(dir, 'state.json'), DEPT_HUB: 'off', DEPT_HUB_URL: '', FOUNDRY_SERVE_PORT: String(foundryPort) });
  child = spawn(process.execPath, [SERVER, '--port', String(dash)], { env });
  let out = '';
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`server did not start:\n${out}`)), 8000);
    const on = (d) => {
      out += d;
      if (out.includes('Agent Department dashboard')) {
        clearTimeout(timer);
        resolve();
      }
    };
    child.stdout.on('data', on);
    child.stderr.on('data', on);
    child.once('exit', (code) => reject(new Error(`server exited (${code}):\n${out}`)));
  });
});

test.after(async () => {
  child?.kill();
  fake?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

test('503 with a start hint when Foundry is not running', async () => {
  const r = await request('GET', '/api/foundry/health');
  assert.equal(r.status, 503);
  assert.equal(r.json.ok, false);
  assert.equal(r.json.code, 'foundry_down');
  assert.match(r.json.error, /Foundry is not running — start it with/);
  assert.match(r.json.error, /start_foundry\.bat/);
});

test('GET health and status pass through to /api/* with a loopback Host', async () => {
  fake = await startFake(foundryPort);
  const h = await request('GET', '/api/foundry/health');
  assert.equal(h.status, 200);
  assert.deepEqual(h.json, { ok: true, repo: '/repo', model: 'gemma', gemma_ok: true, claude_ok: true, version: '2' });
  const last = seen.at(-1);
  assert.equal(last.url, '/api/health');
  assert.equal(last.host, `127.0.0.1:${foundryPort}`);
  const s = await request('GET', '/api/foundry/status');
  assert.equal(s.status, 200);
  assert.deepEqual(s.json.tasks, []);
});

test('POST orders forwards the JSON body and passes validation errors through', async () => {
  const order = { title: 'Fix it', description: 'd', files: ['a.py'], size: 'small' };
  const ok = await request('POST', '/api/foundry/orders', { body: order });
  assert.equal(ok.status, 200);
  assert.equal(ok.json.task_id, 'T-900');
  assert.deepEqual(ok.json.echo, order);
  const bad = await request('POST', '/api/foundry/orders', { body: { description: 'no title' } });
  assert.equal(bad.status, 400);
  assert.deepEqual(bad.json, { ok: false, error: 'title is required' });
  const notJson = await request('POST', '/api/foundry/orders', { body: '{oops' });
  assert.equal(notJson.status, 400);
});

test('POST ask streams the SSE answer chunk by chunk', async () => {
  const text = await new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port: dash, method: 'POST', path: '/api/foundry/ask', headers: { 'content-type': 'application/json' } },
      (res) => {
        assert.equal(res.statusCode, 200);
        assert.match(res.headers['content-type'], /^text\/event-stream/);
        let all = '';
        res.setEncoding('utf8');
        res.on('data', (c) => {
          all += c;
          // The first chunk arrived before the fake finished: it was not buffered.
          if (all.includes('Hello') && fake.release) {
            const go = fake.release;
            fake.release = null;
            go();
          }
        });
        res.on('end', () => resolve(all));
      },
    );
    req.on('error', reject);
    req.end(JSON.stringify({ question: 'How is the line?' }));
  });
  assert.match(text, /data: \{"text":"Hello "\}/);
  assert.match(text, /data: \{"text":"world"\}/);
  assert.match(text, /event: done/);
  assert.equal(JSON.parse(seen.at(-1).body).question, 'How is the line?');
});

test('unknown endpoints get 404, wrong methods 405, and are not forwarded', async () => {
  const before = seen.length;
  assert.equal((await request('GET', '/api/foundry/secret')).status, 404);
  assert.equal((await request('GET', '/api/foundry/../state')).status, 200); // normalised to /api/state by the URL parser
  assert.equal((await request('GET', '/api/foundry/orders')).status, 405);
  assert.equal((await request('POST', '/api/foundry/health')).status, 405);
  assert.equal(seen.length, before);
});

test('only callers on this computer: foreign Host or Origin get 403', async () => {
  const before = seen.length;
  const host = await request('GET', '/api/foundry/health', { headers: { host: 'evil.example:4300' } });
  assert.equal(host.status, 403);
  const origin = await request('POST', '/api/foundry/orders', { body: { title: 'x' }, headers: { origin: 'http://evil.example' } });
  assert.equal(origin.status, 403);
  assert.equal(seen.length, before);
  const same = await request('GET', '/api/foundry/health', { headers: { origin: `http://127.0.0.1:${dash}` } });
  assert.equal(same.status, 200);
});

test('existing board routes still work', async () => {
  const s = await request('GET', '/api/state');
  assert.equal(s.status, 200);
  assert.equal(s.json.ok, true);
});
