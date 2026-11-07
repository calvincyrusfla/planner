// Crew sizing (T-008): each task carries `builders` (1-5), the number of
// builders per team that work on it. Builders beyond the crew stand by; the
// team's reviewer always reviews.
import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';
import { applyOp, importState } from '../lib/ops.mjs';

// Fake clock, as in timer.test.mjs: time only moves when a test says so.
const START = Date.parse('2026-01-01T00:00:00.000Z');
let clock = START;
const advance = (ms) => (clock += ms);

beforeEach(() => {
  clock = START;
  core.setClock(() => clock);
});
afterEach(() => core.setClock(() => Date.now()));

const SEC = 1000;
const MIN = 60 * SEC;
const fresh = () => core.createInitialState();
const team = (s, id) => core.getTeam(s, id);
const agent = (s, id) => core.getAgent(s, id).agent;
const teamsOn = (s, id) => core.teamsOn(s, id).map((t) => t.id);
const op = (s, type, args) => applyOp(s, { type, args });
const json = (v) => JSON.parse(JSON.stringify(v));
const crewError = (e) => e instanceof core.DeptError && e.status === 400 && /builders must be 1.5/.test(e.message);
const INVALID = [0, 6, 2.5, 'x', -1, true, '0', '6', '2.5', NaN];

const builderIds = (teamId) => [1, 2, 3, 4, 5].map((n) => `${teamId}-${n}`);

// Builders 1..n are on the task, n+1..5 stand by, per the contract.
function assertCrew(s, teamId, n, { skip = [] } = {}) {
  for (const [i, id] of builderIds(teamId).entries()) {
    if (skip.includes(id)) continue;
    const a = agent(s, id);
    if (i < n) {
      assert.equal(a.status, 'assigned', `${id} should be assigned (crew ${n})`);
      assert.notEqual(a.work, 'Standby', `${id} is on the crew, not standby`);
    } else {
      assert.equal(a.status, 'idle', `${id} should be idle (crew ${n})`);
      assert.equal(a.work, 'Standby', `${id} should be on standby (crew ${n})`);
    }
  }
}

// ---------- defaults and exports ----------

test('DEFAULT_BUILDERS is small 2, large 5', () => {
  assert.deepEqual({ ...core.DEFAULT_BUILDERS }, { small: 2, large: 5 });
});

test('a small task defaults to 2 builders, a large one to 5', () => {
  const s = fresh();
  assert.equal(core.addTask(s, { title: 'Small' }).task.builders, 2);
  assert.equal(core.addTask(s, { title: 'Small again', size: 'small' }).task.builders, 2);
  assert.equal(core.addTask(s, { title: 'Big', size: 'large' }).task.builders, 5);
});

test('addTask accepts an explicit crew of 1 to 5 regardless of size', () => {
  for (const n of [1, 2, 3, 4, 5]) {
    for (const size of ['small', 'large']) {
      const s = fresh();
      const { task } = core.addTask(s, { title: `Crew ${n}`, size, builders: n });
      assert.equal(task.builders, n, `${size} task with builders ${n}`);
      assert.equal(core.crewFor(task), n);
    }
  }
});

test('builders undefined (or null / empty, "not given") means the size default', () => {
  const s = fresh();
  assert.equal(core.addTask(s, { title: 'A', builders: undefined }).task.builders, 2);
  assert.equal(core.addTask(s, { title: 'B', size: 'large', builders: undefined }).task.builders, 5);
  assert.equal(core.addTask(s, { title: 'C', builders: null }).task.builders, 2);
  assert.equal(core.addTask(s, { title: 'D', size: 'large', builders: '' }).task.builders, 5);
});

test('an integer string crew (from a form or the CLI) is accepted as a number', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: '3' });
  assert.strictEqual(task.builders, 3);
  core.updateTask(s, task.id, { builders: '4' });
  assert.strictEqual(task.builders, 4);
  assertCrew(s, 'alpha', 4);
});

test('addTask rejects an invalid crew and changes nothing', () => {
  for (const bad of INVALID) {
    const s = fresh();
    core.addTask(s, { title: 'Existing' });
    const before = json(s);
    assert.throws(() => core.addTask(s, { title: 'Bad', builders: bad }), crewError, `builders ${JSON.stringify(bad)}`);
    assert.deepEqual(json(s), before, `state unchanged after builders ${JSON.stringify(bad)}`);
  }
});

test('crewFor uses task.builders, else the size default', () => {
  assert.equal(core.crewFor({ size: 'small' }), 2);
  assert.equal(core.crewFor({ size: 'large' }), 5);
  assert.equal(core.crewFor({ size: 'small', builders: 4 }), 4);
  assert.equal(core.crewFor({ size: 'large', builders: 1 }), 1);
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 3 });
  assert.equal(core.crewFor(task), 3);
  assert.equal(typeof core.crewFor(task), 'number');
});

// ---------- agent statuses on attach ----------

test('attaching a small task: builders 1-2 assigned, 3-5 on standby, reviewer assigned', () => {
  const s = fresh();
  core.addTask(s, { title: 'Small' });
  assertCrew(s, 'alpha', 2);
  assert.equal(agent(s, 'alpha-6').status, 'assigned');
  assert.notEqual(agent(s, 'alpha-6').work, 'Standby');
  // Untouched teams stay idle with no standby label.
  for (const id of [...builderIds('bravo'), 'bravo-6']) {
    assert.equal(agent(s, id).status, 'idle');
    assert.equal(agent(s, id).work, '');
  }
});

test('attaching a large task: all 5 builders assigned on every team', () => {
  const s = fresh();
  const { assigned } = core.addTask(s, { title: 'Big', size: 'large' });
  assert.deepEqual(assigned, ['alpha', 'bravo', 'charlie']);
  for (const t of assigned) {
    assertCrew(s, t, 5);
    assert.equal(agent(s, `${t}-6`).status, 'assigned');
  }
});

test('a large task with a crew of 3 applies it to every team', () => {
  const s = fresh();
  core.addTask(s, { title: 'Big', size: 'large', builders: 3 });
  for (const t of ['alpha', 'bravo', 'charlie']) {
    assertCrew(s, t, 3);
    assert.equal(agent(s, `${t}-6`).status, 'assigned');
  }
});

test('a crew of 1 leaves builders 2-5 on standby', () => {
  const s = fresh();
  core.addTask(s, { title: 'Tiny fix', builders: 1 });
  assertCrew(s, 'alpha', 1);
});

test('teams added later (assignTeam) get the task crew', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 4 });
  core.assignTeam(s, task.id, 'bravo');
  assert.deepEqual(teamsOn(s, task.id), ['alpha', 'bravo']);
  assertCrew(s, 'alpha', 4);
  assertCrew(s, 'bravo', 4);
});

test('a team moved to another task takes that task crew', () => {
  const s = fresh();
  core.addTask(s, { title: 'A', builders: 5 });
  const b = core.addTask(s, { title: 'B', builders: 1 }).task; // on bravo
  assertCrew(s, 'alpha', 5);
  core.assignTeam(s, b.id, 'alpha');
  assertCrew(s, 'alpha', 1);
  assert.equal(team(s, 'alpha').taskId, b.id);

});

test('a queued task picked up later by dispatch gets its crew', () => {
  const s = fresh();
  const big = core.addTask(s, { title: 'Big', size: 'large' }).task;
  const waiting = core.addTask(s, { title: 'Waiting', builders: 3 }).task;
  assert.equal(waiting.status, 'queued');
  core.unassignTeam(s, big.id, 'charlie');
  assert.deepEqual(teamsOn(s, waiting.id), ['charlie']);
  assertCrew(s, 'charlie', 3);
});

test('a team joining a task in review: crew applied, reviewer reviewing', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 2 });
  core.updateTask(s, task.id, { status: 'review' });
  core.assignTeam(s, task.id, 'bravo');
  assertCrew(s, 'bravo', 2);
  assert.equal(agent(s, 'bravo-6').status, 'working');
  assert.match(agent(s, 'bravo-6').work, /Reviewing round 1/);
});

// ---------- updateTask builders ----------

test('updateTask builders: shrinking puts the extra builders on standby', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  core.updateTask(s, task.id, { builders: 2 });
  assert.equal(task.builders, 2);
  for (const t of ['alpha', 'bravo', 'charlie']) assertCrew(s, t, 2);
});

test('updateTask builders: growing brings standby builders onto the task', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Small' });
  assertCrew(s, 'alpha', 2);
  core.updateTask(s, task.id, { builders: 4 });
  assert.equal(task.builders, 4);
  assertCrew(s, 'alpha', 4);
});

test('updateTask builders keeps working/blocked/done builders inside the crew', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  core.reportProgress(s, { taskId: task.id, agentId: 'alpha-1', message: 'coding' });
  core.reportProgress(s, { taskId: task.id, agentId: 'alpha-2', status: 'blocked', message: 'stuck' });
  core.reportProgress(s, { taskId: task.id, agentId: 'alpha-3', status: 'done', message: 'finished' });
  assert.equal(agent(s, 'alpha-1').status, 'working');

  core.updateTask(s, task.id, { builders: 4 });
  assert.equal(agent(s, 'alpha-1').status, 'working');
  assert.equal(agent(s, 'alpha-1').work, 'coding');
  assert.equal(agent(s, 'alpha-2').status, 'blocked');
  assert.equal(agent(s, 'alpha-2').work, 'stuck');
  assert.equal(agent(s, 'alpha-3').status, 'done');
  assert.equal(agent(s, 'alpha-3').work, 'finished');
  assertCrew(s, 'alpha', 4, { skip: ['alpha-1', 'alpha-2', 'alpha-3'] });
  // Other teams have nobody busy: plain crew of 4.
  assertCrew(s, 'bravo', 4);
});

test('growing the crew keeps a busy builder and brings standby ones in', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Small', builders: 1 });
  core.reportProgress(s, { taskId: task.id, agentId: 'alpha-1', message: 'coding' });
  core.updateTask(s, task.id, { builders: 3 });
  assert.equal(agent(s, 'alpha-1').status, 'working');
  assertCrew(s, 'alpha', 3, { skip: ['alpha-1'] });
});

test('updateTask builders does not touch the reviewer', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 3 });
  assert.equal(agent(s, 'alpha-6').status, 'assigned');
  core.updateTask(s, task.id, { builders: 1 });
  assert.equal(agent(s, 'alpha-6').status, 'assigned');

  core.updateTask(s, task.id, { status: 'review' });
  assert.equal(agent(s, 'alpha-6').status, 'working');
  core.updateTask(s, task.id, { builders: 5 });
  assert.equal(agent(s, 'alpha-6').status, 'working');
  assert.match(agent(s, 'alpha-6').work, /Reviewing round 1/);
  assert.equal(task.status, 'review');
  assertCrew(s, 'alpha', 5);
});

test('updateTask builders on a task without teams just records it', () => {
  const s = fresh();
  core.addTask(s, { title: 'Big', size: 'large' });
  const waiting = core.addTask(s, { title: 'Waiting' }).task;
  const before = json(s.teams);
  core.updateTask(s, waiting.id, { builders: 4 });
  assert.equal(waiting.builders, 4);
  assert.equal(waiting.status, 'queued');
  assert.deepEqual(json(s.teams).map((t) => t.agents.map((a) => a.status)), before.map((t) => t.agents.map((a) => a.status)));
  // When it is picked up, the new crew applies.
  core.unassignTeam(s, 'T-001', 'bravo');
  assertCrew(s, 'bravo', 4);
});

test('updateTask builders leaves teams on other tasks alone', () => {
  const s = fresh();
  const a = core.addTask(s, { title: 'A', builders: 2 }).task;
  core.addTask(s, { title: 'B', builders: 5 }); // bravo
  core.updateTask(s, a.id, { builders: 1 });
  assertCrew(s, 'alpha', 1);
  assertCrew(s, 'bravo', 5);
});

test('updateTask rejects an invalid crew and changes nothing', () => {
  for (const bad of INVALID) {
    const s = fresh();
    const { task } = core.addTask(s, { title: 'A', builders: 3 });
    const before = json(s);
    assert.throws(() => core.updateTask(s, task.id, { builders: bad }), crewError, `builders ${JSON.stringify(bad)}`);
    assert.deepEqual(json(s), before, `state unchanged after builders ${JSON.stringify(bad)}`);
  }
});

test('an invalid crew in a mixed patch leaves the other fields unchanged too', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 3 });
  const before = json(s);
  assert.throws(() => core.updateTask(s, task.id, { title: 'Renamed', size: 'large', builders: 9 }), crewError);
  assert.deepEqual(json(s), before);
});

test('updateTask with builders null leaves the crew as it is', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 3 });
  core.updateTask(s, task.id, { builders: null, title: 'Renamed' });
  assert.equal(task.builders, 3);
  assert.equal(task.title, 'Renamed');
  assertCrew(s, 'alpha', 3);
});

test('changing size alone does not change an existing crew', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 3 });
  core.updateTask(s, task.id, { size: 'large' });
  assert.equal(task.size, 'large');
  assert.equal(core.crewFor(task), 3);
});

// ---------- detach ----------

test('detach idles every agent, standby label included', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 2 });
  assertCrew(s, 'alpha', 2);
  core.unassignTeam(s, task.id, 'alpha');
  for (const a of team(s, 'alpha').agents) {
    assert.equal(a.status, 'idle', a.id);
    assert.equal(a.work, '', a.id);
  }
});

test('finishing a task idles its teams as before', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A', builders: 1 });
  core.updateTask(s, task.id, { status: 'done', force: true });
  for (const a of team(s, 'alpha').agents) {
    assert.equal(a.status, 'idle', a.id);
    assert.equal(a.work, '', a.id);
  }
});

// ---------- normalize ----------

test('normalize gives old tasks the size default crew', () => {
  const s = fresh();
  core.addTask(s, { title: 'Small' });
  core.addTask(s, { title: 'Big', size: 'large' });
  core.addTask(s, { title: 'Custom', builders: 3 });
  const old = json(s);
  for (const t of old.tasks) delete t.builders;
  old.tasks[2].builders = 3; // a task that already has a crew keeps it
  const n = core.normalize(old);
  assert.deepEqual(n.tasks.map((t) => t.builders), [2, 5, 3]);
});

test('normalize is idempotent for crews', () => {
  const s = fresh();
  core.addTask(s, { title: 'Small' });
  core.addTask(s, { title: 'Big', size: 'large', builders: 4 });
  const old = json(s);
  delete old.tasks[0].builders;
  const once = json(core.normalize(old));
  const twice = json(core.normalize(json(once)));
  assert.deepEqual(twice, once);
  assert.deepEqual(once.tasks.map((t) => t.builders), [2, 4]);
});

// ---------- ops ----------

test('applyOp addTask passes builders through', () => {
  const s = fresh();
  const r = op(s, 'addTask', { title: 'A', size: 'small', builders: 4 });
  assert.equal(r.task.builders, 4);
  assertCrew(s, 'alpha', 4);
  assert.equal(op(s, 'addTask', { title: 'B', size: 'large' }).task.builders, 5);
  assert.throws(() => op(s, 'addTask', { title: 'C', builders: 6 }), crewError);
  assert.equal(s.tasks.length, 2);
});

test('applyOp updateTask passes builders through', () => {
  const s = fresh();
  op(s, 'addTask', { title: 'A' });
  const t = op(s, 'updateTask', { id: 'T-001', patch: { builders: 5 }, by: 'tester' });
  assert.equal(t.builders, 5);
  assertCrew(s, 'alpha', 5);
  assert.throws(() => op(s, 'updateTask', { id: 'T-001', patch: { builders: 0 } }), crewError);
  assert.equal(s.tasks[0].builders, 5);
});

test('importState keeps each task crew', () => {
  const src = fresh();
  core.addTask(src, { title: 'Small' });
  core.addTask(src, { title: 'Custom', builders: 3 });
  core.addTask(src, { title: 'Big', size: 'large', builders: 1 });
  const here = fresh();
  op(here, 'importState', { state: json(src) });
  const byTitle = Object.fromEntries(here.tasks.map((t) => [t.title, t.builders]));
  assert.deepEqual(byTitle, { Small: 2, Custom: 3, Big: 1 });

  // The direct export too.
  const again = fresh();
  importState(again, json(src));
  assert.deepEqual(again.tasks.map((t) => t.builders), [2, 3, 1]);
});

// ---------- unaffected: assignment rules, timers, reviews ----------

test('crew size does not change assignment rules', () => {
  const s = fresh();
  // A small task with a full crew still gets one team.
  assert.deepEqual(core.addTask(s, { title: 'Small', builders: 5 }).assigned, ['alpha']);
  // A large task with a crew of 1 still takes the free teams only when nothing else runs.
  assert.deepEqual(core.addTask(s, { title: 'Big', size: 'large', builders: 1 }).assigned, ['bravo']);

  const s2 = fresh();
  assert.deepEqual(core.addTask(s2, { title: 'Big', size: 'large', builders: 1 }).assigned, ['alpha', 'bravo', 'charlie']);
  assert.equal(core.addTask(s2, { title: 'Next', builders: 2 }).task.status, 'queued');
});

test('changing the crew does not reset timers', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A' });
  advance(10 * MIN);
  const startedAt = task.startedAt;
  const assignedAt = team(s, 'alpha').assignedAt;
  const activeSince = task.activeSince;
  core.updateTask(s, task.id, { builders: 5 });
  assert.equal(task.startedAt, startedAt);
  assert.equal(task.activeSince, activeSince);
  assert.equal(team(s, 'alpha').assignedAt, assignedAt);
  assert.equal(core.taskActiveMs(task), 10 * MIN);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 10 * MIN });
});

test('changing the crew does not touch the review round or sign-off', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'A' });
  core.updateTask(s, task.id, { status: 'review' });
  core.reviewTask(s, { taskId: task.id, teamId: 'alpha', verdict: 'approved' });
  const round = task.reviewRound;
  const approved = task.approvedRound;
  const reviews = json(task.reviews);
  const status = task.status;
  core.updateTask(s, task.id, { builders: 3 });
  assert.equal(task.reviewRound, round);
  assert.equal(task.approvedRound, approved);
  assert.deepEqual(json(task.reviews), reviews);
  assert.equal(task.status, status);
  assert.equal(core.reviewGate(s, task).ok, true);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
});

test('importState gives tasks from a pre-crew board the size default', () => {
  const src = fresh();
  core.addTask(src, { title: 'Small' });
  core.addTask(src, { title: 'Big', size: 'large' });
  const old = json(src);
  for (const t of old.tasks) delete t.builders;
  const here = fresh();
  op(here, 'importState', { state: old });
  assert.deepEqual(here.tasks.map((t) => [t.title, t.builders]), [['Small', 2], ['Big', 5]]);
});
