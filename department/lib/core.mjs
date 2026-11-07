// Department core: teams, agents, tasks and the dispatch rules.
// Pure functions over a plain state object, shared by the server and the CLI.

export const TEAM_DEFS = [
  { id: 'alpha', name: 'Team Alpha', color: '#e8590c' },
  { id: 'bravo', name: 'Team Bravo', color: '#1c7ed6' },
  { id: 'charlie', name: 'Team Charlie', color: '#2f9e44' },
];
export const AGENTS_PER_TEAM = 5;

export const TASK_STATUSES = ['queued', 'active', 'paused', 'done', 'cancelled'];
export const AGENT_STATUSES = ['idle', 'assigned', 'working', 'blocked', 'done'];
const OPEN = new Set(['queued', 'active', 'paused']);
const LOG_LIMIT = 500;

export class DeptError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

const now = () => new Date().toISOString();
const clampPct = (n) => Math.max(0, Math.min(100, Math.round(Number(n))));

export function createInitialState() {
  return {
    version: 1,
    settings: { autoDispatch: true },
    nextTaskNumber: 1,
    teams: TEAM_DEFS.map((t) => ({
      ...t,
      taskId: null,
      focus: '',
      progress: 0,
      agents: Array.from({ length: AGENTS_PER_TEAM }, (_, i) => ({
        id: `${t.id}-${i + 1}`,
        name: `${t.name.replace('Team ', '')} Builder ${i + 1}`,
        role: 'builder',
        status: 'idle',
        work: '',
        updatedAt: null,
      })),
    })),
    tasks: [],
    log: [],
  };
}

// ---------- lookups ----------

export function getTask(state, id) {
  const key = String(id).toUpperCase();
  const task = state.tasks.find((t) => t.id === key);
  if (!task) throw new DeptError(`No task ${id}`, 404);
  return task;
}

export function getTeam(state, id) {
  const key = String(id).toLowerCase().replace(/^team[\s-]*/, '');
  const team = state.teams.find((t) => t.id === key);
  if (!team) throw new DeptError(`No team ${id}`, 404);
  return team;
}

export function getAgent(state, id) {
  for (const team of state.teams) {
    const agent = team.agents.find((a) => a.id === String(id).toLowerCase());
    if (agent) return { team, agent };
  }
  throw new DeptError(`No agent ${id}`, 404);
}

export const idleTeams = (state) => state.teams.filter((t) => !t.taskId);
export const teamsOn = (state, taskId) => state.teams.filter((t) => t.taskId === taskId);
const runningTasks = (state) => state.tasks.filter((t) => t.status === 'active');

function log(state, entry) {
  state.log.push({ at: now(), ...entry });
  if (state.log.length > LOG_LIMIT) state.log.splice(0, state.log.length - LOG_LIMIT);
}

function touch(task) {
  task.updatedAt = now();
}

function setAgents(team, status, work = '') {
  for (const a of team.agents) {
    a.status = status;
    a.work = work;
    a.updatedAt = now();
  }
}

// ---------- assignment primitives ----------

function attach(state, task, team, { focus = '', by = 'dispatcher' } = {}) {
  if (team.taskId === task.id) return false;
  if (team.taskId) detach(state, getTask(state, team.taskId), team, { by, reason: `moved to ${task.id}` });
  team.taskId = task.id;
  team.focus = focus;
  team.progress = task.progress;
  setAgents(team, 'assigned');
  if (!task.teamHistory.includes(team.id)) task.teamHistory.push(team.id);
  task.status = 'active';
  task.startedAt ||= now();
  touch(task);
  log(state, { kind: 'assign', taskId: task.id, teamId: team.id, message: `${team.name} assigned to ${task.id} by ${by}` });
  return true;
}

function detach(state, task, team, { by = 'dispatcher', reason = '' } = {}) {
  if (team.taskId !== task.id) return false;
  team.taskId = null;
  team.focus = '';
  team.progress = 0;
  setAgents(team, 'idle');
  // A task that loses its last team is paused, so the dispatcher won't hand it
  // straight back to someone. Requeue it to make it eligible again.
  const remaining = teamsOn(state, task.id);
  if (remaining.length) task.progress = Math.round(remaining.reduce((s, t) => s + t.progress, 0) / remaining.length);
  else if (task.status === 'active') task.status = 'paused';
  touch(task);
  log(state, {
    kind: 'unassign',
    taskId: task.id,
    teamId: team.id,
    message: `${team.name} removed from ${task.id} by ${by}${reason ? ` (${reason})` : ''}`,
  });
  return true;
}

// ---------- dispatch rules ----------
// 1. Every new task gets a team.
// 2. A large task gets ALL teams when no other task is running.
// 3. A task that can't get a team waits in the queue (oldest first) and is
//    picked up as soon as a team frees up.

export function dispatch(state) {
  const assigned = [];
  if (!state.settings.autoDispatch) return assigned;
  const queue = state.tasks
    .filter((t) => t.status === 'queued')
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  for (const task of queue) {
    const free = idleTeams(state);
    if (!free.length) break;
    const othersRunning = runningTasks(state).some((t) => t.id !== task.id);
    const take = task.size === 'large' && !othersRunning ? free : [free[0]];
    for (const team of take) {
      attach(state, task, team);
      assigned.push({ taskId: task.id, teamId: team.id });
    }
  }
  return assigned;
}

// ---------- task operations ----------

export function addTask(state, { title, description = '', size = 'small' } = {}) {
  title = String(title || '').trim();
  if (!title) throw new DeptError('A task needs a title');
  if (!['small', 'large'].includes(size)) throw new DeptError('size must be small or large');
  const id = `T-${String(state.nextTaskNumber++).padStart(3, '0')}`;
  const task = {
    id,
    title,
    description: String(description || ''),
    size,
    status: 'queued',
    progress: 0,
    teamHistory: [],
    createdAt: now(),
    updatedAt: now(),
    startedAt: null,
    finishedAt: null,
  };
  state.tasks.push(task);
  log(state, { kind: 'create', taskId: id, message: `${id} created (${size}): ${title}` });
  const assigned = dispatch(state).filter((a) => a.taskId === id);
  if (!assigned.length) log(state, { kind: 'queue', taskId: id, message: `${id} queued, no free team` });
  return { task, assigned: assigned.map((a) => a.teamId) };
}

export function assignTeam(state, taskId, teamId, { focus = '', by = 'user' } = {}) {
  const task = getTask(state, taskId);
  const team = getTeam(state, teamId);
  if (!OPEN.has(task.status)) throw new DeptError(`${task.id} is ${task.status}; reopen it first`);
  const previous = team.taskId;
  attach(state, task, team, { focus, by });
  if (focus) team.focus = focus;
  // Moving a team may have freed nothing, but a finished/paused source never
  // grabs teams back, so dispatching here only serves the waiting queue.
  dispatch(state);
  return { task, team, previous };
}

export function unassignTeam(state, taskId, teamId, { by = 'user' } = {}) {
  const task = getTask(state, taskId);
  const team = getTeam(state, teamId);
  if (team.taskId !== task.id) throw new DeptError(`${team.name} is not on ${task.id}`);
  detach(state, task, team, { by });
  dispatch(state); // the freed team may pick up a waiting task
  return { task, team };
}

export function updateTask(state, taskId, patch = {}, { by = 'user' } = {}) {
  const task = getTask(state, taskId);
  if (patch.title !== undefined) {
    const title = String(patch.title).trim();
    if (!title) throw new DeptError('Title cannot be empty');
    task.title = title;
  }
  if (patch.description !== undefined) task.description = String(patch.description);
  if (patch.size !== undefined) {
    if (!['small', 'large'].includes(patch.size)) throw new DeptError('size must be small or large');
    task.size = patch.size;
  }
  if (patch.progress !== undefined) task.progress = clampPct(patch.progress);
  if (patch.status !== undefined && patch.status !== task.status) {
    const status = patch.status;
    if (!TASK_STATUSES.includes(status)) throw new DeptError(`Unknown status ${status}`);
    if (status === 'active' && !teamsOn(state, task.id).length) {
      throw new DeptError('Assign a team to make a task active, or set it to queued');
    }
    if (status === 'done' || status === 'cancelled' || status === 'paused' || status === 'queued') {
      for (const team of teamsOn(state, task.id)) detach(state, task, team, { by, reason: `task ${status}` });
    }
    task.status = status;
    if (status === 'done') task.progress = 100;
    task.finishedAt = status === 'done' || status === 'cancelled' ? now() : null;
    log(state, { kind: 'status', taskId: task.id, message: `${task.id} marked ${status} by ${by}` });
  }
  touch(task);
  dispatch(state);
  return task;
}

export function deleteTask(state, taskId, { by = 'user' } = {}) {
  const task = getTask(state, taskId);
  for (const team of teamsOn(state, task.id)) detach(state, task, team, { by, reason: 'task deleted' });
  state.tasks = state.tasks.filter((t) => t.id !== task.id);
  log(state, { kind: 'delete', taskId: task.id, message: `${task.id} deleted by ${by}` });
  dispatch(state);
  return task;
}

export function reportProgress(state, { taskId, teamId, agentId, progress, message = '', status, work } = {}) {
  let team = teamId ? getTeam(state, teamId) : null;
  let agent = null;
  if (agentId) {
    const found = getAgent(state, agentId);
    agent = found.agent;
    team ||= found.team;
  }
  const task = taskId ? getTask(state, taskId) : team?.taskId ? getTask(state, team.taskId) : null;
  if (!task) throw new DeptError('Say which task this progress is for');

  if (agent) {
    if (status !== undefined) {
      if (!AGENT_STATUSES.includes(status)) throw new DeptError(`Unknown agent status ${status}`);
      agent.status = status;
    } else if (agent.status === 'assigned' || agent.status === 'idle') {
      agent.status = 'working';
    }
    if (work !== undefined) agent.work = String(work);
    else if (message) agent.work = String(message);
    agent.updatedAt = now();
  }

  if (progress !== undefined && progress !== null && progress !== '') {
    const pct = clampPct(progress);
    if (team && team.taskId === task.id) {
      team.progress = pct;
      const onTask = teamsOn(state, task.id);
      task.progress = Math.round(onTask.reduce((s, t) => s + t.progress, 0) / onTask.length);
    } else {
      task.progress = pct;
    }
  }
  touch(task);
  if (message) {
    log(state, { kind: 'progress', taskId: task.id, teamId: team?.id, agentId: agent?.id, message: String(message) });
  }
  return { task, team, agent };
}

export function updateSettings(state, patch = {}) {
  if (patch.autoDispatch !== undefined) state.settings.autoDispatch = Boolean(patch.autoDispatch);
  dispatch(state);
  return state.settings;
}

// Upgrade/repair a state loaded from disk.
export function normalize(state) {
  const fresh = createInitialState();
  if (!state || typeof state !== 'object') return fresh;
  state.settings = { ...fresh.settings, ...(state.settings || {}) };
  state.tasks ||= [];
  state.log ||= [];
  state.nextTaskNumber ||= state.tasks.length + 1;
  const existing = new Map((state.teams || []).map((t) => [t.id, t]));
  state.teams = fresh.teams.map((t) => {
    const old = existing.get(t.id);
    if (!old) return t;
    const agents = new Map((old.agents || []).map((a) => [a.id, a]));
    return { ...t, ...old, agents: t.agents.map((a) => ({ ...a, ...(agents.get(a.id) || {}) })) };
  });
  const taskIds = new Set(state.tasks.map((t) => t.id));
  for (const team of state.teams) if (team.taskId && !taskIds.has(team.taskId)) team.taskId = null;
  return state;
}
