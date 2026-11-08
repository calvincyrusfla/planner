// The needs_help / answered result states and the answerHelp operation.
import test from 'node:test';
import assert from 'node:assert/strict';
import { DeptError } from '../lib/core.mjs';
import {
  LIMITS,
  RESULT_STATES,
  answerHelp,
  batchProgress,
  cancelBatch,
  claimBatch,
  dispatchBatch,
  getBatch,
  importBatches,
  updateBatch,
} from '../lib/foundry-batches.mjs';

const SERVICE = 'foundry';
const HELP = { question: 'Which base?', context: 'Two branches exist', options: ['main', 'dev'] };

function claimed() {
  const state = {};
  const { id } = dispatchBatch(state, {
    title: 'Batch',
    repo: 'owner/name',
    subtasks: [{ key: 'a', title: 'A' }, { key: 'b', title: 'B' }],
  });
  claimBatch(state, { id, host: 'pc', service: SERVICE });
  return { state, id };
}

const report = (state, id, results) => updateBatch(state, { id, service: SERVICE, results });
const resultOf = (state, id, key = 'a') => getBatch(state, id).results[key];

function waiting() {
  const s = claimed();
  report(s.state, s.id, { a: { state: 'needs_help', help: HELP } });
  return s;
}

function refuses(fn, pattern) {
  assert.throws(fn, (err) => err instanceof DeptError && pattern.test(err.message));
}

test('needs_help and answered are valid result states', () => {
  assert.ok(RESULT_STATES.includes('needs_help'));
  assert.ok(RESULT_STATES.includes('answered'));
});

test('needs_help without a help object is refused and stores nothing', () => {
  const { state, id } = claimed();
  refuses(() => report(state, id, { a: { state: 'needs_help' } }), /needs_help requires a help object/);
  refuses(() => report(state, id, { a: { state: 'needs_help', help: null } }), /needs_help requires a help object/);
  assert.equal(resultOf(state, id), undefined);
  assert.equal(getBatch(state, id).status, 'claimed');
});

test('a malformed help object is refused', () => {
  const { state, id } = claimed();
  const ask = (help) => () => report(state, id, { a: { state: 'needs_help', help } });
  const long = 'x'.repeat(LIMITS.help + 1);
  refuses(ask('why?'), /help must be an object/);
  refuses(ask(['why?']), /help must be an object/);
  refuses(ask({ context: 'no question' }), /help question is required/);
  refuses(ask({ question: '   ' }), /help question is required/);
  refuses(ask({ question: long }), /help question is too long/);
  refuses(ask({ question: 'q', context: long }), /help context is too long/);
  refuses(ask({ question: 'q', options: 7 }), /help options must be a list/);
  refuses(ask({ question: 'q', options: ['ok', ''] }), /help option 2 is required/);
  refuses(ask({ question: 'q', options: Array(LIMITS.helpOptions + 1).fill('x') }), /at most 20 entries/);
  assert.equal(resultOf(state, id), undefined);
});

test('needs_help stores the cleaned question and keeps the batch open', () => {
  const { state, id } = claimed();
  const { batch } = report(state, id, { a: { state: 'needs_help', help: { ...HELP, question: '  Which base?  ' } } });
  assert.equal(batch.status, 'running');
  assert.equal(resultOf(state, id).state, 'needs_help');
  assert.deepEqual(resultOf(state, id).help, HELP);
  assert.equal(batchProgress(batch).terminal, 0);
});

test('a worker cannot fill in the answer fields', () => {
  const { state, id } = claimed();
  const help = { ...HELP, answer: 'forged', answeredBy: 'worker', answeredAt: '2026-01-01T00:00:00.000Z' };
  report(state, id, { a: { state: 'needs_help', help } });
  assert.deepEqual(resultOf(state, id).help, HELP);
});

test('updateBatch cannot set answered, and a refused update changes nothing', () => {
  const { state, id } = waiting();
  refuses(
    () => report(state, id, { a: { state: 'answered' }, b: { state: 'complete' } }),
    /only answerHelp can set a result to answered/,
  );
  assert.equal(resultOf(state, id).state, 'needs_help');
  assert.equal(resultOf(state, id, 'b'), undefined);
});

test('answerHelp records the answer and keeps the question', () => {
  const { state, id } = waiting();
  const r = answerHelp(state, { id, key: 'a', answer: '  use main  ', by: 'cal' });
  assert.equal(r.ok, true);
  assert.equal(r.batch.id, id);
  const result = resultOf(state, id);
  assert.equal(result.state, 'answered');
  assert.equal(result.help.question, HELP.question);
  assert.equal(result.help.context, HELP.context);
  assert.deepEqual(result.help.options, HELP.options);
  assert.equal(result.help.answer, 'use main');
  assert.equal(result.help.answeredBy, 'cal');
  assert.ok(Number.isFinite(Date.parse(result.help.answeredAt)));
  assert.equal(result.updatedAt, result.help.answeredAt);
  const entry = state.log.at(-1);
  assert.equal(entry.kind, 'foundry');
  assert.equal(entry.batchId, id);
  assert.match(entry.message, /subtask a answered by cal/);
});

test('answerHelp credits the user by default and accepts a lower-case batch id', () => {
  const { state, id } = waiting();
  answerHelp(state, { id: id.toLowerCase(), key: ' a ', answer: 'go' });
  assert.equal(resultOf(state, id).state, 'answered');
  assert.equal(resultOf(state, id).help.answeredBy, 'user');
});

test('answerHelp refuses bad requests and leaves the subtask waiting', () => {
  const { state, id } = waiting();
  refuses(() => answerHelp(state, { id: 'S-999', key: 'a', answer: 'go' }), /No Foundry batch/);
  refuses(() => answerHelp(state, { id, key: 'nope', answer: 'go' }), /has no subtask nope/);
  refuses(() => answerHelp(state, { id, key: 'b', answer: 'go' }), /subtask b is queued, not needs_help/);
  refuses(() => answerHelp(state, { id, key: 'a', answer: '   ' }), /answer is required/);
  refuses(() => answerHelp(state, { id, key: 'a' }), /answer is required/);
  refuses(() => answerHelp(state, { id, key: 'a', answer: 'x'.repeat(LIMITS.help + 1) }), /answer is too long/);
  assert.equal(resultOf(state, id).state, 'needs_help');
  assert.deepEqual(resultOf(state, id).help, HELP);
});

test('answerHelp refuses a subtask that was already answered', () => {
  const { state, id } = waiting();
  answerHelp(state, { id, key: 'a', answer: 'first' });
  refuses(() => answerHelp(state, { id, key: 'a', answer: 'second' }), /subtask a is answered, not needs_help/);
  assert.equal(resultOf(state, id).help.answer, 'first');
});

test('answerHelp refuses a closed batch', () => {
  const { state, id } = waiting();
  cancelBatch(state, { id, by: 'cal' });
  refuses(() => answerHelp(state, { id, key: 'a', answer: 'go' }), /is cancelled/);
  assert.equal(resultOf(state, id).state, 'needs_help');
});

test('a new question replaces the old answer and can be answered again', () => {
  const { state, id } = waiting();
  answerHelp(state, { id, key: 'a', answer: 'first' });
  const next = { question: 'And the target?', context: '', options: [] };
  report(state, id, { a: { state: 'needs_help', help: next } });
  assert.deepEqual(resultOf(state, id).help, next);
  answerHelp(state, { id, key: 'a', answer: 'second' });
  assert.equal(resultOf(state, id).help.answer, 'second');
});

test('an answered subtask can finish its batch and keeps the exchange', () => {
  const { state, id } = waiting();
  answerHelp(state, { id, key: 'a', answer: 'go' });
  const { batch } = report(state, id, { a: { state: 'complete' }, b: { state: 'complete' } });
  assert.equal(batch.status, 'done');
  assert.equal(resultOf(state, id).help.answer, 'go');
});

const importedBatch = (result) => ({
  id: 'S-007',
  title: 'Imported',
  repo: 'owner/name',
  status: 'running',
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
  subtasks: [{ key: 'a', title: 'A' }],
  results: { a: result },
});

test('import keeps a recorded answer', () => {
  const state = {};
  const answeredAt = '2026-01-02T00:00:00.000Z';
  const help = { question: 'q', answer: 'go', answeredBy: 'cal', answeredAt };
  const result = importBatches(state, [importedBatch({ state: 'answered', help })]);
  assert.equal(result.added, 1);
  assert.deepEqual(result.rejected, []);
  const stored = resultOf(state, 'S-007');
  assert.equal(stored.state, 'answered');
  assert.equal(stored.help.answer, 'go');
  assert.equal(stored.help.answeredBy, 'cal');
  assert.equal(stored.help.answeredAt, answeredAt);
});

test('import rejects needs_help without a help object', () => {
  const state = {};
  const result = importBatches(state, [importedBatch({ state: 'needs_help' })]);
  assert.equal(result.added, 0);
  assert.equal(result.rejected.length, 1);
  assert.match(result.rejected[0].reason, /needs_help requires a help object/);
});
