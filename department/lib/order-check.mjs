// Order check: advice on a Foundry batch before it is dispatched (tests a piece
// may break, test fakes and exact lists it may invalidate, files two pieces share,
// pieces that are too wide). Pure: the caller passes the batch and the repository,
// nothing here touches the disk.
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
const PY_TOKENS = /#[^\n]*|"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/g;
const JS_TOKENS = /\/\/[^\n]*|\/\*[\s\S]*?\*\/|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\[\s\S])*`/g;
const ADDS_WORD = /\b(?:add|adds|adding|added|new)\b/i;
const PARAMETER_WORD = /\b(?:parameters?|arguments?|keywords?|kwargs?|options?)\b/i;

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
  const text = [subtask?.description, ...asList(subtask?.acceptance ?? subtask?.acceptance_criteria)].join('\n');
  return {
    key: String(subtask?.key ?? '').trim() || String(index + 1),
    files,
    readFiles: [...new Set(asList(subtask?.readFiles ?? subtask?.read_files))],
    sources: files.filter((path) => !isTestFile(path)),
    description: String(subtask?.description ?? ''),
    text,
    names: quotedNames(text),
  };
}

// Splits a source file into its code, with strings and comments blanked to spaces
// (offsets stay valid), and its string values by offset. Null for other languages.
function scanSource(text, path) {
  const python = path.endsWith('.py');
  if (!python && !JS_FILE.test(path)) return null;
  const strings = new Map();
  const code = text.replace(python ? PY_TOKENS : JS_TOKENS, (token, offset) => {
    if (!/^(?:#|\/[/*])/.test(token)) {
      const quote = /^(?:"""|''')/.test(token) ? 3 : 1;
      strings.set(offset, token.slice(quote, -quote));
    }
    return token.replace(/[^\n]/g, ' ');
  });
  return { python, code, strings };
}

// Reads the bracketed group opening at `open`. Strings inside an object or dict
// are left out: only the elements of the list or tuple count.
function readGroup(source, open) {
  const stack = [];
  const strings = [];
  const commas = [];
  for (let i = open; i < source.code.length; i++) {
    if (source.strings.has(i) && !stack.includes('{')) strings.push(source.strings.get(i));
    const ch = source.code[i];
    if ('([{'.includes(ch)) stack.push(ch);
    else if (')]}'.includes(ch)) {
      stack.pop();
      if (!stack.length) return { strings, commas };
    } else if (ch === ',' && stack.length === 1) commas.push(i);
  }
  return null;
}

function pythonLists(source) {
  const lists = [];
  for (const line of source.code.matchAll(/^[ \t]*assert\b[^\n]*/gm)) {
    for (const equals of line[0].matchAll(/==[ \t]*([[(])/g)) {
      const index = line.index + equals.index;
      const group = readGroup(source, index + equals[0].length - 1);
      // A parenthesis without a comma only groups an expression: it is no tuple.
      if (group && (equals[1] === '[' || group.commas.length)) lists.push({ index, strings: group.strings });
    }
  }
  return lists;
}

function jsLists(source) {
  const lists = [];
  for (const call of source.code.matchAll(/\bdeep(?:Strict)?Equal[ \t]*\(/g)) {
    const open = call.index + call[0].length - 1;
    const args = readGroup(source, open);
    if (!args) continue;
    for (const before of [open, args.commas[0]]) { // actual and expected; a third argument is the message
      if (before === undefined) continue;
      const lead = source.code.slice(before + 1).match(/^\s*\[/);
      const group = lead && readGroup(source, before + lead[0].length);
      if (group) lists.push({ index: call.index, strings: group.strings });
    }
  }
  return lists;
}

// A whole word of the order, and long enough not to be an everyday short word.
function mentions(text, value) {
  if (value.length < MIN_NAME_LENGTH) return false;
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(?<![\\w-])${escaped}(?![\\w-])`).test(text);
}

// `name` comes from quotedNames, so it holds no regular expression characters.
function definesName(source, name) {
  const pattern = source.python
    ? `^[ \\t]*(?:async[ \\t]+)?def[ \\t]+${name}[ \\t]*\\(`
    : [
        `\\bfunction\\b[ \\t]*\\*?[ \\t]*${name}[ \\t]*\\(`,
        `(?:^|[{,;])[ \\t]*(?:(?:async|static|get|set)[ \\t]+)*\\*?${name}[ \\t]*\\((?:[^()\\n]|\\([^()\\n]*\\))*\\)[ \\t]*\\{`,
        `(?<![\\w$])${name}[ \\t]*[=:][ \\t]*(?:async\\b[ \\t]*)?(?:function\\b[ \\t]*\\*?[ \\t]*)?\\(`,
      ].join('|');
  return new RegExp(pattern, 'm').test(source.code);
}

function outsideTests(piece, repo, source) {
  return repo.files.filter((path) => isTestFile(path) && !piece.files.includes(path) && source(path)).sort();
}

function fakeDefined(piece, repo, source, read) {
  const names = [...new Set(piece.names.map((name) => name.split('.').pop()))].filter(
    (name) => name.replace(/^_+/, '').length >= MIN_NAME_LENGTH,
  );
  if (!names.length || !piece.sources.length) return [];
  return outsideTests(piece, repo, source)
    .filter((path) => piece.sources.some((file) => importsFile(read(path), file)))
    .map((path) => ({ path, defined: names.filter((name) => definesName(source(path), name)) }))
    .filter(({ defined }) => defined.length)
    .slice(0, MAX_RELATED_TESTS)
    .map(({ path, defined }) => ({
      key: piece.key,
      rule: 'fake-defined',
      level: 'warn',
      message: `${path} defines its own ${defined.join(', ')}; keep the call shape compatible or add the test to files`,
      paths: [path],
      names: defined,
    }));
}

function backtickNames(text) {
  const names = new Set();
  for (const match of String(text ?? '').matchAll(/(?<!`)`([^`\n]+)`(?!`)/g)) {
    const name = match[1].replace(/\(\)$/, '');
    if (name.length >= MIN_NAME_LENGTH && /^[\w.-]+$/.test(name)) names.add(name);
  }
  return names;
}

function exactList(piece, repo, source) {
  const quoted = backtickNames(piece.text);
  if (!quoted.size) return [];
  const findings = outsideTests(piece, repo, source).flatMap((path) => {
    const scanned = source(path);
    const hit = (scanned.python ? pythonLists : jsLists)(scanned)
      .map((list) => ({ index: list.index, names: [...new Set(list.strings.filter((value) => quoted.has(value)))] }))
      .find((list) => list.names.length);
    if (!hit) return [];
    const line = scanned.code.slice(0, hit.index).split('\n').length;
    return [
      {
        key: piece.key,
        rule: 'exact-list',
        level: 'warn',
        message: `${path}:${line} asserts an exact list with ${hit.names.join(', ')}; update it or add the test to files`,
        paths: [path],
        names: hit.names,
      },
    ];
  });
  return findings.slice(0, MAX_RELATED_TESTS);
}

function optionalParameter(piece) {
  const sentences = piece.description.split(/[.!?]\s|\n/);
  if (!sentences.some((sentence) => ADDS_WORD.test(sentence) && PARAMETER_WORD.test(sentence))) return null;
  return {
    key: piece.key,
    rule: 'optional-parameter',
    level: 'note',
    message: 'Adding a parameter: keep it optional so existing calls and test fakes keep working.',
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
  const scanned = new Map();
  const source = (path) => {
    if (!scanned.has(path)) scanned.set(path, scanSource(read(path), path));
    return scanned.get(path);
  };
  const pieces = asList(batch?.subtasks).map(toPiece);
  const perPiece = pieces.flatMap((piece) => [
    ...relatedTest(piece, repo, read),
    tooManyFiles(piece),
    notFound(piece, repo, read),
    ...fakeDefined(piece, repo, source, read),
    ...exactList(piece, repo, source),
    optionalParameter(piece),
  ]);
  return [...perPiece.filter(Boolean), ...sharedFiles(pieces)].sort(
    (a, b) => a.key.localeCompare(b.key, 'en', { numeric: true }) || a.rule.localeCompare(b.rule, 'en'),
  );
}
