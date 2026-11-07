// Client for the shared board ("hub") stored on the Netlify site.
// The hub is the Netlify function at <url>/api/department. Zero dependencies.
import fs from 'node:fs';
import path from 'node:path';
import { DeptError } from './core.mjs';
import { DATA_FILE } from './store.mjs';

export const HUB_FILE = path.join(path.dirname(DATA_FILE), 'hub.json');
const TIMEOUT_MS = 8000;

const cleanUrl = (u) => String(u || '').trim().replace(/\/+$/, '');

// {url, key} | null. Env DEPT_HUB_URL (+ DEPT_HUB_KEY) wins over hub.json.
export function hubConfig() {
  // DEPT_HUB=off forces local mode everywhere (server and CLI), e.g. for tests.
  if (String(process.env.DEPT_HUB || '').toLowerCase() === 'off') return null;
  if (process.env.DEPT_HUB_URL) {
    return { url: cleanUrl(process.env.DEPT_HUB_URL), key: process.env.DEPT_HUB_KEY || '', source: 'env' };
  }
  try {
    const cfg = JSON.parse(fs.readFileSync(HUB_FILE, 'utf8'));
    if (cfg && cfg.url) return { url: cleanUrl(cfg.url), key: cfg.key || '', source: 'file' };
  } catch (err) {
    if (err.code !== 'ENOENT') console.warn(`Could not read ${HUB_FILE} (${err.message}); ignoring it`);
  }
  return null;
}

export function saveHubConfig({ url, key }) {
  fs.mkdirSync(path.dirname(HUB_FILE), { recursive: true });
  const tmp = `${HUB_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify({ url: cleanUrl(url), key: key || '' }, null, 2), { mode: 0o600 });
  try { fs.chmodSync(tmp, 0o600); } catch { /* not supported (e.g. Windows) */ }
  fs.renameSync(tmp, HUB_FILE);
}

export function clearHubConfig() {
  try {
    fs.unlinkSync(HUB_FILE);
    return true;
  } catch (err) {
    if (err.code === 'ENOENT') return false;
    throw err;
  }
}

function unreachable(cfg, why) {
  const err = new Error(`Shared board unreachable at ${cfg.url} (${why})`);
  err.code = 'HUB_UNREACHABLE';
  return err;
}

async function request(cfg, method, body) {
  if (!cfg || !cfg.url) throw new DeptError('No shared board configured');
  let res;
  try {
    res = await fetch(`${cleanUrl(cfg.url)}/api/department`, {
      method,
      headers: {
        'content-type': 'application/json',
        // Empty key: in Claude cloud sessions a network proxy injects the real header.
        'x-edit-key': cfg.key || 'proxy',
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    const why = err.name === 'TimeoutError' || err.name === 'AbortError'
      ? `no answer in ${TIMEOUT_MS / 1000} s`
      : (err.cause && (err.cause.code || err.cause.message)) || err.message;
    throw unreachable(cfg, why);
  }
  let json;
  try {
    json = await res.json();
  } catch {
    if (res.status >= 500) throw unreachable(cfg, `HTTP ${res.status}`);
    throw new DeptError(`Shared board at ${cfg.url} gave an unexpected answer (HTTP ${res.status}); is the site deployed with the department function?`, res.status || 502);
  }
  if (!json || !json.ok) {
    const error = (json && json.error) || `HTTP ${res.status}`;
    const msg = error === 'bad_key' ? 'Shared board rejected the passcode (bad_key); check --key / DEPT_HUB_KEY'
      : error === 'editing_disabled' ? 'Shared board has no EDIT_KEY set on the Netlify site (editing_disabled)'
      : error;
    const e = new DeptError(msg, res.status);
    e.hubError = error;
    throw e;
  }
  return json;
}

export async function hubGet(cfg) {
  const json = await request(cfg, 'GET');
  return { state: json.state, rev: json.rev };
}

export async function hubOp(cfg, op) {
  const json = await request(cfg, 'POST', { type: op.type, args: op.args || {} });
  return { result: json.result, state: json.state, rev: json.rev };
}
