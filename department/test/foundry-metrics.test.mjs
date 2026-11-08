import test from 'node:test';
import assert from 'node:assert/strict';
import { createInitialState, DeptError } from '../lib/core.mjs';
import { applyOp, importState } from '../lib/ops.mjs';
import { metricLines, metricRows } from '../lib/foundry-metrics.mjs';

const op = (overrides = {}) => ({
  type: 'reportFoundryMetrics',
  args: {
    service: 'foundry',
    host: 'pc',
    reportedAt: '2026-01-01T10:00:00.000Z',
    metrics: { tasks: { complete: 3 }, ok: true },
    ...overrides,
  },
});

test('keeps the latest report per service', () => {
  const state = createInitialState();
  assert.equal(applyOp(state, op()).ok, true);
  applyOp(state, op({ service: 'other' }));
  applyOp(state, op({ reportedAt: '2026-01-01T11:00:00.000Z', metrics: { tasks: { complete: 4 } } }));
  assert.equal(state.foundryMetrics.length, 2);
  assert.deepEqual(state.foundryMetrics.find((r) => r.service === 'foundry').metrics, { tasks: { complete: 4 } });
});

test('an older report is refused without changing anything', () => {
  const state = createInitialState();
  applyOp(state, op());
  const r = applyOp(state, op({ reportedAt: '2026-01-01T09:00:00.000Z', metrics: { old: 1 } }));
  assert.equal(r.ok, false);
  assert.equal(r.stored, false);
  assert.deepEqual(state.foundryMetrics[0].metrics, { tasks: { complete: 3 }, ok: true });
});

test('bad data is refused and nothing is stored', () => {
  const future = new Date(Date.now() + 48 * 60 * 60 * 1000).toISOString();
  const cases = [
    { service: '' },
    { service: 5 },
    { service: 'a\nb' },
    { host: 5 },
    { metrics: null },
    { metrics: {} },
    { metrics: [1] },
    { metrics: { a: Infinity } },
    { metrics: { a: () => 1 } },
    { metrics: { a: 'x'.repeat(501) } },
    { metrics: { 'bad name': 1 } },
    { metrics: JSON.parse('{"__proto__":1}') },
    { metrics: { a: { b: { c: { d: { e: 1 } } } } } },
    { metrics: { a: new Array(501).fill(1) } },
    { reportedAt: 'yesterday' },
    { reportedAt: 12345 },
    { reportedAt: future },
  ];
  const state = createInitialState();
  for (const c of cases) {
    assert.throws(() => applyOp(state, op(c)), DeptError, JSON.stringify(c));
  }
  assert.equal((state.foundryMetrics || []).length, 0);
});

test('importState keeps the newer report and drops invalid ones', () => {
  const state = createInitialState();
  applyOp(state, op());
  const other = createInitialState();
  other.foundryMetrics = [
    { service: 'foundry', host: 'pc', reportedAt: '2026-01-01T12:00:00.000Z', metrics: { newer: 1 } },
    { service: 'broken', reportedAt: '2026-01-01T12:00:00.000Z', metrics: 'nope' },
  ];
  importState(state, other);
  assert.equal(state.foundryMetrics.length, 1);
  assert.deepEqual(state.foundryMetrics[0].metrics, { newer: 1 });
});

test('metricLines prints totals and a row per task from a task list', () => {
  const lines = metricLines({
    totals: { cost: 1.5 },
    complete: 2,
    tasks: [
      { id: 'F-001', state: 'complete', usage: { input: 10 } },
      { task: 'F-002', state: 'failed' },
      { state: 'queued' },
    ],
  });
  assert.deepEqual(lines, [
    '  Totals:',
    '    cost: 1.5',
    '    complete: 2',
    '  Tasks:',
    '    F-001: state=complete · usage.input=10',
    '    F-002: state=failed',
    '    #3: state=queued',
  ]);
});

test('metricLines reads tasks keyed by id', () => {
  assert.deepEqual(metricLines({ runs: 1, per_task: { 'F-001': { cost: 2 } } }), [
    '  Totals:',
    '    runs: 1',
    '  Tasks:',
    '    F-001: cost=2',
  ]);
});

test('metricLines treats plain task counts as totals', () => {
  assert.deepEqual(metricLines({ tasks: { complete: 3 }, ok: true }), [
    '  Totals:',
    '    tasks.complete: 3',
    '    ok: true',
  ]);
});

test('metricRows flattens nested metrics', () => {
  assert.deepEqual(metricRows({ tasks: { complete: 3 }, ok: true, list: [1, 2] }), [
    ['tasks.complete', '3'],
    ['ok', 'true'],
    ['list', '[1,2]'],
  ]);
});

import { test as usageTest } from 'node:test';
import usageAssert from 'node:assert/strict';
import { createInitialState as usageInitialState } from '../lib/core.mjs';
import * as usageBatches from '../lib/foundry-batches.mjs';

function usageBoard() {
  const state = usageInitialState();
  const batch = usageBatches.dispatchBatch(state, { title: 'Usage', repo: 'owner/name', subtasks: [{ key: 'a', title: 'A' }] });
  usageBatches.claimBatch(state, { id: batch.id, host: 'pc', service: 'foundry' });
  return { state, id: batch.id, send: { id: batch.id, service: 'foundry', host: 'pc' } };
}

usageTest('batch subtask usage round-trips through updates and import', () => {
  const { state, id, send } = usageBoard();
  const usage = { input_tokens: 120, output_tokens: 30, cost_usd: 0.01, models: { opus: { calls: 2 } } };
  usageBatches.updateBatch(state, { ...send, results: { a: { state: 'building', usage } } });
  usageBatches.updateBatch(state, { ...send, results: { a: { state: 'complete' } } });
  usageAssert.deepEqual(usageBatches.getBatch(state, id).results.a.usage, usage);

  const other = usageInitialState();
  const result = usageBatches.importBatches(other, JSON.parse(JSON.stringify(state.foundryBatches)));
  usageAssert.deepEqual(result.rejected, []);
  usageAssert.deepEqual(usageBatches.getBatch(other, id).results.a.usage, usage);
});

usageTest('batch subtask usage must be a valid object', () => {
  const { state, send } = usageBoard();
  usageAssert.throws(() => usageBatches.updateBatch(state, { ...send, results: { a: { usage: 5 } } }), /must be an object/);
  usageAssert.throws(() => usageBatches.updateBatch(state, { ...send, results: { a: { usage: { 'bad key': 1 } } } }), /bad metric name/);
});

usageTest('importing a batch with bad usage keeps the batch and drops the usage', () => {
  const { state, id, send } = usageBoard();
  usageBatches.updateBatch(state, { ...send, results: { a: { state: 'complete', usage: { input_tokens: 1 } } } });
  const copy = JSON.parse(JSON.stringify(state.foundryBatches));
  copy[0].results.a.usage = { 'bad key': 1 };

  const other = usageInitialState();
  const result = usageBatches.importBatches(other, copy);
  usageAssert.deepEqual(result.rejected, []);
  const imported = usageBatches.getBatch(other, id).results.a;
  usageAssert.equal(imported.state, 'complete');
  usageAssert.equal('usage' in imported, false);
});

import cliHttp from 'node:http';
import { execFile as cliExecFile } from 'node:child_process';
import { promisify as cliPromisify } from 'node:util';
import { fileURLToPath as cliFilePath } from 'node:url';
import { reportFoundryMetrics as storeMetricsReport } from '../lib/foundry-metrics.mjs';

test('reports are kept per service, the latest wins and bad data changes nothing', () => {
  const state = createInitialState();
  const metrics = { totals: { complete: 1 } };
  assert.equal(storeMetricsReport(state, { service: 'foundry', reportedAt: '2020-01-01T00:00:00.000Z', metrics }).ok, true);
  assert.equal(storeMetricsReport(state, { service: 'other', reportedAt: '2020-01-01T00:00:00.000Z', metrics }).ok, true);
  const newer = { totals: { complete: 2 } };
  assert.equal(storeMetricsReport(state, { service: 'foundry', reportedAt: '2020-01-02T00:00:00.000Z', metrics: newer }).ok, true);
  assert.deepEqual(state.foundryMetrics.map((r) => r.service), ['foundry', 'other']);
  assert.deepEqual(state.foundryMetrics[0].metrics, newer);

  const before = structuredClone(state.foundryMetrics);
  const stale = storeMetricsReport(state, { service: 'foundry', reportedAt: '2020-01-01T12:00:00.000Z', metrics });
  assert.equal(stale.ok, false);
  assert.equal(stale.stored, false);
  const badReports = [
    { metrics },
    { service: 5, metrics },
    { service: 'foundry', metrics: 'many' },
    { service: 'foundry', metrics: {} },
    { service: 'foundry', metrics: { complete: Infinity } },
    { service: 'foundry', reportedAt: 'yesterday', metrics },
  ];
  for (const report of badReports) assert.throws(() => storeMetricsReport(state, report), DeptError);
  assert.deepEqual(state.foundryMetrics, before);
});

test('the CLI prints stored metrics and returns batch subtask usage', async () => {
  const { state, id, send } = usageBoard();
  const metrics = { totals: { complete: 3 }, tasks: { 'F-1': { tokens: 2 } } };
  storeMetricsReport(state, { service: 'foundry', host: 'pc', reportedAt: '2020-01-01T00:00:00.000Z', metrics });
  usageBatches.updateBatch(state, { ...send, results: { a: { state: 'complete', usage: { tokens: 5 } } } });

  const server = cliHttp.createServer((req, res) => {
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ ok: true, result: state }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, DEPT_URL: `http://127.0.0.1:${server.address().port}` };
  for (const key of ['HTTPS_PROXY', 'https_proxy', 'NODE_USE_ENV_PROXY']) delete env[key];
  const cli = (...args) =>
    cliPromisify(cliExecFile)(process.execPath, [cliFilePath(new URL('../dept.mjs', import.meta.url)), ...args], { env });
  try {
    const { stdout } = await cli('foundry', 'metrics');
    assert.ok(stdout.includes('foundry on pc'), stdout);
    assert.match(stdout, /Totals:/);
    assert.match(stdout, /F-1/);
    for (const line of metricLines(metrics)) assert.ok(stdout.includes(line), `missing ${line} in ${stdout}`);

    const asJson = await cli('foundry', 'metrics', '--json');
    assert.deepEqual(JSON.parse(asJson.stdout), state.foundryMetrics);

    const poll = await cli('foundry', 'poll', id, '--json');
    assert.deepEqual(JSON.parse(poll.stdout).results.a.usage, { tokens: 5 });
  } finally {
    server.close();
  }
});
