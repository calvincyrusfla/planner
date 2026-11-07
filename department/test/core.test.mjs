import test from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';

const fresh = () => core.createInitialState();
const teamsOn = (s, id) => core.teamsOn(s, id).map((t) => t.id);

test('department has 3 teams of 5 builders and a reviewer', () => {
  const s = fresh();
  assert.equal(s.teams.length, 3);
  for (const t of s.teams) {
    assert.equal(t.agents.length, 6);
    assert.equal(t.agents.filter((a) => a.role === 'builder').length, 5);
    assert.equal(t.agents.filter((a) => a.role === 'reviewer').length, 1);
  }
});

test('a small task gets one team', () => {
  const s = fresh();
  const { task, assigned } = core.addTask(s, { title: 'Fix login' });
  assert.deepEqual(assigned, ['alpha']);
  assert.equal(task.status, 'active');
});

test('a large task gets all teams when nothing else is running', () => {
  const s = fresh();
  const { assigned } = core.addTask(s, { title: 'Rebuild app', size: 'large' });
  assert.deepEqual(assigned, ['alpha', 'bravo', 'charlie']);
});

test('a large task gets one team when another task is running', () => {
  const s = fresh();
  core.addTask(s, { title: 'Small' });
  const { assigned } = core.addTask(s, { title: 'Big', size: 'large' });
  assert.deepEqual(assigned, ['bravo']);
});

test('tasks queue when every team is busy and start when one frees up', () => {
  const s = fresh();
  const big = core.addTask(s, { title: 'Big', size: 'large' }).task;
  const waiting = core.addTask(s, { title: 'Next' });
  assert.deepEqual(waiting.assigned, []);
  assert.equal(waiting.task.status, 'queued');
  core.updateTask(s, big.id, { status: 'done', force: true });
  assert.equal(waiting.task.status, 'active');
  assert.equal(teamsOn(s, waiting.task.id).length, 1);
});

test('reassigning a team moves it and pauses a task left with no team', () => {
  const s = fresh();
  const a = core.addTask(s, { title: 'A' }).task;
  const b = core.addTask(s, { title: 'B' }).task;
  core.assignTeam(s, b.id, 'alpha');
  assert.deepEqual(teamsOn(s, b.id).sort(), ['alpha', 'bravo']);
  assert.equal(a.status, 'paused');
  assert.deepEqual(teamsOn(s, a.id), []); // charlie is free but doesn't grab a paused task
});

test('removing a team frees it for queued work but not for the task it left', () => {
  const s = fresh();
  const big = core.addTask(s, { title: 'Big', size: 'large' }).task;
  const next = core.addTask(s, { title: 'Next' }).task;
  core.unassignTeam(s, big.id, 'charlie');
  assert.deepEqual(teamsOn(s, next.id), ['charlie']);
  assert.deepEqual(teamsOn(s, big.id), ['alpha', 'bravo']);
});

test('removing the last team pauses the task and does not hand it back', () => {
  const s = fresh();
  const t = core.addTask(s, { title: 'Solo' }).task;
  core.unassignTeam(s, t.id, 'alpha');
  assert.equal(t.status, 'paused');
  assert.deepEqual(teamsOn(s, t.id), []);
});

test('progress averages across the teams on a task', () => {
  const s = fresh();
  const t = core.addTask(s, { title: 'Big', size: 'large' }).task;
  core.reportProgress(s, { taskId: t.id, teamId: 'alpha', progress: 90 });
  core.reportProgress(s, { taskId: t.id, agentId: 'bravo-3', progress: 30, message: 'API done' });
  assert.equal(t.progress, 40); // (90 + 30 + 0) / 3
  const agent = core.getAgent(s, 'bravo-3').agent;
  assert.equal(agent.status, 'working');
  assert.equal(agent.work, 'API done');
});

test('auto-dispatch off leaves new tasks queued', () => {
  const s = fresh();
  core.updateSettings(s, { autoDispatch: false });
  const { assigned, task } = core.addTask(s, { title: 'Manual' });
  assert.deepEqual(assigned, []);
  assert.equal(task.status, 'queued');
});
