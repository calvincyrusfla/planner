#!/usr/bin/env node
// Connects Claude Code's status line to the Agent Department usage meter.
//
//   node department/scripts/install-usage-meter.mjs              add the statusLine
//   node department/scripts/install-usage-meter.mjs --uninstall  remove it again
//   node department/scripts/install-usage-meter.mjs --dry-run    print, change nothing
//   node department/scripts/install-usage-meter.mjs --force      replace another statusLine
//
// It edits the USER's Claude Code settings: %USERPROFILE%\.claude\settings.json on
// Windows, ~/.claude/settings.json elsewhere, or <CLAUDE_CONFIG_DIR>/settings.json
// when that is set. It backs the file up first (settings.json.bak-agent-department),
// keeps every other key, and never replaces a status line that isn't ours unless
// --force is given. Claude Code then runs department/usage/statusline.mjs, which
// reads the rate limits Claude Code passes it and reports them to the dashboard.
// Nothing here reads passwords, tokens or credential files.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_FILE = fileURLToPath(import.meta.url);
const DEPT_DIR = path.resolve(path.dirname(SCRIPT_FILE), '..');
export const STATUSLINE_SCRIPT = path.join(DEPT_DIR, 'usage', 'statusline.mjs');
export const BACKUP_SUFFIX = '.bak-agent-department';

// ---------------------------------------------------------------- pure helpers

// Where Claude Code keeps the user's settings. CLAUDE_CONFIG_DIR wins; on Windows
// the profile folder comes from USERPROFILE (what Claude Code itself uses).
export function settingsPath({ env = process.env, platform = process.platform, homedir = os.homedir() } = {}) {
  const pathMod = platform === 'win32' ? path.win32 : path.posix;
  if (env.CLAUDE_CONFIG_DIR) return pathMod.join(env.CLAUDE_CONFIG_DIR, 'settings.json');
  const home = (platform === 'win32' && env.USERPROFILE) || homedir;
  return pathMod.join(home, '.claude', 'settings.json');
}

// Characters that can't be put safely inside double quotes for BOTH Windows cmd
// (%VAR% expands inside quotes) and a POSIX shell such as Git Bash ($ and
// backtick). A double quote can't occur in a Windows path at all. Spaces,
// apostrophes, &, (, ), ^, ! (no delayed expansion / history in non-interactive
// shells) and non-ASCII letters are fine.
const WIN_UNSAFE = /["%$`\r\n]/;

// Pure: the statusLine command string. Claude Code runs it through a shell (cmd
// or Git Bash on Windows, sh elsewhere), so each path is quoted.
//  - Windows: double quotes, backslashes turned into forward slashes. Node and
//    both shells accept C:/Program Files/nodejs/node.exe, and a POSIX shell would
//    otherwise eat the backslash in "\\server" or before $ / `.
//  - POSIX: double quotes when nothing in the path is special inside them, else
//    single quotes (with ' written as '\'').
export function statusLineCommand({ node, script, platform = process.platform }) {
  if (platform === 'win32') {
    const q = (p) => {
      const s = String(p);
      if (WIN_UNSAFE.test(s)) {
        throw new Error(`can't quote ${s} safely for the status line (it contains one of " % $ \`); move it to a folder without those characters`);
      }
      return `"${s.replace(/\\/g, '/')}"`;
    };
    return `${q(node)} ${q(script)}`;
  }
  const q = (p) => {
    const s = String(p);
    if (/[\n\r]/.test(s)) throw new Error(`can't use a path with a line break in the status line: ${JSON.stringify(s)}`);
    if (!/["$`\\!]/.test(s)) return `"${s}"`;
    return `'${s.replace(/'/g, `'\\''`)}'`;
  };
  return `${q(node)} ${q(script)}`;
}

// Is this statusLine one we installed? Matched on the script path, so a moved
// repo or a different node still counts as ours.
export function isOurStatusLine(statusLine) {
  if (!statusLine || typeof statusLine !== 'object') return false;
  const cmd = String(statusLine.command || '').replace(/\\/g, '/');
  return /department\/usage\/statusline\.mjs/i.test(cmd);
}

function detectEol(text) {
  return /\r\n/.test(text) ? '\r\n' : '\n';
}

function parseSettings(text, file) {
  const body = text.replace(/^\uFEFF/, '');
  if (!body.trim()) return {};
  let data;
  try {
    data = JSON.parse(body);
  } catch (err) {
    throw new Error(`${file} is not valid JSON (${err.message}); fix or move it, then run this again`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error(`${file} does not hold a JSON object; fix or move it, then run this again`);
  }
  return data;
}

function serialize(data, original) {
  const eol = original == null ? '\n' : detectEol(original);
  const bom = original && original.startsWith('\uFEFF') ? '\uFEFF' : '';
  return bom + JSON.stringify(data, null, 2).replace(/\n/g, eol) + eol;
}

// Pure: what installing does to the settings text (null = file missing).
// Returns { action: 'create'|'add'|'update'|'replace'|'unchanged'|'refuse', text?, previous? }.
export function planInstall(original, command, { force = false, file = 'settings.json' } = {}) {
  const data = original == null ? {} : parseSettings(original, file);
  const wanted = { type: 'command', command };
  const prev = data.statusLine;
  let action;
  if (prev === undefined) action = original == null ? 'create' : 'add';
  else if (isOurStatusLine(prev)) {
    if (prev.type === 'command' && prev.command === command) return { action: 'unchanged', previous: prev };
    action = 'update';
  } else if (!force) return { action: 'refuse', previous: prev };
  else action = 'replace';
  // Keep any extra fields of our own earlier entry (e.g. padding the user added).
  data.statusLine = action === 'update' && prev && typeof prev === 'object' ? { ...prev, ...wanted } : wanted;
  return { action, text: serialize(data, original), previous: prev };
}

// Pure: what uninstalling does. Returns { action: 'missing'|'none'|'foreign'|'remove', text?, previous? }.
export function planUninstall(original, { file = 'settings.json' } = {}) {
  if (original == null) return { action: 'missing' };
  const data = parseSettings(original, file);
  const prev = data.statusLine;
  if (prev === undefined) return { action: 'none' };
  if (!isOurStatusLine(prev)) return { action: 'foreign', previous: prev };
  delete data.statusLine;
  return { action: 'remove', text: serialize(data, original), previous: prev };
}

// ---------------------------------------------------------------- CLI

function parseArgs(argv) {
  const opts = { uninstall: false, dryRun: false, force: false, platform: process.platform, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--uninstall' || a === '--remove') opts.uninstall = true;
    else if (a === '--dry-run' || a === '-n') opts.dryRun = true;
    else if (a === '--force' || a === '-f') opts.force = true;
    else if (a === '--platform') opts.platform = argv[++i];
    else if (a.startsWith('--platform=')) opts.platform = a.slice('--platform='.length);
    else if (a === '--help' || a === '-h') opts.help = true;
    else throw new Error(`Unknown option: ${a}`);
  }
  if (!['win32', 'darwin', 'linux'].includes(opts.platform)) {
    throw new Error(`Unsupported platform "${opts.platform}" (expected win32, darwin or linux)`);
  }
  if (opts.platform !== process.platform && !opts.dryRun) {
    throw new Error(`--platform ${opts.platform} only works together with --dry-run`);
  }
  return opts;
}

function readOrNull(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}

function show(statusLine) {
  return JSON.stringify(statusLine, null, 2).split('\n').map((l) => `    | ${l}`).join('\n');
}

function writeSettings(file, original, text, dryRun) {
  const backup = file + BACKUP_SUFFIX;
  if (dryRun) {
    if (original != null) console.log(`  would back up ${file} -> ${backup}`);
    console.log(`  would write ${file}:`);
    console.log(text.replace(/\r?\n$/, '').split(/\r?\n/).map((l) => `    | ${l}`).join('\n'));
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  if (original != null) {
    fs.copyFileSync(file, backup);
    console.log(`  backed up ${file} -> ${backup}`);
  }
  // Write next to it and rename, so a crash can't leave half a settings file.
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, text);
  fs.renameSync(tmp, file);
  console.log(`  wrote ${file}`);
}

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(`error: ${err.message}`);
    process.exit(2);
  }
  if (opts.help) {
    console.log('usage: node department/scripts/install-usage-meter.mjs [--uninstall] [--force] [--dry-run] [--platform win32|darwin|linux]');
    return;
  }

  const native = opts.platform === process.platform;
  const file = settingsPath({ platform: opts.platform });
  const verb = opts.uninstall ? 'Removing' : 'Installing';
  console.log(`${verb} the Agent Department usage meter in Claude Code's status line${opts.dryRun ? ' (dry run, nothing is written)' : ''}`);
  console.log(`  settings: ${file}`);

  try {
    const original = native ? readOrNull(file) : null;
    if (opts.uninstall) {
      const plan = planUninstall(original, { file });
      if (plan.action === 'missing') console.log('  no settings file; nothing to remove');
      else if (plan.action === 'none') console.log('  no statusLine set; nothing to remove');
      else if (plan.action === 'foreign') {
        console.log("  the statusLine there isn't the usage meter, so it was left alone:");
        console.log(show(plan.previous));
      } else {
        writeSettings(file, original, plan.text, opts.dryRun);
        if (!opts.dryRun) console.log('Done. Restart Claude Code to drop the status line.');
      }
      return;
    }

    const node = native ? process.execPath : opts.platform === 'win32' ? 'C:\\Program Files\\nodejs\\node.exe' : '/usr/local/bin/node';
    const command = statusLineCommand({ node, script: STATUSLINE_SCRIPT, platform: opts.platform });
    if (!fs.existsSync(STATUSLINE_SCRIPT)) console.warn(`warning: ${STATUSLINE_SCRIPT} does not exist yet; the status line will not work until it does`);
    const plan = planInstall(original, command, { force: opts.force, file });
    if (plan.action === 'refuse') {
      console.log('  Claude Code already has a different status line:');
      console.log(show(plan.previous));
      console.log('  Left it as it is. Run again with --force to replace it (the old file is backed up first).');
      process.exitCode = 1;
      return;
    }
    if (plan.action === 'unchanged') {
      console.log('  already installed; nothing to change');
    } else {
      if (plan.action === 'replace') {
        console.log('  replacing this status line (--force):');
        console.log(show(plan.previous));
      } else if (plan.action === 'update') console.log('  updating the usage meter command (node or repo path changed)');
      writeSettings(file, original, plan.text, opts.dryRun);
    }
    console.log(`  status line runs: ${command}`);
    if (!opts.dryRun && plan.action !== 'unchanged') console.log('Done. Restart Claude Code; the dashboard meter fills in on its next reply.');
  } catch (err) {
    console.error(`error: ${err.message}`);
    process.exit(1);
  }
}

function isDirectRun() {
  if (!process.argv[1]) return false;
  try {
    return fs.realpathSync(process.argv[1]) === fs.realpathSync(SCRIPT_FILE);
  } catch {
    return path.resolve(process.argv[1]) === SCRIPT_FILE;
  }
}

if (isDirectRun()) main();
