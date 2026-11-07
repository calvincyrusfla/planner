import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../lib/core.mjs';
import { applyOp, importState, OP_TYPES } from '../lib/ops.mjs';

const T0 = Date.parse('2026-11-07T12:00:00.000Z');
let at = T0;
core.setClock(() => at);
afterEach(() => {
  at = T0;
});
const iso = (ms) => new Date(ms).toISOString();
const fresh = () => core.createInitialState();
const RESET_SEC = Math.floor(Date.parse('2026-11-07T15:10:00.000Z') / 1000);
const RESET_ISO = '2026-11-07T15:10:00.000Z';

test('initial state has usage null', () => {
  assert.equal(fresh().usage, null);
});

test('reportUsage stores both windows with the documented shape', () => {
  const s = fresh();
  const r = core.reportUsage(s, {
    fiveHour: { usedPct: 42, resetsAt: RESET_ISO },
    sevenDay: { usedPct: 18.25, resetsAt: '2026-11-12T00:00:00Z' },
    reportedAt: iso(T0 - 5000),
    source: 'PC',
  });
  assert.equal(r.stored, true);
  assert.deepEqual(s.usage, {
    fiveHour: { usedPct: 42, resetsAt: RESET_ISO },
    sevenDay: { usedPct: 18.3, resetsAt: '2026-11-12T00:00:00.000Z' },
    reportedAt: iso(T0 - 5000),
    receivedAt: iso(T0),
    source: 'PC',
  });
  assert.equal(r.usage, s.usage);
});

test('resetsAt accepts epoch seconds, epoch ms, numeric strings and ISO; bad values become null', () => {
  const s = fresh();
  const cases = [
    [RESET_SEC, RESET_ISO],
    [RESET_SEC * 1000, RESET_ISO],
    [String(RESET_SEC), RESET_ISO],
    [String(RESET_SEC * 1000), RESET_ISO],
    [RESET_ISO, RESET_ISO],
    ['2026-11-07T16:10:00+01:00', RESET_ISO],
    ['not a date', null],
    [null, null],
    [undefined, null],
    [0, null],
    [-5, null],
    [true, null],
    [{}, null],
  ];
  for (const [input, want] of cases) {
    at += 1000;
    core.reportUsage(s, { fiveHour: { usedPct: 1, resetsAt: input } });
    assert.equal(s.usage.fiveHour.resetsAt, want, `resetsAt ${JSON.stringify(input)}`);
  }
  assert.equal(core.usageTimeToIso(new Date(RESET_SEC * 1000)), RESET_ISO);
});

test('accepts Claude Code field names (used_percentage, resets_at)', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { used_percentage: 55.55, resets_at: RESET_SEC } });
  assert.deepEqual(s.usage.fiveHour, { usedPct: 55.6, resetsAt: RESET_ISO });
  assert.equal(s.usage.sevenDay, null);
});

test('usedPct is clamped to 0-100 and rounded to 1 decimal; numeric strings accepted', () => {
  const s = fresh();
  const cases = [[-3, 0], [150, 100], [33.333, 33.3], [99.96, 100], ['12.5', 12.5], [0, 0], [100, 100]];
  for (const [input, want] of cases) {
    at += 1000;
    core.reportUsage(s, { sevenDay: { usedPct: input } });
    assert.equal(s.usage.sevenDay.usedPct, want, `usedPct ${input}`);
  }
});

test('invalid reports are rejected with DeptError 400 and leave state alone', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 10 } });
  const before = JSON.stringify(s);
  const bad = [
    {},
    { fiveHour: null, sevenDay: null },
    { fiveHour: { usedPct: 'lots' } },
    { fiveHour: { usedPct: NaN } },
    { fiveHour: { usedPct: Infinity } },
    { fiveHour: {} },
    { fiveHour: 42 },
    { fiveHour: [10] },
    { sevenDay: { usedPct: '' } },
    { fiveHour: { usedPct: 5 }, reportedAt: 'yesterday-ish' },
  ];
  for (const args of bad) {
    assert.throws(() => core.reportUsage(s, args), (e) => e instanceof core.DeptError && e.status === 400, JSON.stringify(args));
  }
  assert.throws(() => core.reportUsage(s), core.DeptError);
  assert.equal(JSON.stringify(s), before);
});

test('one window may be missing', () => {
  const s = fresh();
  core.reportUsage(s, { sevenDay: { usedPct: 7, resetsAt: RESET_SEC } });
  assert.equal(s.usage.fiveHour, null);
  assert.deepEqual(s.usage.sevenDay, { usedPct: 7, resetsAt: RESET_ISO });
});

test('reportedAt: defaults to now, accepts epoch s/ms, future times are capped at now', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 1 } });
  assert.equal(s.usage.reportedAt, iso(T0));
  at += 10_000;
  core.reportUsage(s, { fiveHour: { usedPct: 2 }, reportedAt: Math.floor((at - 2000) / 1000) });
  assert.equal(s.usage.reportedAt, iso(at - 2000));
  at += 10_000;
  core.reportUsage(s, { fiveHour: { usedPct: 3 }, reportedAt: at - 1000 });
  assert.equal(s.usage.reportedAt, iso(at - 1000));
  at += 10_000;
  core.reportUsage(s, { fiveHour: { usedPct: 4 }, reportedAt: iso(at + 3_600_000) });
  assert.equal(s.usage.reportedAt, iso(at));
  assert.equal(s.usage.receivedAt, iso(at));
  // ...so a correct clock afterwards still gets through.
  at += 1000;
  assert.equal(core.reportUsage(s, { fiveHour: { usedPct: 5 } }).stored, true);
});

test('an older report is ignored; equal or newer replaces', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 50 }, reportedAt: iso(T0 - 1000), source: 'A' });
  const kept = structuredClone(s.usage);
  const r = core.reportUsage(s, { fiveHour: { usedPct: 10 }, reportedAt: iso(T0 - 60_000), source: 'B' });
  assert.equal(r.stored, false);
  assert.deepEqual(r.usage, kept);
  assert.deepEqual(s.usage, kept);
  const same = core.reportUsage(s, { fiveHour: { usedPct: 51 }, reportedAt: iso(T0 - 1000), source: 'C' });
  assert.equal(same.stored, true);
  assert.equal(s.usage.source, 'C');
  at += 5000;
  const newer = core.reportUsage(s, { sevenDay: { usedPct: 9 }, source: 'D' });
  assert.equal(newer.stored, true);
  assert.equal(s.usage.fiveHour, null, 'a report replaces the whole object, not merges windows');
  assert.equal(s.usage.sevenDay.usedPct, 9);
});

test('source is a short single-line label', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 1 }, source: `  my-pc\n\u0007${'x'.repeat(80)}` });
  assert.ok(s.usage.source.length <= core.USAGE_SOURCE_MAX);
  assert.ok(s.usage.source.startsWith('my-pc x'));
  assert.ok(!/[\u0000-\u001f]/.test(s.usage.source));
  at += 1000;
  core.reportUsage(s, { fiveHour: { usedPct: 1 }, source: { evil: true } });
  assert.equal(s.usage.source, '');
  at += 1000;
  core.reportUsage(s, { fiveHour: { usedPct: 1 } });
  assert.equal(s.usage.source, '');
});

test('reportUsage writes no log entry and leaves tasks, teams and settings untouched', () => {
  const s = fresh();
  core.addTask(s, { title: 'Busy', size: 'large' });
  core.reportProgress(s, { taskId: 'T-001', teamId: 'alpha', progress: 30, message: 'hi' });
  at += 60_000;
  const before = structuredClone({ ...s, usage: undefined });
  for (let i = 0; i < 20; i++) {
    at += 1000;
    core.reportUsage(s, { fiveHour: { usedPct: i }, sevenDay: { usedPct: i / 2 } });
  }
  assert.deepEqual({ ...s, usage: undefined }, before);
  assert.equal(s.usage.fiveHour.usedPct, 19);
});

test('reportUsage op via applyOp, listed in OP_TYPES', () => {
  assert.ok(OP_TYPES.includes('reportUsage'));
  const s = fresh();
  const r = applyOp(s, {
    type: 'reportUsage',
    args: { fiveHour: { usedPct: 42, resetsAt: RESET_SEC }, sevenDay: null, source: 'PC', extra: 'ignored' },
  });
  assert.equal(r.stored, true);
  assert.deepEqual(s.usage.fiveHour, { usedPct: 42, resetsAt: RESET_ISO });
  assert.equal(s.log.length, 0);
  // The result survives JSON (the server/hub send it back as JSON).
  assert.deepEqual(JSON.parse(JSON.stringify(r)), r);
  assert.throws(() => applyOp(s, { type: 'reportUsage' }), core.DeptError);
});

test('reset clears usage', () => {
  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 1 } });
  applyOp(s, { type: 'reset' });
  assert.equal(s.usage, null);
});

test('normalize: missing usage becomes null, valid usage is kept, garbage is dropped', () => {
  const old = fresh();
  delete old.usage;
  assert.equal(core.normalize(old).usage, null);

  const s = fresh();
  core.reportUsage(s, { fiveHour: { usedPct: 42, resetsAt: RESET_SEC }, source: 'PC' });
  const saved = structuredClone(s.usage);
  assert.deepEqual(core.normalize(JSON.parse(JSON.stringify(s))).usage, saved);

  const garbage = [
    'yes',
    42,
    [],
    {},
    { fiveHour: null, sevenDay: null, reportedAt: iso(T0) },
    { fiveHour: { usedPct: 'x' }, reportedAt: iso(T0) },
    { fiveHour: { usedPct: 5 } },
    { fiveHour: { usedPct: 5 }, reportedAt: 'never' },
  ];
  for (const g of garbage) {
    const st = { ...fresh(), usage: g };
    assert.equal(core.normalize(st).usage, null, JSON.stringify(g));
  }

  // Repairs: clamps, fills receivedAt, coerces source, drops a broken window.
  const repaired = core.normalize({
    ...fresh(),
    usage: { fiveHour: { usedPct: 140, resetsAt: 'bad' }, sevenDay: 'nope', reportedAt: iso(T0), source: 7, junk: 1 },
  }).usage;
  assert.deepEqual(repaired, {
    fiveHour: { usedPct: 100, resetsAt: null },
    sevenDay: null,
    reportedAt: iso(T0),
    receivedAt: iso(T0),
    source: '7',
  });
});

test('importState keeps the newer usage by reportedAt', () => {
  const mk = (pct, ms, source) => {
    const s = fresh();
    at = ms;
    core.reportUsage(s, { fiveHour: { usedPct: pct }, source });
    return s;
  };
  // Theirs newer -> taken.
  let ours = mk(10, T0, 'ours');
  let theirs = mk(20, T0 + 60_000, 'theirs');
  importState(ours, JSON.parse(JSON.stringify(theirs)));
  assert.equal(ours.usage.source, 'theirs');
  assert.equal(ours.usage.fiveHour.usedPct, 20);
  // Theirs older -> ours kept.
  ours = mk(10, T0 + 60_000, 'ours');
  theirs = mk(20, T0, 'theirs');
  importState(ours, theirs);
  assert.equal(ours.usage.source, 'ours');
  // We have none -> theirs taken; they have none -> ours kept.
  ours = fresh();
  importState(ours, mk(30, T0, 'theirs'));
  assert.equal(ours.usage.fiveHour.usedPct, 30);
  const kept = structuredClone(ours.usage);
  importState(ours, fresh());
  assert.deepEqual(ours.usage, kept);
  // Their usage is invalid or missing (older board) -> ours kept.
  const older = fresh();
  delete older.usage;
  importState(ours, older);
  assert.deepEqual(ours.usage, kept);
  importState(ours, { ...fresh(), usage: { fiveHour: { usedPct: 'x' }, reportedAt: iso(T0 + 999_999) } });
  assert.deepEqual(ours.usage, kept);
  // Taken usage is a copy, not shared with the incoming object.
  const other = mk(40, T0 + 120_000, 'copy');
  ours = fresh();
  importState(ours, other);
  other.usage.fiveHour.usedPct = 99;
  assert.equal(ours.usage.fiveHour.usedPct, 40);
});

test('importState via applyOp also merges usage and still merges tasks', () => {
  const ours = fresh();
  const theirs = fresh();
  core.addTask(theirs, { title: 'From hub' });
  at += 1000;
  core.reportUsage(theirs, { sevenDay: { usedPct: 61 }, source: 'hub' });
  const r = applyOp(ours, { type: 'importState', args: { state: JSON.parse(JSON.stringify(theirs)) } });
  assert.equal(r.added, 1);
  assert.equal(ours.usage.sevenDay.usedPct, 61);
});
