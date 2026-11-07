import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';
import { applyOp, OP_TYPES } from '../lib/ops.mjs';

// Fake clock, as in timer.test.mjs: time only moves when a test says so.
const START = Date.parse('2026-01-01T00:00:00.000Z');
let clock = START;
const advance = (ms) => (clock += ms);
const iso = (ms = clock) => new Date(ms).toISOString();

beforeEach(() => {
  clock = START;
  core.setClock(() => clock);
});
afterEach(() => core.setClock(() => Date.now()));

const SEC = 1000;
const fresh = () => core.createInitialState();
const team = (s, id) => core.getTeam(s, id);
const agent = (s, id) => core.getAgent(s, id).agent;
const teamsOn = (s, id) => core.teamsOn(s, id).map((t) => t.id);
const json = (v) => JSON.parse(JSON.stringify(v));
const isDeptError = (re) => (e) => e instanceof core.DeptError && (!re || re.test(e.message));

const toReview = (s, id, by) => core.updateTask(s, id, { status: 'review' }, by ? { by } : {});
const verdict = (s, taskId, teamId, v, extra = {}) => core.reviewTask(s, { taskId, teamId, verdict: v, ...extra });

// A small task on alpha, sent to review once.
function inReview() {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Review me' });
  toReview(s, task.id);
  return { s, task };
}

// ---------- agents ----------

test('each team has 5 builders and one reviewer as <team>-6', () => {
  assert.equal(core.BUILDERS_PER_TEAM, 5);
  assert.equal(core.AGENTS_PER_TEAM, 6);
  const s = fresh();
  for (const t of s.teams) {
    assert.equal(t.agents.length, 6);
    const builders = t.agents.filter((a) => a.role === 'builder');
    assert.deepEqual(builders.map((a) => a.id), [1, 2, 3, 4, 5].map((n) => `${t.id}-${n}`));
    const reviewers = t.agents.filter((a) => a.role === 'reviewer');
    assert.equal(reviewers.length, 1);
    assert.equal(reviewers[0].id, `${t.id}-6`);
    assert.match(reviewers[0].name, /Reviewer/);
    assert.equal(reviewers[0].status, 'idle');
    assert.equal(t.issuesFound, 0);
  }
});

test('normalize adds the reviewer to an old 5-agent state and keeps existing agents', () => {
  const old = fresh();
  for (const t of old.teams) {
    t.agents = t.agents.filter((a) => a.role === 'builder');
    delete t.issuesFound;
  }
  old.teams[0].agents[1].status = 'working';
  old.teams[0].agents[1].work = 'Old work';
  old.nextTaskNumber = 2;
  old.tasks = [{
    id: 'T-001', title: 'Old', description: '', size: 'small', status: 'paused', progress: 10,
    teamHistory: ['alpha'], createdAt: iso(), updatedAt: iso(), startedAt: iso(), finishedAt: null,
    activeMs: 0, activeSince: null, teamMs: {},
  }];

  const s = core.normalize(old);
  for (const t of s.teams) {
    assert.equal(t.agents.length, 6);
    assert.equal(t.agents.filter((a) => a.role === 'builder').length, 5);
    assert.equal(t.agents.find((a) => a.id === `${t.id}-6`).role, 'reviewer');
    assert.equal(t.issuesFound, 0);
  }
  assert.equal(agent(s, 'alpha-2').status, 'working');
  assert.equal(agent(s, 'alpha-2').work, 'Old work');
  const task = s.tasks[0];
  assert.equal(task.reviewRound, 0);
  assert.deepEqual(task.reviews, {});
  assert.equal(task.issuesFound, 0);

  const again = core.normalize(json(s));
  assert.deepEqual(json(again), json(s), 'normalize is idempotent');
});

test('normalize keeps a review task running', () => {
  const { s, task } = inReview();
  advance(5 * SEC);
  const copy = core.normalize(json(s));
  const t = copy.tasks.find((x) => x.id === task.id);
  assert.equal(t.status, 'review');
  assert.equal(t.activeSince, iso(START));
  assert.equal(t.reviewRound, 1);
  assert.equal(core.taskActiveMs(t), 5 * SEC);
  assert.deepEqual(teamsOn(copy, task.id), ['alpha']);
});

// ---------- sending to review ----------

test("'review' is an open task status", () => {
  assert.ok(core.TASK_STATUSES.includes('review'));
});

test('review requires a team on the task', () => {
  const s = fresh();
  core.updateSettings(s, { autoDispatch: false });
  const { task } = core.addTask(s, { title: 'Nobody' });
  assert.throws(() => toReview(s, task.id), isDeptError(/Assign a team before review/));
  assert.equal(task.status, 'queued');
  assert.equal(task.reviewRound, 0);
});

test('review bumps reviewRound, keeps teams and sets the reviewers working', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  assert.equal(task.reviewRound, 0);
  assert.deepEqual(task.reviews, {});
  assert.equal(task.issuesFound, 0);

  toReview(s, task.id, 'head');
  assert.equal(task.status, 'review');
  assert.equal(task.reviewRound, 1);
  assert.deepEqual(teamsOn(s, task.id), ['alpha', 'bravo', 'charlie']);
  for (const id of ['alpha', 'bravo', 'charlie']) {
    const r = agent(s, `${id}-6`);
    assert.equal(r.status, 'working');
    assert.equal(r.work, 'Reviewing round 1');
  }
  assert.ok(s.log.some((e) => e.message.includes(`${task.id} sent to review (round 1)`)));

  // Back to active, then review again: round 2.
  verdict(s, task.id, 'alpha', 'changes', { note: 'tests missing' });
  toReview(s, task.id);
  assert.equal(task.reviewRound, 2);
  assert.equal(agent(s, 'alpha-6').work, 'Reviewing round 2');
});

// ---------- reviewTask validation ----------

test('reviewTask rejects a task that is not in review', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Active' });
  assert.throws(() => verdict(s, task.id, 'alpha', 'approved'), isDeptError(new RegExp(`${task.id} is not in review`)));
});

test('reviewTask rejects a team that is not on the task', () => {
  const { s, task } = inReview();
  assert.throws(() => verdict(s, task.id, 'bravo', 'approved'), isDeptError());
  assert.deepEqual(task.reviews, {});
});

test('reviewTask rejects bad verdicts and negative issue counts', () => {
  const { s, task } = inReview();
  assert.throws(() => verdict(s, task.id, 'alpha', 'maybe'), isDeptError());
  assert.throws(() => verdict(s, task.id, 'alpha', ''), isDeptError());
  assert.throws(() => verdict(s, task.id, 'alpha', 'approved', { issues: -1 }), isDeptError());
  assert.deepEqual(task.reviews, {});
  assert.equal(task.issuesFound, 0);
  assert.equal(team(s, 'alpha').issuesFound, 0);
});

// ---------- verdicts ----------

test("'changes' sends the task back to active and records issues", () => {
  const { s, task } = inReview();
  const r = verdict(s, task.id, 'alpha', 'changes', { note: 'Edge case broken', issues: 3, by: 'alpha-6' });
  assert.equal(task.status, 'active');
  assert.deepEqual(teamsOn(s, task.id), ['alpha']);
  assert.equal(r.approved, false);
  assert.deepEqual(r.pending, ['alpha']);
  assert.equal(r.task, task);
  assert.equal(r.team, team(s, 'alpha'));

  const rec = task.reviews.alpha;
  assert.equal(rec.verdict, 'changes');
  assert.equal(rec.note, 'Edge case broken');
  assert.equal(rec.issues, 3);
  assert.equal(rec.round, 1);
  assert.equal(rec.at, iso());
  assert.equal(task.issuesFound, 3);
  assert.equal(team(s, 'alpha').issuesFound, 3);

  const rev = agent(s, 'alpha-6');
  assert.equal(rev.status, 'working');
  assert.equal(rev.work, 'Edge case broken');
  assert.ok(s.log.some((e) => e.message.includes(`reviewer requested changes on ${task.id}: Edge case broken`)));
});

test('approval keeps the task in review and marks the reviewer done', () => {
  const { s, task } = inReview();
  const r = verdict(s, task.id, 'alpha', 'approved', { note: 'LGTM', issues: 1 });
  assert.equal(task.status, 'review');
  assert.equal(r.approved, true);
  assert.deepEqual(r.pending, []);
  assert.equal(task.reviews.alpha.verdict, 'approved');
  assert.equal(task.reviews.alpha.round, 1);
  assert.equal(task.issuesFound, 1);
  const rev = agent(s, 'alpha-6');
  assert.equal(rev.status, 'done');
  assert.equal(rev.work, 'Approved round 1');
  assert.ok(s.log.some((e) => e.message.includes(`reviewer approved ${task.id}`)));
});

test('reviewStatus reports pending, approved and changes for the current round', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  let st = core.reviewStatus(s, task);
  assert.equal(st.round, 1);
  assert.deepEqual(st.approved, []);
  assert.deepEqual(st.changes, []);
  assert.deepEqual([...st.pending].sort(), ['alpha', 'bravo', 'charlie']);

  verdict(s, task.id, 'alpha', 'approved');
  st = core.reviewStatus(s, task);
  assert.deepEqual(st.approved, ['alpha']);
  assert.deepEqual([...st.pending].sort(), ['bravo', 'charlie']);

  verdict(s, task.id, 'bravo', 'changes', { note: 'nope' });
  st = core.reviewStatus(s, task);
  assert.deepEqual(st.approved, ['alpha']);
  assert.deepEqual(st.changes, ['bravo']);
  assert.deepEqual(st.pending, ['charlie']);
});

test('round 2 ignores verdicts from round 1', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved');
  verdict(s, task.id, 'bravo', 'approved');
  verdict(s, task.id, 'charlie', 'changes', { note: 'fix it', issues: 2 });
  assert.equal(task.status, 'active');

  toReview(s, task.id);
  const st = core.reviewStatus(s, task);
  assert.equal(st.round, 2);
  assert.deepEqual(st.approved, []);
  assert.deepEqual(st.changes, []);
  assert.deepEqual([...st.pending].sort(), ['alpha', 'bravo', 'charlie']);
  assert.throws(() => core.updateTask(s, task.id, { status: 'done' }), isDeptError(/needs reviewer sign-off/));

  const r = verdict(s, task.id, 'charlie', 'approved');
  assert.equal(r.approved, false);
  assert.deepEqual([...r.pending].sort(), ['alpha', 'bravo']);
  assert.equal(task.reviews.charlie.round, 2);
  assert.equal(task.issuesFound, 2);
});

// ---------- done gate ----------

test('done is refused until every team approved, naming the missing teams', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });

  // Never reviewed: refused, every team named.
  assert.throws(
    () => core.updateTask(s, task.id, { status: 'done' }),
    (e) => e instanceof core.DeptError
      && e.message.startsWith(`${task.id} needs reviewer sign-off from`)
      && /Team Alpha/.test(e.message) && /Team Bravo/.test(e.message) && /Team Charlie/.test(e.message)
      && /force/.test(e.message),
  );
  assert.equal(task.status, 'active');

  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved');
  verdict(s, task.id, 'bravo', 'approved');
  assert.throws(
    () => core.updateTask(s, task.id, { status: 'done' }),
    (e) => e instanceof core.DeptError && /Team Charlie/.test(e.message)
      && !/Team Alpha/.test(e.message) && !/Team Bravo/.test(e.message),
  );
  assert.equal(task.status, 'review');
  assert.deepEqual(teamsOn(s, task.id), ['alpha', 'bravo', 'charlie']);

  const r = verdict(s, task.id, 'charlie', 'approved');
  assert.equal(r.approved, true);
  assert.deepEqual(r.pending, []);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
  assert.deepEqual(teamsOn(s, task.id), []);
});

test('done on a single-team task works once its reviewer approved', () => {
  const { s, task } = inReview();
  verdict(s, task.id, 'alpha', 'approved');
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
  assert.equal(task.progress, 100);
});

test('done with force skips the review and logs it', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Rush' });
  core.updateTask(s, task.id, { status: 'done', force: true }, { by: 'boss' });
  assert.equal(task.status, 'done');
  assert.deepEqual(teamsOn(s, task.id), []);
  assert.ok(s.log.some((e) => e.message.includes(`${task.id} marked done without review by boss`)));
});

test('a task with no teams can be marked done without review', () => {
  const s = fresh();
  core.updateSettings(s, { autoDispatch: false });
  const { task } = core.addTask(s, { title: 'Nobody' });
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
});

// ---------- timers ----------

test('timers keep running while a task is in review', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Timed' });
  advance(10 * SEC);
  toReview(s, task.id);
  assert.equal(task.activeSince, iso(START), 'the active interval is not interrupted');
  advance(20 * SEC);
  assert.equal(core.taskActiveMs(task), 30 * SEC);
  assert.equal(core.teamTotalMs(team(s, 'alpha')), 30 * SEC);
  assert.deepEqual(core.taskTeamMs(s, task), { alpha: 30 * SEC });

  verdict(s, task.id, 'alpha', 'changes', { note: 'again' });
  assert.equal(task.activeSince, iso(START));
  advance(5 * SEC);
  toReview(s, task.id);
  advance(5 * SEC);
  verdict(s, task.id, 'alpha', 'approved');
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.activeSince, null);
  assert.equal(task.activeMs, 40 * SEC);
  assert.equal(task.teamMs.alpha, 40 * SEC);
  assert.equal(team(s, 'alpha').totalMs, 40 * SEC);
  advance(60 * SEC);
  assert.equal(core.taskActiveMs(task), 40 * SEC);
});

test('removing the last team from a review task pauses it', () => {
  const { s, task } = inReview();
  advance(8 * SEC);
  core.unassignTeam(s, task.id, 'alpha');
  assert.equal(task.status, 'paused');
  assert.equal(task.activeSince, null);
  assert.equal(task.activeMs, 8 * SEC);
  assert.deepEqual(teamsOn(s, task.id), []);
});

test('removing one of several teams keeps the task in review', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  core.unassignTeam(s, task.id, 'charlie');
  assert.equal(task.status, 'review');
  verdict(s, task.id, 'alpha', 'approved');
  const r = verdict(s, task.id, 'bravo', 'approved');
  assert.equal(r.approved, true, 'only the teams still on the task need to approve');
});

// ---------- assignment and dispatch ----------

test('a team joining a review task is pending', () => {
  const { s, task } = inReview();
  verdict(s, task.id, 'alpha', 'approved');
  core.assignTeam(s, task.id, 'bravo');
  assert.equal(task.status, 'review');
  assert.deepEqual(teamsOn(s, task.id).sort(), ['alpha', 'bravo']);
  const st = core.reviewStatus(s, task);
  assert.deepEqual(st.approved, ['alpha']);
  assert.deepEqual(st.pending, ['bravo']);
  assert.throws(() => core.updateTask(s, task.id, { status: 'done' }), isDeptError(/Team Bravo/));
});

test('a task in review counts as running for dispatch', () => {
  const { s, task } = inReview();
  const { task: big, assigned } = core.addTask(s, { title: 'Big', size: 'large' });
  assert.deepEqual(assigned, ['bravo'], 'a large task does not get every free team while another is in review');
  assert.equal(big.status, 'active');
  assert.deepEqual(teamsOn(s, task.id), ['alpha'], 'the review task keeps its team');
});

test('a task in review does not free its team for the queue', () => {
  const s = fresh();
  const a = core.addTask(s, { title: 'A', size: 'large' }).task;
  const waiting = core.addTask(s, { title: 'Waiting' }).task;
  assert.equal(waiting.status, 'queued');
  toReview(s, a.id);
  assert.equal(waiting.status, 'queued');
  assert.deepEqual(teamsOn(s, a.id), ['alpha', 'bravo', 'charlie']);
});

// ---------- ops ----------

test('applyOp reviewTask maps onto core.reviewTask', () => {
  assert.ok(OP_TYPES.includes('reviewTask'));
  const { s, task } = inReview();
  const r = applyOp(s, {
    type: 'reviewTask',
    args: { taskId: task.id, teamId: 'alpha', verdict: 'changes', note: 'via op', issues: 2, by: 'user' },
  });
  assert.equal(r.approved, false);
  assert.equal(task.status, 'active');
  assert.equal(task.reviews.alpha.note, 'via op');
  assert.equal(task.issuesFound, 2);
  assert.doesNotThrow(() => JSON.stringify(r));

  applyOp(s, { type: 'updateTask', args: { id: task.id, patch: { status: 'review' } } });
  const ok = applyOp(s, { type: 'reviewTask', args: { taskId: task.id, teamId: 'alpha', verdict: 'approved' } });
  assert.equal(ok.approved, true);
  assert.throws(
    () => applyOp(s, { type: 'reviewTask', args: { taskId: task.id, teamId: 'alpha', verdict: 'bogus' } }),
    isDeptError(),
  );
});

test('applyOp updateTask passes force through the patch', () => {
  const s = fresh();
  applyOp(s, { type: 'addTask', args: { title: 'A' } });
  assert.throws(
    () => applyOp(s, { type: 'updateTask', args: { id: 'T-001', patch: { status: 'done' } } }),
    isDeptError(/needs reviewer sign-off from Team Alpha/),
  );
  const t = applyOp(s, { type: 'updateTask', args: { id: 'T-001', patch: { status: 'done', force: true }, by: 'tester' } });
  assert.equal(t.status, 'done');
  assert.ok(s.log.some((e) => e.message.includes('T-001 marked done without review by tester')));
});

// ---------- stats ----------

test('teamStats includes issuesFound', () => {
  const { s, task } = inReview();
  verdict(s, task.id, 'alpha', 'changes', { note: 'a', issues: 3 });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved', { issues: 1 });
  const stats = core.teamStats(s);
  assert.equal(stats.find((x) => x.teamId === 'alpha').issuesFound, 4);
  assert.equal(stats.find((x) => x.teamId === 'bravo').issuesFound, 0);
  assert.equal(task.issuesFound, 4);
});
