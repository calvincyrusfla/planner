import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as core from '../lib/core.mjs';
import { applyOp, OP_TYPES } from '../lib/ops.mjs';
import { getFoundryUpdate, latestFoundryUpdate } from '../lib/foundry-updates.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(here, '..', 'dept.mjs');
const fresh = () => core.createInitialState();
const op = (s, type, args) => applyOp(s, { type, args });
const worker = { host: 'PC1', service: 'foundry@PC1' };
const status = (code) => (e) => e instanceof core.DeptError && e.status === code;

test('update ops are registered', () => {
  for (const t of ['requestFoundryUpdate', 'claimFoundryUpdate', 'finishFoundryUpdate', 'cancelFoundryUpdate']) assert.ok(OP_TYPES.includes(t), t);
});

test('requestFoundryUpdate stores the request shape', () => {
  const s = fresh();
  const u = op(s, 'requestFoundryUpdate', { repo: 'me/planner', settings: { FOUNDRY_LEDGER_READ: 'on', FOUNDRY_N: 3, FOUNDRY_B: true }, now: true, by: 'fm' });
  assert.equal(u.id, 'U-001');
  assert.equal(u.repo, 'me/planner');
  assert.deepEqual(u.settings, { FOUNDRY_LEDGER_READ: 'on', FOUNDRY_N: '3', FOUNDRY_B: 'true' });
  assert.equal(u.now, true);
  assert.equal(u.status, 'queued');
  assert.equal(u.createdBy, 'fm');
  assert.equal(u.claimedBy, null);
  assert.equal(u.finishedAt, null);
  assert.equal(u.result, null);
  assert.ok(u.createdAt);
  assert.equal(s.foundryUpdates.length, 1);
  assert.equal(s.nextUpdateNumber, 2);
  const plain = op(s, 'requestFoundryUpdate', { repo: 'me/other' });
  assert.equal(plain.id, 'U-002');
  assert.deepEqual(plain.settings, {});
  assert.equal(plain.now, false);
  assert.equal(latestFoundryUpdate(s, 'me/planner').id, 'U-001');
  assert.equal(latestFoundryUpdate(s).id, 'U-002');
  assert.equal(latestFoundryUpdate(s, 'x/y'), null);
  assert.equal(getFoundryUpdate(s, 'u-002').id, 'U-002');
  assert.throws(() => getFoundryUpdate(s, 'U-099'), status(404));
});

test('repo and settings are validated', () => {
  const s = fresh();
  assert.throws(() => op(s, 'requestFoundryUpdate', {}), status(400));
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'not a repo' }), /owner\/name/);
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: { PATH: 'x' } }), /PATH is not allowed/);
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: { foundry_x: 'x' } }), status(400));
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: { FOUNDRY_X: 'v'.repeat(201) } }), /too long/);
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: { FOUNDRY_X: { a: 1 } } }), /must be a string/);
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: 'FOUNDRY_X=1' }), status(400));
  const many = Object.fromEntries(Array.from({ length: 21 }, (_, i) => [`FOUNDRY_K${i}`, 'x']));
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: many }), /at most 20/);
  delete many.FOUNDRY_K20;
  assert.equal(op(s, 'requestFoundryUpdate', { repo: 'me/p', settings: many }).id, 'U-001');
  assert.equal(op(s, 'requestFoundryUpdate', { repo: 'me/q', settings: { FOUNDRY_X: 'v'.repeat(200) } }).id, 'U-002');
});

test('one open request per repo', () => {
  const s = fresh();
  op(s, 'requestFoundryUpdate', { repo: 'me/p' });
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p' }), (e) => status(409)(e) && /U-001/.test(e.message));
  op(s, 'claimFoundryUpdate', { id: 'U-001', ...worker });
  assert.throws(() => op(s, 'requestFoundryUpdate', { repo: 'me/p' }), status(409));
  op(s, 'finishFoundryUpdate', { id: 'U-001', service: worker.service, status: 'done' });
  assert.equal(op(s, 'requestFoundryUpdate', { repo: 'me/p' }).id, 'U-002');
});

test('claim and finish transitions', () => {
  const s = fresh();
  op(s, 'requestFoundryUpdate', { repo: 'me/p' });
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-001', service: worker.service, status: 'done' }), status(409));
  assert.throws(() => op(s, 'claimFoundryUpdate', { id: 'U-001', host: 'PC1' }), status(400));
  const c = op(s, 'claimFoundryUpdate', { id: 'U-001', ...worker });
  assert.equal(c.ok, true);
  assert.equal(c.update.status, 'claimed');
  assert.equal(c.update.claimedBy.host, 'PC1');
  assert.equal(c.update.claimedBy.service, 'foundry@PC1');
  assert.ok(c.update.claimedBy.at);
  assert.throws(() => op(s, 'claimFoundryUpdate', { id: 'U-001', ...worker }), status(409));
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-001', service: 'other', status: 'done' }), status(409));
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-001', service: worker.service, status: 'weird' }), status(400));
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-001', service: worker.service, status: 'done', result: { message: 5 } }), status(400));
  const f = op(s, 'finishFoundryUpdate', {
    id: 'U-001', service: worker.service, status: 'done', result: { commitBefore: 'abc', commitAfter: 'def', message: 'm'.repeat(2500) },
  });
  assert.equal(f.update.status, 'done');
  assert.equal(f.update.result.commitBefore, 'abc');
  assert.equal(f.update.result.commitAfter, 'def');
  assert.equal(f.update.result.message.length, 2000);
  assert.ok(f.update.finishedAt);
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-001', service: worker.service, status: 'failed' }), status(409));
  assert.throws(() => op(s, 'cancelFoundryUpdate', { id: 'U-001' }), status(409));

  op(s, 'requestFoundryUpdate', { repo: 'me/p' });
  op(s, 'claimFoundryUpdate', { id: 'U-002', ...worker });
  const r = op(s, 'finishFoundryUpdate', { id: 'U-002', service: worker.service, status: 'refused', result: { message: 'dirty tree' } });
  assert.equal(r.update.status, 'refused');
  assert.equal(r.update.result.message, 'dirty tree');
  assert.equal(r.update.result.commitBefore, null);
});

test('cancel works on queued and claimed requests only', () => {
  const s = fresh();
  op(s, 'requestFoundryUpdate', { repo: 'me/p' });
  const c = op(s, 'cancelFoundryUpdate', { id: 'U-001', by: 'me' });
  assert.equal(c.update.status, 'cancelled');
  assert.ok(c.update.finishedAt);
  assert.match(s.log.at(-1).message, /U-001 cancelled by me/);
  assert.throws(() => op(s, 'claimFoundryUpdate', { id: 'U-001', ...worker }), status(409));
  assert.throws(() => op(s, 'cancelFoundryUpdate', { id: 'U-001' }), status(409));
  op(s, 'requestFoundryUpdate', { repo: 'me/p' });
  op(s, 'claimFoundryUpdate', { id: 'U-002', ...worker });
  assert.equal(op(s, 'cancelFoundryUpdate', { id: 'U-002' }).update.status, 'cancelled');
  assert.throws(() => op(s, 'finishFoundryUpdate', { id: 'U-002', service: worker.service, status: 'done' }), status(409));
  assert.throws(() => op(s, 'cancelFoundryUpdate', { id: 'U-404' }), status(404));
});

test('only the newest 50 requests are kept', () => {
  const s = fresh();
  for (let i = 0; i < 60; i++) {
    const u = op(s, 'requestFoundryUpdate', { repo: 'me/p' });
    op(s, 'cancelFoundryUpdate', { id: u.id });
  }
  assert.equal(s.foundryUpdates.length, 50);
  assert.equal(s.foundryUpdates[0].id, 'U-011');
  assert.equal(s.foundryUpdates.at(-1).id, 'U-060');
  assert.equal(op(s, 'requestFoundryUpdate', { repo: 'me/p' }).id, 'U-061');
});

test('CLI foundry update / status / cancel on a scratch board', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dept-update-'));
  const env = { ...process.env, DEPT_HUB: 'off', DEPT_DATA: path.join(dir, 'state.json'), DEPT_URL: 'http://127.0.0.1:9', HTTPS_PROXY: '', https_proxy: '' };
  const cli = (...args) => execFileSync(process.execPath, [CLI, ...args], { env, encoding: 'utf8' });
  try {
    let out = cli('foundry', 'update', '--repo', 'me/planner', '--set', 'FOUNDRY_LEDGER_READ=on');
    assert.match(out, /U-001 requested for me\/planner: pull, then restart \(FOUNDRY_LEDGER_READ=on\)/);
    const stored = JSON.parse(fs.readFileSync(env.DEPT_DATA, 'utf8')).foundryUpdates[0];
    assert.deepEqual(stored.settings, { FOUNDRY_LEDGER_READ: 'on' });
    assert.equal(stored.now, false);
    out = cli('foundry', 'update', 'status', 'U-001');
    assert.match(out, /U-001 \[queued\] me\/planner/);
    assert.match(out, /FOUNDRY_LEDGER_READ=on/);
    assert.match(out, /waiting for Foundry/);
    out = cli('foundry', 'update', 'status', '--repo', 'me/planner');
    assert.match(out, /U-001 \[queued\]/);
    out = cli('foundry', 'update', 'cancel', 'U-001');
    assert.match(out, /U-001 cancelled/);
    out = cli('foundry', 'update', '--repo', 'me/planner', '--now');
    assert.match(out, /U-002 requested for me\/planner: pull, then restart \(no settings\)/);
    const second = JSON.parse(fs.readFileSync(env.DEPT_DATA, 'utf8')).foundryUpdates[1];
    assert.equal(second.now, true);
    assert.equal(JSON.parse(cli('foundry', 'update', 'status', 'U-002', '--json')).status, 'queued');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
