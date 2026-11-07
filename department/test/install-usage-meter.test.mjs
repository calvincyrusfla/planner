import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import {
  statusLineCommand,
  settingsPath,
  isOurStatusLine,
  planInstall,
  planUninstall,
  sidecarText,
  parseSidecar,
  SIDECAR_NAME,
} from '../scripts/install-usage-meter.mjs';

const INSTALLER = fileURLToPath(new URL('../scripts/install-usage-meter.mjs', import.meta.url));

test('Windows command: quoted, forward slashes, spaces and apostrophes kept', () => {
  const cmd = statusLineCommand({
    node: 'C:\\Program Files\\nodejs\\node.exe',
    script: "C:\\Users\\O'Brien\\My Projects\\planner\\department\\usage\\statusline.mjs",
    platform: 'win32',
  });
  assert.equal(cmd, `"C:/Program Files/nodejs/node.exe" "C:/Users/O'Brien/My Projects/planner/department/usage/statusline.mjs"`);
});

test('Windows command: & ( ) ^ ! and non-ASCII are fine, UNC paths survive', () => {
  const cmd = statusLineCommand({
    node: '\\\\server\\share\\node.exe',
    script: 'D:\\Ä & B (x) ^!\\department\\usage\\statusline.mjs',
    platform: 'win32',
  });
  assert.equal(cmd, '"//server/share/node.exe" "D:/Ä & B (x) ^!/department/usage/statusline.mjs"');
});

test('Windows command: refuses characters cmd or Git Bash would expand', () => {
  for (const bad of ['C:\\100%\\node.exe', 'C:\\$HOME\\node.exe', 'C:\\a`b\\node.exe']) {
    assert.throws(() => statusLineCommand({ node: bad, script: 'C:\\x.mjs', platform: 'win32' }), /can't quote/);
  }
});

test('POSIX command: double quotes normally, single quotes when needed', () => {
  assert.equal(
    statusLineCommand({ node: '/usr/bin/node', script: "/home/o'brien/My Projects/statusline.mjs", platform: 'linux' }),
    `"/usr/bin/node" "/home/o'brien/My Projects/statusline.mjs"`,
  );
  assert.equal(
    statusLineCommand({ node: '/usr/bin/node', script: "/home/a$b/it's/statusline.mjs", platform: 'darwin' }),
    `"/usr/bin/node" '/home/a$b/it'\\''s/statusline.mjs'`,
  );
});

test('POSIX command really runs through sh with awkward paths', { skip: process.platform === 'win32' }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "usage meter $x 'q' "));
  try {
    const script = path.join(dir, 'statusline.mjs');
    fs.writeFileSync(script, 'console.log("ran " + process.argv[1]);');
    const cmd = statusLineCommand({ node: process.execPath, script, platform: process.platform });
    const r = spawnSync('/bin/sh', ['-c', cmd], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), `ran ${script}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('settings path: CLAUDE_CONFIG_DIR, USERPROFILE on Windows, home elsewhere', () => {
  assert.equal(settingsPath({ env: { CLAUDE_CONFIG_DIR: 'D:\\cfg' }, platform: 'win32', homedir: 'C:\\x' }), 'D:\\cfg\\settings.json');
  assert.equal(settingsPath({ env: { USERPROFILE: "C:\\Users\\O'Brien" }, platform: 'win32', homedir: 'C:\\x' }), "C:\\Users\\O'Brien\\.claude\\settings.json");
  assert.equal(settingsPath({ env: {}, platform: 'win32', homedir: 'C:\\Users\\me' }), 'C:\\Users\\me\\.claude\\settings.json');
  assert.equal(settingsPath({ env: { USERPROFILE: '/ignored' }, platform: 'linux', homedir: '/home/me' }), '/home/me/.claude/settings.json');
  assert.equal(settingsPath({ env: { CLAUDE_CONFIG_DIR: '/cfg' }, platform: 'linux', homedir: '/home/me' }), '/cfg/settings.json');
});

test('isOurStatusLine matches on the script, either slash style', () => {
  assert.ok(isOurStatusLine({ type: 'command', command: '"C:\\node.exe" "C:\\p\\department\\usage\\statusline.mjs"' }));
  assert.ok(isOurStatusLine({ type: 'command', command: '"/n" "/p/department/usage/statusline.mjs"' }));
  assert.ok(!isOurStatusLine({ type: 'command', command: '~/statusline.sh' }));
  assert.ok(!isOurStatusLine(null));
});

const CMD = '"/usr/bin/node" "/repo/department/usage/statusline.mjs"';

test('planInstall: missing file, other keys kept, CRLF and BOM kept', () => {
  const fresh = planInstall(null, CMD);
  assert.equal(fresh.action, 'create');
  assert.deepEqual(JSON.parse(fresh.text), { statusLine: { type: 'command', command: CMD } });
  assert.ok(fresh.text.endsWith('}\n'));

  const original = '\uFEFF{\r\n  "model": "opus",\r\n  "env": { "A": "1" }\r\n}\r\n';
  const p = planInstall(original, CMD);
  assert.equal(p.action, 'add');
  assert.ok(p.text.startsWith('\uFEFF{\r\n  "model": "opus",\r\n'));
  assert.ok(!/[^\r]\n/.test(p.text));
  const data = JSON.parse(p.text.slice(1));
  assert.deepEqual(Object.keys(data), ['model', 'env', 'statusLine']);
  assert.deepEqual(data.env, { A: '1' });
});

test('planInstall: unchanged, update of ours, refuse/force for a foreign one', () => {
  const ours = JSON.stringify({ statusLine: { type: 'command', command: CMD } });
  assert.equal(planInstall(ours, CMD).action, 'unchanged');

  const moved = JSON.stringify({ statusLine: { type: 'command', command: '"/old/node" "/old/department/usage/statusline.mjs"', padding: 1 } });
  const up = planInstall(moved, CMD);
  assert.equal(up.action, 'update');
  assert.deepEqual(JSON.parse(up.text).statusLine, { type: 'command', command: CMD, padding: 1 });

  const foreign = JSON.stringify({ statusLine: { type: 'command', command: 'mine.sh' }, theme: 'dark' });
  const no = planInstall(foreign, CMD);
  assert.equal(no.action, 'refuse');
  assert.equal(no.text, undefined);
  assert.deepEqual(no.previous, { type: 'command', command: 'mine.sh' });
  const yes = planInstall(foreign, CMD, { force: true });
  assert.equal(yes.action, 'replace');
  assert.deepEqual(JSON.parse(yes.text), { statusLine: { type: 'command', command: CMD }, theme: 'dark' });
});

test('planInstall / planUninstall refuse invalid JSON instead of overwriting', () => {
  assert.throws(() => planInstall('{oops', CMD), /not valid JSON/);
  assert.throws(() => planInstall('[1]', CMD), /JSON object/);
  assert.throws(() => planUninstall('{oops'), /not valid JSON/);
  assert.equal(planInstall('', CMD).action, 'add');
});

test('planUninstall removes only our statusLine', () => {
  assert.equal(planUninstall(null).action, 'missing');
  assert.equal(planUninstall('{"a":1}').action, 'none');
  assert.equal(planUninstall('{"statusLine":{"type":"command","command":"mine.sh"}}').action, 'foreign');
  const r = planUninstall(JSON.stringify({ a: 1, statusLine: { type: 'command', command: CMD } }));
  assert.equal(r.action, 'remove');
  assert.deepEqual(JSON.parse(r.text), { a: 1 });
});

test('CLI end to end in a scratch config dir: install, refuse, force, uninstall', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'usage-cfg-'));
  const env = { ...process.env, CLAUDE_CONFIG_DIR: dir, HOME: dir, USERPROFILE: dir };
  const run = (...args) => spawnSync(process.execPath, [INSTALLER, ...args], { env, encoding: 'utf8' });
  const file = path.join(dir, 'settings.json');
  try {
    let r = run('--dry-run');
    assert.equal(r.status, 0, r.stderr);
    assert.ok(!fs.existsSync(file));

    fs.writeFileSync(file, JSON.stringify({ statusLine: { type: 'command', command: 'mine.sh' }, model: 'x' }));
    r = run();
    assert.equal(r.status, 1);
    assert.match(r.stdout, /mine\.sh/);
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).statusLine.command, 'mine.sh');

    r = run('--force');
    assert.equal(r.status, 0, r.stderr);
    const after = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.ok(isOurStatusLine(after.statusLine));
    assert.equal(after.model, 'x');
    assert.equal(JSON.parse(fs.readFileSync(file + '.bak-agent-department', 'utf8')).statusLine.command, 'mine.sh');

    r = run('--uninstall');
    assert.equal(r.status, 0, r.stderr);
    // The replaced status line comes back; the sidecar is gone.
    assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { statusLine: { type: 'command', command: 'mine.sh' }, model: 'x' });
    assert.deepEqual(fs.readdirSync(dir).sort(), ['settings.json', 'settings.json.bak-agent-department']);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('planUninstall restores a saved statusLine; sidecar round trip', () => {
  const saved = { type: 'command', command: 'mine.sh', padding: 2 };
  const r = planUninstall(JSON.stringify({ a: 1, statusLine: { type: 'command', command: CMD } }), { restore: saved });
  assert.equal(r.action, 'restore');
  assert.deepEqual(JSON.parse(r.text), { a: 1, statusLine: saved });
  assert.deepEqual(parseSidecar(sidecarText(saved)), saved);
  assert.throws(() => parseSidecar('{oops'), /not valid JSON/);
  assert.throws(() => parseSidecar('{"x":1}'), /saved statusLine/);
  // A foreign line is never touched, even with something to restore.
  assert.equal(planUninstall('{"statusLine":{"type":"command","command":"other.sh"}}', { restore: saved }).action, 'foreign');
});

// bravo-6's repro: foreign line -> --force -> (update) -> --uninstall must give
// the user's original status line back, and the backup must keep the original.
for (const withUpdate of [false, true]) {
  test(`foreign statusLine survives --force${withUpdate ? ' -> path update' : ''} -> --uninstall`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'usage-cfg-'));
    const env = { ...process.env, CLAUDE_CONFIG_DIR: dir, HOME: dir, USERPROFILE: dir };
    const run = (...args) => spawnSync(process.execPath, [INSTALLER, ...args], { env, encoding: 'utf8' });
    const file = path.join(dir, 'settings.json');
    const backup = file + '.bak-agent-department';
    const sidecar = path.join(dir, SIDECAR_NAME);
    const mine = { type: 'command', command: 'bash ~/.claude/my-line.sh', padding: 0 };
    const original = JSON.stringify({ theme: 'dark', statusLine: mine }, null, 2) + '\n';
    try {
      fs.writeFileSync(file, original);
      let r = run('--force');
      assert.equal(r.status, 0, r.stderr);
      assert.deepEqual(JSON.parse(fs.readFileSync(sidecar, 'utf8')).statusLine, mine);
      assert.equal(fs.readFileSync(backup, 'utf8'), original);

      if (withUpdate) {
        // Simulate a moved repo / new node: our entry with another path.
        const s = JSON.parse(fs.readFileSync(file, 'utf8'));
        s.statusLine.command = '"/old/node" "/old/repo/department/usage/statusline.mjs"';
        fs.writeFileSync(file, JSON.stringify(s, null, 2) + '\n');
        r = run();
        assert.equal(r.status, 0, r.stderr);
        assert.match(r.stdout, /updating/);
        assert.ok(isOurStatusLine(JSON.parse(fs.readFileSync(file, 'utf8')).statusLine));
        assert.ok(fs.existsSync(sidecar), 'update keeps the sidecar');
        assert.equal(fs.readFileSync(backup, 'utf8'), original, 'backup is never overwritten');
      }

      r = run('--uninstall', '--dry-run');
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stdout, /would put back/);
      assert.ok(fs.existsSync(sidecar));

      r = run('--uninstall');
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stdout, /my-line\.sh/);
      assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { theme: 'dark', statusLine: mine });
      assert.ok(!fs.existsSync(sidecar), 'sidecar removed after restoring');
      assert.equal(fs.readFileSync(backup, 'utf8'), original);

      // Uninstalling again leaves the restored foreign line alone.
      r = run('--uninstall');
      assert.equal(r.status, 0, r.stderr);
      assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).statusLine, mine);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}

test('backup is made once and kept across install, uninstall and reinstall', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'usage-cfg-'));
  const env = { ...process.env, CLAUDE_CONFIG_DIR: dir, HOME: dir, USERPROFILE: dir };
  const run = (...args) => spawnSync(process.execPath, [INSTALLER, ...args], { env, encoding: 'utf8' });
  const file = path.join(dir, 'settings.json');
  const original = '{"model":"opus"}';
  try {
    fs.writeFileSync(file, original);
    for (const args of [[], ['--uninstall'], []]) assert.equal(run(...args).status, 0);
    assert.equal(fs.readFileSync(file + '.bak-agent-department', 'utf8'), original);
    assert.ok(!fs.existsSync(path.join(dir, SIDECAR_NAME)));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
