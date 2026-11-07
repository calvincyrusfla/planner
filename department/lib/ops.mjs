// Department operations: one entry point that maps a serialisable op
// ({type, args}) onto the core functions. The dashboard server, the CLI and
// the shared-board Netlify function all mutate state through applyOp, so a
// board behaves the same whether it lives on this PC or on the hub.

import * as core from './core.mjs';

const LOG_LIMIT = 500;
const OPEN = new Set(['queued', 'active', 'paused']);
const clone = (v) => JSON.parse(JSON.stringify(v));
const taskNumber = (n) => `T-${String(n).padStart(3, '0')}`;

const OPS = {
  addTask: (state, { title, description, size } = {}) => core.addTask(state, { title, description, size }),
  updateTask: (state, { id, patch, by } = {}) => core.updateTask(state, id, patch || {}, by ? { by } : {}),
  deleteTask: (state, { id, by } = {}) => core.deleteTask(state, id, by ? { by } : {}),
  assignTeam: (state, { taskId, teamId, focus, by } = {}) =>
    core.assignTeam(state, taskId, teamId, { ...(focus ? { focus } : {}), ...(by ? { by } : {}) }),
  unassignTeam: (state, { taskId, teamId, by } = {}) => core.unassignTeam(state, taskId, teamId, by ? { by } : {}),
  reportProgress: (state, args = {}) => core.reportProgress(state, args),
  dispatch: (state) => core.dispatch(state),
  updateSettings: (state, { autoDispatch } = {}) => core.updateSettings(state, { autoDispatch }),
  reset: (state) => {
    const fresh = core.createInitialState();
    for (const key of Object.keys(state)) delete state[key];
    Object.assign(state, fresh);
    return { ok: true };
  },
  importState: (state, { state: other } = {}) => importState(state, other),
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
// - Open tasks keep a team only if that team is free here; an active task that
//   keeps no team goes back to the queue. Done/cancelled tasks have no team.
// - Logs are merged (duplicates dropped), sorted by `at` and capped at 500.
// - Finally dispatch runs so queued work picks up free teams.

const TEAM_IDENTITY = new Set(['id', 'name', 'color', 'taskId', 'totalMs']);
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
    const open = OPEN.has(task.status);
    let kept = 0;
    if (open) {
      for (const theirs of (incoming.teams || []).filter((t) => t.taskId === oldId)) {
        const ours = state.teams.find((t) => t.id === theirs.id);
        if (!ours || ours.taskId) continue;
        for (const [k, v] of Object.entries(theirs)) if (!TEAM_IDENTITY.has(k)) ours[k] = v;
        ours.taskId = task.id;
        kept++;
      }
      if (!kept && task.status === 'active') {
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

  core.dispatch(state);
  return { added: addedTasks.length, skipped: skippedIds.size, idMap };
}
