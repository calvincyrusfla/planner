#!/usr/bin/env node
// Local dashboard server for the agent department. No dependencies.
//   node department/server.mjs [--port 4300] [--open] [--hub-off]
// Local mode (no hub configured): the board lives in department/data/state.json.
// Hub mode (DEPT_HUB_URL or data/hub.json): the shared board on the Netlify site is
// the source of truth; mutations are forwarded to it, it is polled every 3 s and
// state.json is kept only as an offline cache. --hub-off or DEPT_HUB=off forces local.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as core from './lib/core.mjs';
import { load, save, DATA_FILE } from './lib/store.mjs';
import { applyOp } from './lib/ops.mjs';
import { hubConfig, hubGet, hubOp } from './lib/hub.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(here, 'public');
const ASSETS = path.join(here, 'assets');
const args = process.argv.slice(2);
const argPort = args.includes('--port') ? Number(args[args.indexOf('--port') + 1]) : null;
const PORT = argPort || Number(process.env.PORT) || 4300;
const HOST = process.env.HOST || '127.0.0.1';
const POLL_MS = 3000;
const UNREACHABLE = 'Shared board unreachable, try again shortly';

const hubOff = args.includes('--hub-off') || String(process.env.DEPT_HUB || '').toLowerCase() === 'off';
const hub = hubOff ? null : hubConfig();

let state = load();
let lastWrite = 0;
function persist() {
  save(state);
  lastWrite = fs.statSync(DATA_FILE).mtimeMs;
}

// Sync status goes on the payloads only, never into the saved state.
const sync = hub ? { mode: 'hub', ok: false, lastSync: null, error: null, url: hub.url } : { mode: 'local' };
const payload = () => ({ ...state, sync: { ...sync } });

const clients = new Set();
function broadcast() {
  const msg = `event: state\ndata: ${JSON.stringify(payload())}\n\n`;
  for (const res of clients) res.write(msg);
}

// ---------- hub mode ----------
let hubRev = null; // rev of the state we hold, null until the first successful sync
let mutationSeq = 0; // bumped by every hub mutation, so a poll that overlapped one is discarded

function hubFailed(err) {
  const changed = sync.ok || sync.error !== err.message;
  sync.ok = false;
  sync.error = err.code === 'HUB_UNREACHABLE' ? err.message || 'unreachable' : err.message;
  if (changed) {
    console.warn(`Shared board: ${sync.error}`);
    broadcast();
  }
}

function adoptHubState(next, rev) {
  state = next;
  hubRev = rev;
  try {
    persist(); // offline cache
  } catch (err) {
    console.warn(`Could not cache the shared board to ${DATA_FILE}: ${err.message}`);
  }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  const seq = mutationSeq;
  try {
    const { state: next, rev } = await hubGet(hub);
    const wasOk = sync.ok;
    sync.ok = true;
    sync.error = null;
    sync.lastSync = new Date().toISOString();
    if (seq !== mutationSeq) return; // a mutation landed meanwhile; its result is newer
    if (rev !== hubRev) {
      if (!wasOk) console.log(`Shared board: connected (rev ${rev})`);
      adoptHubState(next, rev);
      broadcast();
    } else if (!wasOk) {
      console.log(`Shared board: reconnected (rev ${rev})`);
      broadcast();
    }
  } catch (err) {
    hubFailed(err);
  } finally {
    polling = false;
  }
}

async function hubMutate(op) {
  mutationSeq++;
  let out;
  try {
    out = await hubOp(hub, op);
  } catch (err) {
    if (err.code === 'HUB_UNREACHABLE') {
      hubFailed(err);
      throw new core.DeptError(UNREACHABLE, 503);
    }
    throw err; // the hub's own error (400/404/409/401...) passes through
  }
  sync.ok = true;
  sync.error = null;
  sync.lastSync = new Date().toISOString();
  // Concurrent mutations may answer out of order; never step back to an older rev.
  if (hubRev === null || out.rev >= hubRev) adoptHubState(out.state, out.rev);
  broadcast();
  return out.result;
}

// ---------- local mode ----------
function localMutate(op) {
  const result = applyOp(state, op);
  persist();
  broadcast();
  return result;
}

const mutate = (type, opArgs = {}) => (hub ? hubMutate({ type, args: opArgs }) : localMutate({ type, args: opArgs }));

if (hub) {
  poll();
  setInterval(poll, POLL_MS).unref();
} else {
  persist();
  // Pick up edits made by the CLI's offline mode or by hand.
  fs.watchFile(DATA_FILE, { interval: 1000 }, (cur) => {
    if (!cur.mtimeMs || cur.mtimeMs === lastWrite) return;
    lastWrite = cur.mtimeMs;
    state = load();
    broadcast();
  });
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json', '.json': 'application/manifest+json' };

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 1e6) throw new core.DeptError('Body too large', 413);
  }
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new core.DeptError('Body must be JSON');
  }
}

const routes = [
  ['GET', /^\/api\/state$/, () => payload()],
  ['POST', /^\/api\/tasks$/, (b) => mutate('addTask', { title: b.title, description: b.description, size: b.size })],
  ['PATCH', /^\/api\/tasks\/([\w-]+)$/, (b, [id]) => mutate('updateTask', { id, patch: b, by: b.by || 'user' })],
  ['DELETE', /^\/api\/tasks\/([\w-]+)$/, (b, [id]) => mutate('deleteTask', { id, by: b.by || 'user' })],
  ['POST', /^\/api\/tasks\/([\w-]+)\/assign$/, (b, [id]) => mutate('assignTeam', { taskId: id, teamId: b.teamId, focus: b.focus, by: b.by })],
  ['POST', /^\/api\/tasks\/([\w-]+)\/unassign$/, (b, [id]) => mutate('unassignTeam', { taskId: id, teamId: b.teamId, by: b.by })],
  ['POST', /^\/api\/progress$/, (b) => mutate('reportProgress', b)],
  ['POST', /^\/api\/dispatch$/, () => mutate('dispatch', {})],
  ['PATCH', /^\/api\/settings$/, (b) => mutate('updateSettings', b)],
  ['POST', /^\/api\/reset$/, () => mutate('reset', {})],
  // Generic form, {type, args}, for any applyOp operation (e.g. importState).
  ['POST', /^\/api\/op$/, (b) => mutate(String(b.type || ''), b.args || {})],
];

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === '/api/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store', connection: 'keep-alive' });
      res.write(`event: state\ndata: ${JSON.stringify(payload())}\n\n`);
      clients.add(res);
      const ping = setInterval(() => res.write(': ping\n\n'), 25000);
      req.on('close', () => {
        clearInterval(ping);
        clients.delete(res);
      });
      return;
    }
    if (url.pathname.startsWith('/api/')) {
      for (const [method, re, handler] of routes) {
        const m = url.pathname.match(re);
        if (m && req.method === method) {
          const body = method === 'GET' ? {} : await readBody(req);
          return send(res, 200, { ok: true, result: await handler(body, m.slice(1)) });
        }
      }
      return send(res, 404, { ok: false, error: 'Not found' });
    }
    // /assets/* comes from department/assets (icons); everything else from public/.
    const inAssets = url.pathname.startsWith('/assets/');
    const root = inAssets ? ASSETS : PUBLIC;
    const rel = inAssets ? url.pathname.slice('/assets'.length) : url.pathname;
    const file = path.join(root, rel === '/' ? 'index.html' : path.normalize(rel));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404).end('Not found');
      return;
    }
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  } catch (err) {
    send(res, err.status || 500, { ok: false, error: err.message });
  }
});

server.listen(PORT, HOST, () => {
  const link = `http://${HOST === '0.0.0.0' ? 'localhost' : HOST}:${PORT}`;
  console.log(`Agent Department dashboard: ${link}`);
  console.log(hub ? `Shared board: ${hub.url} (cache: ${DATA_FILE})` : `State file: ${DATA_FILE}`);
  if (args.includes('--open')) {
    const opener = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'start' : 'xdg-open';
    spawn(opener, [link], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' }).unref();
  }
});
