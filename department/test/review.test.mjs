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

  // Every team gives a verdict, one asks for changes: back to active, then round 2.
  verdict(s, task.id, 'alpha', 'changes', { note: 'tests missing' });
  verdict(s, task.id, 'bravo', 'approved');
  verdict(s, task.id, 'charlie', 'approved');
  assert.equal(task.status, 'active');
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
  assert.deepEqual(r.pending, [], 'pending means no verdict yet');
  assert.deepEqual(r.changes, ['alpha']);
  assert.equal(r.complete, true);
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
  assert.deepEqual(r.changes, []);
  assert.equal(r.complete, true);
  assert.equal(task.approvedRound, task.reviewRound);
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

// ---------- round 2 decisions: multi-team rounds ----------

test('a multi-team task stays in review until every team has a verdict', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);

  let r = verdict(s, task.id, 'alpha', 'changes', { note: 'broken', issues: 2 });
  assert.equal(task.status, 'review', 'one "changes" does not end the round');
  assert.equal(r.complete, false);
  assert.equal(r.approved, false);
  assert.deepEqual(r.changes, ['alpha']);
  assert.deepEqual([...r.pending].sort(), ['bravo', 'charlie']);
  assert.equal(agent(s, 'bravo-6').status, 'working', 'other reviewers keep reviewing');

  r = verdict(s, task.id, 'bravo', 'approved', { issues: 1 });
  assert.equal(task.status, 'review');
  assert.equal(r.complete, false);
  assert.deepEqual(r.pending, ['charlie']);

  const before = s.log.length;
  r = verdict(s, task.id, 'charlie', 'approved');
  assert.equal(r.complete, true);
  assert.equal(r.approved, false);
  assert.deepEqual(r.pending, []);
  assert.deepEqual(r.changes, ['alpha']);
  assert.equal(task.status, 'active', 'back to the builders after the last verdict');
  assert.deepEqual(teamsOn(s, task.id), ['alpha', 'bravo', 'charlie']);

  assert.deepEqual(Object.keys(task.reviews).sort(), ['alpha', 'bravo', 'charlie']);
  assert.equal(task.reviews.alpha.verdict, 'changes');
  assert.equal(task.reviews.bravo.verdict, 'approved');
  assert.equal(task.reviews.charlie.verdict, 'approved');
  for (const id of ['alpha', 'bravo', 'charlie']) assert.equal(task.reviews[id].round, 1);
  assert.equal(task.issuesFound, 3);
  assert.equal(team(s, 'alpha').issuesFound, 2);
  assert.equal(team(s, 'bravo').issuesFound, 1);
  assert.ok(
    s.log.slice(before).some((e) => e.message.includes('back to the builders: changes requested by Team Alpha')),
    'log names the teams that asked for changes',
  );
  assert.notEqual(task.approvedRound, task.reviewRound);
});

test('all teams approving keeps the task in review and sets approvedRound', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved');
  verdict(s, task.id, 'bravo', 'approved');
  assert.notEqual(task.approvedRound, task.reviewRound, 'not approved until the last team signs off');
  const r = verdict(s, task.id, 'charlie', 'approved');
  assert.equal(r.approved, true);
  assert.equal(r.complete, true);
  assert.deepEqual(r.pending, []);
  assert.deepEqual(r.changes, []);
  assert.equal(task.status, 'review');
  assert.equal(task.approvedRound, task.reviewRound);
  assert.equal(task.approvedRound, 1);
});

test('a team re-giving a verdict in the same round: the latest wins', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'changes', { note: 'hmm' });
  verdict(s, task.id, 'alpha', 'approved', { note: 'fine after all' });
  let st = core.reviewStatus(s, task);
  assert.deepEqual(st.approved, ['alpha']);
  assert.deepEqual(st.changes, []);
  assert.deepEqual([...st.pending].sort(), ['bravo', 'charlie']);
  assert.equal(task.reviews.alpha.note, 'fine after all');

  verdict(s, task.id, 'bravo', 'approved');
  verdict(s, task.id, 'bravo', 'changes', { note: 'spotted a bug' });
  st = core.reviewStatus(s, task);
  assert.deepEqual(st.approved, ['alpha']);
  assert.deepEqual(st.changes, ['bravo']);
  assert.deepEqual(st.pending, ['charlie']);
  assert.equal(task.status, 'review');

  const r = verdict(s, task.id, 'charlie', 'approved');
  assert.equal(r.complete, true);
  assert.deepEqual(r.changes, ['bravo']);
  assert.equal(task.status, 'active');
});

// ---------- round 2 decisions: done gate ----------

test('a worked task paused without sign-off needs force to be done', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Worked' });
  core.updateTask(s, task.id, { status: 'paused' });
  assert.deepEqual(teamsOn(s, task.id), []);
  assert.throws(
    () => core.updateTask(s, task.id, { status: 'done' }),
    isDeptError(/has no reviewer sign-off/),
  );
  assert.equal(task.status, 'paused');
  const gate = core.reviewGate(s, task);
  assert.equal(gate.ok, false);
  assert.ok(Array.isArray(gate.missing));
  assert.match(gate.message, /has no reviewer sign-off/);

  core.updateTask(s, task.id, { status: 'done', force: true });
  assert.equal(task.status, 'done');
});

test('a task worked and reviewed but paused before approval still needs force', () => {
  const { s, task } = inReview();
  core.updateTask(s, task.id, { status: 'paused' });
  assert.throws(() => core.updateTask(s, task.id, { status: 'done' }), isDeptError(/has no reviewer sign-off/));
});

test('a task that never had a team passes the gate', () => {
  const s = fresh();
  core.updateSettings(s, { autoDispatch: false });
  const { task } = core.addTask(s, { title: 'Never staffed' });
  const gate = core.reviewGate(s, task);
  assert.equal(gate.ok, true);
  assert.deepEqual(gate.missing, []);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
});

test('a task approved by all, then paused, can be done without force', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  for (const id of ['alpha', 'bravo', 'charlie']) verdict(s, task.id, id, 'approved');
  core.updateTask(s, task.id, { status: 'paused' });
  assert.deepEqual(teamsOn(s, task.id), []);
  assert.equal(core.reviewGate(s, task).ok, true);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
  assert.ok(!s.log.some((e) => e.message.includes('without review')));
});

test('reviewGate reports missing teams and the refusal message', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Big', size: 'large' });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved');
  const gate = core.reviewGate(s, task);
  assert.equal(gate.ok, false);
  assert.equal(gate.missing.length, 2);
  assert.ok(gate.missing.every((m) => /bravo|charlie/i.test(String(m?.id ?? m?.name ?? m))));
  assert.match(gate.message, /needs reviewer sign-off from/);
  assert.match(gate.message, /Team Bravo/);
  assert.match(gate.message, /Team Charlie/);
  assert.doesNotMatch(gate.message, /Team Alpha/);
  assert.throws(() => core.updateTask(s, task.id, { status: 'done' }), (e) => e instanceof core.DeptError && e.message === gate.message);

  verdict(s, task.id, 'bravo', 'approved');
  verdict(s, task.id, 'charlie', 'approved');
  const ok = core.reviewGate(s, task);
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.missing, []);
});

// ---------- round 2 decisions: log attribution ----------

test("verdict log says '(by user)' for the user and nothing for the reviewer", () => {
  const { s, task } = inReview();
  verdict(s, task.id, 'alpha', 'changes', { note: 'from the dashboard', by: 'user' });
  const userLine = s.log.findLast((e) => e.message.includes('reviewer requested changes'));
  assert.match(userLine.message, /\(by user\)/);

  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'approved', { by: 'reviewer' });
  const revLine = s.log.findLast((e) => e.message.includes(`reviewer approved ${task.id}`));
  assert.doesNotMatch(revLine.message, /\(by /);

  // Still in review; re-verdict in the same round without `by` defaults to the reviewer.
  verdict(s, task.id, 'alpha', 'approved');
  const defLine = s.log.findLast((e) => e.message.includes(`reviewer approved ${task.id}`));
  assert.doesNotMatch(defLine.message, /\(by /);
});

// ---------- round 2 decisions: normalize ----------

test('normalize sets the idle reviewer of a team on a task to assigned', () => {
  const old = fresh();
  const { task } = core.addTask(old, { title: 'Running' });
  for (const t of old.teams) t.agents = t.agents.filter((a) => a.role === 'builder');
  const s = core.normalize(json(old));
  assert.equal(team(s, 'alpha').taskId, task.id);
  assert.equal(agent(s, 'alpha-6').status, 'assigned');
  assert.equal(agent(s, 'bravo-6').status, 'idle', 'a free team keeps an idle reviewer');
  assert.equal(agent(s, 'alpha-2').status, 'assigned', 'builders are untouched');

  // A reviewer that is already busy is left alone.
  const busy = core.normalize(json(old));
  agent(busy, 'alpha-6').status = 'working';
  core.normalize(busy);
  assert.equal(agent(busy, 'alpha-6').status, 'working');
});

// ---------- alpha-1 (round 3): teamIssues running total ----------

test('alpha-1: task.teamIssues totals issues per team across rounds, latest wins within a round', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Totals', size: 'large' });
  assert.deepEqual(task.teamIssues, {});
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'changes', { note: 'a', issues: 2 });
  verdict(s, task.id, 'alpha', 'changes', { note: 'a2', issues: 3 }); // same round: replaces 2
  verdict(s, task.id, 'bravo', 'changes', { note: 'b', issues: 1 });
  verdict(s, task.id, 'charlie', 'approved');
  assert.equal(task.status, 'active');
  assert.deepEqual(task.teamIssues, { alpha: 3, bravo: 1, charlie: 0 });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'changes', { note: 'again', issues: 4 });
  verdict(s, task.id, 'bravo', 'approved');
  verdict(s, task.id, 'charlie', 'approved');
  assert.deepEqual(task.teamIssues, { alpha: 7, bravo: 1, charlie: 0 });
  assert.equal(task.issuesFound, 8);
  assert.equal(team(s, 'alpha').issuesFound, 7);
});

test('alpha-1: normalize fills teamIssues from reviews where missing and is idempotent', () => {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Old', size: 'large' });
  toReview(s, task.id);
  verdict(s, task.id, 'alpha', 'changes', { note: 'x', issues: 2 });
  const old = json(s);
  delete old.tasks[0].teamIssues;
  const n1 = core.normalize(json(old));
  assert.deepEqual(n1.tasks[0].teamIssues, { alpha: 2 });
  assert.deepEqual(core.normalize(json(n1)), n1);
  const kept = json(s);
  kept.tasks[0].teamIssues = { alpha: 9 };
  assert.deepEqual(core.normalize(kept).tasks[0].teamIssues, { alpha: 9 }, 'an existing total is not overwritten');
});

// ---------- alpha-1 (round 3): a team leaving a task in review settles the round ----------

function threeTeamReview() {
  const s = fresh();
  const { task } = core.addTask(s, { title: 'Three teams', size: 'large' });
  assert.deepEqual(teamsOn(s, task.id), ['alpha', 'bravo', 'charlie']);
  toReview(s, task.id);
  return { s, task };
}

test('alpha-1: unassigning the last pending team when the rest approved completes the round', () => {
  const { s, task } = threeTeamReview();
  verdict(s, task.id, 'alpha', 'approved');
  verdict(s, task.id, 'bravo', 'approved');
  core.unassignTeam(s, task.id, 'charlie');
  assert.equal(task.status, 'review');
  assert.equal(task.approvedRound, 1);
  assert.ok(s.log.some((e) => e.message === `${task.id} approved by all reviewers (round 1)`));
  assert.equal(core.reviewGate(s, task).ok, true);
  core.updateTask(s, task.id, { status: 'done' });
  assert.equal(task.status, 'done');
});

test('alpha-1: unassigning the last pending team when another asked for changes sends it back to active', () => {
  const { s, task } = threeTeamReview();
  verdict(s, task.id, 'alpha', 'changes', { note: 'fix', issues: 1 });
  verdict(s, task.id, 'bravo', 'approved');
  assert.equal(task.status, 'review');
  core.unassignTeam(s, task.id, 'charlie');
  assert.equal(task.status, 'active');
  assert.ok(task.activeSince, 'timer keeps running');
  assert.ok(s.log.some((e) => e.message === `${task.id} back to the builders: changes requested by Team Alpha`));
});

test('alpha-1: removing a team while others are still pending leaves the round open', () => {
  const { s, task } = threeTeamReview();
  verdict(s, task.id, 'alpha', 'approved');
  core.unassignTeam(s, task.id, 'charlie');
  assert.equal(task.status, 'review');
  assert.deepEqual(core.reviewStatus(s, task).pending, ['bravo']);
  assert.notEqual(task.approvedRound, 1);
});

test('alpha-1: moving the pending team to another task settles the round it left', () => {
  const { s, task } = threeTeamReview();
  verdict(s, task.id, 'alpha', 'approved');
  verdict(s, task.id, 'bravo', 'approved');
  const other = core.addTask(s, { title: 'Elsewhere' }).task; // queued, all teams busy
  assert.equal(other.status, 'queued');
  core.assignTeam(s, other.id, 'charlie');
  assert.equal(task.status, 'review');
  assert.equal(task.approvedRound, 1);
});

test('alpha-1: removing a team after the round was already approved does not log approval twice', () => {
  const { s, task } = threeTeamReview();
  for (const id of ['alpha', 'bravo', 'charlie']) verdict(s, task.id, id, 'approved');
  core.unassignTeam(s, task.id, 'charlie');
  const approvals = s.log.filter((e) => e.message === `${task.id} approved by all reviewers (round 1)`);
  assert.equal(approvals.length, 1);
});

test('alpha-1: deleting or pausing a task in review does not settle the round on the way out', () => {
  const { s, task } = threeTeamReview();
  verdict(s, task.id, 'alpha', 'changes', { note: 'x' });
  core.updateTask(s, task.id, { status: 'paused' });
  assert.equal(task.status, 'paused');
  assert.ok(!s.log.some((e) => /back to the builders/.test(e.message)));
  const b = threeTeamReview();
  verdict(b.s, b.task.id, 'alpha', 'approved');
  core.deleteTask(b.s, b.task.id);
  assert.ok(!b.s.log.some((e) => /approved by all reviewers|back to the builders/.test(e.message)));
});
