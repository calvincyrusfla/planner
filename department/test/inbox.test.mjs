import test from 'node:test';
import assert from 'node:assert/strict';
import { inboxItems, inboxCount, answerOperation } from '../lib/inbox.mjs';

const NOW = Date.parse('2026-11-10T12:00:00Z');
const state = () => ({
  foundryBatches: [
    {
      id: 'S-001', title: 'Open', status: 'running', updatedAt: '2026-11-10T10:00:00Z',
      subtasks: [{ key: 'a', title: 'A' }, { key: 'b', title: 'B' }],
      results: {
        a: { state: 'needs_help', updatedAt: '2026-11-10T11:00:00Z', help: { question: 'Which?', context: 'ctx', options: ['x', 'y'] } },
        b: { state: 'complete', updatedAt: '2026-11-10T09:00:00Z', pr: { url: 'https://github.com/o/r/pull/1', state: 'open' } },
      },
    },
    {
      id: 'S-002', title: 'Done', status: 'failed', updatedAt: '2026-11-09T10:00:00Z',
      subtasks: [{ key: 'a', title: 'A' }, { key: 'b', title: 'B' }],
      results: {
        a: { state: 'quarantined', updatedAt: '2026-11-09T10:00:00Z', error: 'boom' },
        b: { state: 'failed', updatedAt: '2026-10-01T10:00:00Z', error: 'old' },
      },
    },
    { id: 'S-003', status: 'cancelled', subtasks: [{ key: 'a' }], results: { a: { state: 'needs_help', help: { question: 'q' } } } },
  ],
});

test('inboxItems picks help, recent failures and open batches, newest first', () => {
  const items = inboxItems(state(), NOW);
  assert.deepEqual(items.map((i) => `${i.kind}:${i.id}`), ['help:S-001/a', 'batch:S-001', 'failed:S-002/a']);
  assert.equal(items[0].question, 'Which?');
  assert.deepEqual(items[0].options, ['x', 'y']);
  assert.equal(items[1].prCount, 1);
  assert.equal(items[2].error, 'boom');
  assert.equal(inboxCount(state(), NOW), 3);
});

test('inboxItems tolerates a missing or junk state', () => {
  assert.deepEqual(inboxItems({}, NOW), []);
  assert.deepEqual(inboxItems(null, NOW), []);
  assert.deepEqual(inboxItems({ foundryBatches: [null, 5, {}] }, NOW), []);
});

test('answerOperation builds the answerHelp operation', () => {
  const [help] = inboxItems(state(), NOW);
  assert.deepEqual(answerOperation(help, '  use x  '), { type: 'answerHelp', args: { id: 'S-001', key: 'a', answer: 'use x', by: 'user' } });
  assert.equal(answerOperation(help, '   '), null);
  assert.equal(answerOperation(null, 'x'), null);
});
