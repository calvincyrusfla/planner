// Department core: teams, agents, tasks and the dispatch rules.
// Pure functions over a plain state object, shared by the server and the CLI.

export const TEAM_DEFS = [
  { id: 'alpha', name: 'Team Alpha', color: '#e8590c' },
  { id: 'bravo', name: 'Team Bravo', color: '#1c7ed6' },
  { id: 'charlie', name: 'Team Charlie', color: '#2f9e44' },
];
// Each team: 5 builders (<team>-1..5) plus 1 reviewer (<team>-6) who checks
// the team's work before a task is called done.
export const BUILDERS_PER_TEAM = 5;
export const AGENTS_PER_TEAM = BUILDERS_PER_TEAM + 1;

export const TASK_STATUSES = ['queued', 'active', 'review', 'paused', 'done', 'cancelled'];
export const AGENT_STATUSES = ['idle', 'assigned', 'working', 'blocked', 'done'];
export const REVIEW_VERDICTS = ['approved', 'changes'];
const OPEN = new Set(['queued', 'active', 'review', 'paused']);
// Statuses in which a task's clock runs and its teams are at work.
const RUNNING = new Set(['active', 'review']);
export const isRunning = (task) => RUNNING.has(task?.status);
const LOG_LIMIT = 500;

export class DeptError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

// ---------- clock ----------
// Every timestamp in core comes from this clock, so tests can control time.
let clock = () => Date.now();
export function setClock(fn) {
  clock = typeof fn === 'function' ? fn : () => Date.now();
}
export const clockNow = () => clock();
const now = () => new Date(clock()).toISOString();
const since = (iso, at) => {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Math.max(0, at - t) : 0;
};
const clampPct = (n) => Math.max(0, Math.min(100, Math.round(Number(n))));

function makeAgents(t) {
  const short = t.name.replace('Team ', '');
  return Array.from({ length: AGENTS_PER_TEAM }, (_, i) => {
    const reviewer = i === BUILDERS_PER_TEAM;
    return {
      id: `${t.id}-${i + 1}`,
      name: reviewer ? `${short} Reviewer` : `${short} Builder ${i + 1}`,
      role: reviewer ? 'reviewer' : 'builder',
      status: 'idle',
      work: '',
      updatedAt: null,
    };
  });
}

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
      assignedAt: null,
      totalMs: 0,
      issuesFound: 0,
      agents: makeAgents(t),
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
const runningTasks = (state) => state.tasks.filter(isRunning);
export const reviewerOf = (team) =>
  team.agents.find((a) => a.role === 'reviewer') || team.agents.find((a) => a.id === `${team.id}-${AGENTS_PER_TEAM}`) || null;

function log(state, entry) {
  state.log.push({ at: now(), ...entry });
  if (state.log.length > LOG_LIMIT) state.log.splice(0, state.log.length - LOG_LIMIT);
}

function touch(task) {
  task.updatedAt = now();
}

// ---------- time tracking ----------
// Open/close the task's active interval so activeSince is set exactly while
// the task is running (status 'active' or 'review'). Call after every status change.
function syncActive(task) {
  if (isRunning(task)) {
    if (!task.activeSince) task.activeSince = now();
  } else if (task.activeSince) {
    task.activeMs = (task.activeMs || 0) + since(task.activeSince, clockNow());
    task.activeSince = null;
  }
}

// Close a team's assignment interval on `task`.
function closeTeam(task, team) {
  if (!team.assignedAt) return;
  const ms = since(team.assignedAt, clockNow());
  task.teamMs ||= {};
  task.teamMs[team.id] = (task.teamMs[team.id] || 0) + ms;
  team.totalMs = (team.totalMs || 0) + ms;
  team.assignedAt = null;
}

export function taskActiveMs(task, at = clockNow()) {
  return (task.activeMs || 0) + (task.activeSince ? since(task.activeSince, at) : 0);
}

export function taskTeamMs(state, task, at = clockNow()) {
  const out = { ...(task.teamMs || {}) };
  for (const team of teamsOn(state, task.id)) {
    if (team.assignedAt) out[team.id] = (out[team.id] || 0) + since(team.assignedAt, at);
  }
  return out;
}

export function teamTotalMs(team, at = clockNow()) {
  return (team.totalMs || 0) + (team.taskId && team.assignedAt ? since(team.assignedAt, at) : 0);
}

export function teamStats(state, at = clockNow()) {
  return state.teams.map((team) => {
    const current = team.taskId ? state.tasks.find((t) => t.id === team.taskId) : null;
    const done = state.tasks.filter((t) => t.status === 'done' && (t.teamMs?.[team.id] || 0) > 0);
    const doneMs = done.reduce((s, t) => s + t.teamMs[team.id], 0);
    return {
      teamId: team.id,
      name: team.name,
      color: team.color,
      totalMs: teamTotalMs(team, at),
      currentTaskId: current ? current.id : null,
      // Everything this team has spent on its current task, earlier stints included.
      currentMs: current ? taskTeamMs(state, current, at)[team.id] || 0 : 0,
      tasksDone: done.length,
      avgDoneMs: done.length ? Math.round(doneMs / done.length) : 0,
      issuesFound: team.issuesFound || 0,
    };
  });
}

export function formatDuration(ms) {
  ms = Number(ms);
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

function setAgents(team, status, work = '') {
  for (const a of team.agents) setAgent(a, status, work);
}

function setAgent(agent, status, work = '') {
  if (!agent) return;
  agent.status = status;
  agent.work = work;
  agent.updatedAt = now();
}

// ---------- assignment primitives ----------

function attach(state, task, team, { focus = '', by = 'dispatcher' } = {}) {
  if (team.taskId === task.id) return false;
  if (team.taskId) detach(state, getTask(state, team.taskId), team, { by, reason: `moved to ${task.id}` });
  team.taskId = task.id;
  team.assignedAt = now();
  team.focus = focus;
  team.progress = task.progress;
  setAgents(team, 'assigned');
  if (!task.teamHistory.includes(team.id)) task.teamHistory.push(team.id);
  // Sign-off only covers the work that was reviewed: a team joining a task
  // whose round was fully approved voids it. In review, that team is simply
  // pending; otherwise sign-off is gone until a new review round.
  task.approvedRound = 0;
  // A team joining a task in review joins the review: its reviewer is pending.
  if (task.status === 'review') setAgent(reviewerOf(team), 'working', `Reviewing round ${task.reviewRound}`);
  else task.status = 'active';
  syncActive(task);
  task.startedAt ||= now();
  touch(task);
  log(state, { kind: 'assign', taskId: task.id, teamId: team.id, message: `${team.name} assigned to ${task.id} by ${by}` });
  return true;
}

// settle: re-check the review round for the teams still on the task. Off when
// the whole task is leaving its teams (status change, delete).
function detach(state, task, team, { by = 'dispatcher', reason = '', settle = true } = {}) {
  if (team.taskId !== task.id) return false;
  closeTeam(task, team);
  team.taskId = null;
  team.focus = '';
  team.progress = 0;
  setAgents(team, 'idle');
  // A task that loses its last team is paused, so the dispatcher won't hand it
  // straight back to someone. Requeue it to make it eligible again.
  const remaining = teamsOn(state, task.id);
  if (remaining.length) task.progress = Math.round(remaining.reduce((s, t) => s + t.progress, 0) / remaining.length);
  else if (isRunning(task)) task.status = 'paused';
  syncActive(task);
  touch(task);
  log(state, {
    kind: 'unassign',
    taskId: task.id,
    teamId: team.id,
    message: `${team.name} removed from ${task.id} by ${by}${reason ? ` (${reason})` : ''}`,
  });
  // The team that left may have been the last one the round was waiting on.
  if (settle && remaining.length && task.status === 'review') settleReview(state, task);
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
    activeMs: 0,
    activeSince: null,
    teamMs: {},
    reviewRound: 0,
    approvedRound: 0, // last round every reviewer approved (0 = none)
    reviews: {},
    issuesFound: 0,
    teamIssues: {}, // {teamId: issues caught across all rounds}
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
    const onTask = teamsOn(state, task.id);
    if (status === 'active' && !onTask.length) {
      throw new DeptError('Assign a team to make a task active, or set it to queued');
    }
    if (status === 'review' && !onTask.length) throw new DeptError('Assign a team before review');
    let forced = false;
    if (status === 'done') {
      const gate = reviewGate(state, task);
      if (!gate.ok) {
        if (patch.force !== true) throw new DeptError(gate.message);
        forced = true;
      }
    }
    // Reopening a finished task, or taking a task out of review for more work,
    // voids any sign-off: the work changes after it was reviewed.
    const reopened = (task.status === 'done' || task.status === 'cancelled') && OPEN.has(status);
    if (reopened || (task.status === 'review' && status === 'active')) task.approvedRound = 0;
    if (status === 'review') {
      task.reviewRound = (task.reviewRound || 0) + 1;
      task.reviews ||= {};
      for (const team of onTask) setAgent(reviewerOf(team), 'working', `Reviewing round ${task.reviewRound}`);
    }
    if (status === 'done' || status === 'cancelled' || status === 'paused' || status === 'queued') {
      for (const team of teamsOn(state, task.id)) detach(state, task, team, { by, reason: `task ${status}`, settle: false });
    }
    task.status = status;
    syncActive(task);
    if (status === 'done') task.progress = 100;
    task.finishedAt = status === 'done' || status === 'cancelled' ? now() : null;
    if (status === 'review') {
      log(state, { kind: 'review', taskId: task.id, message: `${task.id} sent to review (round ${task.reviewRound})` });
    } else if (forced) {
      log(state, { kind: 'status', taskId: task.id, message: `${task.id} marked done without review by ${by}` });
    } else {
      log(state, { kind: 'status', taskId: task.id, message: `${task.id} marked ${status} by ${by}` });
    }
  }
  touch(task);
  dispatch(state);
  return task;
}

// ---------- reviews ----------
// Each team on a task has a reviewer (<team>-6). Sending a task to review opens
// a new round; every attached team's reviewer must approve that round before
// the task can be marked done (unless forced).

// Can `task` be marked done without force? Mirrors updateTask's done gate.
// - Teams on the task: each must have approved the current round, and that
//   round's sign-off must still stand (approvedRound === reviewRound).
// - No teams, but worked on before: its latest round must have been approved
//   by all reviewers (task.approvedRound === task.reviewRound).
// - Never worked by a team: always ok.
export function reviewGate(state, task) {
  if (typeof task === 'string') task = getTask(state, task);
  const onTask = teamsOn(state, task.id);
  const signedOff = task.reviewRound > 0 && task.approvedRound === task.reviewRound;
  if (onTask.length) {
    const { changes, pending } = reviewStatus(state, task);
    let missing = onTask.map((t) => t.id).filter((id) => changes.includes(id) || pending.includes(id));
    // Approvals from a round whose sign-off was voided (reopened, taken back to
    // active) no longer count: every team on the task must review again.
    if (!missing.length && !signedOff) missing = onTask.map((t) => t.id);
    if (!missing.length) return { ok: true, missing: [], message: '' };
    const names = missing.map((id) => getTeam(state, id).name).join(', ');
    return {
      ok: false,
      missing,
      message: `${task.id} needs reviewer sign-off from ${names}. Send it to review, or mark it done with force.`,
    };
  }
  const worked = Array.isArray(task.teamHistory) && task.teamHistory.length > 0;
  if (!worked || signedOff) return { ok: true, missing: [], message: '' };
  return { ok: false, missing: [], message: `${task.id} was worked on but has no reviewer sign-off. Mark it done with force.` };
}

// approved / changes / pending (no verdict yet) for the teams on the task, current round only.
export function reviewStatus(state, task) {
  if (typeof task === 'string') task = getTask(state, task);
  const round = task.reviewRound || 0;
  const out = { round, approved: [], changes: [], pending: [] };
  for (const team of teamsOn(state, task.id)) {
    const r = task.reviews?.[team.id];
    if (round > 0 && r && r.round === round && r.verdict === 'approved') out.approved.push(team.id);
    else if (round > 0 && r && r.round === round && r.verdict === 'changes') out.changes.push(team.id);
    else out.pending.push(team.id);
  }
  return out;
}

export function reviewTask(state, { taskId, teamId, verdict, note = '', issues = 0, by = 'reviewer' } = {}) {
  const task = getTask(state, taskId);
  if (task.status !== 'review') throw new DeptError(`${task.id} is not in review`);
  if (!teamId) throw new DeptError('Say which team is reviewing');
  const team = getTeam(state, teamId);
  if (team.taskId !== task.id) throw new DeptError(`${team.name} is not on ${task.id}`);
  if (verdict === 'approve') verdict = 'approved';
  if (!REVIEW_VERDICTS.includes(verdict)) throw new DeptError("verdict must be 'approved' or 'changes'");
  const count = issues === undefined || issues === null || issues === '' ? 0 : Number(issues);
  if (!Number.isInteger(count) || count < 0) throw new DeptError('issues must be a non-negative whole number');
  note = String(note ?? '');
  const round = task.reviewRound || 0;
  task.reviews ||= {};
  // A second verdict from the same team in the same round replaces the first,
  // issue count included (so issues aren't counted twice).
  const prev = task.reviews[team.id];
  const prevIssues = prev && prev.round === round && Number.isFinite(prev.issues) ? prev.issues : 0;
  task.reviews[team.id] = { verdict, note, issues: count, at: now(), round };
  task.issuesFound = Math.max(0, (task.issuesFound || 0) - prevIssues + count);
  team.issuesFound = Math.max(0, (team.issuesFound || 0) - prevIssues + count);
  // Running per-team total across all rounds (what importState credits).
  if (!task.teamIssues || typeof task.teamIssues !== 'object') task.teamIssues = {};
  task.teamIssues[team.id] = Math.max(0, (task.teamIssues[team.id] || 0) - prevIssues + count);
  const reviewer = reviewerOf(team);
  const byNote = by && by !== 'reviewer' ? ` (by ${by})` : '';
  if (verdict === 'approved') {
    setAgent(reviewer, 'done', `Approved round ${round}`);
    log(state, { kind: 'review', taskId: task.id, teamId: team.id, agentId: reviewer?.id, message: `${team.name} reviewer approved ${task.id}${byNote}` });
  } else {
    setAgent(reviewer, 'working', note);
    log(state, {
      kind: 'review',
      taskId: task.id,
      teamId: team.id,
      agentId: reviewer?.id,
      message: `${team.name} reviewer requested changes on ${task.id}: ${note}${byNote}`,
    });
  }
  const { status, approved, complete } = settleReview(state, task);
  touch(task);
  return { task, team, approved, pending: status.pending, changes: status.changes, complete };
}

// Close the current review round once every team on the task has a verdict:
// all approved -> stays in review, approvedRound = round; any changes -> back
// to 'active'. Still pending -> nothing happens. Used after each verdict and
// when a team leaves a task in review.
function settleReview(state, task) {
  const status = reviewStatus(state, task);
  const round = status.round;
  const onTask = status.approved.length + status.changes.length + status.pending.length;
  const complete = task.status === 'review' && onTask > 0 && status.pending.length === 0;
  const approved = complete && status.changes.length === 0;
  if (complete) {
    if (approved) {
      if (task.approvedRound !== round) {
        task.approvedRound = round;
        log(state, { kind: 'review', taskId: task.id, message: `${task.id} approved by all reviewers (round ${round})` });
      }
    } else {
      task.status = 'active'; // back to the builders
      task.approvedRound = 0; // sign-off is void once a reviewer asks for changes
      syncActive(task);
      const names = status.changes.map((id) => getTeam(state, id).name).join(', ');
      log(state, { kind: 'review', taskId: task.id, message: `${task.id} back to the builders: changes requested by ${names}` });
    }
    touch(task);
  }
  return { status, approved, complete };
}

export function deleteTask(state, taskId, { by = 'user' } = {}) {
  const task = getTask(state, taskId);
  for (const team of teamsOn(state, task.id)) detach(state, task, team, { by, reason: 'task deleted', settle: false });
  if (task.activeSince) {
    task.activeMs = (task.activeMs || 0) + since(task.activeSince, clockNow());
    task.activeSince = null;
  }
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
  for (const task of state.tasks) {
    if (typeof task.activeMs !== 'number' || !Number.isFinite(task.activeMs)) task.activeMs = 0;
    if (!task.teamMs || typeof task.teamMs !== 'object') task.teamMs = {};
    if (!Number.isInteger(task.reviewRound) || task.reviewRound < 0) task.reviewRound = 0;
    if (!Number.isInteger(task.approvedRound) || task.approvedRound < 0) task.approvedRound = 0;
    if (!task.reviews || typeof task.reviews !== 'object' || Array.isArray(task.reviews)) task.reviews = {};
    if (typeof task.issuesFound !== 'number' || !Number.isFinite(task.issuesFound)) task.issuesFound = 0;
    if (!task.teamIssues || typeof task.teamIssues !== 'object' || Array.isArray(task.teamIssues)) task.teamIssues = {};
    for (const [teamId, r] of Object.entries(task.reviews)) {
      const n = Number(r?.issues);
      if (!(teamId in task.teamIssues)) task.teamIssues[teamId] = Number.isInteger(n) && n > 0 ? n : 0;
    }
    if (isRunning(task)) task.activeSince ||= task.startedAt || task.updatedAt || now();
    else task.activeSince = null;
  }
  const existing = new Map((state.teams || []).map((t) => [t.id, t]));
  state.teams = fresh.teams.map((t) => {
    const old = existing.get(t.id);
    if (!old) return t;
    const agents = new Map((old.agents || []).map((a) => [a.id, a]));
    // Agents come from the current roster (adds the reviewer to old states);
    // saved status/work is kept, identity (role, and the reviewer's name) is not.
    return {
      ...t,
      ...old,
      agents: t.agents.map((a) => {
        const saved = agents.get(a.id);
        if (!saved) return a;
        return a.role === 'reviewer' ? { ...a, ...saved, role: a.role, name: a.name } : { ...a, ...saved, role: a.role };
      }),
    };
  });
  const tasks = new Map(state.tasks.map((t) => [t.id, t]));
  for (const team of state.teams) {
    if (team.taskId && !tasks.has(team.taskId)) team.taskId = null;
    if (typeof team.totalMs !== 'number' || !Number.isFinite(team.totalMs)) team.totalMs = 0;
    if (typeof team.issuesFound !== 'number' || !Number.isFinite(team.issuesFound)) team.issuesFound = 0;
    const reviewer = reviewerOf(team);
    if (team.taskId && reviewer && reviewer.status === 'idle') reviewer.status = 'assigned';
    if (team.taskId) team.assignedAt ||= tasks.get(team.taskId).startedAt || now();
    else team.assignedAt = null;
  }
  // States saved before approvedRound existed: a task in review whose teams
  // all approved the current round is signed off.
  for (const task of state.tasks) {
    if (task.status !== 'review' || !task.reviewRound || task.approvedRound === task.reviewRound) continue;
    const st = reviewStatus(state, task);
    if (st.approved.length && !st.changes.length && !st.pending.length) task.approvedRound = task.reviewRound;
  }
  return state;
}
