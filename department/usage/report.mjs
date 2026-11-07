#!/usr/bin/env node
// Background helper for the usage meter: sends one usage report to the board as
// op {type: 'reportUsage', args}. Started detached by statusline.mjs with the
// payload in env DEPT_USAGE_PAYLOAD (or as the first argument, for testing):
//   { fiveHour: {usedPct, resetsAt} | null, sevenDay: {...} | null, reportedAt }
// Transport is the CLI's: the local dashboard server (POST /api/op) if it is up,
// else the shared board if one is configured, else the local state file.
// It prints nothing; a failure appends one line to usage-report.log in the
// data dir (kept to the last 200 lines).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { save, DATA_FILE } from '../lib/store.mjs';
import { createInitialState, normalize } from '../lib/core.mjs';
import { applyOp } from '../lib/ops.mjs';
import { hubConfig, hubOp } from '../lib/hub.mjs';

const LOG_FILE = path.join(path.dirname(DATA_FILE), 'usage-report.log');
const LOG_LINES = 200;
const SERVER_TIMEOUT_MS = 3000;
const HARD_EXIT_MS = 20_000;

function logFailure(message) {
  try {
    let lines = [];
    try {
      lines = fs.readFileSync(LOG_FILE, 'utf8').split(/\r?\n/).filter(Boolean);
    } catch { /* no log yet */ }
    const one = String(message).replace(/\s+/g, ' ').trim().slice(0, 300);
    lines.push(`${new Date().toISOString()} ${one}`);
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.writeFileSync(LOG_FILE, lines.slice(-LOG_LINES).join('\n') + '\n');
  } catch { /* nowhere left to complain */ }
}

// Like dept.mjs: Node's fetch only honours HTTPS_PROXY with NODE_USE_ENV_PROXY
// (Node 22.21+), so re-run once with it set. NO_PROXY keeps 127.0.0.1 direct.
if ((process.env.HTTPS_PROXY || process.env.https_proxy) && !process.env.NODE_USE_ENV_PROXY) {
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, ['--disable-warning=UNDICI-EHPA', ...process.execArgv, ...process.argv.slice(1)], {
    stdio: 'ignore',
    windowsHide: true,
    env: { ...process.env, NODE_USE_ENV_PROXY: '1' },
  });
  if (r.error) logFailure(`re-exec behind proxy failed: ${r.error.message}`);
  process.exit(r.status ?? 1);
}

// PORT alone is not trusted: the user's shell may set it for some other app.
// DEPT_URL, else DEPT_PORT, else the dashboard's default 4300.
const BASE = process.env.DEPT_URL || `http://127.0.0.1:${process.env.DEPT_PORT || 4300}`;

function defaultSource() {
  const s = process.env.DEPT_USAGE_SOURCE ||
    (process.env.CLAUDE_CODE_REMOTE === 'true' ? 'Claude cloud' : os.hostname()) || 'PC';
  return String(s).trim().slice(0, 40) || 'PC';
}

function readPayload() {
  const raw = process.env.DEPT_USAGE_PAYLOAD || process.argv[2];
  if (!raw) throw new Error('no payload (DEPT_USAGE_PAYLOAD is empty)');
  const p = JSON.parse(raw);
  if (!p || typeof p !== 'object') throw new Error('payload is not an object');
  return {
    fiveHour: p.fiveHour ?? null,
    sevenDay: p.sevenDay ?? null,
    reportedAt: p.reportedAt || new Date().toISOString(),
    source: String(p.source || defaultSource()).slice(0, 40),
  };
}

// {offline: true} when no server answers; throws on the server's own error.
async function viaServer(op) {
  let res;
  try {
    res = await fetch(`${BASE}/api/op`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(op),
      signal: AbortSignal.timeout(SERVER_TIMEOUT_MS),
    });
  } catch {
    return { offline: true };
  }
  let json;
  try {
    json = await res.json();
  } catch {
    json = null;
  }
  // Not Agent Department JSON: something else listens on that port.
  if (!json || typeof json !== 'object' || typeof json.ok !== 'boolean') return { offline: true, foreign: res.status };
  if (!json.ok) throw new Error(`dashboard server: ${json.error || `HTTP ${res.status}`}`);
  return { result: json.result, via: 'server' };
}

// Like store.load(), except that an unreadable or corrupt file (EPERM/EBUSY
// mid-rename on Windows, half-written JSON) throws instead of turning into an
// empty board that save() would then write over the real one.
function loadForWrite() {
  let raw;
  try {
    raw = fs.readFileSync(DATA_FILE, 'utf8');
  } catch (err) {
    if (err.code === 'ENOENT') return createInitialState();
    throw new Error(`could not read ${DATA_FILE} (${err.code || err.message}); not writing`);
  }
  try {
    return normalize(JSON.parse(raw));
  } catch (err) {
    throw new Error(`could not parse ${DATA_FILE} (${err.message}); not writing`);
  }
}

async function send(args) {
  const op = { type: 'reportUsage', args };
  const r = await viaServer(op);
  if (!r.offline) return r;
  const cfg = hubConfig();
  if (cfg) return { result: (await hubOp(cfg, op)).result, via: 'hub' };
  const state = loadForWrite();
  const result = applyOp(state, op);
  if (!result || result.stored !== false) save(state);
  return { result, via: 'file' };
}

async function main() {
  setTimeout(() => {
    logFailure(`gave up after ${HARD_EXIT_MS / 1000} s`);
    process.exit(1);
  }, HARD_EXIT_MS).unref();
  try {
    await send(readPayload());
    process.exitCode = 0;
  } catch (err) {
    logFailure(`report failed: ${err && err.message ? err.message : err}`);
    process.exitCode = 1;
  }
}

await main();
