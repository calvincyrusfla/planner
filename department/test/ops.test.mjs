import test from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';
import { applyOp, OP_TYPES } from '../lib/ops.mjs';

const fresh = () => core.createInitialState();
const teamsOn = (s, id) => core.teamsOn(s, id).map((t) => t.id);
const op = (s, type, args) => applyOp(s, { type, args });
const json = (v) => JSON.parse(JSON.stringify(v));

// ---------- simple ops ----------

test('addTask maps onto core.addTask', () => {
  const s = fresh();
  const r = op(s, 'addTask', { title: 'Fix login', description: 'd', size: 'small' });
  assert.equal(r.task.id, 'T-001');
  assert.deepEqual(r.assigned, ['alpha']);
  assert.equal(s.tasks[0].description, 'd');
  assert.doesNotThrow(() => JSON.stringify(r));
});

test('updateTask passes patch and by', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  const t = op(s, 'updateTask', { id: 'T-001', patch: { status: 'done', force: true }, by: 'tester' });
  assert.equal(t.status, 'done');
  assert.match(s.log.at(-1).message, /by tester/);
  assert.deepEqual(teamsOn(s, 'T-001'), []);
});

test('deleteTask removes the task', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  op(s, 'deleteTask', { id: 'T-001', by: 'me' });
  assert.equal(s.tasks.length, 0);
  assert.match(s.log.at(-1).message, /deleted by me/);
});

test('assignTeam and unassignTeam', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  const r = op(s, 'assignTeam', { taskId: 'T-001', teamId: 'bravo', focus: 'api', by: 'me' });
  assert.equal(r.team.focus, 'api');
  assert.deepEqual(teamsOn(s, 'T-001'), ['alpha', 'bravo']);
  op(s, 'unassignTeam', { taskId: 'T-001', teamId: 'alpha', by: 'me' });
  assert.deepEqual(teamsOn(s, 'T-001'), ['bravo']);
});

test('reportProgress passes the whole body', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  const r = op(s, 'reportProgress', { taskId: 'T-001', agentId: 'alpha-2', progress: 40, message: 'half' });
  assert.equal(r.agent.status, 'working');
  assert.equal(s.teams[0].progress, 40);
  assert.equal(s.tasks[0].progress, 40);
});

test('dispatch and updateSettings', () => {
  const s = fresh();
  op(s, 'updateSettings', { autoDispatch: false });
  assert.equal(s.settings.autoDispatch, false);
  op(s, 'addTask', { title: 'A' });
  assert.equal(s.tasks[0].status, 'queued');
  s.settings.autoDispatch = true;
  const assigned = op(s, 'dispatch', {});
  assert.deepEqual(assigned, [{ taskId: 'T-001', teamId: 'alpha' }]);
  const settings = op(s, 'updateSettings', { autoDispatch: true });
  assert.deepEqual(settings, { autoDispatch: true });
});

test('every op type is covered and missing args are tolerated', () => {
  assert.deepEqual(OP_TYPES.sort(), [
    'addTask', 'answerHelp', 'assignTeam', 'cancelBatch', 'cancelFoundryUpdate', 'claimBatch', 'claimFoundryUpdate', 'deleteTask', 'dispatch',
    'dispatchBatch', 'finishFoundryUpdate', 'importState', 'reportFoundryMetrics', 'reportProgress', 'reportUsage', 'requestFoundryUpdate',
    'reset', 'reviewTask', 'unassignTeam', 'updateBatch', 'updateSettings', 'updateTask',
  ]);
  const s = fresh();
  assert.deepEqual(applyOp(s, { type: 'dispatch' }), []);
});

test('unknown op type throws DeptError', () => {
  const s = fresh();
  assert.throws(() => op(s, 'explode', {}), (e) => e instanceof core.DeptError && e.status === 400 && /Unknown operation explode/.test(e.message));
  assert.throws(() => applyOp(s, {}), core.DeptError);
  assert.throws(() => op(s, 'toString', {}), core.DeptError);
});

test('core errors propagate with their status', () => {
  const s = fresh();
  assert.throws(() => op(s, 'deleteTask', { id: 'T-999' }), (e) => e instanceof core.DeptError && e.status === 404);
  assert.throws(() => op(s, 'addTask', {}), (e) => e instanceof core.DeptError && e.status === 400);
});

test('reset replaces contents in place', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  s.stray = 1;
  const ref = s;
  op(s, 'reset', {});
  assert.equal(s, ref);
  assert.deepEqual(s, fresh());
  assert.equal('stray' in s, false);
});

// ---------- importState ----------

function boardWith(titles, setup) {
  const s = fresh();
  for (const t of titles) core.addTask(s, typeof t === 'string' ? { title: t } : t);
  setup?.(s);
  return s;
}

test('importing a board into itself adds nothing', () => {
  const s = boardWith(['A', 'B', 'C', 'D']);
  const before = json(s);
  const r = op(s, 'importState', { state: json(s) });
  assert.deepEqual(r, { added: 0, skipped: 4, idMap: {}, batches: { added: 0, updated: 0, idMap: {}, rejected: [] } });
  assert.deepEqual(s, before);
});

test('importing into an empty board keeps tasks, teams and log', () => {
  const src = boardWith(['A', { title: 'B' }], (s) => {
    core.reportProgress(s, { agentId: 'alpha-1', progress: 30, message: 'going' });
    core.assignTeam(s, 'T-002', 'charlie', { focus: 'ui' });
  });
  const s = fresh();
  const r = op(s, 'importState', { state: json(src) });
  assert.equal(r.added, 2);
  assert.equal(r.skipped, 0);
  assert.deepEqual(r.idMap, { 'T-001': 'T-001', 'T-002': 'T-002' });
  assert.equal(s.nextTaskNumber, 3);
  assert.deepEqual(teamsOn(s, 'T-001'), ['alpha']);
  assert.deepEqual(teamsOn(s, 'T-002'), ['bravo', 'charlie']);
  assert.equal(core.getTeam(s, 'alpha').progress, 30);
  assert.equal(core.getTeam(s, 'charlie').focus, 'ui');
  assert.equal(core.getAgent(s, 'alpha-1').agent.status, 'working');
  assert.deepEqual(s.log, src.log);
  assert.deepEqual(s.tasks, src.tasks);
});

test('conflicting ids are renumbered and their log entries rewritten', () => {
  const here = boardWith(['Local one']);
  const there = boardWith(['Remote one', 'Remote two']);
  // Make createdAt differ so T-001 is a different task on each board.
  there.tasks[0].createdAt = '2000-01-01T00:00:00.000Z';
  const r = op(here, 'importState', { state: json(there) });
  assert.equal(r.added, 2);
  assert.deepEqual(r.idMap, { 'T-001': 'T-002', 'T-002': 'T-003' });
  assert.deepEqual(here.tasks.map((t) => [t.id, t.title]), [
    ['T-001', 'Local one'], ['T-002', 'Remote one'], ['T-003', 'Remote two'],
  ]);
  assert.equal(here.nextTaskNumber, 4);
  const created = here.log.filter((e) => e.kind === 'create').map((e) => [e.taskId, e.message]);
  assert.deepEqual(created.sort(), [
    ['T-001', 'T-001 created (small): Local one'],
    ['T-002', 'T-002 created (small): Remote one'],
    ['T-003', 'T-003 created (small): Remote two'],
  ]);
  // Chained renames are not applied twice (T-001 -> T-002 must not become T-003).
  assert.ok(!here.log.some((e) => e.taskId === 'T-002' && /T-003/.test(e.message)));
});

test('same id + createdAt is skipped, others are added', () => {
  const here = boardWith(['A']);
  const there = json(here);
  core.addTask(there, { title: 'B' });
  const r = op(here, 'importState', { state: there });
  assert.equal(r.added, 1);
  assert.equal(r.skipped, 1);
  assert.deepEqual(r.idMap, { 'T-002': 'T-002' });
  assert.equal(here.tasks.length, 2);
  // shared log entries are not duplicated
  assert.equal(here.log.filter((e) => e.kind === 'create' && e.taskId === 'T-001').length, 1);
});

test('imported open tasks keep a team only if it is free, else queue', () => {
  const here = boardWith(['Local busy alpha']); // alpha busy here
  const there = boardWith(['Remote on alpha', 'Remote on bravo']);
  there.tasks.forEach((t) => (t.createdAt = '2001-01-01T00:00:00.000Z'));
  const r = op(here, 'importState', { state: json(there) });
  const [onAlpha, onBravo] = [r.idMap['T-001'], r.idMap['T-002']];
  assert.equal(core.getTeam(here, 'alpha').taskId, 'T-001');
  assert.equal(core.getTeam(here, 'bravo').taskId, onBravo);
  // onAlpha lost its team, was queued, then dispatch handed it charlie.
  assert.deepEqual(teamsOn(here, onAlpha), ['charlie']);
  assert.equal(core.getTask(here, onAlpha).status, 'active');
});

test('an imported task with no free team stays queued', () => {
  const here = boardWith([{ title: 'Big', size: 'large' }]); // all teams busy
  const there = boardWith(['Remote']);
  there.tasks[0].createdAt = '2001-01-01T00:00:00.000Z';
  there.tasks[0].activeSince = '2001-01-01T00:00:00.000Z';
  const r = op(here, 'importState', { state: json(there) });
  const t = core.getTask(here, r.idMap['T-001']);
  assert.equal(t.status, 'queued');
  assert.equal(t.activeSince, null);
  assert.deepEqual(teamsOn(here, t.id), []);
  assert.deepEqual(teamsOn(here, 'T-001'), ['alpha', 'bravo', 'charlie']);
});

test('done and cancelled tasks import without teams', () => {
  const there = boardWith(['Done', 'Cancelled', 'Open'], (s) => {
    core.updateTask(s, 'T-001', { status: 'done', force: true });
    core.updateTask(s, 'T-002', { status: 'cancelled' });
  });
  // Simulate a stale team pointer on the source board.
  there.teams.find((t) => t.taskId === null).taskId = 'T-001';
  const s = fresh();
  op(s, 'importState', { state: json(there) });
  assert.equal(core.getTask(s, 'T-001').status, 'done');
  assert.equal(core.getTask(s, 'T-002').status, 'cancelled');
  assert.deepEqual(teamsOn(s, 'T-001'), []);
  assert.deepEqual(teamsOn(s, 'T-002'), []);
  assert.equal(core.getTask(s, 'T-003').status, 'active');
});

test('unknown task and team fields pass through untouched', () => {
  const there = boardWith(['A']);
  Object.assign(there.tasks[0], { activeMs: 1234, activeSince: '2026-01-01T00:00:00.000Z', teamMs: { alpha: 1234 }, extra: { deep: [1] } });
  Object.assign(core.getTeam(there, 'alpha'), { assignedAt: '2026-01-01T00:00:00.000Z', totalMs: 999 });
  const s = fresh();
  const src = json(there);
  op(s, 'importState', { state: src });
  const t = core.getTask(s, 'T-001');
  assert.equal(t.activeMs, 1234);
  assert.equal(t.activeSince, '2026-01-01T00:00:00.000Z');
  assert.deepEqual(t.teamMs, { alpha: 1234 });
  assert.deepEqual(t.extra, { deep: [1] });
  assert.equal(core.getTeam(s, 'alpha').assignedAt, '2026-01-01T00:00:00.000Z');
  // copies, not shared references into the imported object
  t.extra.deep.push(2);
  assert.deepEqual(src.tasks[0].extra.deep, [1]);
});

test('logs are merged, sorted by time and capped at 500', () => {
  const here = fresh();
  const there = fresh();
  for (let i = 0; i < 300; i++) {
    here.log.push({ at: new Date(Date.UTC(2026, 0, 1, 0, 0, i * 2)).toISOString(), kind: 'note', message: `here ${i}` });
    there.log.push({ at: new Date(Date.UTC(2026, 0, 1, 0, 0, i * 2 + 1)).toISOString(), kind: 'note', message: `there ${i}` });
  }
  op(here, 'importState', { state: json(there) });
  assert.equal(here.log.length, 500);
  const ats = here.log.map((e) => e.at);
  assert.deepEqual(ats, [...ats].sort());
  assert.equal(here.log.at(-1).message, 'there 299');
  assert.equal(here.log[0].message, 'here 50');
});

test('importState runs dispatch afterwards', () => {
  const there = fresh();
  there.settings.autoDispatch = false;
  core.addTask(there, { title: 'Waiting' });
  assert.equal(there.tasks[0].status, 'queued');
  const s = fresh();
  op(s, 'importState', { state: json(there) });
  assert.equal(s.tasks[0].status, 'active');
  assert.deepEqual(teamsOn(s, 'T-001'), ['alpha']);
});

test('importState rejects something that is not a board', () => {
  assert.throws(() => op(fresh(), 'importState', {}), core.DeptError);
  assert.throws(() => op(fresh(), 'importState', { state: { tasks: 'no' } }), core.DeptError);
});

test('importState result is JSON-serialisable', () => {
  const s = fresh();
  const r = op(s, 'importState', { state: json(boardWith(['A'])) });
  assert.deepEqual(json(r), r);
});

test('importState credits team issuesFound from added tasks only once', () => {
  const other = core.createInitialState();
  const t = core.addTask(other, { title: 'Reviewed elsewhere', size: 'large' }).task;
  core.updateTask(other, t.id, { status: 'review' });
  core.reviewTask(other, { taskId: t.id, teamId: 'alpha', verdict: 'changes', note: 'x', issues: 2 });
  core.reviewTask(other, { taskId: t.id, teamId: 'bravo', verdict: 'approved', issues: 1 });
  core.reviewTask(other, { taskId: t.id, teamId: 'charlie', verdict: 'approved' });
  core.updateTask(other, t.id, { status: 'done', force: true });
  const here = core.createInitialState();
  applyOp(here, { type: 'importState', args: { state: json(other) } });
  const issues = (id) => here.teams.find((x) => x.id === id).issuesFound;
  assert.equal(issues('alpha'), 2);
  assert.equal(issues('bravo'), 1);
  assert.equal(issues('charlie'), 0);
  applyOp(here, { type: 'importState', args: { state: json(other) } });
  assert.equal(issues('alpha'), 2, 'a second import of the same board does not double it');
  assert.equal(issues('bravo'), 1);
});

test('importState credits team time from added tasks only once', () => {
  const other = core.createInitialState();
  const t = core.addTask(other, { title: 'Done elsewhere' }).task;
  core.updateTask(other, t.id, { status: 'done', force: true });
  t.teamMs = { alpha: 60_000 };
  const here = core.createInitialState();
  applyOp(here, { type: 'importState', args: { state: other } });
  assert.equal(here.teams.find((x) => x.id === 'alpha').totalMs, 60_000);
  applyOp(here, { type: 'importState', args: { state: other } });
  assert.equal(here.teams.find((x) => x.id === 'alpha').totalMs, 60_000);
});

test('pushing the same board twice adds nothing the second time, even after renumbering', () => {
  const hubBoard = core.createInitialState();
  core.addTask(hubBoard, { title: 'Already on the hub' });
  const pc = core.createInitialState();
  core.addTask(pc, { title: 'From the PC' }); // T-001 here, T-002 on the hub
  const first = applyOp(hubBoard, { type: 'importState', args: { state: pc } });
  assert.equal(first.added, 1);
  assert.equal(first.idMap['T-001'], 'T-002');
  const second = applyOp(hubBoard, { type: 'importState', args: { state: pc } });
  assert.equal(second.added, 0);
  assert.equal(second.skipped, 1);
  assert.equal(hubBoard.tasks.length, 2);
});

// ---------- alpha-1 (round 3): importState credits teamIssues (all rounds) ----------

test('alpha-1: importState credits issues from every round via teamIssues, once', () => {
  const other = core.createInitialState();
  const t = core.addTask(other, { title: 'Two rounds elsewhere' }).task;
  core.updateTask(other, t.id, { status: 'review' });
  core.reviewTask(other, { taskId: t.id, teamId: 'alpha', verdict: 'changes', note: 'x', issues: 2 });
  core.updateTask(other, t.id, { status: 'review' });
  core.reviewTask(other, { taskId: t.id, teamId: 'alpha', verdict: 'approved' }); // latest verdict: 0 issues
  core.updateTask(other, t.id, { status: 'done' });
  assert.deepEqual(t.teamIssues, { alpha: 2 });
  const here = core.createInitialState();
  here.teams[0].issuesFound = 1;
  op(here, 'importState', { state: json(other) });
  assert.equal(here.teams[0].issuesFound, 3, 'credits the round-1 issues, not just the latest verdict');
  op(here, 'importState', { state: json(other) });
  assert.equal(here.teams[0].issuesFound, 3, 're-push does not double count');
});

test('alpha-1: importState falls back to reviews for boards without teamIssues', () => {
  const other = core.createInitialState();
  const t = core.addTask(other, { title: 'Older board' }).task;
  core.updateTask(other, t.id, { status: 'review' });
  core.reviewTask(other, { taskId: t.id, teamId: 'alpha', verdict: 'changes', note: 'x', issues: 4 });
  const old = json(other);
  delete old.tasks[0].teamIssues;
  const here = core.createInitialState();
  op(here, 'importState', { state: old });
  assert.equal(here.teams[0].issuesFound, 4);
});

// ---------- answerHelp ----------

test('answerHelp records the reply on a subtask in needs_help', () => {
  const s = fresh();
  const batch = op(s, 'dispatchBatch', {
    title: 'Needs a hand',
    repo: 'owner/name',
    subtasks: [{ title: 'One', description: 'Do the thing', files: ['a.py'], size: 'S' }],
    by: 'fm',
  });
  const { id } = batch;
  const key = batch.subtasks[0].key;
  assert.equal(key, '1');
  const worker = { id, host: 'pc', service: 'foundry' };
  assert.equal(op(s, 'claimBatch', worker).ok, true);
  op(s, 'updateBatch', { ...worker, results: { [key]: { state: 'needs_help', help: { question: 'Q?' } } } });
  const r = op(s, 'answerHelp', { id, key, answer: 'Use X', by: 'fm' });
  assert.equal(r.ok, true);
  assert.equal(r.batch.results[key].state, 'answered');
  assert.equal(r.batch.results[key].help.answer, 'Use X');
});
