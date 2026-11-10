import test from 'node:test';
import assert from 'node:assert/strict';
import { checkOrder, isTestFile, quotedNames } from '../lib/order-check.mjs';

const fakeRepo = (contents) => ({ files: Object.keys(contents), read: (path) => contents[path] });
const order = (...subtasks) => ({ title: 'Batch', goal: '', repo: 'owner/name', base: null, subtasks });
const byRule = (findings, rule) => findings.filter((f) => f.rule === rule);

test('isTestFile recognises each test pattern', () => {
  for (const path of [
    'test/helper.js',
    'pkg/tests/helper.py',
    'src/__tests__/thing.tsx',
    'src/a.test.js',
    'src/a.test.mjs',
    'src/a.test.cjs',
    'src/a.test.ts',
    'src/a.spec.tsx',
    'pkg/test_a.py',
    'pkg/a_test.py',
  ]) {
    assert.equal(isTestFile(path), true, path);
  }
});

test('isTestFile rejects other files', () => {
  for (const path of ['department/lib/order-check.mjs', 'pkg/contest/a.py', 'src/latest.js', 'pkg/testing.py', 'src/a.test.py', 'test']) {
    assert.equal(isTestFile(path), false, path);
  }
});

test('quotedNames picks code-like names in order, once each', () => {
  assert.deepEqual(
    quotedNames('Use `LIMITS.help`, `order_schema`, `builderClaudeArgs`, `MAX_RETRIES`, `foundry.order_schema`, `_usage_results`, `PascalCase`, `MAX2` and `order_schema` again.'),
    ['LIMITS.help', 'order_schema', 'builderClaudeArgs', 'MAX_RETRIES', 'foundry.order_schema', '_usage_results', 'PascalCase', 'MAX2'],
  );
});

test('quotedNames strips a trailing () first', () => {
  assert.deepEqual(quotedNames('Call `checkOrder()` and `order_schema()`.'), ['checkOrder', 'order_schema']);
});

test('quotedNames ignores commands, paths, calls and plain words', () => {
  assert.deepEqual(
    quotedNames('Run `npm run test`, see `foundry/README.md`, `x`, `run(a_b)`, `plain`, `Title`, `HTTP`, `a.b` and unquoted_name.'),
    [],
  );
});

test('related-test finds JS tests importing a changed module', () => {
  const repo = fakeRepo({
    'department/lib/foundry-batches.mjs': '',
    'department/lib/other.mjs': "import './foundry-batches.mjs';\n",
    'department/test/a.test.mjs': "import { LIMITS } from '../lib/foundry-batches.mjs';\n",
    'department/test/b.test.mjs': 'const m = require("../lib/foundry-batches.mjs");\n',
    'department/test/c.test.mjs': "const m = await import('../lib/foundry-batches.mjs');\n",
    'department/test/d.test.mjs': "import '../lib/not-foundry-batches.mjs';\n",
    'department/test/e.test.mjs': 'no imports here\n',
  });
  const [finding] = byRule(checkOrder(order({ key: 'a', files: ['department/lib/foundry-batches.mjs'] }), repo), 'related-test');
  assert.equal(finding.key, 'a');
  assert.equal(finding.level, 'warn');
  assert.ok(finding.message);
  assert.deepEqual(finding.paths, ['department/test/a.test.mjs', 'department/test/b.test.mjs', 'department/test/c.test.mjs']);
});

test('related-test finds Python tests importing a changed module', () => {
  const repo = fakeRepo({
    'foundry/board_dispatch.py': '',
    'foundry/tests/test_a.py': 'import foundry.board_dispatch\n',
    'foundry/tests/test_b.py': 'from foundry.board_dispatch import _submit\n',
    'foundry/tests/test_c.py': 'from foundry import board_dispatch as bd\n',
    'foundry/tests/test_d.py': 'from foundry import (\n    other,  # note\n    board_dispatch,\n)\n',
    'foundry/tests/test_e.py': 'import foundry.board_dispatcher\nfrom foundry import board_dispatch_extra\n',
  });
  const [finding] = byRule(checkOrder(order({ files: ['foundry/board_dispatch.py'] }), repo), 'related-test');
  assert.deepEqual(finding.paths, [
    'foundry/tests/test_a.py',
    'foundry/tests/test_b.py',
    'foundry/tests/test_c.py',
    'foundry/tests/test_d.py',
  ]);
});

test('related-test finds tests mentioning a name quoted in the description or acceptance', () => {
  const repo = fakeRepo({
    'department/test/a.test.mjs': 'builderClaudeArgs();\n',
    'department/test/b.test.mjs': 'MAX_RETRIES;\n',
    'department/test/c.test.mjs': 'unrelated;\n',
  });
  const subtask = { files: ['department/lib/x.mjs'], description: 'Change `builderClaudeArgs`.', acceptance: ['Keeps `MAX_RETRIES`.'] };
  const [finding] = byRule(checkOrder(order(subtask), repo), 'related-test');
  assert.deepEqual(finding.paths, ['department/test/a.test.mjs', 'department/test/b.test.mjs']);
});

test('related-test skips tests already listed in files or readFiles', () => {
  const importer = "import '../lib/mod.mjs';\n";
  const repo = fakeRepo({ 'lib/mod.mjs': '', 'test/a.test.mjs': importer, 'test/b.test.mjs': importer });
  const subtask = { files: ['lib/mod.mjs', 'test/a.test.mjs'], readFiles: ['test/b.test.mjs'] };
  assert.deepEqual(byRule(checkOrder(order(subtask), repo), 'related-test'), []);
});

test('related-test reports at most 8 paths, sorted', () => {
  const contents = { 'lib/mod.mjs': '', 'lib/other.mjs': '' };
  for (let i = 9; i >= 0; i--) contents[`test/t${i}.test.mjs`] = `import '../lib/${i < 5 ? 'mod' : 'other'}.mjs';\n`;
  const [finding] = byRule(checkOrder(order({ files: ['lib/mod.mjs', 'lib/other.mjs'] }), fakeRepo(contents)), 'related-test');
  assert.deepEqual(finding.paths, [0, 1, 2, 3, 4, 5, 6, 7].map((i) => `test/t${i}.test.mjs`));
  assert.match(finding.message, /2 more/);
});

test('too-many-files triggers at 4 non-test files', () => {
  const files = ['a.mjs', 'b.mjs', 'c.mjs', 'd.mjs'];
  const [finding] = byRule(checkOrder(order({ files }), fakeRepo({})), 'too-many-files');
  assert.equal(finding.key, '1');
  assert.equal(finding.level, 'warn');
  assert.ok(finding.message);
  assert.deepEqual(finding.paths, files);
});

test('too-many-files does not trigger at 3 non-test files plus tests', () => {
  const files = ['a.mjs', 'b.mjs', 'c.mjs', 'test/a.test.mjs', 'test/b.test.mjs'];
  assert.deepEqual(byRule(checkOrder(order({ files }), fakeRepo({})), 'too-many-files'), []);
});

test('not-found lists names that appear in no file', () => {
  const repo = fakeRepo({ 'a.mjs': 'const present_name = 1;\n' });
  const subtask = { files: ['a.mjs'], description: 'Use `present_name`.', acceptance: ['Adds `missing_name`.'] };
  const [finding] = byRule(checkOrder(order(subtask), repo), 'not-found');
  assert.equal(finding.level, 'note');
  assert.deepEqual(finding.names, ['missing_name']);
  assert.match(finding.message, /creates/);
});

test('not-found stays quiet when every name exists', () => {
  const repo = fakeRepo({ 'a.mjs': 'const present_name = 1;\n' });
  assert.deepEqual(byRule(checkOrder(order({ files: ['a.mjs'], description: 'Use `present_name`.' }), repo), 'not-found'), []);
});

test('shared-file names both keys and reports on the later subtask', () => {
  const batch = order({ key: 'alpha', files: ['a.mjs', 'b.mjs'] }, { key: 'beta', files: ['b.mjs', 'c.mjs'] });
  const findings = byRule(checkOrder(batch, fakeRepo({})), 'shared-file');
  assert.equal(findings.length, 1);
  assert.equal(findings[0].key, 'beta');
  assert.equal(findings[0].level, 'warn');
  assert.deepEqual(findings[0].paths, ['b.mjs']);
  assert.match(findings[0].message, /alpha/);
  assert.match(findings[0].message, /beta/);
});

test('keys default to the 1-based index', () => {
  const findings = byRule(checkOrder(order({ files: ['a.mjs'] }, { files: ['a.mjs'] }), fakeRepo({})), 'shared-file');
  assert.equal(findings[0].key, '2');
  assert.match(findings[0].message, /1 and 2/);
});

test('findings are ordered by subtask key then rule', () => {
  const description = 'Add `missing_name`.';
  const batch = order(
    { key: '10', files: ['a.mjs', 'b.mjs', 'c.mjs', 'd.mjs'], description },
    { key: '2', files: ['e.mjs', 'f.mjs', 'g.mjs', 'h.mjs'], description },
  );
  const findings = checkOrder(batch, fakeRepo({ 'README.md': 'nothing\n' }));
  assert.deepEqual(findings.map((f) => `${f.key}:${f.rule}`), [
    '2:not-found',
    '2:too-many-files',
    '10:not-found',
    '10:too-many-files',
  ]);
});

const manyTests = (count, text) => Object.fromEntries(Array.from({ length: count }, (_, i) => [`test/t${i}.test.mjs`, text]));

test('a module imported by 6 tests gives one widely-imported note and no listing', () => {
  const repo = fakeRepo({ 'lib/mod.mjs': '', ...manyTests(6, "import '../lib/mod.mjs';\n") });
  const findings = checkOrder(order({ key: 'a', files: ['lib/mod.mjs'] }), repo);
  assert.deepEqual(byRule(findings, 'related-test'), []);
  assert.deepEqual(byRule(findings, 'widely-imported'), [
    { key: 'a', rule: 'widely-imported', level: 'note', message: '6 tests import lib/mod.mjs; run them', paths: ['lib/mod.mjs'] },
  ]);
});

test('a module imported by 5 tests still lists them', () => {
  const repo = fakeRepo({ 'lib/mod.mjs': '', ...manyTests(5, "import '../lib/mod.mjs';\n") });
  const findings = checkOrder(order({ files: ['lib/mod.mjs'] }), repo);
  assert.deepEqual(byRule(findings, 'widely-imported'), []);
  assert.deepEqual(byRule(findings, 'related-test')[0].paths, [0, 1, 2, 3, 4].map((i) => `test/t${i}.test.mjs`));
});

test('a quoted name found in 6 tests is ignored', () => {
  const repo = fakeRepo(manyTests(6, 'shared_name;\n'));
  const findings = checkOrder(order({ files: ['lib/x.mjs'], description: 'Change `shared_name`.' }), repo);
  assert.deepEqual(byRule(findings, 'related-test'), []);
  assert.deepEqual(byRule(findings, 'not-found'), []);
});

test('a quoted name found in 5 tests still counts', () => {
  const repo = fakeRepo(manyTests(5, 'shared_name;\n'));
  const [finding] = byRule(checkOrder(order({ files: ['lib/x.mjs'], description: 'Change `shared_name`.' }), repo), 'related-test');
  assert.equal(finding.paths.length, 5);
});
