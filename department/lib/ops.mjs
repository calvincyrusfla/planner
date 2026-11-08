// Department operations: one entry point that maps a serialisable op
// ({type, args}) onto the core functions. The dashboard server, the CLI and
// the shared-board Netlify function all mutate state through applyOp, so a
// board behaves the same whether it lives on this PC or on the hub.

import * as core from './core.mjs';
import * as batches from './foundry-batches.mjs';
import * as foundryMetrics from './foundry-metrics.mjs';

const LOG_LIMIT = 500;
const OPEN = new Set(['queued', 'active', 'review', 'paused']);
const clone = (v) => JSON.parse(JSON.stringify(v));
const taskNumber = (n) => `T-${String(n).padStart(3, '0')}`;

const OPS = {
  addTask: (state, { title, description, size, builders } = {}) => core.addTask(state, { title, description, size, builders }),
  // patch is passed whole, so patch.builders (crew size) and patch.force (done without review sign-off) gets through.
  updateTask: (state, { id, patch, by } = {}) => core.updateTask(state, id, patch || {}, by ? { by } : {}),
  deleteTask: (state, { id, by } = {}) => core.deleteTask(state, id, by ? { by } : {}),
  assignTeam: (state, { taskId, teamId, focus, by } = {}) =>
    core.assignTeam(state, taskId, teamId, { ...(focus ? { focus } : {}), ...(by ? { by } : {}) }),
  unassignTeam: (state, { taskId, teamId, by } = {}) => core.unassignTeam(state, taskId, teamId, by ? { by } : {}),
  reportProgress: (state, args = {}) => core.reportProgress(state, args),
  reviewTask: (state, { taskId, teamId, verdict, note, issues, by } = {}) =>
    core.reviewTask(state, {
      taskId,
      teamId,
      verdict,
      ...(note !== undefined ? { note } : {}),
      ...(issues !== undefined ? { issues } : {}),
      ...(by ? { by } : {}),
    }),
  reportUsage: (state, { fiveHour, sevenDay, reportedAt, source } = {}) =>
    core.reportUsage(state, { fiveHour, sevenDay, reportedAt, source }),
  dispatch: (state) => core.dispatch(state),
  updateSettings: (state, { autoDispatch } = {}) => core.updateSettings(state, { autoDispatch }),
  reset: (state) => {
    const fresh = core.createInitialState();
    for (const key of Object.keys(state)) delete state[key];
    Object.assign(state, fresh);
    return { ok: true };
  },
  importState: (state, { state: other } = {}) => importState(state, other),
  // Foundry batches (see lib/foundry-batches.mjs for the shape).
  dispatchBatch: (state, { title, goal, repo, base, subtasks, by } = {}) =>
    batches.dispatchBatch(state, { title, goal, repo, base, subtasks, by }),
  claimBatch: (state, { id, host, service } = {}) => batches.claimBatch(state, { id, host, service }),
  updateBatch: (state, { id, service, host, status, results, heartbeat, note } = {}) =>
    batches.updateBatch(state, { id, service, host, status, results, heartbeat, note }),
  cancelBatch: (state, { id, by } = {}) => batches.cancelBatch(state, { id, by }),
  // Foundry metrics: latest report per service (see lib/foundry-metrics.mjs).
  reportFoundryMetrics: (state, { service, host, reportedAt, metrics } = {}) =>
    foundryMetrics.reportFoundryMetrics(state, { service, host, reportedAt, metrics }),
};

export const OP_TYPES = Object.keys(OPS);

export function applyOp(state, op) {
  const type = op?.type;
  if (!type || !Object.hasOwn(OPS, type)) throw new core.DeptError(`Unknown operation ${type}`);
  return OPS[type](state, op.args || {});
}

// ---------- importState ----------
// Merge another board into this one without losing anything.
// - A task with the same createdAt and title as one here is the same task:
//   skipped. (Not the id: a pushed task gets a new id here, so a second push
//   must still recognise it.)
// - Every other task is appended under a fresh id (T-### from nextTaskNumber);
//   its log entries (and task ids mentioned in them) are rewritten to match.
// - Open tasks keep a team only if that team is free here; an active (or
//   in-review) task that keeps no team goes back to the queue. Done/cancelled
//   tasks have no team. A kept team keeps this board's roster (so an older
//   5-agent board can't drop the reviewer); saved agent status/work is taken.
//   Team issuesFound is not overwritten; like totalMs, it is credited from the
//   added tasks only (teamIssues[team], all rounds), so a re-push can't double count.
// - Logs are merged (duplicates dropped), sorted by `at` and capped at 500.
// - Usage: the newer report (by reportedAt) wins; an older or invalid one is ignored.
// - Foundry batches merge by id (lib/foundry-batches.mjs importBatches): same
//   id + createdAt keeps the newer copy, a clashing id gets a fresh S-###.
// - Foundry metrics: the newer report of each service wins; invalid ones are dropped.
// - Finally dispatch runs so queued work picks up free teams.

const TEAM_IDENTITY = new Set(['id', 'name', 'color', 'taskId', 'totalMs', 'issuesFound', 'agents']);
const logKey = (e) => JSON.stringify([e.at, e.kind, e.taskId, e.teamId, e.agentId, e.message]);

export function importState(state, other) {
  if (!other || typeof other !== 'object' || !Array.isArray(other.tasks)) {
    throw new core.DeptError('importState needs a department state with a tasks list');
  }
  const incoming = clone(other);
  const sameTask = (t) => `${t.createdAt}\u0000${t.title}`;
  const existing = new Set(state.tasks.map(sameTask));
  const idMap = {};
  const skippedIds = new Set();
  const addedTasks = [];

  for (const task of incoming.tasks) {
    if (existing.has(sameTask(task))) {
      skippedIds.add(task.id);
      continue;
    }
    const newId = taskNumber(state.nextTaskNumber++);
    idMap[task.id] = newId;
    addedTasks.push({ oldId: task.id, task });
  }

  const rewriteIds = (text) =>
    typeof text === 'string' && Object.keys(idMap).length
      ? text.replace(/\bT-\d{3,}\b/g, (m) => idMap[m] || m)
      : text;

  for (const { oldId, task } of addedTasks) {
    task.id = idMap[oldId];
    // builders comes along with the task; boards from before crew sizing get the size default.
    task.builders = core.crewFor(task);
    const open = OPEN.has(task.status);
    let kept = 0;
    if (open) {
      for (const theirs of (incoming.teams || []).filter((t) => t.taskId === oldId)) {
        const ours = state.teams.find((t) => t.id === theirs.id);
        if (!ours || ours.taskId) continue;
        for (const [k, v] of Object.entries(theirs)) if (!TEAM_IDENTITY.has(k)) ours[k] = v;
        const theirAgents = new Map((Array.isArray(theirs.agents) ? theirs.agents : []).map((a) => [a?.id, a]));
        for (const agent of ours.agents) {
          const a = theirAgents.get(agent.id);
          if (!a) continue;
          for (const k of ['status', 'work', 'updatedAt']) if (a[k] !== undefined) agent[k] = a[k];
        }
        ours.taskId = task.id;
        kept++;
      }
      if (!kept && core.isRunning(task)) {
        task.status = 'queued';
        if ('activeSince' in task) task.activeSince = null;
      }
    }
    state.tasks.push(task);
    // Credit each team with the closed time it spent on this task on the other
    // board. Only added tasks count, so importing the same board twice can't
    // double a team's total.
    for (const [teamId, ms] of Object.entries(task.teamMs || {})) {
      const team = state.teams.find((t) => t.id === teamId);
      if (team && Number.isFinite(ms)) team.totalMs = (team.totalMs || 0) + ms;
    }
    // Same for issues caught: credit each team's running total on the task
    // (teamIssues, all rounds). Boards from before teamIssues fall back to the
    // latest verdict in reviews.
    const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : null);
    const perTeam = obj(task.teamIssues) ||
      Object.fromEntries(Object.entries(obj(task.reviews) || {}).map(([id, r]) => [id, r?.issues]));
    for (const [teamId, issues] of Object.entries(perTeam)) {
      const team = state.teams.find((t) => t.id === teamId);
      const n = Number(issues);
      if (team && Number.isInteger(n) && n > 0) team.issuesFound = (team.issuesFound || 0) + n;
    }
  }

  // Merge logs: entries about added tasks are new by definition (rewritten);
  // anything else is dropped when an identical entry already exists here.
  const seen = new Set(state.log.map(logKey));
  const merged = [...state.log];
  for (const entry of incoming.log || []) {
    if (entry.taskId && idMap[entry.taskId]) {
      merged.push({ ...entry, taskId: idMap[entry.taskId], message: rewriteIds(entry.message) });
      continue;
    }
    const key = logKey(entry);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push({ ...entry, message: rewriteIds(entry.message) });
  }
  merged.sort((a, b) => String(a.at || '').localeCompare(String(b.at || '')));
  state.log = merged.slice(-LOG_LIMIT);

  const theirUsage = core.normalizeUsage(incoming.usage);
  const ourUsage = core.normalizeUsage(state.usage);
  if (theirUsage && (!ourUsage || Date.parse(theirUsage.reportedAt) > Date.parse(ourUsage.reportedAt))) {
    state.usage = theirUsage;
  } else state.usage = ourUsage;

  const batchResult = batches.importBatches(state, incoming.foundryBatches);
  foundryMetrics.importMetrics(state, incoming.foundryMetrics);

  core.dispatch(state);
  return { added: addedTasks.length, skipped: skippedIds.size, idMap, batches: batchResult };
}
