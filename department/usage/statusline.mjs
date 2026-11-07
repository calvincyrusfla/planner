#!/usr/bin/env node
// Claude Code statusLine command for the usage meter.
//   "statusLine": {"type": "command", "command": "\"<node>\" \"<repo>/department/usage/statusline.mjs\""}
// Claude Code passes session JSON on stdin; on subscription plans it includes
//   rate_limits: { five_hour: { used_percentage, resets_at }, seven_day: { ... } }
// (resets_at is epoch seconds or an ISO string; either window, or the whole
// object, may be missing). This prints ONE short line for the status bar and,
// when the numbers changed or a minute has passed since the last send, hands
// them to report.mjs in a detached background process. It never waits for the
// network and never reads credentials.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const REPORT_SCRIPT = path.join(here, 'report.mjs');
export const RESEND_MS = 60_000;
const STDIN_WAIT_MS = 1000;

// Same data dir as lib/store.mjs (DEPT_DATA's folder, else department/data),
// computed here so the status line doesn't have to load the core.
export const dataDir = () =>
  process.env.DEPT_DATA ? path.dirname(path.resolve(process.env.DEPT_DATA)) : path.join(here, '..', 'data');
export const cacheFile = () => path.join(dataDir(), 'usage-last.json');

// epoch seconds | epoch ms | numeric string | ISO string -> ISO string | null
export function toIso(v) {
  if (v === null || v === undefined || v === '') return null;
  let ms;
  if (typeof v === 'number' || (typeof v === 'string' && /^\s*\d+(\.\d+)?\s*$/.test(v))) {
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return null;
    ms = n < 1e12 ? n * 1000 : n; // seconds below ~2001-09 in ms terms
  } else if (typeof v === 'string') {
    ms = Date.parse(v);
  } else return null;
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

function readWindow(w) {
  if (!w || typeof w !== 'object') return null;
  const raw = w.used_percentage ?? w.usedPct;
  if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') return null;
  const pct = Number(raw);
  if (!Number.isFinite(pct)) return null;
  const usedPct = Math.round(Math.min(100, Math.max(0, pct)) * 10) / 10;
  return { usedPct, resetsAt: toIso(w.resets_at ?? w.resetsAt) };
}

// Session JSON -> { fiveHour, sevenDay } or null when there is nothing to report.
export function readUsage(input) {
  const rl = input && typeof input === 'object' ? input.rate_limits : null;
  if (!rl || typeof rl !== 'object') return null;
  const fiveHour = readWindow(rl.five_hour);
  const sevenDay = readWindow(rl.seven_day);
  return fiveHour || sevenDay ? { fiveHour, sevenDay } : null;
}

// "3:10pm" in local time
function clock(iso) {
  const d = new Date(iso);
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'pm' : 'am';
  h = h % 12 || 12;
  return `${h}:${m}${ap}`;
}

const pct = (n) => `${Math.round(n)}%`;

export function formatLine(input, now = Date.now()) {
  const parts = [];
  const usage = readUsage(input);
  if (usage?.fiveHour) {
    const { usedPct, resetsAt } = usage.fiveHour;
    const future = resetsAt && Date.parse(resetsAt) > now;
    parts.push(`5h ${pct(usedPct)}${future ? ` (resets ${clock(resetsAt)})` : ''}`);
  }
  if (usage?.sevenDay) parts.push(`week ${pct(usage.sevenDay.usedPct)}`);
  const model = input && typeof input === 'object' && input.model && typeof input.model === 'object'
    ? String(input.model.display_name || input.model.id || '').trim()
    : '';
  if (model) parts.push(model.slice(0, 40));
  return parts.join(' · ') || 'Claude';
}

// Values that matter for "did it change" (reportedAt/source excluded).
export const usageKey = (u) => JSON.stringify([
  u.fiveHour?.usedPct ?? null, u.fiveHour?.resetsAt ?? null,
  u.sevenDay?.usedPct ?? null, u.sevenDay?.resetsAt ?? null,
]);

function readCache() {
  try {
    const c = JSON.parse(fs.readFileSync(cacheFile(), 'utf8'));
    return c && typeof c === 'object' ? c : null;
  } catch {
    return null;
  }
}

export function shouldSend(usage, cache, now = Date.now()) {
  if (!usage) return false;
  if (!cache || typeof cache.key !== 'string' || !Number.isFinite(cache.sentAt)) return true;
  if (cache.key !== usageKey(usage)) return true;
  return now - cache.sentAt >= RESEND_MS || now < cache.sentAt; // clock went back: send
}

function writeCache(usage, now) {
  const file = cacheFile();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify({ key: usageKey(usage), sentAt: now, sentAtIso: new Date(now).toISOString() }));
  fs.renameSync(tmp, file);
}

function sendDetached(payload) {
  const child = spawn(process.execPath, [REPORT_SCRIPT], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
    env: { ...process.env, DEPT_USAGE_PAYLOAD: JSON.stringify(payload) },
  });
  child.on('error', () => {});
  child.unref();
}

// Throttled hand-off to report.mjs. Never throws.
export function maybeReport(input, now = Date.now()) {
  try {
    const usage = readUsage(input);
    if (!shouldSend(usage, readCache(), now)) return false;
    writeCache(usage, now); // first, so status lines started right after this one don't send again
    sendDetached({ ...usage, reportedAt: new Date(now).toISOString() });
    return true;
  } catch {
    return false;
  }
}

function main() {
  let raw = '';
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    let input = null;
    try {
      input = raw.trim() ? JSON.parse(raw) : null;
    } catch {
      input = null;
    }
    let line = 'Claude';
    try {
      line = formatLine(input);
    } catch { /* keep the fallback */ }
    process.stdout.write(line + '\n');
    if (process.env.DEPT_USAGE_SEND !== 'off') maybeReport(input);
    process.exitCode = 0;
    // Don't let a stdin that never closes keep us alive.
    process.stdin.destroy();
  };
  if (process.stdin.isTTY) return finish();
  setTimeout(finish, STDIN_WAIT_MS).unref();
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => {
    raw += c;
    if (raw.length > 1e6) finish();
  });
  process.stdin.on('end', finish);
  process.stdin.on('error', finish);
}

const isMain = (() => {
  try {
    return !!process.argv[1] && pathToFileURL(fs.realpathSync(process.argv[1])).href === pathToFileURL(fs.realpathSync(fileURLToPath(import.meta.url))).href;
  } catch {
    return false;
  }
})();
if (isMain) main();
