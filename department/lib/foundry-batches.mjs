// Foundry batches ("sprints"): engineering work the Floor Manager hands to
// Foundry through the board. A batch is NOT a board task (it never takes an
// agent team). Foundry on the user's PC claims batches for its repo, turns each
// subtask into a Foundry order and reports results back onto the batch.
//
// state.foundryBatches: [batch], newest last. state.nextBatchNumber: next S-###.
// batch = {
//   id: "S-001", title, goal, repo: "owner/name", base: branch|null,
//   createdAt, createdBy, updatedAt, finishedAt: iso|null,
//   status: "queued"|"claimed"|"running"|"done"|"failed"|"cancelled",
//   claimedBy: {host, service, at} | null, heartbeatAt: iso|null, note: string,
//   subtasks: [{key, title, description, files: [], size: "S"|"M"|"L"|"XL"|null,
//               acceptance: [], dependsOn: [keys], readFiles: []}],
//   results: {[key]: {foundryTask: "F-012"|null, state, pr: {state,url,branch,error}|null,
//                     error: string|null, updatedAt, usage?: {...}|null,
//                     help?: {question, context, options: [], answer?, answeredBy?, answeredAt?}}},
// }
// Log entries go to state.log with kind "foundry" and batchId: dispatch, claims,
// batch status changes and cancel only (not every result update).

import { DeptError, clockNow } from './core.mjs';
import { cleanUsage } from './foundry-metrics.mjs';

export const BATCH_STATUSES = ['queued', 'claimed', 'running', 'done', 'failed', 'cancelled'];
export const RESULT_STATES = ['queued', 'sizing', 'building', 'reviewing', 'needs_help', 'answered', 'complete', 'quarantined', 'failed'];
export const TERMINAL_RESULT = new Set(['complete', 'quarantined', 'failed']);
export const OPEN_BATCH = new Set(['queued', 'claimed', 'running']);
export const SIZES = ['S', 'M', 'L', 'XL'];
export const LIMITS = Object.freeze({
  subtasks: 20,
  title: 200,
  goal: 20000,
  description: 20000,
  // Longest order text the builder sees whole; must match MAX_ORDER_DETAILS_CHARS
  // in foundry/builder_worker.py. Checked on new dispatches only.
  builderOrder: 6000,
  files: 50,
  path: 500,
  acceptance: 50,
  acceptanceItem: 2000,
  error: 2000,
  note: 2000,
  help: 4000,
  helpOptions: 20,
  keep: 200, // batches kept on the board; the oldest finished ones go first
});
export const TAKEOVER_MS = 10 * 60 * 1000;

const LOG_LIMIT = 500;
const now = () => new Date(clockNow()).toISOString();
const batchNumber = (n) => `S-${String(n).padStart(3, '0')}`;
const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
const bad = (msg) => new DeptError(msg, 400);

function log(state, batchId, message) {
  state.log ||= [];
  state.log.push({ at: now(), kind: 'foundry', batchId, message });
  if (state.log.length > LOG_LIMIT) state.log.splice(0, state.log.length - LOG_LIMIT);
}

// ---------- validation ----------

function text(value, name, max, { required = false } = {}) {
  if (value === undefined || value === null) value = '';
  if (typeof value !== 'string') throw bad(`${name} must be text`);
  const v = value.trim();
  if (required && !v) throw bad(`${name} is required`);
  if (v.length > max) throw bad(`${name} is too long (max ${max} characters)`);
  return v;
}

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
function repoSlug(value) {
  const v = text(value, 'repo', 200, { required: true }).replace(/\.git$/i, '');
  if (!REPO_RE.test(v) || v.split('/').some((p) => p === '.' || p === '..')) throw bad('repo must look like owner/name');
  return v;
}

function branch(value) {
  const v = text(value, 'base', 200);
  if (!v) return null;
  if (!/^[A-Za-z0-9_./-]+$/.test(v) || v.includes('..') || v.startsWith('/') || v.startsWith('-') || v.endsWith('/')) {
    throw bad('base must be a branch name');
  }
  return v;
}

function relPath(value, name) {
  if (typeof value !== 'string') throw bad(`${name} must be text`);
  const v = value.trim().replace(/\\/g, '/');
  if (!v) throw bad(`${name} is empty`);
  if (v.length > LIMITS.path) throw bad(`${name} is too long`);
  if (v.startsWith('/') || /^[A-Za-z]:/.test(v) || v.startsWith('~')) throw bad(`${name} must be a relative path: ${v}`);
  if (v.split('/').some((p) => p === '..')) throw bad(`${name} must not contain "..": ${v}`);
  if (/[\u0000-\u001f]/.test(v)) throw bad(`${name} has control characters`);
  return v;
}

function list(value, name, max) {
  if (value === undefined || value === null) return [];
  if (typeof value === 'string') value = value.split(',').map((s) => s.trim()).filter(Boolean);
  if (!Array.isArray(value)) throw bad(`${name} must be a list`);
  if (value.length > max) throw bad(`${name}: at most ${max} entries`);
  return value;
}

const KEY_RE = /^[A-Za-z0-9_.-]{1,40}$/;

function cleanSubtasks(raw) {
  if (!Array.isArray(raw) || !raw.length) throw bad('a batch needs at least one subtask');
  if (raw.length > LIMITS.subtasks) throw bad(`a batch takes at most ${LIMITS.subtasks} subtasks`);
  const keys = new Set();
  const out = raw.map((st, i) => {
    if (!isObj(st)) throw bad(`subtask ${i + 1} must be an object`);
    const n = `subtask ${i + 1}`;
    const key = st.key === undefined || st.key === null || st.key === '' ? String(i + 1) : String(st.key).trim();
    if (!KEY_RE.test(key)) throw bad(`${n}: key must be 1-40 letters, digits, "_", "-" or "."`);
    if (keys.has(key)) throw bad(`${n}: duplicate key ${key}`);
    keys.add(key);
    let size = st.size ?? st.size_hint ?? null;
    if (size !== null && size !== '') {
      size = String(size).trim().toUpperCase();
      if (!SIZES.includes(size)) throw bad(`${n}: size must be one of ${SIZES.join(', ')}`);
    } else size = null;
    const acceptance = list(st.acceptance ?? st.acceptance_criteria, `${n} acceptance`, LIMITS.acceptance).map((a, j) =>
      text(a, `${n} acceptance ${j + 1}`, LIMITS.acceptanceItem, { required: true }),
    );
    const files = list(st.files, `${n} files`, LIMITS.files).map((f) => relPath(f, `${n} file`));
    const readFiles = list(st.readFiles ?? st.read_files, `${n} readFiles`, LIMITS.files).map((f) => relPath(f, `${n} read file`));
    return {
      key,
      title: text(st.title, `${n} title`, LIMITS.title, { required: true }),
      description: text(st.description, `${n} description`, LIMITS.description),
      files: [...new Set(files)],
      readFiles: [...new Set(readFiles)],
      size,
      acceptance,
      dependsOn: list(st.dependsOn ?? st.depends_on, `${n} dependsOn`, LIMITS.subtasks).map((d) => String(d).trim()),
    };
  });
  for (const st of out) {
    for (const d of st.dependsOn) {
      if (d === st.key) throw bad(`subtask ${st.key} depends on itself`);
      if (!keys.has(d)) throw bad(`subtask ${st.key} depends on unknown subtask ${d}`);
    }
    st.dependsOn = [...new Set(st.dependsOn)];
  }
  // No cycles.
  const byKey = new Map(out.map((s) => [s.key, s]));
  const mark = new Map();
  const visit = (k) => {
    if (mark.get(k) === 2) return;
    if (mark.get(k) === 1) throw bad(`subtask dependencies form a cycle at ${k}`);
    mark.set(k, 1);
    for (const d of byKey.get(k).dependsOn) visit(d);
    mark.set(k, 2);
  };
  for (const k of byKey.keys()) visit(k);
  return out;
}

function cleanPr(pr) {
  if (pr === undefined) return undefined;
  if (pr === null) return null;
  if (!isObj(pr)) throw bad('pr must be an object');
  const url = text(pr.url, 'pr url', 1000);
  return {
    state: text(pr.state, 'pr state', 40) || null,
    url: /^https?:\/\//i.test(url) ? url : null,
    branch: text(pr.branch, 'pr branch', 300) || null,
    error: text(pr.error, 'pr error', LIMITS.error) || null,
  };
}

// What a subtask asks the user when it is stuck. The answer fields are set only
// by answerHelp, so anything else a worker sends is dropped here.
function cleanHelp(help, name) {
  if (help === null) return null;
  if (!isObj(help)) throw bad(`${name} must be an object`);
  return {
    question: text(help.question, `${name} question`, LIMITS.help, { required: true }),
    context: text(help.context, `${name} context`, LIMITS.help),
    options: list(help.options, `${name} options`, LIMITS.helpOptions).map((o, i) =>
      text(o, `${name} option ${i + 1}`, LIMITS.help, { required: true }),
    ),
  };
}

function cleanResult(batch, key, partial) {
  if (!batch.subtasks.some((s) => s.key === key)) throw bad(`batch ${batch.id} has no subtask ${key}`);
  if (!isObj(partial)) throw bad(`result for ${key} must be an object`);
  const out = {};
  if (partial.foundryTask !== undefined) {
    const f = partial.foundryTask === null ? '' : text(partial.foundryTask, 'foundryTask', 40);
    if (f && !/^[A-Za-z]+-\d+$/.test(f)) throw bad('foundryTask must look like F-012');
    out.foundryTask = f || null;
  }
  if (partial.state !== undefined) {
    if (!RESULT_STATES.includes(partial.state)) throw bad(`result state must be one of ${RESULT_STATES.join(', ')}`);
    out.state = partial.state;
  }
  const pr = cleanPr(partial.pr);
  if (pr !== undefined) out.pr = pr;
  if (partial.error !== undefined) out.error = partial.error === null ? null : text(partial.error, 'error', LIMITS.error) || null;
  if (partial.usage !== undefined) out.usage = cleanUsage(partial.usage, `result for ${key} usage`);
  if (partial.help !== undefined) out.help = cleanHelp(partial.help, `result for ${key} help`);
  if (out.state === 'needs_help' && !out.help) throw bad(`result for ${key}: needs_help requires a help object`);
  return out;
}

// ---------- lookups / housekeeping ----------

export function normalizeBatches(state) {
  if (!Array.isArray(state.foundryBatches)) state.foundryBatches = [];
  state.foundryBatches = state.foundryBatches.filter((b) => isObj(b) && typeof b.id === 'string');
  for (const b of state.foundryBatches) {
    if (!BATCH_STATUSES.includes(b.status)) b.status = 'queued';
    // Junk subtasks (e.g. from an imported board) are dropped so updateBatch can't trip on them.
    b.subtasks = (Array.isArray(b.subtasks) ? b.subtasks : []).filter(
      (st) => isObj(st) && typeof st.key === 'string' && st.key !== '',
    );
    if (!isObj(b.results)) b.results = {};
    b.claimedBy = isObj(b.claimedBy) ? b.claimedBy : null;
    b.heartbeatAt ??= null;
    b.finishedAt ??= null;
  }
  const highest = state.foundryBatches.reduce((m, b) => Math.max(m, Number(/^S-(\d+)$/.exec(b.id)?.[1]) || 0), 0);
  if (!Number.isInteger(state.nextBatchNumber) || state.nextBatchNumber <= highest) state.nextBatchNumber = highest + 1;
  return state;
}

export function getBatch(state, id) {
  normalizeBatches(state);
  const key = String(id ?? '').trim().toUpperCase();
  const b = state.foundryBatches.find((x) => x.id === key);
  if (!b) throw new DeptError(`No Foundry batch ${id}`, 404);
  return b;
}

function prune(state) {
  const extra = state.foundryBatches.length - LIMITS.keep;
  if (extra <= 0) return;
  let drop = extra;
  state.foundryBatches = state.foundryBatches.filter((b) => {
    if (drop > 0 && !OPEN_BATCH.has(b.status)) {
      drop--;
      return false;
    }
    return true;
  });
}

const lastBeat = (b) => Date.parse(b.heartbeatAt || b.claimedBy?.at || '') || 0;
export const isStale = (b, at = clockNow()) => at - lastBeat(b) > TAKEOVER_MS;

// Subtask counts: {total, complete, failed (failed+quarantined), terminal}.
export function batchProgress(b) {
  const total = b.subtasks.length;
  let complete = 0;
  let failed = 0;
  for (const st of b.subtasks) {
    const s = b.results?.[st.key]?.state;
    if (s === 'complete') complete++;
    else if (s === 'failed' || s === 'quarantined') failed++;
  }
  return { total, complete, failed, terminal: complete + failed };
}

// Room for the marker line board_dispatch.py ends every order with.
const ORDER_MARKER_ALLOWANCE = 100;
const plainText = (v) => (typeof v === 'string' ? v : '');

// Length of the order text as _submit in foundry/board_dispatch.py composes it.
// Counts code points, as Python's len() does when the builder prompt is capped.
export function orderTextLength(batch, subtask) {
  const size = String(subtask?.size ?? subtask?.size_hint ?? '').trim();
  const goal = plainText(batch?.goal).trim();
  let order = plainText(subtask?.description);
  if (size) order = `${order ? `${order}\n\n` : ''}Requested size: ${size}`;
  const context = `Part of "${plainText(batch?.title)}"${goal ? `: ${goal}` : ''}`;
  order = `${order ? `${order}\n\n` : ''}${context}\n`;
  return [...order].length + ORDER_MARKER_ALLOWANCE;
}

// Subtasks whose order text the builder would get cut off: [{key, title, length, limit}].
export function oversizedOrders(batch) {
  const subtasks = Array.isArray(batch?.subtasks) ? batch.subtasks : [];
  return subtasks.flatMap((st, i) => {
    const length = orderTextLength(batch, st);
    if (length <= LIMITS.builderOrder) return [];
    const key = st?.key === undefined || st?.key === null || st?.key === '' ? String(i + 1) : String(st.key).trim();
    return [{ key, title: plainText(st?.title), length, limit: LIMITS.builderOrder }];
  });
}

export const oversizedOrderMessage = ({ key, title, length, limit }) =>
  `subtask ${key} ("${title}") order is ${length} characters; the builder sees only ${limit} (description + goal + title). Shorten it, move detail into readFiles, or split the subtask.`;

// ---------- ops ----------

export function dispatchBatch(state, { title, goal, repo, base, subtasks, by } = {}) {
  normalizeBatches(state);
  if (state.foundryBatches.filter((b) => OPEN_BATCH.has(b.status)).length >= LIMITS.keep) {
    throw new DeptError(`The board already holds ${LIMITS.keep} open Foundry batches; cancel or finish some first`, 409);
  }
  const batch = {
    id: batchNumber(state.nextBatchNumber),
    title: text(title, 'title', LIMITS.title, { required: true }),
    goal: text(goal, 'goal', LIMITS.goal),
    repo: repoSlug(repo),
    base: branch(base),
    createdAt: now(),
    createdBy: text(by, 'by', 100) || 'floor-manager',
    updatedAt: now(),
    finishedAt: null,
    status: 'queued',
    claimedBy: null,
    heartbeatAt: null,
    note: '',
    subtasks: cleanSubtasks(subtasks),
    results: {},
  };
  const oversized = oversizedOrders(batch);
  if (oversized.length) throw bad(oversized.map(oversizedOrderMessage).join('\n'));
  state.nextBatchNumber++;
  state.foundryBatches.push(batch);
  prune(state);
  log(state, batch.id, `${batch.id} dispatched to Foundry (${batch.repo}, ${batch.subtasks.length} subtask${batch.subtasks.length === 1 ? '' : 's'}): ${batch.title}`);
  return batch;
}

// Refusals come back as {ok:false, reason, stored:false} (nothing changed, so
// the hub does not write) rather than errors: losing a race is normal.
export function claimBatch(state, { id, host, service } = {}) {
  const b = getBatch(state, id);
  const h = text(host, 'host', 200, { required: true });
  const svc = text(service, 'service', 200, { required: true });
  const at = clockNow();
  const mine = b.claimedBy && b.claimedBy.service === svc && b.claimedBy.host === h;
  let takeover = false;
  if (b.status === 'queued' || (mine && (b.status === 'claimed' || b.status === 'running'))) {
    // free, or our own claim (restart resumes)
  } else if ((b.status === 'claimed' || b.status === 'running') && isStale(b, at)) {
    takeover = true;
  } else {
    const reason = OPEN_BATCH.has(b.status)
      ? `already claimed by ${b.claimedBy?.service || '?'} on ${b.claimedBy?.host || '?'}`
      : `batch is ${b.status}`;
    return { ok: false, reason, stored: false, batch: b };
  }
  const iso = new Date(at).toISOString();
  const previous = b.claimedBy;
  if (!mine) b.claimedBy = { host: h, service: svc, at: iso };
  if (b.status === 'queued') b.status = 'claimed';
  b.heartbeatAt = iso;
  b.updatedAt = iso;
  if (!mine) {
    log(state, b.id, takeover
      ? `${b.id} taken over by ${svc} on ${h} (no heartbeat from ${previous?.service || '?'} on ${previous?.host || '?'} for 10 min)`
      : `${b.id} claimed by ${svc} on ${h}`);
  }
  return { ok: true, batch: b, takeover };
}

export function updateBatch(state, { id, service, host, status, results, heartbeat, note } = {}) {
  const b = getBatch(state, id);
  const svc = text(service, 'service', 200, { required: true });
  if (!b.claimedBy || b.claimedBy.service !== svc || (host !== undefined && host !== null && b.claimedBy.host !== String(host))) {
    throw new DeptError(`${b.id} is not claimed by ${svc}`, 409);
  }
  if (!OPEN_BATCH.has(b.status)) throw new DeptError(`${b.id} is ${b.status}`, 409);
  if (status !== undefined && status !== null && !['running', 'done', 'failed'].includes(status)) {
    throw bad('status must be running, done or failed');
  }
  if (results !== undefined && results !== null && !isObj(results)) throw bad('results must be an object keyed by subtask');
  // Validate everything before changing anything.
  const cleaned = Object.entries(results || {}).map(([key, partial]) => [key, cleanResult(b, key, partial)]);
  if (cleaned.some(([, patch]) => patch.state === 'answered')) throw bad('only answerHelp can set a result to answered');
  const noteText = note === undefined ? undefined : text(note, 'note', LIMITS.note);
  const iso = now();
  for (const [key, patch] of cleaned) {
    const prev = b.results[key] || { foundryTask: null, state: 'queued', pr: null, error: null };
    const next = { ...prev, ...patch, updatedAt: iso };
    b.results[key] = next;
  }
  if (noteText !== undefined) b.note = noteText;
  if (heartbeat || cleaned.length || status) b.heartbeatAt = iso;
  const before = b.status;
  if (status) b.status = status;
  else if (cleaned.length && b.status === 'claimed') b.status = 'running';
  if (!status || status === 'running') {
    const p = batchProgress(b);
    if (p.total && p.terminal === p.total) b.status = p.failed ? 'failed' : 'done';
  }
  if (b.status === 'done' || b.status === 'failed') b.finishedAt = iso;
  b.updatedAt = iso;
  if (before !== b.status) {
    const p = batchProgress(b);
    log(state, b.id, `${b.id} ${b.status} (${p.complete}/${p.total} complete)${noteText ? `: ${noteText}` : ''}`);
  }
  return { ok: true, batch: b };
}

// The user's reply to a subtask in needs_help. Foundry picks it up from the
// result's help.answer once the state reads answered.
export function answerHelp(state, { id, key, answer, by } = {}) {
  const b = getBatch(state, id);
  if (!OPEN_BATCH.has(b.status)) throw new DeptError(`${b.id} is ${b.status}`, 409);
  const k = String(key ?? '').trim();
  if (!b.subtasks.some((s) => s.key === k)) throw bad(`batch ${b.id} has no subtask ${k}`);
  const result = b.results[k];
  if (result?.state !== 'needs_help') throw new DeptError(`${b.id} subtask ${k} is ${result?.state || 'queued'}, not needs_help`, 409);
  const reply = text(answer, 'answer', LIMITS.help, { required: true });
  const who = text(by, 'by', 100) || 'user';
  const iso = now();
  result.state = 'answered';
  result.help = { ...result.help, answer: reply, answeredBy: who, answeredAt: iso };
  result.updatedAt = iso;
  b.updatedAt = iso;
  log(state, b.id, `${b.id} subtask ${k} answered by ${who}`);
  return { ok: true, batch: b };
}

export function cancelBatch(state, { id, by } = {}) {
  const b = getBatch(state, id);
  if (b.status === 'cancelled') return { ok: true, batch: b, stored: false };
  if (!OPEN_BATCH.has(b.status)) throw new DeptError(`${b.id} is already ${b.status}`, 409);
  b.status = 'cancelled';
  b.finishedAt = b.updatedAt = now();
  log(state, b.id, `${b.id} cancelled by ${text(by, 'by', 100) || 'user'}`);
  return { ok: true, batch: b };
}

// Usage is optional reporting: an imported result with bad usage loses the
// usage, not its whole batch. Returns undefined when there is none to keep.
function importedUsage(usage, name) {
  try {
    return cleanUsage(usage, name);
  } catch (err) {
    if (!(err instanceof DeptError)) throw err;
    return undefined;
  }
}

// An imported batch gets the same checks as dispatchBatch (repo, base, title,
// subtasks with files/limits/keys/deps) plus checks on status, claim and
// results. Returns a clean copy or throws DeptError.
function validateImported(raw) {
  if (!isObj(raw)) throw bad('batch must be an object');
  const id = String(raw.id || '');
  if (!/^S-\d{1,9}$/.test(id)) throw bad('bad batch id');
  if (!BATCH_STATUSES.includes(raw.status)) throw bad(`bad status ${raw.status}`);
  const iso = (v, name) => {
    if (v === undefined || v === null || v === '') return null;
    if (typeof v !== 'string' || !Number.isFinite(Date.parse(v))) throw bad(`${name} must be a time`);
    return v;
  };
  const b = {
    id,
    title: text(raw.title, 'title', LIMITS.title, { required: true }),
    goal: text(raw.goal, 'goal', LIMITS.goal),
    repo: repoSlug(raw.repo),
    base: branch(raw.base),
    createdAt: iso(raw.createdAt, 'createdAt'),
    createdBy: text(raw.createdBy, 'createdBy', 100) || 'floor-manager',
    updatedAt: iso(raw.updatedAt, 'updatedAt'),
    finishedAt: iso(raw.finishedAt, 'finishedAt'),
    status: raw.status,
    claimedBy: null,
    heartbeatAt: iso(raw.heartbeatAt, 'heartbeatAt'),
    note: text(raw.note, 'note', LIMITS.note),
    subtasks: cleanSubtasks(raw.subtasks),
    results: {},
  };
  if (raw.claimedBy !== undefined && raw.claimedBy !== null) {
    if (!isObj(raw.claimedBy)) throw bad('claimedBy must be an object');
    b.claimedBy = {
      host: text(raw.claimedBy.host, 'claimedBy host', 200, { required: true }),
      service: text(raw.claimedBy.service, 'claimedBy service', 200, { required: true }),
      at: iso(raw.claimedBy.at, 'claimedBy at'),
    };
  }
  if (raw.results !== undefined && raw.results !== null) {
    if (!isObj(raw.results)) throw bad('results must be an object');
    for (const [key, r] of Object.entries(raw.results)) {
      const clean = cleanResult(b, key, isObj(r) ? { ...r, usage: undefined } : r);
      const usage = importedUsage(r.usage, `result for ${key} usage`);
      const help = clean.help && {
        ...clean.help,
        ...(r.help.answer ? {
          answer: text(r.help.answer, 'help answer', LIMITS.help),
          answeredBy: text(r.help.answeredBy, 'help answeredBy', 100) || 'user',
          answeredAt: iso(r.help.answeredAt, 'help answeredAt'),
        } : {}),
      };
      b.results[key] = {
        foundryTask: clean.foundryTask ?? null,
        state: clean.state ?? 'queued',
        pr: clean.pr ?? null,
        error: clean.error ?? null,
        ...(usage !== undefined ? { usage } : {}),
        ...(help ? { help } : {}),
        updatedAt: iso(r.updatedAt, 'result updatedAt'),
      };
    }
  }
  return b;
}

// ---------- importState ----------
// Same id + same createdAt = the same batch: the more recently updated copy
// wins. Same id, different batch: appended under a fresh S-###. New ids are kept.
// Every incoming batch is validated first; invalid ones are dropped and listed
// in result.rejected as {id, reason}.
export function importBatches(state, incoming) {
  normalizeBatches(state);
  const result = { added: 0, updated: 0, idMap: {}, rejected: [] };
  const theirs = [];
  for (const raw of Array.isArray(incoming) ? incoming : []) {
    try {
      theirs.push(validateImported(raw));
    } catch (err) {
      if (!(err instanceof DeptError)) throw err;
      result.rejected.push({ id: isObj(raw) ? String(raw.id ?? '').slice(0, 40) : '', reason: err.message });
    }
  }
  const highest = theirs.reduce((m, b) => Math.max(m, Number(/^S-(\d+)$/.exec(b.id)?.[1]) || 0), 0);
  // Reserve numbers above both boards' batches so kept ids and fresh ids never collide.
  if (state.nextBatchNumber <= highest) state.nextBatchNumber = highest + 1;
  for (const raw of theirs) {
    const b = JSON.parse(JSON.stringify(raw));
    const i = state.foundryBatches.findIndex((x) => x.id === b.id);
    if (i === -1) {
      state.foundryBatches.push(b);
      result.added++;
    } else if (state.foundryBatches[i].createdAt === b.createdAt) {
      const ours = state.foundryBatches[i];
      if (String(b.updatedAt || '') > String(ours.updatedAt || '')) {
        state.foundryBatches[i] = b;
        result.updated++;
      }
    } else {
      const newId = batchNumber(state.nextBatchNumber++);
      result.idMap[b.id] = newId;
      b.id = newId;
      state.foundryBatches.push(b);
      result.added++;
    }
  }
  state.foundryBatches.sort((a, b) => String(a.createdAt || '').localeCompare(String(b.createdAt || '')));
  normalizeBatches(state);
  prune(state);
  return result;
}
