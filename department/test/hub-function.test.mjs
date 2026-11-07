import test from 'node:test';
import assert from 'node:assert/strict';
import { makeHandler } from '../../netlify/functions/department.mjs';

// In-memory stand-in for a Netlify Blobs store with etag-based conditional writes.
function fakeStore() {
  const docs = new Map();
  let n = 0;
  const s = {
    writes: 0,
    beforeWrite: null, // hook to simulate another writer racing us
    async getWithMetadata(key, { type } = {}) {
      const d = docs.get(key);
      if (!d) return null;
      return { data: type === 'json' ? JSON.parse(d.body) : d.body, etag: d.etag };
    },
    async setJSON(key, data, opts = {}) {
      if (s.beforeWrite) await s.beforeWrite(key);
      const cur = docs.get(key);
      if (opts.onlyIfNew && cur) return { modified: false };
      if (opts.onlyIfMatch && (!cur || cur.etag !== opts.onlyIfMatch)) return { modified: false };
      docs.set(key, { body: JSON.stringify(data), etag: `"e${++n}"` });
      s.writes++;
      return { modified: true, etag: `"e${n}"` };
    },
    // direct write that bypasses conditions (the "other" writer)
    force(key, data) { docs.set(key, { body: JSON.stringify(data), etag: `"e${++n}"` }); },
    raw(key) { const d = docs.get(key); return d && JSON.parse(d.body); },
  };
  return s;
}

const URL_ = 'https://site.example/api/department';
const get = (key) => new Request(URL_, { headers: key === undefined ? {} : { 'x-edit-key': key } });
const post = (body, key = 'sesame') =>
  new Request(URL_, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-edit-key': key },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

async function withKey(value, fn) {
  const had = Object.prototype.hasOwnProperty.call(process.env, 'EDIT_KEY');
  const old = process.env.EDIT_KEY;
  if (value === undefined) delete process.env.EDIT_KEY; else process.env.EDIT_KEY = value;
  try { return await fn(); } finally {
    if (had) process.env.EDIT_KEY = old; else delete process.env.EDIT_KEY;
  }
}

const setup = () => { const store = fakeStore(); return { store, handler: makeHandler(() => store) }; };

test('GET without EDIT_KEY configured is 403 editing_disabled', () => withKey(undefined, async () => {
  const { handler } = setup();
  const r = await handler(get('anything'));
  assert.equal(r.status, 403);
  assert.deepEqual(await r.json(), { ok: false, error: 'editing_disabled' });
}));

test('GET with wrong or missing key is 401 bad_key', () => withKey('sesame', async () => {
  const { handler } = setup();
  for (const k of ['nope', undefined, '']) {
    const r = await handler(get(k));
    assert.equal(r.status, 401);
    assert.deepEqual(await r.json(), { ok: false, error: 'bad_key' });
  }
}));

test('POST is also protected by the key', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  const r = await handler(post({ type: 'addTask', args: { title: 'x' } }, 'wrong'));
  assert.equal(r.status, 401);
  assert.equal(store.writes, 0);
}));

test('GET with the right key returns the initial state at rev 0', () => withKey('sesame', async () => {
  const { handler } = setup();
  const r = await handler(get('sesame'));
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('cache-control'), 'no-store');
  const body = await r.json();
  assert.equal(body.ok, true);
  assert.equal(body.rev, 0);
  assert.equal(body.state.teams.length, 3);
  assert.deepEqual(body.state.tasks, []);
}));

test('POST addTask then GET shows it at rev 1', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  const r = await handler(post({ type: 'addTask', args: { title: 'Shared board', size: 'small' } }));
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.ok, true);
  assert.equal(body.rev, 1);
  assert.ok(body.result);
  assert.equal(body.state.tasks.length, 1);
  assert.equal(store.raw('state').rev, 1);

  const g = await (await handler(get('sesame'))).json();
  assert.equal(g.rev, 1);
  assert.equal(g.state.tasks.length, 1);
  assert.equal(g.state.tasks[0].title, 'Shared board');
  assert.equal(g.state.tasks[0].id, 'T-001');

  const r2 = await (await handler(post({ type: 'addTask', args: { title: 'Second' } }))).json();
  assert.equal(r2.rev, 2);
  assert.equal(r2.state.tasks.length, 2);
}));

test('DeptError maps to its status with the message', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  const nf = await handler(post({ type: 'updateTask', args: { id: 'T-999', patch: { title: 'x' } } }));
  assert.equal(nf.status, 404);
  const nb = await nf.json();
  assert.equal(nb.ok, false);
  assert.match(nb.error, /T-999/);

  const unk = await handler(post({ type: 'frobnicate', args: {} }));
  assert.equal(unk.status, 400);
  const ub = await unk.json();
  assert.equal(ub.ok, false);
  assert.match(ub.error, /Unknown operation/);
  assert.equal(store.writes, 0);
}));

test('bad JSON and missing type are 400', () => withKey('sesame', async () => {
  const { handler } = setup();
  const r = await handler(post('{not json'));
  assert.equal(r.status, 400);
  assert.deepEqual(await r.json(), { ok: false, error: 'bad_json' });
  const r2 = await handler(post({ args: {} }));
  assert.equal(r2.status, 400);
  assert.equal((await r2.json()).ok, false);
}));

test('a conflicting concurrent write is retried on fresh data', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  await handler(post({ type: 'addTask', args: { title: 'First' } }));
  let raced = false;
  store.beforeWrite = async (key) => {
    if (raced) return;
    raced = true;
    // another session sneaks in a write between our read and our write
    const other = store.raw(key);
    other.state.tasks[0].title = 'Renamed elsewhere';
    other.rev += 1;
    store.force(key, other);
  };
  const r = await handler(post({ type: 'addTask', args: { title: 'Second' } }));
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.rev, 3);
  assert.deepEqual(body.state.tasks.map((t) => t.title), ['Renamed elsewhere', 'Second']);
  assert.equal(store.raw('state').rev, 3);
}));

test('a conflict on the very first write (onlyIfNew) is retried too', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  let raced = false;
  store.beforeWrite = async (key) => {
    if (raced) return;
    raced = true;
    const { createInitialState, addTask } = await import('../lib/core.mjs');
    const st = createInitialState();
    addTask(st, { title: 'Other first' });
    store.force(key, { state: st, rev: 1 });
  };
  const body = await (await handler(post({ type: 'addTask', args: { title: 'Mine' } }))).json();
  assert.equal(body.rev, 2);
  assert.deepEqual(body.state.tasks.map((t) => t.title), ['Other first', 'Mine']);
}));

test('persistent conflicts give 409 busy', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  await handler(post({ type: 'addTask', args: { title: 'First' } }));
  let races = 0;
  store.beforeWrite = async (key) => { races++; store.force(key, store.raw(key)); };
  const r = await handler(post({ type: 'addTask', args: { title: 'Never lands' } }));
  assert.equal(r.status, 409);
  assert.deepEqual(await r.json(), { ok: false, error: 'busy' });
  assert.equal(races, 6);
  assert.equal(store.raw('state').state.tasks.length, 1);
}));

test('other methods are 405', () => withKey('sesame', async () => {
  const { handler } = setup();
  for (const method of ['PUT', 'DELETE', 'PATCH']) {
    const r = await handler(new Request(URL_, { method, headers: { 'x-edit-key': 'sesame' } }));
    assert.equal(r.status, 405);
    assert.equal((await r.json()).ok, false);
  }
}));

test('an ignored (older) usage report is not written and keeps the rev', () => withKey('sesame', async () => {
  const { handler, store } = setup();
  const newer = new Date(Date.now() - 1000).toISOString();
  const older = new Date(Date.now() - 60_000).toISOString();
  const first = await (await handler(post({ type: 'reportUsage', args: { fiveHour: { usedPct: 50 }, reportedAt: newer, source: 'PC' } }))).json();
  assert.equal(first.ok, true);
  assert.equal(first.result.stored, true);
  assert.equal(first.rev, 1);
  assert.equal(store.writes, 1);
  const etagBefore = (await store.getWithMetadata('state')).etag;

  const r = await handler(post({ type: 'reportUsage', args: { fiveHour: { usedPct: 10 }, reportedAt: older, source: 'old' } }));
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.ok, true);
  assert.equal(body.result.stored, false);
  assert.equal(body.rev, 1);
  assert.equal(body.state.usage.fiveHour.usedPct, 50);
  assert.equal(store.writes, 1, 'no write for an ignored report');
  assert.equal((await store.getWithMetadata('state')).etag, etagBefore);
  assert.equal(store.raw('state').rev, 1);
  assert.equal(store.raw('state').state.usage.source, 'PC');

  // A newer report is written as usual.
  const next = await (await handler(post({ type: 'reportUsage', args: { fiveHour: { usedPct: 55 }, source: 'PC' } }))).json();
  assert.equal(next.result.stored, true);
  assert.equal(next.rev, 2);
  assert.equal(store.writes, 2);
}));
