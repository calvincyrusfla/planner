// Foundry update requests: a Claude session asks Foundry on the user's PC to
// pull its base branch, apply allowlisted settings and restart itself, through
// the board. Modelled on Foundry batches (lib/foundry-batches.mjs).
//
// state.foundryUpdates: [request], newest last (the newest 50 are kept).
// state.nextUpdateNumber: next U-###.
// request = {
//   id: "U-001", repo: "owner/name", settings: {FOUNDRY_KEY: "value"}, now: boolean,
//   status: "queued"|"claimed"|"done"|"refused"|"failed"|"cancelled",
//   createdAt, createdBy, claimedBy: {host, service, at} | null, finishedAt: iso|null,
//   result: {commitBefore, commitAfter, message} | null,
// }

import { DeptError, clockNow } from './core.mjs';

export const UPDATE_STATUSES = ['queued', 'claimed', 'done', 'refused', 'failed', 'cancelled'];
export const OPEN_UPDATE = new Set(['queued', 'claimed']);
export const FINISH_STATUSES = ['done', 'refused', 'failed'];
export const UPDATE_LIMITS = { keep: 50, settings: 20, value: 200, message: 2000, commit: 200 };
export const SETTING_KEY_RE = /^FOUNDRY_[A-Z0-9_]+$/;

const LOG_LIMIT = 500;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const now = () => new Date(clockNow()).toISOString();
const updateNumber = (n) => `U-${String(n).padStart(3, '0')}`;
const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
const bad = (msg) => new DeptError(msg, 400);

function log(state, updateId, message) {
  if (!Array.isArray(state.log)) state.log = [];
  state.log.push({ at: now(), kind: 'foundry', updateId, message });
  if (state.log.length > LOG_LIMIT) state.log.splice(0, state.log.length - LOG_LIMIT);
}

function text(value, name, max, { required = false } = {}) {
  if (value === undefined || value === null) value = '';
  if (typeof value !== 'string') throw bad(`${name} must be text`);
  const v = value.trim();
  if (required && !v) throw bad(`${name} is required`);
  if (v.length > max) throw bad(`${name} is too long (max ${max} characters)`);
  return v;
}

// Same check as dispatchBatch's repo.
function repoSlug(value) {
  const v = text(value, 'repo', 200, { required: true }).replace(/\.git$/i, '');
  if (!REPO_RE.test(v) || v.split('/').some((p) => p === '.' || p === '..')) throw bad('repo must look like owner/name');
  return v;
}

export function cleanSettings(raw) {
  if (raw === undefined || raw === null) return {};
  if (!isObj(raw)) throw bad('settings must be an object of KEY: value');
  const entries = Object.entries(raw);
  if (entries.length > UPDATE_LIMITS.settings) throw bad(`settings: at most ${UPDATE_LIMITS.settings} keys`);
  const out = {};
  for (const [key, value] of entries) {
    if (!SETTING_KEY_RE.test(key)) throw bad(`setting ${key} is not allowed: keys look like FOUNDRY_NAME (capitals, digits, _)`);
    let v = value;
    if (typeof v === 'number') {
      if (!Number.isFinite(v)) throw bad(`setting ${key} must be a string`);
      v = String(v);
    } else if (typeof v === 'boolean') v = String(v);
    if (typeof v !== 'string') throw bad(`setting ${key} must be a string`);
    if (v.length > UPDATE_LIMITS.value) throw bad(`setting ${key} is too long (max ${UPDATE_LIMITS.value} characters)`);
    out[key] = v;
  }
  return out;
}

function resultText(value, name, max) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') throw bad(`result ${name} must be text`);
  return value.slice(0, max);
}

function cleanResult(raw) {
  if (raw === undefined || raw === null) return { commitBefore: null, commitAfter: null, message: null };
  if (!isObj(raw)) throw bad('result must be an object');
  return {
    commitBefore: resultText(raw.commitBefore, 'commitBefore', UPDATE_LIMITS.commit),
    commitAfter: resultText(raw.commitAfter, 'commitAfter', UPDATE_LIMITS.commit),
    message: resultText(raw.message, 'message', UPDATE_LIMITS.message),
  };
}

export function normalizeUpdates(state) {
  if (!Array.isArray(state.foundryUpdates)) state.foundryUpdates = [];
  state.foundryUpdates = state.foundryUpdates.filter((u) => isObj(u) && typeof u.id === 'string');
  const highest = state.foundryUpdates.reduce((m, u) => Math.max(m, Number(/^U-(\d+)$/.exec(u.id)?.[1]) || 0), 0);
  if (!Number.isInteger(state.nextUpdateNumber) || state.nextUpdateNumber <= highest) state.nextUpdateNumber = highest + 1;
  return state;
}

// Keep the newest 50: the oldest finished requests go first.
function prune(state) {
  let extra = state.foundryUpdates.length - UPDATE_LIMITS.keep;
  if (extra <= 0) return;
  state.foundryUpdates = state.foundryUpdates.filter((u) => {
    if (extra > 0 && !OPEN_UPDATE.has(u.status)) {
      extra--;
      return false;
    }
    return true;
  });
  if (state.foundryUpdates.length > UPDATE_LIMITS.keep) state.foundryUpdates = state.foundryUpdates.slice(-UPDATE_LIMITS.keep);
}

export function getFoundryUpdate(state, id) {
  normalizeUpdates(state);
  const key = String(id ?? '').trim().toUpperCase();
  const u = state.foundryUpdates.find((x) => x.id === key);
  if (!u) throw new DeptError(`No Foundry update request ${id}`, 404);
  return u;
}

// The newest request for repo (or the newest of all without one), else null.
export function latestFoundryUpdate(state, repo) {
  normalizeUpdates(state);
  const want = repo === undefined || repo === null || repo === '' ? null : String(repo).trim().replace(/\.git$/i, '');
  for (let i = state.foundryUpdates.length - 1; i >= 0; i--) {
    const u = state.foundryUpdates[i];
    if (!want || u.repo === want) return u;
  }
  return null;
}

export function requestFoundryUpdate(state, { repo, settings, now: immediate, by } = {}) {
  normalizeUpdates(state);
  const slug = repoSlug(repo);
  const clean = cleanSettings(settings);
  const open = state.foundryUpdates.find((u) => u.repo === slug && OPEN_UPDATE.has(u.status));
  if (open) throw new DeptError(`${slug} already has an open update request ${open.id} (${open.status}); wait for it or cancel it first`, 409);
  const u = {
    id: updateNumber(state.nextUpdateNumber),
    repo: slug,
    settings: clean,
    now: Boolean(immediate),
    status: 'queued',
    createdAt: now(),
    createdBy: text(by, 'by', 100) || 'floor-manager',
    claimedBy: null,
    finishedAt: null,
    result: null,
  };
  state.nextUpdateNumber++;
  state.foundryUpdates.push(u);
  prune(state);
  const keys = Object.keys(clean);
  log(state, u.id, `${u.id} update requested for ${slug} by ${u.createdBy}${keys.length ? ` (${keys.join(', ')})` : ''}`);
  return u;
}

export function claimFoundryUpdate(state, { id, host, service } = {}) {
  const u = getFoundryUpdate(state, id);
  const h = text(host, 'host', 200, { required: true });
  const svc = text(service, 'service', 200, { required: true });
  if (u.status !== 'queued') throw new DeptError(`${u.id} is ${u.status}, not queued`, 409);
  u.status = 'claimed';
  u.claimedBy = { host: h, service: svc, at: now() };
  log(state, u.id, `${u.id} claimed by ${svc} on ${h}`);
  return { ok: true, update: u };
}

export function finishFoundryUpdate(state, { id, service, status, result } = {}) {
  const u = getFoundryUpdate(state, id);
  const svc = text(service, 'service', 200, { required: true });
  if (!FINISH_STATUSES.includes(status)) throw bad(`status must be one of ${FINISH_STATUSES.join(', ')}`);
  if (u.status !== 'claimed') throw new DeptError(`${u.id} is ${u.status}, not claimed`, 409);
  if (!u.claimedBy || u.claimedBy.service !== svc) throw new DeptError(`${u.id} is not claimed by ${svc}`, 409);
  const clean = cleanResult(result);
  u.status = status;
  u.result = clean;
  u.finishedAt = now();
  log(state, u.id, `${u.id} ${status}${clean.message ? `: ${clean.message.slice(0, 200)}` : ''}`);
  return { ok: true, update: u };
}

export function cancelFoundryUpdate(state, { id, by } = {}) {
  const u = getFoundryUpdate(state, id);
  if (!OPEN_UPDATE.has(u.status)) throw new DeptError(`${u.id} is already ${u.status}`, 409);
  u.status = 'cancelled';
  u.finishedAt = now();
  log(state, u.id, `${u.id} cancelled by ${text(by, 'by', 100) || 'user'}`);
  return { ok: true, update: u };
}
