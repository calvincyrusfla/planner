// Foundry metrics reports: Foundry on the user's PC posts its numbers to the
// board and the CLI and dashboard show them. Only the latest report of each
// service is kept.
//
// state.foundryMetrics: [report], at most one per service (a list, not a map,
// so a service name can never collide with an object key).
// report = {service, host: string|null, reportedAt: iso, metrics: {...}}
// metrics: named numbers, text, true/false, null, lists and nested objects,
// within METRIC_LIMITS.

import { DeptError, clockNow } from './core.mjs';

export const METRIC_LIMITS = Object.freeze({
  services: 20,
  label: 200,
  key: 80,
  text: 500,
  depth: 4,
  values: 500,
});

// Far enough to forgive clock skew and timestamps without a zone, close enough
// that a bad clock can't pin the "latest" report for long.
const FUTURE_MS = 24 * 60 * 60 * 1000;
// Starts with a letter or digit, which also rules out __proto__.
const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;
const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
const bad = (msg) => new DeptError(msg, 400);
const time = (r) => Date.parse(r?.reportedAt) || 0;

function label(value, name, { required = false } = {}) {
  if (value === undefined || value === null) value = '';
  if (typeof value !== 'string') throw bad(`${name} must be text`);
  const v = value.trim();
  if (required && !v) throw bad(`${name} is required`);
  if (v.length > METRIC_LIMITS.label) throw bad(`${name} is too long (max ${METRIC_LIMITS.label} characters)`);
  if (CONTROL_RE.test(v)) throw bad(`${name} has control characters`);
  return v;
}

function reportTime(value, at) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) throw bad('reportedAt must be an ISO time');
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) throw bad('reportedAt must be an ISO time');
  if (ms > at + FUTURE_MS) throw bad('reportedAt is in the future');
  return new Date(ms).toISOString();
}

function cleanValue(value, name, depth, budget) {
  if (budget.left-- <= 0) throw bad(`metrics: at most ${METRIC_LIMITS.values} values`);
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw bad(`${name} must be a finite number`);
    return value;
  }
  if (typeof value === 'string') {
    if (value.length > METRIC_LIMITS.text) throw bad(`${name} is too long (max ${METRIC_LIMITS.text} characters)`);
    if (CONTROL_RE.test(value)) throw bad(`${name} has control characters`);
    return value;
  }
  if (!Array.isArray(value) && !isObj(value)) throw bad(`${name} must be a number, text, true/false, a list or an object`);
  if (depth >= METRIC_LIMITS.depth) throw bad(`${name} is nested too deeply`);
  if (Array.isArray(value)) return value.map((v, i) => cleanValue(v, `${name}[${i}]`, depth + 1, budget));
  return cleanObject(value, name, depth + 1, budget);
}

function cleanObject(obj, name, depth, budget) {
  const out = {};
  for (const [key, value] of Object.entries(obj)) {
    if (key.length > METRIC_LIMITS.key || !KEY_RE.test(key)) throw bad(`${name}: bad metric name ${JSON.stringify(key.slice(0, 40))}`);
    out[key] = cleanValue(value, `${name}.${key}`, depth, budget);
  }
  return out;
}

function cleanReport({ service, host, reportedAt, metrics }, at) {
  if (!isObj(metrics) || !Object.keys(metrics).length) throw bad('metrics must be an object with at least one value');
  return {
    service: label(service, 'service', { required: true }),
    host: label(host, 'host') || null,
    reportedAt: reportTime(reportedAt, at),
    metrics: cleanObject(metrics, 'metrics', 1, { left: METRIC_LIMITS.values }),
  };
}

// A batch subtask's usage (tokens, cost, ...): an object held to the same rules
// as a report's metrics. null clears it. Returns a clean copy or throws DeptError.
export function cleanUsage(usage, name = 'usage') {
  if (usage === null) return null;
  if (!isObj(usage)) throw bad(`${name} must be an object`);
  return cleanObject(usage, name, 1, { left: METRIC_LIMITS.values });
}

// Keeps report unless the service already has a newer one. Returns whether it was kept.
function store(state, report) {
  if (!Array.isArray(state.foundryMetrics)) state.foundryMetrics = [];
  const list = state.foundryMetrics;
  const i = list.findIndex((r) => r?.service === report.service);
  if (i !== -1 && time(list[i]) > time(report)) return false;
  if (i !== -1) list[i] = report;
  else list.push(report);
  if (list.length > METRIC_LIMITS.services) {
    const oldest = list.filter((r) => r !== report).reduce((a, b) => (time(b) < time(a) ? b : a));
    list.splice(list.indexOf(oldest), 1);
  }
  return true;
}

// Bad data throws. An out-of-date report is a {ok:false, stored:false} refusal
// (nothing changed, so the hub does not write), like losing a claim race.
export function reportFoundryMetrics(state, { service, host, reportedAt, metrics } = {}) {
  const at = clockNow();
  const report = cleanReport({ service, host, metrics, reportedAt: reportedAt ?? new Date(at).toISOString() }, at);
  if (!store(state, report)) {
    return { ok: false, reason: `a newer report from ${report.service} is already stored`, stored: false };
  }
  return { ok: true, report };
}

// importState: the newer report of each service wins. Invalid reports are
// dropped and listed in result.rejected as {service, reason}.
export function importMetrics(state, incoming) {
  const result = { stored: 0, rejected: [] };
  if (!Array.isArray(incoming)) return result;
  const at = clockNow();
  for (const raw of incoming) {
    try {
      if (!isObj(raw)) throw bad('report must be an object');
      if (!raw.reportedAt) throw bad('reportedAt is required');
      if (store(state, cleanReport(raw, at))) result.stored++;
    } catch (err) {
      if (!(err instanceof DeptError)) throw err;
      result.rejected.push({ service: isObj(raw) ? String(raw.service ?? '').slice(0, 40) : '', reason: err.message });
    }
  }
  return result;
}

// Where a report may list its tasks, and the fields that may name a task.
const TASK_LIST_KEYS = ['tasks', 'per_task', 'perTask'];
const TASK_ID_KEYS = ['id', 'task', 'task_id', 'taskId'];

// [[taskId, fields], ...] when value lists tasks (a list of objects, or an
// object of objects). Anything else, like {complete: 3}, gives [].
function taskEntries(value) {
  if (Array.isArray(value)) {
    if (!value.length || !value.every(isObj)) return [];
    return value.map((item, i) => {
      const idKey = TASK_ID_KEYS.find((k) => typeof item[k] === 'string' || typeof item[k] === 'number');
      const fields = { ...item };
      if (idKey) delete fields[idKey];
      return [idKey ? String(item[idKey]) : `#${i + 1}`, fields];
    });
  }
  const entries = isObj(value) ? Object.entries(value) : [];
  return entries.length && entries.every(([, v]) => isObj(v)) ? entries : [];
}

// A report's metrics as printable lines: a Totals section, then one row per task.
export function metricLines(metrics) {
  const key = TASK_LIST_KEYS.find((k) => taskEntries(metrics?.[k]).length);
  const rest = { ...metrics };
  if (key) delete rest[key];
  const totals = metricRows(rest).map(([name, value]) => `    ${name.replace(/^totals\./, '')}: ${value}`);
  const lines = totals.length ? ['  Totals:', ...totals] : [];
  if (key) {
    lines.push('  Tasks:');
    for (const [id, fields] of taskEntries(metrics[key])) {
      const cells = metricRows(fields).map(([name, value]) => `${name}=${value}`).join(' · ');
      lines.push(`    ${id}: ${cells || '(no values)'}`);
    }
  }
  return lines;
}

// [["tasks.complete", "3"], ...]: a report's metrics as flat rows for display.
export function metricRows(value, prefix = '') {
  if (isObj(value)) return Object.entries(value).flatMap(([k, v]) => metricRows(v, prefix ? `${prefix}.${k}` : k));
  return [[prefix, Array.isArray(value) ? JSON.stringify(value) : String(value)]];
}
