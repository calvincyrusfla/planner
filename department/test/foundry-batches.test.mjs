import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as core from '../lib/core.mjs';
import { applyOp, OP_TYPES } from '../lib/ops.mjs';
import { TAKEOVER_MS } from '../lib/foundry-batches.mjs';
import { makeHandler } from '../../netlify/functions/department.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(here, '..', 'dept.mjs');
const fresh = () => core.createInitialState();
const op = (s, type, args) => applyOp(s, { type, args });
const host = 'PC1';
const service = 'foundry@PC1';
const sample = (extra = {}) => ({
  title: 'Two things',
  repo: 'me/planner',
  base: 'main',
  subtasks: [
    { title: 'Add X', description: 'do x', files: ['a.py', 'b\\c.py'], size: 's', acceptance: ['x works'] },
    { title: 'Add Y', depends_on: ['1'], acceptance_criteria: ['y works'] },
  ],
  ...extra,
});

function withClock(start, fn) {
  let t = start;
  core.setClock(() => t);
  try {
    return fn((ms) => (t += ms));
  } finally {
    core.setClock(null);
  }
}

test('ops are registered', () => {
  for (const t of ['dispatchBatch', 'claimBatch', 'updateBatch', 'cancelBatch']) assert.ok(OP_TYPES.includes(t), t);
});

test('fresh and old states have foundryBatches', () => {
  assert.deepEqual(fresh().foundryBatches, []);
  assert.equal(fresh().nextBatchNumber, 1);
  const old = fresh();
  delete old.foundryBatches;
  delete old.nextBatchNumber;
  const n = core.normalize(old);
  assert.deepEqual(n.foundryBatches, []);
  assert.equal(n.nextBatchNumber, 1);
  // ops work on an un-normalised old state too
  const raw = { ...fresh() };
  delete raw.foundryBatches;
  assert.equal(op(raw, 'dispatchBatch', sample()).id, 'S-001');
});

test('dispatchBatch builds the batch shape and does not take a team', () => {
  const s = fresh();
  const b = op(s, 'dispatchBatch', { ...sample(), by: 'floor' });
  assert.equal(b.id, 'S-001');
  assert.equal(b.status, 'queued');
  assert.equal(b.repo, 'me/planner');
  assert.equal(b.base, 'main');
  assert.equal(b.createdBy, 'floor');
  assert.equal(b.claimedBy, null);
  assert.deepEqual(b.results, {});
  assert.deepEqual(b.subtasks[0], {
    key: '1', title: 'Add X', description: 'do x', files: ['a.py', 'b/c.py'], readFiles: [], size: 'S', acceptance: ['x works'], dependsOn: [],
  });
  assert.deepEqual(b.subtasks[1].dependsOn, ['1']);
  assert.deepEqual(b.subtasks[1].acceptance, ['y works']);
  assert.equal(s.tasks.length, 0);
  assert.ok(s.teams.every((t) => !t.taskId));
  assert.equal(s.log.at(-1).kind, 'foundry');
  assert.equal(s.log.at(-1).batchId, 'S-001');
  assert.equal(op(s, 'dispatchBatch', sample()).id, 'S-002');
  assert.equal(s.foundryBatches.length, 2);
});

test('dispatchBatch validates', () => {
  const s = fresh();
  const bad = (patch, re) => assert.throws(() => op(s, 'dispatchBatch', { ...sample(), ...patch }), re);
  bad({ title: '' }, /title is required/);
  bad({ title: 'x'.repeat(201) }, /too long/);
  bad({ repo: 'nope' }, /owner\/name/);
  bad({ repo: 'a/b/c' }, /owner\/name/);
  bad({ base: '../x' }, /branch/);
  bad({ subtasks: [] }, /at least one/);
  bad({ subtasks: Array.from({ length: 21 }, (_, i) => ({ title: `t${i}` })) }, /at most 20/);
  bad({ subtasks: [{ title: 'a', files: ['/etc/passwd'] }] }, /relative/);
  bad({ subtasks: [{ title: 'a', files: ['C:\\x'] }] }, /relative/);
  bad({ subtasks: [{ title: 'a', files: ['a/../../x'] }] }, /\.\./);
  bad({ subtasks: [{ title: 'a', files: Array.from({ length: 51 }, (_, i) => `f${i}`) }] }, /at most 50/);
  bad({ subtasks: [{ title: 'a', description: 'x'.repeat(20001) }] }, /too long/);
  bad({ subtasks: [{ title: 'a', size: 'XXL' }] }, /size/);
  bad({ subtasks: [{ title: 'a', dependsOn: ['9'] }] }, /unknown subtask/);
  bad({ subtasks: [{ title: 'a', dependsOn: ['1'] }] }, /itself/);
  bad({ subtasks: [{ key: 'a', title: 'a', dependsOn: ['b'] }, { key: 'b', title: 'b', dependsOn: ['a'] }] }, /cycle/);
  bad({ subtasks: [{ key: 'a', title: 'a' }, { key: 'a', title: 'b' }] }, /duplicate/);
  assert.equal(s.foundryBatches.length, 0);
  assert.equal(s.nextBatchNumber, 1);
});

test('dispatchBatch stores readFiles cleaned, deduplicated and apart from files', () => {
  const s = fresh();
  const b = op(s, 'dispatchBatch', sample({ subtasks: [
    { title: 'A', files: ['x.py'], readFiles: ['a/b.py', 'a\\b.py', 'c.py'] },
    { title: 'B', read_files: 'd.py, e/f.py' },
    { title: 'C' },
  ] }));
  assert.deepEqual(b.subtasks[0].readFiles, ['a/b.py', 'c.py']);
  assert.deepEqual(b.subtasks[0].files, ['x.py']);
  assert.deepEqual(b.subtasks[1].readFiles, ['d.py', 'e/f.py']);
  assert.deepEqual(b.subtasks[1].files, []);
  assert.deepEqual(b.subtasks[2].readFiles, []);
});

test('dispatchBatch validates readFiles and writes nothing when they are bad', () => {
  const s = fresh();
  const bad = (readFiles, re) => assert.throws(() => op(s, 'dispatchBatch', sample({ subtasks: [{ title: 'a', readFiles }] })), re);
  bad(['/etc/passwd'], /relative/);
  bad(['C:\\x'], /relative/);
  bad(['a/../../x'], /\.\./);
  bad(Array.from({ length: 51 }, (_, i) => `f${i}`), /at most 50/);
  bad(5, /must be a list/);
  assert.equal(s.foundryBatches.length, 0);
  assert.equal(s.nextBatchNumber, 1);
});

test('readFiles survive a save/load and an importState', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample({ subtasks: [{ title: 'A', readFiles: ['a/b.py', 'c.py'] }] }));
  const loaded = core.normalize(JSON.parse(JSON.stringify(s)));
  assert.deepEqual(loaded.foundryBatches[0].subtasks[0].readFiles, ['a/b.py', 'c.py']);
  const ours = fresh();
  op(ours, 'importState', { state: s });
  assert.deepEqual(ours.foundryBatches[0].subtasks[0].readFiles, ['a/b.py', 'c.py']);
});

test('claimBatch: first claimer wins, others refused without a write, own re-claim ok', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample());
  const r = op(s, 'claimBatch', { id: 's-001', host, service });
  assert.equal(r.ok, true);
  assert.equal(r.batch.status, 'claimed');
  assert.equal(r.batch.claimedBy.host, host);
  assert.ok(r.batch.heartbeatAt);
  const other = op(s, 'claimBatch', { id: 'S-001', host: 'PC2', service: 'foundry@PC2' });
  assert.equal(other.ok, false);
  assert.equal(other.stored, false);
  assert.match(other.reason, /already claimed by foundry@PC1/);
  assert.equal(op(s, 'claimBatch', { id: 'S-001', host, service }).ok, true);
  assert.throws(() => op(s, 'claimBatch', { id: 'S-009', host, service }), /No Foundry batch/);
  assert.throws(() => op(s, 'claimBatch', { id: 'S-001', host: '', service }), /host is required/);
});

test('claimBatch takes over a batch with a stale heartbeat', () =>
  withClock(Date.parse('2026-01-01T00:00:00Z'), (advance) => {
    const s = fresh();
    op(s, 'dispatchBatch', sample());
    op(s, 'claimBatch', { id: 'S-001', host, service });
    op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'building', foundryTask: 'F-001' } } });
    advance(TAKEOVER_MS - 1000);
    assert.equal(op(s, 'claimBatch', { id: 'S-001', host: 'PC2', service: 'f2' }).ok, false);
    advance(2000);
    const r = op(s, 'claimBatch', { id: 'S-001', host: 'PC2', service: 'f2' });
    assert.equal(r.ok, true);
    assert.equal(r.takeover, true);
    assert.equal(r.batch.status, 'running');
    assert.equal(r.batch.claimedBy.service, 'f2');
    assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, heartbeat: true }), /not claimed by foundry@PC1/);
  }));

test('updateBatch merges results, runs, and finishes done', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample());
  assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, heartbeat: true }), /not claimed/);
  op(s, 'claimBatch', { id: 'S-001', host, service });
  let b = op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { foundryTask: 'F-012', state: 'building' } } }).batch;
  assert.equal(b.status, 'running');
  assert.equal(b.results['1'].foundryTask, 'F-012');
  b = op(s, 'updateBatch', {
    id: 'S-001', service,
    results: { 1: { state: 'complete', pr: { state: 'open', url: 'https://github.com/me/planner/pull/3', branch: 'f-012' } } },
  }).batch;
  assert.equal(b.results['1'].foundryTask, 'F-012', 'partial update keeps earlier fields');
  assert.equal(b.results['1'].pr.url, 'https://github.com/me/planner/pull/3');
  assert.equal(b.status, 'running');
  // unsafe PR url is dropped
  b = op(s, 'updateBatch', { id: 'S-001', service, results: { 2: { foundryTask: 'F-013', state: 'reviewing', pr: { url: 'javascript:alert(1)' } } } }).batch;
  assert.equal(b.results['2'].pr.url, null);
  assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, results: { 9: { state: 'complete' } } }), /no subtask 9/);
  assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, results: { 2: { state: 'weird' } } }), /result state/);
  assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, status: 'queued' }), /status must be/);
  b = op(s, 'updateBatch', { id: 'S-001', service, results: { 2: { state: 'complete' } } }).batch;
  assert.equal(b.status, 'done');
  assert.ok(b.finishedAt);
  assert.throws(() => op(s, 'updateBatch', { id: 'S-001', service, heartbeat: true }), /is done/);
  assert.match(s.log.at(-1).message, /S-001 done \(2\/2 complete\)/);
});

test('updateBatch: any failed/quarantined → failed; explicit failed with note; heartbeat only', () =>
  withClock(Date.parse('2026-01-01T00:00:00Z'), (advance) => {
    const s = fresh();
    op(s, 'dispatchBatch', sample());
    op(s, 'claimBatch', { id: 'S-001', host, service });
    advance(5000);
    let b = op(s, 'updateBatch', { id: 'S-001', service, heartbeat: true }).batch;
    assert.equal(b.status, 'claimed');
    assert.equal(b.heartbeatAt, '2026-01-01T00:00:05.000Z');
    b = op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'quarantined', error: 'tests failed' }, 2: { state: 'complete' } } }).batch;
    assert.equal(b.status, 'failed');
    op(s, 'dispatchBatch', sample());
    op(s, 'claimBatch', { id: 'S-002', host, service });
    b = op(s, 'updateBatch', { id: 'S-002', service, status: 'failed', note: 'could not submit #1' }).batch;
    assert.equal(b.status, 'failed');
    assert.equal(b.note, 'could not submit #1');
  }));

test('cancelBatch', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample());
  const r = op(s, 'cancelBatch', { id: 'S-001', by: 'me' });
  assert.equal(r.batch.status, 'cancelled');
  assert.match(s.log.at(-1).message, /cancelled by me/);
  assert.equal(op(s, 'cancelBatch', { id: 'S-001' }).stored, false);
  assert.equal(op(s, 'claimBatch', { id: 'S-001', host, service }).ok, false);
  op(s, 'dispatchBatch', sample());
  op(s, 'claimBatch', { id: 'S-002', host, service });
  op(s, 'updateBatch', { id: 'S-002', service, status: 'done' });
  assert.throws(() => op(s, 'cancelBatch', { id: 'S-002' }), /already done/);
});

test('importState merges batches by id without loss', () => {
  const ours = fresh();
  op(ours, 'dispatchBatch', sample({ title: 'ours 1' }));
  const theirs = JSON.parse(JSON.stringify(ours));
  op(ours, 'claimBatch', { id: 'S-001', host, service }); // ours is newer
  op(theirs, 'dispatchBatch', sample({ title: 'theirs 2' }));
  const clash = fresh();
  core.setClock(() => Date.parse('2020-01-01T00:00:00Z'));
  try { op(clash, 'dispatchBatch', sample({ title: 'clash 1' })); } finally { core.setClock(null); }
  const r1 = op(ours, 'importState', { state: theirs });
  assert.equal(r1.batches.added, 1);
  assert.equal(ours.foundryBatches.find((b) => b.id === 'S-001').status, 'claimed', 'newer copy kept');
  assert.equal(ours.foundryBatches.find((b) => b.id === 'S-002').title, 'theirs 2');
  const r2 = op(ours, 'importState', { state: clash });
  assert.deepEqual(r2.batches.idMap, { 'S-001': 'S-003' });
  assert.deepEqual(ours.foundryBatches.map((b) => b.title).sort(), ['clash 1', 'ours 1', 'theirs 2']);
  assert.equal(op(ours, 'dispatchBatch', sample()).id, 'S-004');
  // re-import adds nothing; old board without batches is fine
  assert.equal(op(ours, 'importState', { state: theirs }).batches.added, 0);
  const old = fresh();
  delete old.foundryBatches;
  assert.equal(op(ours, 'importState', { state: old }).batches.added, 0);
  assert.equal(ours.foundryBatches.length, 4);
});

test('dispatchBatch refuses with 409 once 200 batches are open', () => {
  const s = fresh();
  for (let i = 0; i < 200; i++) op(s, 'dispatchBatch', sample());
  assert.throws(() => op(s, 'dispatchBatch', sample()), (e) => e.status === 409 && /200 open/.test(e.message));
  op(s, 'cancelBatch', { id: 'S-001' });
  assert.equal(op(s, 'dispatchBatch', sample()).id, 'S-201');
  assert.equal(s.foundryBatches.length, 200, 'the finished one was pruned');
});

test('normalize drops junk subtasks so updateBatch still works', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample());
  s.foundryBatches[0].subtasks.push(null, 'junk', { title: 'no key' }, { key: '' });
  op(s, 'claimBatch', { id: 'S-001', host, service });
  const b = op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'complete' }, 2: { state: 'complete' } } }).batch;
  assert.equal(b.subtasks.length, 2);
  assert.equal(b.status, 'done');
});

test('result updates are not logged one by one; status changes and claims are', () => {
  const s = fresh();
  op(s, 'dispatchBatch', sample());
  op(s, 'claimBatch', { id: 'S-001', host, service });
  const n = s.log.length;
  op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'building' } } }); // claimed -> running
  op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'reviewing' } } });
  op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { state: 'complete' } } });
  assert.equal(s.log.length, n + 1);
  assert.match(s.log.at(-1).message, /S-001 running/);
});

test('importState validates imported batches and drops bad ones (charlie-6 repro)', () => {
  const src = fresh();
  op(src, 'dispatchBatch', sample({ title: 'good' }));
  op(src, 'dispatchBatch', sample({ title: 'evil base' }));
  op(src, 'dispatchBatch', sample({ title: 'evil files' }));
  op(src, 'dispatchBatch', sample({ title: 'evil url' }));
  op(src, 'claimBatch', { id: 'S-004', host, service });
  op(src, 'updateBatch', { id: 'S-004', service, results: { 1: { state: 'building', pr: { url: 'https://ok' } } } });
  const raw = JSON.parse(JSON.stringify(src));
  raw.foundryBatches[1].base = '--upload-pack=touch /tmp/pwned';
  raw.foundryBatches[2].subtasks[0].files = ['../../etc/passwd'];
  raw.foundryBatches[3].results['1'].pr.url = 'javascript:alert(1)';
  raw.foundryBatches.push(
    { id: 'S-005', title: 'x', repo: 'a/b', status: 'queued', subtasks: [{ key: '1', title: 'a', dependsOn: ['9'] }] },
    { id: 'S-006', title: 'x', repo: 'evil', status: 'queued', subtasks: [{ title: 'a' }] },
    { id: 'S-007', title: 'x', repo: 'a/b', status: 'hacked', subtasks: [{ title: 'a' }] },
    { id: 'S-008', title: 'x', repo: 'a/b', status: 'queued', subtasks: Array.from({ length: 21 }, (_, i) => ({ title: `t${i}` })) },
    'junk',
  );
  const s = fresh();
  const r = op(s, 'importState', { state: raw });
  assert.deepEqual(s.foundryBatches.map((b) => b.title), ['good', 'evil url']);
  assert.deepEqual(r.batches.rejected.map((x) => x.id), ['S-002', 'S-003', 'S-005', 'S-006', 'S-007', 'S-008', '']);
  assert.match(r.batches.rejected[0].reason, /branch/);
  assert.match(r.batches.rejected[1].reason, /\.\./);
  assert.equal(s.foundryBatches[1].results['1'].pr.url, null, 'unsafe PR url cleaned');
  assert.equal(s.foundryBatches[1].claimedBy.service, service);
  for (const base of ['-x', 'a..b', 'a b', 'a;b', 'feat/ok']) {
    const ok = base === 'feat/ok';
    const one = JSON.parse(JSON.stringify(src));
    one.foundryBatches = [{ ...one.foundryBatches[0], base }];
    const t = fresh();
    assert.equal(op(t, 'importState', { state: one }).batches.added, ok ? 1 : 0, base);
  }
});

// ---------- hub function ----------

function fakeStore() {
  const docs = new Map();
  let n = 0;
  return {
    writes: 0,
    async getWithMetadata(key) {
      const d = docs.get(key);
      return d ? { data: JSON.parse(d.body), etag: d.etag } : null;
    },
    async setJSON(key, data, opts = {}) {
      const cur = docs.get(key);
      if (opts.onlyIfNew && cur) return { modified: false };
      if (opts.onlyIfMatch && (!cur || cur.etag !== opts.onlyIfMatch)) return { modified: false };
      docs.set(key, { body: JSON.stringify(data), etag: `"e${++n}"` });
      this.writes++;
      return { modified: true };
    },
    force(key, data) { docs.set(key, { body: JSON.stringify(data), etag: `"e${++n}"` }); },
  };
}

test('hub function runs batch ops; a refused claim does not write; old stored state works', async () => {
  const old = process.env.EDIT_KEY;
  process.env.EDIT_KEY = 'sesame';
  try {
    const store = fakeStore();
    const legacy = fresh();
    delete legacy.foundryBatches;
    delete legacy.nextBatchNumber;
    store.force('state', { state: legacy, rev: 7 });
    const handler = makeHandler(() => store);
    const post = (type, args) =>
      handler(new Request('https://x/api/department', {
        method: 'POST', headers: { 'content-type': 'application/json', 'x-edit-key': 'sesame' }, body: JSON.stringify({ type, args }),
      })).then((r) => r.json());
    const d = await post('dispatchBatch', sample());
    assert.equal(d.ok, true);
    assert.equal(d.result.id, 'S-001');
    assert.equal(d.rev, 8);
    assert.equal((await post('claimBatch', { id: 'S-001', host, service })).result.ok, true);
    const writes = store.writes;
    const refused = await post('claimBatch', { id: 'S-001', host: 'PC2', service: 'f2' });
    assert.equal(refused.ok, true);
    assert.equal(refused.result.ok, false);
    assert.equal(refused.rev, 9);
    assert.equal(store.writes, writes, 'refused claim is not written');
    const bad = await post('updateBatch', { id: 'S-001', service: 'f2', heartbeat: true });
    assert.equal(bad.ok, false);
    assert.match(bad.error, /not claimed/);
  } finally {
    if (old === undefined) delete process.env.EDIT_KEY; else process.env.EDIT_KEY = old;
  }
});

test('hub function: two concurrent claims, exactly one wins (compare-and-swap)', async () => {
  const old = process.env.EDIT_KEY;
  process.env.EDIT_KEY = 'k';
  try {
    const docs = new Map();
    let n = 0;
    let gate = null;
    const store = {
      async getWithMetadata(key) {
        const d = docs.get(key);
        const r = d ? { data: JSON.parse(d.body), etag: d.etag } : null;
        if (gate) await gate; // both requests read the same revision
        return r;
      },
      async setJSON(key, data, o = {}) {
        const cur = docs.get(key);
        if (o.onlyIfMatch && (!cur || cur.etag !== o.onlyIfMatch)) return { modified: false };
        docs.set(key, { body: JSON.stringify(data), etag: `e${++n}` });
        return { modified: true };
      },
    };
    const h = makeHandler(() => store);
    const post = (type, args) =>
      h(new Request('https://x/api/department', { method: 'POST', headers: { 'x-edit-key': 'k' }, body: JSON.stringify({ type, args }) })).then((r) => r.json());
    await post('dispatchBatch', { title: 't', repo: 'me/p', subtasks: [{ title: 'a' }] });
    let release;
    gate = new Promise((r) => (release = r));
    const a = post('claimBatch', { id: 'S-001', host: 'A', service: 'fa' });
    const b = post('claimBatch', { id: 'S-001', host: 'B', service: 'fb' });
    setTimeout(() => { gate = null; release(); }, 50);
    const [ra, rb] = await Promise.all([a, b]);
    const wins = [ra, rb].filter((r) => r.result.ok);
    assert.equal(wins.length, 1);
    const loser = [ra, rb].find((r) => !r.result.ok);
    assert.match(loser.result.reason, /already claimed/);
    const final = JSON.parse(docs.get('state').body).state.foundryBatches[0].claimedBy;
    assert.equal(final.service, wins[0].result.batch.claimedBy.service);
  } finally {
    if (old === undefined) delete process.env.EDIT_KEY; else process.env.EDIT_KEY = old;
  }
});

// ---------- CLI (file mode) ----------

test('CLI foundry dispatch / list / poll / cancel on a scratch board', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-foundry-'));
  const env = { ...process.env, DEPT_HUB: 'off', DEPT_DATA: path.join(dir, 'state.json'), DEPT_URL: 'http://127.0.0.1:9', HTTPS_PROXY: '', https_proxy: '' };
  const cli = (...args) => execFileSync(process.execPath, [CLI, ...args], { env, encoding: 'utf8' });
  try {
    let out = cli('foundry', 'dispatch', '--title', 'Two things', '--repo', 'me/planner', '--base', 'main',
      '--subtask', 'Add X::do x::a.py,b/c.py::M', '--subtask', 'Add Y');
    assert.match(out, /S-001 dispatched to Foundry \(me\/planner, 2 subtasks\)/);
    assert.match(cli('foundry', 'dispatch', '--title=Eq form', '--repo=me/planner', '--base=dev', '--subtask=Only one'), /S-002 dispatched/);
    const eq = JSON.parse(fs.readFileSync(env.DEPT_DATA, 'utf8')).foundryBatches[1];
    assert.equal(eq.base, 'dev');
    assert.equal(eq.title, 'Eq form');
    assert.equal(eq.subtasks[0].title, 'Only one');
    const badBase = spawnSync(process.execPath, [CLI, 'foundry', 'dispatch', '--title=x', '--repo=me/p', '--base=--upload-pack=x', '--subtask=a'], { env, encoding: 'utf8' });
    assert.notEqual(badBase.status, 0);
    assert.match(badBase.stderr, /branch/);
    const flagValue = spawnSync(process.execPath, [CLI, 'foundry', 'list', '--open=yes'], { env, encoding: 'utf8' });
    assert.match(flagValue.stderr, /--open takes no value/);
    cli('foundry', 'cancel', 'S-002');
    const file = path.join(dir, 'batch.json');
    fs.writeFileSync(file, JSON.stringify(sample({ title: 'From JSON' })));
    assert.match(cli('foundry', 'dispatch', '--json', file), /S-003 dispatched/);
    const st = JSON.parse(fs.readFileSync(env.DEPT_DATA, 'utf8'));
    assert.deepEqual(st.foundryBatches[0].subtasks[0].files, ['a.py', 'b/c.py']);
    assert.equal(st.foundryBatches[0].subtasks[0].size, 'M');
    assert.equal(st.tasks.length, 0);
    // simulate Foundry progress directly on the file
    const s = core.normalize(st);
    op(s, 'claimBatch', { id: 'S-001', host, service });
    op(s, 'updateBatch', { id: 'S-001', service, results: { 1: { foundryTask: 'F-007', state: 'complete', pr: { state: 'open', url: 'https://github.com/me/planner/pull/9' } } } });
    fs.writeFileSync(env.DEPT_DATA, JSON.stringify(s));
    out = cli('foundry', 'poll', 's-001');
    assert.match(out, /S-001 \[running\] Two things/);
    assert.match(out, /Foundry on PC1/);
    assert.match(out, /1\/2 complete/);
    assert.match(out, /F-007 +complete +Add X +PR https:\/\/github.com\/me\/planner\/pull\/9 \(open\)/);
    assert.equal(JSON.parse(cli('foundry', 'poll', 'S-001', '--json')).results['1'].foundryTask, 'F-007');
    assert.equal(JSON.parse(cli('foundry', 'poll', '--json', 'S-001')).id, 'S-001');
    out = cli('foundry', 'list');
    assert.ok(out.indexOf('S-003') < out.indexOf('S-001'), 'newest first');
    assert.match(cli('foundry', 'cancel', 'S-003'), /S-003 cancelled/);
    assert.doesNotMatch(cli('foundry', 'list', '--open'), /S-003/);
    const bad = spawnSync(process.execPath, [CLI, 'foundry', 'dispatch', '--title', 'x', '--repo', 'me/p', '--subtask', 'A::d::../up'], { env, encoding: 'utf8' });
    assert.notEqual(bad.status, 0);
    assert.match(bad.stderr, /must not contain/);
    const missing = spawnSync(process.execPath, [CLI, 'foundry', 'poll', 'S-099'], { env, encoding: 'utf8' });
    assert.match(missing.stderr, /No Foundry batch S-099/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---------- order length: what the builder sees whole ----------

import { test as orderLimitTest } from 'node:test';
import orderAssert from 'node:assert/strict';
import {
  LIMITS as ORDER_LIMITS,
  dispatchBatch as orderDispatchBatch,
  getBatch as orderGetBatch,
  importBatches as orderImportBatches,
  orderTextLength,
  oversizedOrders,
} from '../lib/foundry-batches.mjs';

const ORDER_MARKER = 100;
const orderBatch = (subtasks, extra = {}) => ({ title: 'Batch', repo: 'owner/name', subtasks, ...extra });
// A description that brings the subtask's order text to exactly `total` characters.
const descriptionFor = (batch, total) => 'x'.repeat(total - (orderTextLength(batch, { description: 'x' }) - 1));

orderLimitTest('builderOrder is 6000 and the stored limits stay at 20000', () => {
  orderAssert.equal(ORDER_LIMITS.builderOrder, 6000);
  orderAssert.equal(ORDER_LIMITS.description, 20000);
  orderAssert.equal(ORDER_LIMITS.goal, 20000);
});

orderLimitTest('orderTextLength follows the composition in _submit', () => {
  const plain = 'abc\n\nPart of "Batch"\n';
  orderAssert.equal(orderTextLength({ title: 'Batch' }, { description: 'abc' }), plain.length + ORDER_MARKER);

  const sized = 'abc\n\nRequested size: M\n\nPart of "Batch"\n';
  orderAssert.equal(orderTextLength({ title: 'Batch' }, { description: 'abc', size: 'M' }), sized.length + ORDER_MARKER);

  const full = 'abc\n\nRequested size: M\n\nPart of "Batch": the goal\n';
  orderAssert.equal(orderTextLength({ title: 'Batch', goal: '  the goal  ' }, { description: 'abc', size: 'M' }), full.length + ORDER_MARKER);

  const bare = 'Part of "Batch"\n';
  orderAssert.equal(orderTextLength({ title: 'Batch' }, {}), bare.length + ORDER_MARKER);

  const onlySize = 'Requested size: S\n\nPart of "Batch"\n';
  orderAssert.equal(orderTextLength({ title: 'Batch' }, { size: 'S' }), onlySize.length + ORDER_MARKER);
});

orderLimitTest('a subtask at exactly the limit passes', () => {
  const batch = orderBatch([]);
  const description = descriptionFor(batch, ORDER_LIMITS.builderOrder);
  batch.subtasks = [{ title: 'Fits', description }];
  orderAssert.equal(orderTextLength(batch, batch.subtasks[0]), ORDER_LIMITS.builderOrder);
  orderAssert.deepEqual(oversizedOrders(batch), []);
  const stored = orderDispatchBatch({}, batch);
  orderAssert.equal(stored.subtasks[0].description, description);
});

orderLimitTest('one character over is listed and refused, naming the subtask', () => {
  const batch = orderBatch([]);
  batch.subtasks = [
    { title: 'Small', description: 'short' },
    { key: 'big', title: 'Big', description: descriptionFor(batch, ORDER_LIMITS.builderOrder + 1) },
  ];
  orderAssert.deepEqual(oversizedOrders(batch), [{ key: 'big', title: 'Big', length: 6001, limit: 6000 }]);
  const state = {};
  orderAssert.throws(
    () => orderDispatchBatch(state, batch),
    (err) => err.status === 400 || /subtask big \("Big"\) order is 6001 characters; the builder sees only 6000/.test(err.message),
  );
  orderAssert.throws(() => orderDispatchBatch(state, batch), /subtask big \("Big"\) order is 6001 characters; the builder sees only 6000/);
  orderAssert.deepEqual(state.foundryBatches, []);
  orderAssert.equal(state.nextBatchNumber, 1);
});

orderLimitTest('the key of an oversized subtask defaults to its 1-based position', () => {
  const batch = orderBatch([{ title: 'A' }, { title: 'B', description: 'y'.repeat(7000) }]);
  orderAssert.deepEqual(oversizedOrders(batch).map((o) => o.key), ['2']);
});

orderLimitTest('a long goal alone pushes every subtask over', () => {
  const batch = orderBatch([{ title: 'A', description: 'a' }, { title: 'B', description: 'b' }], { goal: 'g'.repeat(ORDER_LIMITS.builderOrder) });
  orderAssert.deepEqual(oversizedOrders(batch).map((o) => o.key), ['1', '2']);
  orderAssert.throws(() => orderDispatchBatch({}, batch), (err) => /subtask 1 /.test(err.message) && /subtask 2 /.test(err.message));
});

orderLimitTest('stored batches with long descriptions still load', () => {
  const state = {};
  const at = '2026-01-01T00:00:00.000Z';
  const result = orderImportBatches(state, [{
    id: 'S-007',
    status: 'done',
    title: 'Old batch',
    repo: 'owner/name',
    createdAt: at,
    updatedAt: at,
    subtasks: [{ key: '1', title: 'Long', description: 'd'.repeat(13500) }],
  }]);
  orderAssert.deepEqual(result.rejected, []);
  orderAssert.equal(result.added, 1);
  orderAssert.equal(orderGetBatch(state, 'S-007').subtasks[0].description.length, 13500);
});
