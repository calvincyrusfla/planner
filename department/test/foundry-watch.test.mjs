// `foundry watch`: the pure event logic, and one real CLI run on a scratch board.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { watchEvents } from '../lib/foundry-watch.mjs';

const NOW = Date.parse('2026-11-08T12:00:00.000Z');
const PR_URL = 'https://github.com/owner/name/pull/7';
const ANSWER = 'node department/dept.mjs foundry answer S-001 a --guidance "..."';

const minutesAgo = (n) => new Date(NOW - n * 60000).toISOString();
const kinds = (events) => events.map((e) => e.kind);
const result = (state, extra = {}) => ({ foundryTask: 'F-012', state, pr: null, error: null, ...extra });

function batch(status, results = {}, extra = {}) {
  return { id: 'S-001', status, heartbeatAt: minutesAgo(1), subtasks: [{ key: 'a', title: 'Build it' }], results, ...extra };
}

// Events for one subtask going from one result to another in a running batch.
const change = (from, to) => watchEvents([batch('running', { a: from })], [batch('running', { a: to })], NOW);
const asking = () => result('needs_help', { help: { question: 'Which base?' } });

test('HELP when a subtask starts waiting for an answer', () => {
  const events = change(result('building'), asking());
  assert.deepEqual(kinds(events), ['HELP']);
  assert.equal(events[0].line, `HELP  S-001 #a F-012 Build it: Which base?\n  answer: ${ANSWER}`);
  assert.equal(events[0].question, 'Which base?');
  assert.equal(events[0].answer, ANSWER);
});

test('HELP when the question was already waiting at the start', () => {
  assert.deepEqual(kinds(change(asking(), asking())), ['HELP']);
});

test('no HELP for a closed batch, which cannot be answered', () => {
  const closed = batch('cancelled', { a: asking() });
  assert.deepEqual(watchEvents([closed], [closed], NOW), []);
});

test('DONE when a subtask becomes complete', () => {
  const events = change(result('reviewing'), result('complete', { pr: { state: 'open', url: PR_URL } }));
  assert.deepEqual(kinds(events), ['DONE']);
  assert.equal(events[0].line, `DONE  S-001 #a F-012 Build it PR ${PR_URL}`);
  assert.equal(events[0].url, PR_URL);
});

test('DONE when a subtask gets a PR url it did not have', () => {
  const events = change(result('reviewing'), result('reviewing', { pr: { state: 'open', url: PR_URL } }));
  assert.deepEqual(kinds(events), ['DONE']);
});

test('no DONE for a subtask that was already complete with the same PR', () => {
  const complete = result('complete', { pr: { state: 'open', url: PR_URL } });
  assert.deepEqual(change(complete, complete), []);
});

for (const state of ['quarantined', 'failed']) {
  test(`FAIL when a subtask becomes ${state}`, () => {
    const events = change(result('building'), result(state, { error: 'tests red' }));
    assert.deepEqual(kinds(events), ['FAIL']);
    assert.equal(events[0].line, 'FAIL  S-001 #a F-012 Build it: tests red');
    assert.equal(events[0].error, 'tests red');
  });
}

test('FAIL when the PR state becomes failed', () => {
  const open = result('complete', { pr: { state: 'open', url: PR_URL } });
  const failed = result('complete', { pr: { state: 'failed', url: PR_URL, error: 'merge conflict' } });
  const events = change(open, failed);
  assert.deepEqual(kinds(events), ['FAIL']);
  assert.equal(events[0].line, 'FAIL  S-001 #a F-012 Build it: merge conflict');
});

test('no FAIL for a subtask that had already failed', () => {
  const failed = result('failed', { error: 'tests red' });
  assert.deepEqual(change(failed, failed), []);
});

for (const status of ['done', 'failed', 'cancelled']) {
  test(`BATCH when a batch becomes ${status}`, () => {
    const events = watchEvents([batch('running')], [batch(status)], NOW);
    assert.deepEqual(kinds(events), ['BATCH']);
    assert.equal(events[0].line, `BATCH S-001 ${status}`);
    assert.equal(events[0].status, status);
  });
}

test('no BATCH for a batch that was already closed at the start', () => {
  assert.deepEqual(watchEvents([batch('done')], [batch('done')], NOW), []);
});

for (const status of ['claimed', 'running']) {
  test(`STALE when a ${status} batch has had no heartbeat for over 10 minutes`, () => {
    const old = batch(status, {}, { heartbeatAt: minutesAgo(11) });
    const events = watchEvents([old], [old], NOW);
    assert.deepEqual(kinds(events), ['STALE']);
    assert.equal(events[0].line, 'STALE S-001 no Foundry heartbeat for 11 min');
    assert.equal(events[0].minutes, 11);
  });
}

test('STALE falls back to the claim time when there was never a heartbeat', () => {
  const old = batch('claimed', {}, { heartbeatAt: null, claimedBy: { host: 'pc', service: 'foundry', at: minutesAgo(25) } });
  assert.equal(watchEvents([old], [old], NOW)[0].line, 'STALE S-001 no Foundry heartbeat for 25 min');
});

test('no STALE at exactly 10 minutes or for a batch nobody has claimed', () => {
  const recent = batch('running', {}, { heartbeatAt: minutesAgo(10) });
  const queued = batch('queued', {}, { heartbeatAt: minutesAgo(60) });
  assert.deepEqual(watchEvents([recent], [recent], NOW), []);
  assert.deepEqual(watchEvents([queued], [queued], NOW), []);
});

test('transient subtask states produce no events', () => {
  const steps = ['queued', 'sizing', 'building', 'reviewing', 'answered'];
  for (const from of steps) {
    for (const to of steps) assert.deepEqual(change(result(from), result(to)), [], `${from} -> ${to}`);
  }
  assert.deepEqual(change(asking(), result('answered')), []);
});

test('a subtask getting its first result or its F-id produces no events', () => {
  assert.deepEqual(watchEvents([batch('claimed')], [batch('running', { a: result('sizing') })], NOW), []);
  assert.deepEqual(change(result('queued', { foundryTask: null }), result('sizing')), []);
});

test('a batch moving through queued, claimed and running produces no events', () => {
  assert.deepEqual(watchEvents([batch('queued')], [batch('claimed')], NOW), []);
  assert.deepEqual(watchEvents([batch('claimed')], [batch('running')], NOW), []);
});

test('a batch dispatched during the watch is included', () => {
  const known = batch('running');
  const added = batch('running', { a: asking() }, { id: 'S-002' });
  const events = watchEvents([known], [known, added], NOW);
  assert.deepEqual(kinds(events), ['HELP']);
  assert.equal(events[0].batch, 'S-002');
});

test('a batch dispatched during the watch that is simply queued is not an event', () => {
  const known = batch('running');
  assert.deepEqual(watchEvents([known], [known, batch('queued', {}, { id: 'S-002' })], NOW), []);
});

test('a batch dispatched and closed during the watch counts as BATCH', () => {
  const known = batch('running');
  const events = watchEvents([known], [known, batch('cancelled', {}, { id: 'S-002' })], NOW);
  assert.deepEqual(kinds(events), ['BATCH']);
  assert.equal(events[0].line, 'BATCH S-002 cancelled');
});

// The child must only ever see the scratch board: no dashboard, hub or proxy.
function scratchEnv(dataPath) {
  const env = { ...process.env, DEPT_DATA: dataPath, DEPT_HUB: 'off', DEPT_URL: 'http://127.0.0.1:9' };
  for (const key of Object.keys(env)) {
    if (/^(https?_proxy|dept_hub_url)$/i.test(key)) delete env[key];
  }
  return env;
}

// Written through store.mjs in a child process, which reads DEPT_DATA on import,
// so the board lands exactly where the CLI will look for it.
function seedBoard(env, seeded) {
  const script = 'const { load, save } = await import(process.env.SEED_STORE); const s = load(); s.foundryBatches = [JSON.parse(process.env.SEED_BATCH)]; save(s);';
  execFileSync(process.execPath, ['--input-type=module', '-e', script], {
    env: { ...env, SEED_STORE: new URL('../lib/store.mjs', import.meta.url).href, SEED_BATCH: JSON.stringify(seeded) },
    stdio: 'pipe',
  });
}

test('CLI: a question already waiting ends the watch at once with a HELP line', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'foundry-watch-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const env = scratchEnv(path.join(dir, 'state.json'));
  seedBoard(env, {
    ...batch('running', { a: asking() }, { heartbeatAt: new Date().toISOString() }),
    title: 'Batch',
    repo: 'owner/name',
    base: null,
    goal: '',
    createdAt: new Date().toISOString(),
    createdBy: 'test',
    updatedAt: new Date().toISOString(),
    finishedAt: null,
    claimedBy: { host: 'pc', service: 'foundry', at: new Date().toISOString() },
    note: '',
  });

  const timeoutS = 30;
  const started = Date.now();
  const run = spawnSync(
    process.execPath,
    [fileURLToPath(new URL('../dept.mjs', import.meta.url)), 'foundry', 'watch', '--interval', '5', '--timeout', String(timeoutS)],
    { env, encoding: 'utf8', timeout: 2 * timeoutS * 1000 },
  );

  assert.equal(run.status, 0, run.stderr);
  assert.ok(Date.now() - started < timeoutS * 1000, 'the watch should end well before its timeout');
  assert.ok(run.stdout.includes('HELP  S-001 #a F-012 Build it: Which base?'), run.stdout);
  assert.ok(run.stdout.includes(`  answer: ${ANSWER}`), run.stdout);
  assert.ok(!run.stdout.includes('TIMEOUT'), run.stdout);
});
