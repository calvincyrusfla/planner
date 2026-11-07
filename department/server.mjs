#!/usr/bin/env node
// Local dashboard server for the agent department. No dependencies.
//   node department/server.mjs [--port 4300] [--open]
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as core from './lib/core.mjs';
import { load, save, DATA_FILE } from './lib/store.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(here, 'public');
const args = process.argv.slice(2);
const argPort = args.includes('--port') ? Number(args[args.indexOf('--port') + 1]) : null;
const PORT = argPort || Number(process.env.PORT) || 4300;
const HOST = process.env.HOST || '127.0.0.1';

let state = load();
let lastWrite = 0;
function persist() {
  save(state);
  lastWrite = fs.statSync(DATA_FILE).mtimeMs;
}
persist();
const clients = new Set();

function broadcast() {
  const payload = `event: state\ndata: ${JSON.stringify(state)}\n\n`;
  for (const res of clients) res.write(payload);
}

function mutate(fn) {
  const result = fn(state);
  persist();
  broadcast();
  return result;
}

// Pick up edits made by the CLI's offline mode or by hand.
fs.watchFile(DATA_FILE, { interval: 1000 }, (cur) => {
  if (!cur.mtimeMs || cur.mtimeMs === lastWrite) return;
  lastWrite = cur.mtimeMs;
  state = load();
  broadcast();
});

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

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
  ['GET', /^\/api\/state$/, () => state],
  ['POST', /^\/api\/tasks$/, (b) => mutate((s) => core.addTask(s, b))],
  ['PATCH', /^\/api\/tasks\/([\w-]+)$/, (b, [id]) => mutate((s) => core.updateTask(s, id, b, { by: b.by || 'user' }))],
  ['DELETE', /^\/api\/tasks\/([\w-]+)$/, (b, [id]) => mutate((s) => core.deleteTask(s, id))],
  ['POST', /^\/api\/tasks\/([\w-]+)\/assign$/, (b, [id]) => mutate((s) => core.assignTeam(s, id, b.teamId, b))],
  ['POST', /^\/api\/tasks\/([\w-]+)\/unassign$/, (b, [id]) => mutate((s) => core.unassignTeam(s, id, b.teamId, b))],
  ['POST', /^\/api\/progress$/, (b) => mutate((s) => core.reportProgress(s, b))],
  ['POST', /^\/api\/dispatch$/, () => mutate((s) => core.dispatch(s))],
  ['PATCH', /^\/api\/settings$/, (b) => mutate((s) => core.updateSettings(s, b))],
  ['POST', /^\/api\/reset$/, () => mutate((s) => Object.assign(s, core.createInitialState()))],
];

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === '/api/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store', connection: 'keep-alive' });
      res.write(`event: state\ndata: ${JSON.stringify(state)}\n\n`);
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
    const file = path.join(PUBLIC, url.pathname === '/' ? 'index.html' : path.normalize(url.pathname));
    if (!file.startsWith(PUBLIC) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
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
  console.log(`State file: ${DATA_FILE}`);
  if (args.includes('--open')) {
    const opener = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'start' : 'xdg-open';
    spawn(opener, [link], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' }).unref();
  }
});
