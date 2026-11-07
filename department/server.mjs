#!/usr/bin/env node
// Local dashboard server for the agent department. No dependencies.
//   node department/server.mjs [--port 4300] [--open] [--hub-off]
// Local mode (no hub configured): the board lives in department/data/state.json.
// Hub mode (DEPT_HUB_URL or data/hub.json): the shared board on the Netlify site is
// the source of truth; mutations are forwarded to it, it is polled every 3 s and
// data/hub-cache.json is kept as an offline cache (state.json is left untouched). --hub-off or DEPT_HUB=off forces local.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as core from './lib/core.mjs';
import { load, save, DATA_FILE } from './lib/store.mjs';
import { applyOp } from './lib/ops.mjs';
import { hubConfig, hubGet, hubOp } from './lib/hub.mjs';
import { refreshUsageFromAnthropic, isUsageDirectEnabled, defaultSource, CODE_STATUS, MESSAGES } from './lib/usage-direct.mjs';

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

// Hub mode never touches state.json (the local board stays intact for `hub push`);
// its offline cache is a separate file next to it.
const CACHE_FILE = path.join(path.dirname(DATA_FILE), 'hub-cache.json');
let state = hub ? load(CACHE_FILE) : load();
let lastWrite = 0;
function persist() {
  save(state);
  lastWrite = fs.statSync(DATA_FILE).mtimeMs;
}

// Sync status goes on the payloads only, never into the saved state.
const sync = hub ? { mode: 'hub', ok: false, lastSync: null, error: null, url: hub.url } : { mode: 'local' };
// Capabilities, like sync, are computed per payload and never saved. They depend on
// who asks: usage refresh only works for callers on this computer (see
// refreshAllowed), so a phone on the LAN is told it can't, and shows no button.
const usageDirectOff = !isUsageDirectEnabled(process.env); // DEPT_USAGE_DIRECT=off
const capabilities = (local) => ({ usageRefresh: !usageDirectOff && local });
const payload = (local = false) => ({ ...state, sync: { ...sync }, capabilities: capabilities(local) });

const clients = new Map(); // SSE response -> whether its request came from this computer
function broadcast() {
  const msgs = {};
  for (const [res, local] of clients) {
    msgs[local] ??= `event: state\ndata: ${JSON.stringify(payload(local))}\n\n`;
    res.write(msgs[local]);
  }
}

// ---------- hub mode ----------
let hubRev = null; // rev of the state we hold, null until the first successful sync
let mutationSeq = 0; // bumped by every hub mutation, so a poll that overlapped one is discarded

function hubFailed(err) {
  const changed = sync.ok || sync.error !== err.message;
  sync.ok = false;
  sync.error = err.code === 'HUB_UNREACHABLE' ? err.message || 'unreachable' : err.message;
  if (changed) {
    console.warn(sync.error.startsWith('Shared board') ? sync.error : `Shared board: ${sync.error}`);
    broadcast();
  }
}

function adoptHubState(next, rev) {
  state = next;
  hubRev = rev;
  try {
    save(state, CACHE_FILE); // offline cache
  } catch (err) {
    console.warn(`Could not cache the shared board to ${CACHE_FILE}: ${err.message}`);
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
  if (!sync.ok) console.log(`Shared board: reconnected (rev ${out.rev})`);
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

// ---------- live usage refresh (POST /api/usage/refresh) ----------
// Reads Claude Code's saved login on this PC (lib/usage-direct.mjs), asks
// Anthropic for the current usage and stores it as a normal reportUsage op.
// The token never leaves usage-direct.mjs except towards Anthropic: nothing
// here sees, logs or returns it. Only callers on this machine may trigger it,
// and at most once per REFRESH_GAP_MS.
const REFRESH_GAP_MS = 30 * 1000;
const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
// Short messages for every code usage-direct.mjs can raise. An error without one of
// these codes gets the generic bad_response text, so unexpected text never reaches the browser.
const REFRESH_FALLBACK = {
  disabled: MESSAGES.disabled,
  no_login: MESSAGES.noLogin,
  expired: MESSAGES.expired,
  rate_limited: MESSAGES.rateLimited,
  unreachable: MESSAGES.unreachable,
  bad_response: MESSAGES.badResponse(),
};
let lastRefreshAt = 0;

const hostOf = (value) => String(value || '').toLowerCase().replace(/:\d+$/, '');

// A request from this computer: loopback socket and a local Host header (blocks DNS rebinding).
function isLocalRequest(req) {
  return LOOPBACK.has(req.socket.remoteAddress) && LOCAL_HOSTS.has(hostOf(req.headers.host));
}

// Refresh requests must come from this computer and, if a browser sends an Origin,
// from the same local host.
function refreshAllowed(req) {
  if (!isLocalRequest(req)) return false;
  const origin = req.headers.origin;
  if (origin === undefined) return true;
  try {
    const u = new URL(origin);
    return LOCAL_HOSTS.has(u.hostname.toLowerCase().replace(/^::1$/, '[::1]')) && u.host === req.headers.host;
  } catch {
    return false;
  }
}

async function refreshUsage(req, res) {
  if (!refreshAllowed(req)) return send(res, 403, { ok: false, error: 'Usage refresh is only allowed from this computer', code: 'forbidden' });
  if (usageDirectOff) return send(res, 403, { ok: false, error: MESSAGES.disabled, code: 'disabled' });
  const wait = lastRefreshAt + REFRESH_GAP_MS - Date.now();
  if (wait > 0) {
    const retryAfter = Math.ceil(wait / 1000);
    res.setHeader('retry-after', String(retryAfter));
    return send(res, 429, { ok: false, error: `Wait ${retryAfter} s before refreshing again`, code: 'throttled', retryAfter });
  }
  lastRefreshAt = Date.now(); // taken before the await, so parallel clicks are throttled too
  let report;
  // Only attempts that actually sent a request to Anthropic use up the throttle, so
  // fixing a missing/expired login can be followed by an immediate retry.
  let sent = false;
  const fetchImpl = (...a) => {
    sent = true;
    return globalThis.fetch(...a); // passed straight through; the request is never inspected or kept
  };
  try {
    // Resolves to {type: 'reportUsage', args}; the token stays inside usage-direct.mjs.
    report = (await refreshUsageFromAnthropic({ env: process.env, source: defaultSource(), fetchImpl })).args;
  } catch (err) {
    const code = err && Object.hasOwn(REFRESH_FALLBACK, err.code) ? err.code : 'bad_response';
    if (!sent) lastRefreshAt = 0;
    // usage-direct's messages are short fixed texts by contract; anything else gets ours.
    const message = err && err.code === code && typeof err.message === 'string' && err.message.length <= 200 ? err.message : REFRESH_FALLBACK[code];
    const status = CODE_STATUS[code] || 502;
    console.warn(`Usage refresh failed (${code})`);
    return send(res, status, { ok: false, error: message, code });
  }
  const result = await mutate('reportUsage', report); // same path as every other mutation (hub or local)
  return send(res, 200, { ok: true, result: { usage: result?.usage ?? null, stored: result?.stored !== false } });
}

const routes = [
  ['GET', /^\/api\/state$/, (b, m, req) => payload(isLocalRequest(req))],
  ['POST', /^\/api\/tasks$/, (b) => mutate('addTask', { title: b.title, description: b.description, size: b.size, builders: b.builders })],
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
      const local = isLocalRequest(req);
      res.write(`event: state\ndata: ${JSON.stringify(payload(local))}\n\n`);
      clients.set(res, local);
      const ping = setInterval(() => res.write(': ping\n\n'), 25000);
      req.on('close', () => {
        clearInterval(ping);
        clients.delete(res);
      });
      return;
    }
    if (url.pathname === '/api/usage/refresh') {
      if (req.method !== 'POST') return send(res, 405, { ok: false, error: 'Use POST', code: 'method' });
      return await refreshUsage(req, res);
    }
    if (url.pathname.startsWith('/api/')) {
      for (const [method, re, handler] of routes) {
        const m = url.pathname.match(re);
        if (m && req.method === method) {
          const body = method === 'GET' ? {} : await readBody(req);
          return send(res, 200, { ok: true, result: await handler(body, m.slice(1), req) });
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
  console.log(hub ? `Shared board: ${hub.url} (cache: ${CACHE_FILE})` : `State file: ${DATA_FILE}`);
  if (args.includes('--open')) {
    const opener = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'start' : 'xdg-open';
    spawn(opener, [link], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' }).unref();
  }
});
