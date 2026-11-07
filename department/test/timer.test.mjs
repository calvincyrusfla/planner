import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';

// A fake clock that each test resets, so time only moves when we say so.
const START = Date.parse('2026-01-01T00:00:00.000Z');
let clock = START;
const advance = (ms) => (clock += ms);
const iso = (ms = clock) => new Date(ms).toISOString();

beforeEach(() => {
  clock = START;
  core.setClock(() => clock);
});
afterEach(() => core.setClock(() => Date.now()));

const fresh = () => core.createInitialState();
const team = (s, id) => core.getTeam(s, id);
const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;

test('clockNow follows setClock', () => {
  assert.equal(core.clockNow(), START);
  advance(5 * SEC);
  assert.equal(core.clockNow(), START + 5 * SEC);
});

test('new tasks and teams start with zeroed time fields', () => {
  const s = fresh();
  for (const t of s.teams) {
    assert.equal(t.totalMs, 0);
    assert.equal(t.assignedAt, null);
  }
  core.updateSettings(s, { autoDispatch: false });
  const { task } = core.addTask(s, { title: 'Idle' });
  assert.equal(task.activeMs, 0);
  assert.equal(task.activeSince, null);
  assert.deepEqual(task.teamMs, {});
});

test('a task accrues active time only while active', () => {
  const s = fresh();
  core.updateSettings(s, { autoDispatch: false });
  const { task } = core.addTask(s, { title: 'Waits first' });
  assert.equal(task.status, 'queued');
  advance(5 * SEC);
  assert.equal(core.taskActiveMs(task), 0);

  core.assignTeam(s, task.id, 'alpha');
  assert.equal(task.status, 'active');
  assert.equal(task.activeSince, iso());
  assert.equal(team(s, 'alpha').assignedAt, iso());
  advance(10 * SEC);
  assert.equal(task.activeMs, 0, 'open interval is not closed yet');
  assert.equal(core.taskActiveMs(task), 10 * SEC);
  assert.equal(core.taskActiveMs(task, clock + 2 * SEC), 12 * SEC, 'explicit `at` is honoured');
});

test('pausing stops the timer and resuming continues it', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Pause me' });
  advance(10 * SEC);
  core.updateTask(s, task.id, { status: 'paused' });
  assert.equal(task.status, 'paused');
  assert.equal(task.activeMs, 10 * SEC);
  assert.equal(task.activeSince, null);
  assert.equal(task.teamMs.alpha, 10 * SEC);
  assert.equal(team(s, 'alpha').totalMs, 10 * SEC);
  assert.equal(team(s, 'alpha').assignedAt, null);

  advance(50 * SEC);
  assert.equal(core.taskActiveMs(task), 10 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 10 * SEC);

  core.assignTeam(s, task.id, 'alpha');
  assert.equal(task.activeSince, iso());
  advance(5 * SEC);
  assert.equal(core.taskActiveMs(task), 15 * SEC);
  assert.equal(core.taskTeamMs(s, task).alpha, 15 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 15 * SEC);
});

test('requeueing stops the timer and dispatching again continues it', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Requeue me' });
  advance(20 * SEC);
  core.updateSettings(s, { autoDispatch: false });
  core.updateTask(s, task.id, { status: 'queued' });
  assert.equal(task.status, 'queued');
  assert.equal(task.activeMs, 20 * SEC);
  assert.equal(task.activeSince, null);
  assert.equal(team(s, 'alpha').totalMs, 20 * SEC);
  assert.equal(team(s, 'alpha').assignedAt, null);

  advance(HOUR);
  assert.equal(core.taskActiveMs(task), 20 * SEC);

  core.updateSettings(s, { autoDispatch: true });
  core.dispatch(s);
  assert.equal(task.status, 'active');
  advance(7 * SEC);
  assert.equal(core.taskActiveMs(task), 27 * SEC);
  assert.equal(core.taskTeamMs(s, task).alpha, 27 * SEC);
});

test('done freezes the timer and reopening continues from the frozen value', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Finish me' });
  advance(20 * SEC);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
  assert.equal(task.activeMs, 20 * SEC);
  assert.equal(task.activeSince, null);
  assert.equal(task.finishedAt, iso());
  assert.equal(task.teamMs.alpha, 20 * SEC);
  assert.equal(team(s, 'alpha').assignedAt, null);

  advance(3 * HOUR);
  assert.equal(core.taskActiveMs(task), 20 * SEC);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 20 * SEC });
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 20 * SEC);

  // Reopen: requeue it, the free team picks it straight back up.
  core.updateTask(s, task.id, { status: 'queued' });
  assert.equal(task.status, 'active');
  assert.equal(task.finishedAt, null);
  advance(5 * SEC);
  assert.equal(core.taskActiveMs(task), 25 * SEC);
  assert.equal(core.taskTeamMs(s, task).alpha, 25 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 25 * SEC);
});

test('a large task with three teams accrues time per team', () => {
  const s = fresh();
  const { task, assigned } = core.addTask(s, { title: 'Big', size: 'large' });
  assert.deepEqual(assigned, ['alpha', 'bravo', 'charlie']);
  advance(30 * SEC);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 30 * SEC, bravo: 30 * SEC, charlie: 30 * SEC });
  assert.equal(core.taskActiveMs(task), 30 * SEC, 'active time is wall-clock, not summed per team');

  core.unassignTeam(s, task.id, 'charlie');
  assert.equal(task.status, 'active');
  assert.equal(task.activeSince, iso(START), 'task stays open while other teams remain');
  assert.equal(task.teamMs.charlie, 30 * SEC);
  advance(10 * SEC);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 40 * SEC, bravo: 40 * SEC, charlie: 30 * SEC });
  assert.equal(core.taskActiveMs(task), 40 * SEC);
  for (const id of ['alpha', 'bravo']) assert.equal(core.teamTotalMs(team(s, id)), 40 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'charlie')), 30 * SEC);
});

test('moving a team to another task splits its time between both', () => {
  const s = fresh();
  const a = core.addTask(s, { title: 'A' }).task; // alpha
  const b = core.addTask(s, { title: 'B' }).task; // bravo
  advance(10 * SEC);
  core.assignTeam(s, b.id, 'alpha');
  assert.equal(a.status, 'paused');
  assert.equal(a.activeMs, 10 * SEC);
  assert.equal(a.activeSince, null);
  assert.equal(a.teamMs.alpha, 10 * SEC);
  assert.equal(team(s, 'alpha').totalMs, 10 * SEC);
  assert.equal(team(s, 'alpha').assignedAt, iso(), 'assignedAt restarts on the new task');

  advance(20 * SEC);
  assert.deepEqual(core.taskTeamMs(s, a), { alpha: 10 * SEC });
  assert.deepEqual(core.taskTeamMs(s, b), { alpha: 20 * SEC, bravo: 30 * SEC });
  assert.equal(core.taskActiveMs(a), 10 * SEC);
  assert.equal(core.taskActiveMs(b), 30 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 30 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'bravo')), 30 * SEC);
});

test("removing a task's last team pauses it and stops both timers", () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Solo' });
  advance(12 * SEC);
  core.unassignTeam(s, task.id, 'alpha');
  assert.equal(task.status, 'paused');
  assert.equal(task.activeSince, null);
  assert.equal(task.activeMs, 12 * SEC);
  assert.equal(task.teamMs.alpha, 12 * SEC);
  const alpha = team(s, 'alpha');
  assert.equal(alpha.assignedAt, null);
  assert.equal(alpha.totalMs, 12 * SEC);

  advance(HOUR);
  assert.equal(core.taskActiveMs(task), 12 * SEC);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 12 * SEC });
  assert.equal(core.teamTotalMs(alpha), 12 * SEC);
});

test("deleting a task keeps the team's totalMs", () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Doomed' });
  advance(15 * SEC);
  core.deleteTask(s, task.id);
  const alpha = team(s, 'alpha');
  assert.equal(alpha.taskId, null);
  assert.equal(alpha.assignedAt, null);
  assert.equal(alpha.totalMs, 15 * SEC);
  advance(HOUR);
  assert.equal(core.teamTotalMs(alpha), 15 * SEC);
  const stats = core.teamStats(s).find((x) => x.teamId === 'alpha');
  assert.equal(stats.totalMs, 15 * SEC);
  assert.equal(stats.tasksDone, 0);
});

test('taskTeamMs and teamTotalMs include open intervals', () => {
  const s = fresh();
  const first = core.addTask(s, { title: 'First' }).task;
  advance(10 * SEC);
  core.updateTask(s, first.id, { status: 'done' });
  const second = core.addTask(s, { title: 'Second' }).task; // alpha again
  advance(4 * SEC);
  const alpha = team(s, 'alpha');
  assert.equal(alpha.totalMs, 10 * SEC, 'closed total only');
  assert.equal(core.teamTotalMs(alpha), 14 * SEC);
  assert.equal(core.teamTotalMs(alpha, clock + 6 * SEC), 20 * SEC);
  assert.deepEqual(second.teamMs, {}, 'nothing closed yet on the second task');
  assert.deepEqual(core.taskTeamMs(s, second), { alpha: 4 * SEC });
  assert.deepEqual(core.taskTeamMs(s, second, clock + SEC), { alpha: 5 * SEC });
  // An idle team's total is just its closed time.
  assert.equal(core.teamTotalMs(team(s, 'bravo')), 0);
});

test('teamStats reports totals, current task, tasksDone and avgDoneMs', () => {
  const s = fresh();
  const a = core.addTask(s, { title: 'A' }).task;
  advance(10 * SEC);
  core.updateTask(s, a.id, { status: 'done' });
  const b = core.addTask(s, { title: 'B' }).task;
  advance(30 * SEC);
  core.updateTask(s, b.id, { status: 'done' });
  const c = core.addTask(s, { title: 'C' }).task;
  advance(5 * SEC);
  core.updateTask(s, c.id, { status: 'cancelled' }); // cancelled work counts toward total, not tasksDone
  const d = core.addTask(s, { title: 'D' }).task;
  advance(7 * SEC);

  const stats = core.teamStats(s);
  assert.equal(stats.length, 3);
  const alpha = stats.find((x) => x.teamId === 'alpha');
  const bravo = stats.find((x) => x.teamId === 'bravo');
  assert.equal(alpha.name, team(s, 'alpha').name);
  assert.equal(alpha.color, team(s, 'alpha').color);
  assert.equal(alpha.totalMs, 52 * SEC, 'includes the open interval on D');
  assert.equal(alpha.currentTaskId, d.id);
  assert.equal(alpha.currentMs, 7 * SEC);
  assert.equal(alpha.tasksDone, 2);
  assert.equal(alpha.avgDoneMs, 20 * SEC);

  assert.equal(bravo.totalMs, 0);
  assert.equal(bravo.currentTaskId, null);
  assert.equal(bravo.currentMs, 0);
  assert.equal(bravo.tasksDone, 0);
  assert.equal(bravo.avgDoneMs, 0);

  const later = core.teamStats(s, clock + 3 * SEC).find((x) => x.teamId === 'alpha');
  assert.equal(later.currentMs, 10 * SEC);
  assert.equal(later.totalMs, 55 * SEC);
});

test('formatDuration boundaries', () => {
  const cases = [
    [0, '0s'],
    [45_000, '45s'],
    [59_999, '59s'],
    [60_000, '1m'],
    [12 * MIN, '12m'],
    [3_599_999, '59m'],
    [3_600_000, '1h 00m'],
    [3_660_000, '1h 01m'],
    [3_900_000, '1h 05m'],
    [86_399_999, '23h 59m'],
    [86_400_000, '1d 0h'],
    [90_000_000, '1d 1h'],
    [2 * 86_400_000 + 3 * HOUR, '2d 3h'],
  ];
  for (const [ms, want] of cases) assert.equal(core.formatDuration(ms), want, `formatDuration(${ms})`);
});

test('normalize migrates an old state without time fields', () => {
  const startedAt = iso(START - 2 * HOUR);
  const created = iso(START - 3 * HOUR);
  const old = fresh();
  for (const t of old.teams) {
    delete t.totalMs;
    delete t.assignedAt;
  }
  old.teams[0].taskId = 'T-001';
  old.nextTaskNumber = 3;
  const base = { description: '', size: 'small', progress: 0, teamHistory: ['alpha'], createdAt: created };
  old.tasks = [
    { ...base, id: 'T-001', title: 'Running', status: 'active', updatedAt: startedAt, startedAt, finishedAt: null },
    { ...base, id: 'T-002', title: 'Old done', status: 'done', progress: 100, updatedAt: created, startedAt: created, finishedAt: startedAt },
  ];

  let s;
  assert.doesNotThrow(() => (s = core.normalize(old)));
  const [running, finished] = s.tasks;
  assert.equal(running.activeMs, 0);
  assert.deepEqual(running.teamMs, {});
  assert.equal(running.activeSince, startedAt);
  assert.equal(finished.activeMs, 0);
  assert.deepEqual(finished.teamMs, {});
  assert.equal(finished.activeSince, null);

  const alpha = team(s, 'alpha');
  assert.equal(alpha.totalMs, 0);
  assert.equal(alpha.assignedAt, startedAt);
  for (const id of ['bravo', 'charlie']) {
    assert.equal(team(s, id).totalMs, 0);
    assert.equal(team(s, id).assignedAt, null);
  }

  // Migrated values feed the helpers sensibly.
  assert.equal(core.taskActiveMs(running), 2 * HOUR);
  assert.deepEqual(core.taskTeamMs(s, running), { alpha: 2 * HOUR });
  assert.equal(core.teamTotalMs(alpha), 2 * HOUR);
  assert.equal(core.taskActiveMs(finished), 0);
  assert.doesNotThrow(() => core.teamStats(s));

  // And normal operations keep working on the migrated state.
  advance(MIN);
  core.updateTask(s, running.id, { status: 'done' });
  assert.equal(running.activeMs, 2 * HOUR + MIN);
  assert.equal(running.teamMs.alpha, 2 * HOUR + MIN);
  assert.equal(alpha.totalMs, 2 * HOUR + MIN);
});

test('normalize is idempotent on an already-migrated state', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Keep' });
  advance(9 * SEC);
  core.updateTask(s, task.id, { status: 'paused' });
  const copy = core.normalize(JSON.parse(JSON.stringify(s)));
  assert.equal(copy.tasks[0].activeMs, 9 * SEC);
  assert.deepEqual(copy.tasks[0].teamMs, { alpha: 9 * SEC });
  assert.equal(core.getTeam(copy, 'alpha').totalMs, 9 * SEC);
});
