// Order check: advice on a Foundry batch before it is dispatched (tests a piece
// may break, files two pieces share, pieces that are too wide). Pure: the caller
// passes the batch and the repository, nothing here touches the disk.
//
// finding = { key, rule, level: 'warn'|'note', message, paths?: [], names?: [] }

const MAX_RELATED_TESTS = 8;
const MAX_TESTS_PER_MATCH = 5; // a module or name used by more tests than this says nothing about one piece
const MAX_NON_TEST_FILES = 3;
const MIN_NAME_LENGTH = 4;

const TEST_DIRS = new Set(['test', 'tests', '__tests__']);
const TEST_FILE_NAMES = [
  /\.test\.(?:js|mjs|cjs|ts)$/,
  /.\.spec\../,
  /^test_.*\.py$/,
  /_test\.py$/,
];
const IDENTIFIER = /^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/;
const JS_FILE = /\.(?:[cm]?[jt]s|[jt]sx)$/;
const PY_IMPORT = /^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+)?import[ \t]+(\([^)]*\)|[^\n#]*)/gm;
const JS_IMPORT = /(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)(['"`])([^'"`\n]+)\1/g;

export function isTestFile(path) {
  const dirs = String(path).split('/');
  const name = dirs.pop();
  return dirs.some((dir) => TEST_DIRS.has(dir)) || TEST_FILE_NAMES.some((re) => re.test(name));
}

function isCodeLikeName(name) {
  if (name.length < MIN_NAME_LENGTH || !IDENTIFIER.test(name)) return false;
  if (/[_.]/.test(name)) return true;
  if (/[a-z]/.test(name) && /[A-Z]/.test(name.slice(1))) return true; // camelCase, PascalCase
  return /^[A-Z0-9]+$/.test(name) && /[0-9]/.test(name);
}

export function quotedNames(text) {
  const names = new Set();
  for (const match of String(text ?? '').matchAll(/(?<!`)`([^`\n]+)`(?!`)/g)) {
    const name = match[1].replace(/\(\)$/, '');
    if (isCodeLikeName(name)) names.add(name);
  }
  return [...names];
}

function importsPython(text, path) {
  const parts = path.replace(/\.py$/, '').split('/');
  if (parts.at(-1) === '__init__') parts.pop();
  const module = parts.join('.');
  const leaf = parts.pop();
  const parent = parts.join('.');
  for (const [, from, clause] of text.matchAll(PY_IMPORT)) {
    const names = clause
      .replace(/#.*/g, '')
      .replace(/[()]/g, '')
      .split(',')
      .map((item) => item.trim().split(/\s+/)[0])
      .filter(Boolean);
    if (from === undefined ? names.includes(module) : from === module || (from === parent && names.includes(leaf))) return true;
  }
  return false;
}

function importsJs(text, path) {
  const basename = path.split('/').pop();
  for (const [, , specifier] of text.matchAll(JS_IMPORT)) {
    if (specifier.split('/').pop() === basename) return true;
  }
  return false;
}

function importsFile(text, path) {
  if (path.endsWith('.py')) return importsPython(text, path);
  return JS_FILE.test(path) && importsJs(text, path);
}

const asList = (value) => (Array.isArray(value) ? value : []);

function toPiece(subtask, index) {
  const files = [...new Set(asList(subtask?.files))];
  return {
    key: String(subtask?.key ?? '').trim() || String(index + 1),
    files,
    readFiles: [...new Set(asList(subtask?.readFiles ?? subtask?.read_files))],
    sources: files.filter((path) => !isTestFile(path)),
    names: quotedNames([subtask?.description, ...asList(subtask?.acceptance ?? subtask?.acceptance_criteria)].join('\n')),
  };
}

function relatedTest(piece, repo, read) {
  const listed = new Set([...piece.files, ...piece.readFiles]);
  const tests = repo.files.filter((path) => isTestFile(path));
  const found = new Set();
  const widelyImported = [];
  for (const file of piece.sources) {
    const importers = tests.filter((path) => importsFile(read(path), file));
    if (importers.length > MAX_TESTS_PER_MATCH) {
      widelyImported.push({
        key: piece.key,
        rule: 'widely-imported',
        level: 'note',
        message: `${importers.length} tests import ${file}; run them`,
        paths: [file],
      });
    } else importers.forEach((path) => found.add(path));
  }
  for (const name of piece.names) {
    const users = tests.filter((path) => read(path).includes(name));
    if (users.length <= MAX_TESTS_PER_MATCH) users.forEach((path) => found.add(path));
  }
  const related = [...found].filter((path) => !listed.has(path)).sort();
  if (!related.length) return widelyImported;
  const paths = related.slice(0, MAX_RELATED_TESTS);
  const more = related.length - paths.length;
  return [
    {
      key: piece.key,
      rule: 'related-test',
      level: 'warn',
      message: `Tests outside this piece look related: ${paths.join(', ')}${more ? ` and ${more} more` : ''}. Add the ones it affects to files or readFiles.`,
      paths,
    },
    ...widelyImported,
  ];
}

function tooManyFiles(piece) {
  if (piece.sources.length <= MAX_NON_TEST_FILES) return null;
  return {
    key: piece.key,
    rule: 'too-many-files',
    level: 'warn',
    message: `This piece changes ${piece.sources.length} non-test files (more than ${MAX_NON_TEST_FILES}). Consider splitting it.`,
    paths: piece.sources,
  };
}

function notFound(piece, repo, read) {
  const names = piece.names.filter((name) => !repo.files.some((path) => read(path).includes(name)));
  if (!names.length) return null;
  return {
    key: piece.key,
    rule: 'not-found',
    level: 'note',
    message: `Not found anywhere in the repository: ${names.join(', ')}. Fine when this piece creates them; otherwise check the spelling.`,
    names,
  };
}

function sharedFiles(pieces) {
  const findings = [];
  pieces.forEach((later, index) => {
    for (const earlier of pieces.slice(0, index)) {
      const paths = later.files.filter((path) => earlier.files.includes(path));
      if (!paths.length) continue;
      findings.push({
        key: later.key,
        rule: 'shared-file',
        level: 'warn',
        message: `Subtasks ${earlier.key} and ${later.key} both change ${paths.join(', ')}; their pull requests may conflict.`,
        paths,
      });
    }
  });
  return findings;
}

export function checkOrder(batch, repo) {
  const contents = new Map();
  const read = (path) => {
    if (!contents.has(path)) contents.set(path, repo.read(path));
    return contents.get(path);
  };
  const pieces = asList(batch?.subtasks).map(toPiece);
  const perPiece = pieces.flatMap((piece) => [...relatedTest(piece, repo, read), tooManyFiles(piece), notFound(piece, repo, read)]);
  return [...perPiece.filter(Boolean), ...sharedFiles(pieces)].sort(
    (a, b) => a.key.localeCompare(b.key, 'en', { numeric: true }) || a.rule.localeCompare(b.rule, 'en'),
  );
}
