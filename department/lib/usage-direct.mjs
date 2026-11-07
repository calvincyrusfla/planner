// Usage meter "Update" button: fetch live usage straight from Anthropic with the
// login Claude Code saved on this computer, and turn it into a reportUsage op.
//
// Token rules (non-negotiable): the OAuth access token is read only at the
// moment of a refresh, lives in a local variable, is sent only in the
// Authorization header to Anthropic (or a loopback test server set with
// DEPT_ANTHROPIC_BASE), and never appears in an error, log, return value or
// the board. We never refresh an expired token and never write the
// credentials file. Errors carry only short fixed messages and a code.
//
// The endpoint (GET /api/oauth/usage) is unofficial and may change.
//
// Every dependency is injectable so tests never touch real credentials or the
// network.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { DeptError, usageTimeToIso, USAGE_SOURCE_MAX } from './core.mjs';

export const ANTHROPIC_BASE = 'https://api.anthropic.com';
export const USAGE_PATH = '/api/oauth/usage';
export const OAUTH_BETA = 'oauth-2025-04-20';
export const DEFAULT_TIMEOUT_MS = 10_000;
export const KEYCHAIN_SERVICE = 'Claude Code-credentials';
const KEYCHAIN_TIMEOUT_MS = 5_000;
const SOURCE_SUFFIX = ' · Anthropic';

export const MESSAGES = Object.freeze({
  disabled: 'Usage refresh is turned off on this computer (DEPT_USAGE_DIRECT=off).',
  badBase: 'DEPT_ANTHROPIC_BASE must point at api.anthropic.com or this computer.',
  noLogin: 'No Claude Code login found on this computer — sign in to Claude Code once, then try again.',
  keychain: "Couldn't read the Claude Code login from the macOS Keychain.",
  expired: 'Your Claude login has expired — open Claude Code once to renew it, then try again.',
  rateLimited: 'Anthropic is rate-limiting usage checks; try again in a minute.',
  unreachable: "Couldn't reach Anthropic.",
  noData: "Anthropic didn't return usage data (the unofficial endpoint may have changed)",
  badResponse: (status) => `Anthropic returned an unexpected response${status ? ` (HTTP ${status})` : ''}.`,
});

// HTTP status a server should answer with for each code.
export const CODE_STATUS = Object.freeze({
  disabled: 403,
  no_login: 401,
  expired: 401,
  rate_limited: 429,
  unreachable: 502,
  bad_response: 502,
});

export class UsageDirectError extends DeptError {
  constructor(code, message) {
    super(message, CODE_STATUS[code] || 502);
    this.name = 'UsageDirectError';
    this.code = code;
  }
}
const fail = (code, message) => new UsageDirectError(code, message);

export function isUsageDirectEnabled(env = process.env) {
  const v = String(env?.DEPT_USAGE_DIRECT ?? '').trim().toLowerCase();
  return !['off', '0', 'false', 'no', 'disabled'].includes(v);
}

// <config>/.credentials.json: CLAUDE_CONFIG_DIR, else %USERPROFILE%\.claude on
// Windows, else ~/.claude.
export function credentialsPath({ env = process.env, platform = process.platform, homedir } = {}) {
  const p = platform === 'win32' ? path.win32 : path.posix;
  const configDir = typeof env.CLAUDE_CONFIG_DIR === 'string' ? env.CLAUDE_CONFIG_DIR.trim() : '';
  if (configDir) return p.join(configDir, '.credentials.json');
  let home;
  if (platform === 'win32') {
    home = env.USERPROFILE || (env.HOMEDRIVE && env.HOMEPATH ? env.HOMEDRIVE + env.HOMEPATH : '');
  } else home = env.HOME;
  if (!home) home = typeof homedir === 'function' ? homedir() : homedir || os.homedir();
  return p.join(home, '.claude', '.credentials.json');
}

function defaultExec(cmd, args) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: KEYCHAIN_TIMEOUT_MS, encoding: 'utf8', windowsHide: true }, (err, stdout) => {
      // Never pass on err: its message can include the command's output.
      if (err) reject(new Error('exec failed'));
      else resolve(stdout);
    });
  });
}

// Credentials JSON text -> {token, expiresAt}. Throws coded errors only.
function parseCredentials(text) {
  let data;
  try {
    data = JSON.parse(String(text));
  } catch {
    throw fail('no_login', MESSAGES.noLogin);
  }
  const oauth = data && typeof data === 'object' ? data.claudeAiOauth : null;
  if (!oauth || typeof oauth !== 'object') throw fail('no_login', MESSAGES.noLogin);
  const token = typeof oauth.accessToken === 'string' ? oauth.accessToken.trim() : '';
  if (!token) throw fail('expired', MESSAGES.expired);
  const expiresIso = usageTimeToIso(oauth.expiresAt);
  return { token, expiresAt: expiresIso ? Date.parse(expiresIso) : null };
}

// Returns {token, expiresAt} (expiresAt epoch ms or null when unknown).
// The caller must keep the token in a local variable only.
export async function readClaudeToken({
  env = process.env,
  platform = process.platform,
  fsImpl = fs,
  execImpl = defaultExec,
  homedir,
  now = Date.now,
} = {}) {
  let creds = null;
  let keychainFailed = false;
  if (platform === 'darwin') {
    try {
      const out = await execImpl('security', ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-w']);
      creds = parseCredentials(out);
    } catch {
      keychainFailed = true;
    }
  }
  if (!creds) {
    let text;
    try {
      text = fsImpl.readFileSync(credentialsPath({ env, platform, homedir }), 'utf8');
    } catch {
      throw keychainFailed ? fail('no_login', MESSAGES.keychain) : fail('no_login', MESSAGES.noLogin);
    }
    creds = parseCredentials(text);
  }
  const at = typeof now === 'function' ? now() : Number(now);
  if (creds.expiresAt !== null && creds.expiresAt <= at) throw fail('expired', MESSAGES.expired);
  return creds;
}

// The base URL may be overridden (tests) only to api.anthropic.com or loopback,
// so the token can't be sent anywhere else.
export function resolveBase(base) {
  const raw = typeof base === 'string' && base.trim() ? base.trim() : ANTHROPIC_BASE;
  let u;
  try {
    u = new URL(raw);
  } catch {
    throw fail('disabled', MESSAGES.badBase);
  }
  const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  const loopback = host === 'localhost' || host === '::1' || /^127(\.\d{1,3}){3}$/.test(host);
  const ok = (u.protocol === 'https:' && host === 'api.anthropic.com') || ((u.protocol === 'http:' || u.protocol === 'https:') && loopback);
  if (!ok || u.username || u.password) throw fail('disabled', MESSAGES.badBase);
  return u.origin;
}

// GET the usage JSON. Throws coded errors with short messages; never includes
// the response body or the token.
export async function fetchAnthropicUsage({ token, fetchImpl = globalThis.fetch, base, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  if (typeof token !== 'string' || !token) throw fail('expired', MESSAGES.expired);
  const url = resolveBase(base) + USAGE_PATH;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetchImpl(url, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}`, 'anthropic-beta': OAUTH_BETA, Accept: 'application/json' },
      redirect: 'error',
      signal: ctrl.signal,
    });
  } catch {
    clearTimeout(timer);
    throw fail('unreachable', MESSAGES.unreachable);
  }
  try {
    const status = Number(res?.status) || 0;
    if (status === 401 || status === 403) throw fail('expired', MESSAGES.expired);
    if (status === 429) throw fail('rate_limited', MESSAGES.rateLimited);
    if (status < 200 || status >= 300) throw fail('bad_response', MESSAGES.badResponse(status));
    let json;
    try {
      json = await res.json();
    } catch (err) {
      if (ctrl.signal.aborted) throw fail('unreachable', MESSAGES.unreachable);
      throw fail('bad_response', MESSAGES.noData);
    }
    if (!json || typeof json !== 'object' || Array.isArray(json)) throw fail('bad_response', MESSAGES.noData);
    return json;
  } catch (err) {
    if (err instanceof UsageDirectError) throw err;
    throw fail(ctrl.signal.aborted ? 'unreachable' : 'bad_response', ctrl.signal.aborted ? MESSAGES.unreachable : MESSAGES.badResponse());
  } finally {
    clearTimeout(timer);
  }
}

function apiWindow(win) {
  if (!win || typeof win !== 'object' || Array.isArray(win)) return null;
  const raw = win.utilization;
  const n = typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : raw;
  if (typeof n !== 'number' || !Number.isFinite(n)) return null;
  const usedPct = Math.round(Math.max(0, Math.min(100, n)) * 10) / 10;
  return { usedPct, resetsAt: usageTimeToIso(win.resets_at ?? win.resetsAt) };
}

export function defaultSource(hostname = os.hostname) {
  let host = '';
  try {
    host = String((typeof hostname === 'function' ? hostname() : hostname) || '');
  } catch { /* no hostname */ }
  host = host.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim() || 'PC';
  return host.slice(0, USAGE_SOURCE_MAX - SOURCE_SUFFIX.length).trim() + SOURCE_SUFFIX;
}

// Anthropic's JSON -> {type: 'reportUsage', args}. Only the two windows are
// read; extra keys are ignored.
export function toReport(apiJson, { source, now = Date.now } = {}) {
  const json = apiJson && typeof apiJson === 'object' && !Array.isArray(apiJson) ? apiJson : {};
  const fiveHour = apiWindow(json.five_hour);
  const sevenDay = apiWindow(json.seven_day);
  if (!fiveHour && !sevenDay) throw fail('bad_response', MESSAGES.noData);
  const at = typeof now === 'function' ? now() : now;
  return {
    type: 'reportUsage',
    args: {
      fiveHour,
      sevenDay,
      reportedAt: usageTimeToIso(at instanceof Date ? at : Number(at)) || new Date().toISOString(),
      source: typeof source === 'string' && source.trim() ? source.trim().slice(0, USAGE_SOURCE_MAX) : defaultSource(),
    },
  };
}

// Read the token, fetch, map. Resolves to the reportUsage op; rejects with a
// UsageDirectError (any other failure is mapped to a generic one so nothing
// unexpected — and never the token — reaches the caller).
// deps: {env, platform, fsImpl, execImpl, homedir, fetchImpl, base, timeoutMs, now, hostname, source}
export async function refreshUsageFromAnthropic(deps = {}) {
  const env = deps.env || process.env;
  if (!isUsageDirectEnabled(env)) throw fail('disabled', MESSAGES.disabled);
  try {
    const base = deps.base ?? env.DEPT_ANTHROPIC_BASE;
    resolveBase(base); // refuse a bad base before reading the login
    const { token } = await readClaudeToken({ ...deps, env });
    const json = await fetchAnthropicUsage({ token, fetchImpl: deps.fetchImpl, base, timeoutMs: deps.timeoutMs });
    return toReport(json, { source: deps.source || defaultSource(deps.hostname), now: deps.now });
  } catch (err) {
    if (err instanceof UsageDirectError) throw fail(err.code, err.message);
    throw fail('bad_response', MESSAGES.badResponse());
  }
}
