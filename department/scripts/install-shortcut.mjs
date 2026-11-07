#!/usr/bin/env node
// Puts an "Agent Department" shortcut on the desktop that opens the dashboard.
// No dependencies, Node 18+.
//
//   node department/scripts/install-shortcut.mjs              create the shortcut
//   node department/scripts/install-shortcut.mjs --uninstall  remove it again
//   node department/scripts/install-shortcut.mjs --dry-run    show what would be written
//
// Extra options:
//   --platform win32|darwin|linux   generate for another OS (only together with --dry-run;
//                                   handy for inspecting the output of every platform)
//   DEPT_DESKTOP_DIR=<dir>          use this folder instead of the detected desktop
//
// The shortcut runs `<absolute node> department/launch.mjs`. Shortcuts don't get the
// user's shell PATH, so the node binary that runs this installer (process.execPath) is
// baked in. If you later move or upgrade node to a different path, run this again.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SHORTCUT_NAME = 'Agent Department';
const DESCRIPTION = 'Open the Agent Department dashboard';
const BUNDLE_ID = 'local.agent-department';
const MAC_EXECUTABLE = 'AgentDepartment';
const LINUX_FILE = 'agent-department.desktop';

const SCRIPT_FILE = fileURLToPath(import.meta.url);
const DEPT_DIR = path.resolve(path.dirname(SCRIPT_FILE), '..');
const REPO_ROOT = path.resolve(DEPT_DIR, '..');

function repoPaths() {
  const assets = path.join(DEPT_DIR, 'assets');
  return {
    node: process.execPath,
    launch: path.join(DEPT_DIR, 'launch.mjs'),
    root: REPO_ROOT,
    ico: path.join(assets, 'icon.ico'),
    icns: path.join(assets, 'icon.icns'),
    png: path.join(assets, 'icon-256.png'),
  };
}

// ---------------------------------------------------------------- helpers

function parseArgs(argv) {
  const opts = { uninstall: false, dryRun: false, platform: process.platform, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--uninstall' || a === '--remove') opts.uninstall = true;
    else if (a === '--dry-run' || a === '-n') opts.dryRun = true;
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

// Collects actions so --dry-run can print instead of touching the disk.
function makeRunner(dryRun) {
  const log = [];
  const warnings = [];
  return {
    log,
    warnings,
    dryRun,
    mkdir(dir) {
      if (!dryRun) fs.mkdirSync(dir, { recursive: true });
    },
    write(file, content, mode) {
      log.push(`${dryRun ? 'would write' : 'wrote'} ${file}`);
      if (dryRun) {
        log.push(indent(content));
        return;
      }
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, content);
      if (mode) fs.chmodSync(file, mode);
    },
    copy(src, dest) {
      log.push(`${dryRun ? 'would copy' : 'copied'} ${src} -> ${dest}`);
      if (dryRun) return;
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(src, dest);
    },
    remove(target) {
      if (!fs.existsSync(target)) {
        log.push(`nothing at ${target}`);
        return;
      }
      log.push(`${dryRun ? 'would remove' : 'removed'} ${target}`);
      if (!dryRun) fs.rmSync(target, { recursive: true, force: true });
    },
    note(msg) {
      log.push(msg);
    },
    warn(msg) {
      warnings.push(msg);
    },
  };
}

function indent(text) {
  return String(text)
    .replace(/\n$/, '')
    .split('\n')
    .map((l) => `    | ${l}`)
    .join('\n');
}

function checkIcon(run, file) {
  if (!fs.existsSync(file)) {
    run.warn(`icon file ${file} does not exist yet; run "npm run icons" and then install the shortcut again`);
    return false;
  }
  return true;
}

function tryRun(cmd, args) {
  try {
    const r = spawnSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    if (r.error || r.status !== 0) return null;
    return r.stdout;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- Windows

// PowerShell single-quoted literal. Doubling is the only escape; PowerShell also
// treats the typographic quotes U+2018..U+201B as single quotes, so double those too.
function psQuote(s) {
  return `'${String(s).replace(/['\u2018\u2019\u201A\u201B]/g, (c) => c + c)}'`;
}

// Base64 of UTF-16LE, the format powershell -EncodedCommand expects. Passing the
// script this way means spaces, quotes and apostrophes in paths can't break it.
function psEncode(script) {
  return Buffer.from(script, 'utf16le').toString('base64');
}

function runPowerShell(script) {
  const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', psEncode(script)];
  for (const exe of ['powershell.exe', 'pwsh.exe', 'pwsh']) {
    const r = spawnSync(exe, args, { encoding: 'utf8', windowsHide: true });
    if (r.error && r.error.code === 'ENOENT') continue;
    return r;
  }
  return { status: 1, stdout: '', stderr: 'PowerShell was not found', error: new Error('PowerShell was not found') };
}

// Pure: the PowerShell that creates the .lnk via the WScript.Shell COM object.
function windowsShortcutScript({ lnk, node, launch, root, ico }) {
  return [
    "$ErrorActionPreference = 'Stop'",
    '$shell = New-Object -ComObject WScript.Shell',
    `$lnk = $shell.CreateShortcut(${psQuote(lnk)})`,
    `$lnk.TargetPath = ${psQuote(node)}`,
    // Arguments is a command line string: wrap the script path in double quotes.
    // (Windows paths can't contain double quotes, so no further escaping is needed.)
    `$lnk.Arguments = ${psQuote(`"${launch}"`)}`,
    `$lnk.WorkingDirectory = ${psQuote(root)}`,
    `$lnk.IconLocation = ${psQuote(`${ico},0`)}`,
    '$lnk.WindowStyle = 7', // 7 = minimized, so the node console doesn't pop up in front
    `$lnk.Description = ${psQuote(DESCRIPTION)}`,
    '$lnk.Save()',
    '',
  ].join('\r\n');
}

// Prints the desktop folder as base64 UTF-8 so the console code page can't mangle it.
// GetFolderPath follows OneDrive / group-policy redirection of the desktop.
const WINDOWS_DESKTOP_SCRIPT =
  "$p = [Environment]::GetFolderPath('Desktop'); " +
  '[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p)))';

function windowsDesktopDir(native) {
  if (process.env.DEPT_DESKTOP_DIR) return process.env.DEPT_DESKTOP_DIR;
  if (native) {
    const r = runPowerShell(WINDOWS_DESKTOP_SCRIPT);
    if (r.status === 0 && r.stdout && r.stdout.trim()) {
      const dir = Buffer.from(r.stdout.trim(), 'base64').toString('utf8');
      if (dir) return dir;
    }
  }
  const profile = process.env.USERPROFILE || os.homedir();
  return path.win32.join(profile, 'Desktop');
}

function installWindows(run, p, native) {
  const desktop = windowsDesktopDir(native);
  const lnk = path.win32.join(desktop, `${SHORTCUT_NAME}.lnk`);
  checkIcon(run, p.ico);
  const script = windowsShortcutScript({ ...p, lnk });
  if (run.dryRun) {
    run.note(`would create ${lnk} with PowerShell:`);
    run.note(indent(script));
    return;
  }
  run.mkdir(desktop);
  const r = runPowerShell(script);
  if (r.status !== 0) {
    throw new Error(`PowerShell could not create the shortcut:\n${(r.stderr || r.error?.message || '').trim()}`);
  }
  if (!fs.existsSync(lnk)) throw new Error(`PowerShell reported success but ${lnk} was not created`);
  run.note(`created ${lnk}`);
  // Tip: right-click the desktop shortcut and choose "Pin to taskbar" or "Pin to Start".
  run.note('tip: right-click the shortcut and choose "Pin to taskbar" or "Pin to Start" to keep it handy');
}

function uninstallWindows(run, native) {
  run.remove(path.win32.join(windowsDesktopDir(native), `${SHORTCUT_NAME}.lnk`));
}

// ---------------------------------------------------------------- macOS

function xmlEscape(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Pure: Contents/Info.plist.
function macInfoPlist() {
  const entries = [
    ['CFBundleName', SHORTCUT_NAME],
    ['CFBundleDisplayName', SHORTCUT_NAME],
    ['CFBundleIdentifier', BUNDLE_ID],
    ['CFBundleExecutable', MAC_EXECUTABLE],
    ['CFBundleIconFile', 'icon'],
    ['CFBundlePackageType', 'APPL'],
    ['CFBundleInfoDictionaryVersion', '6.0'],
    ['CFBundleVersion', '1.0'],
    ['CFBundleShortVersionString', '1.0'],
  ];
  const body = entries
    .map(([k, v]) => `\t<key>${xmlEscape(k)}</key>\n\t<string>${xmlEscape(v)}</string>`)
    .join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
${body}
\t<key>LSUIElement</key>
\t<true/>
</dict>
</plist>
`;
}

// POSIX shell single-quoting: close the quote, emit an escaped ', reopen.
function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

// Pure: Contents/MacOS/AgentDepartment.
function macLauncherScript({ node, launch, root }) {
  return `#!/bin/sh
# Generated by department/scripts/install-shortcut.mjs; run it again if node moves.
cd ${shQuote(root)} || exit 1
exec ${shQuote(node)} ${shQuote(launch)} "$@"
`;
}

// Pure: every file in the bundle, relative to the .app folder.
function macBundleFiles(p) {
  return {
    'Contents/Info.plist': { content: macInfoPlist() },
    [`Contents/MacOS/${MAC_EXECUTABLE}`]: { content: macLauncherScript(p), mode: 0o755 },
  };
}

function macAppPath() {
  const desktop = process.env.DEPT_DESKTOP_DIR || path.join(os.homedir(), 'Desktop');
  return path.join(desktop, `${SHORTCUT_NAME}.app`);
}

function installMac(run, p) {
  const app = macAppPath();
  // Start clean so files from an older layout don't linger inside the bundle.
  if (fs.existsSync(app)) run.remove(app);
  for (const [rel, { content, mode }] of Object.entries(macBundleFiles(p))) {
    run.write(path.join(app, rel), content, mode);
  }
  if (checkIcon(run, p.icns)) run.copy(p.icns, path.join(app, 'Contents/Resources/icon.icns'));
  if (!run.dryRun) {
    // Bump the mtime so Finder / LaunchServices pick up the new icon.
    const now = new Date();
    fs.utimesSync(app, now, now);
    tryRun('touch', [app]);
  }
  run.note(`${run.dryRun ? 'would create' : 'created'} ${app}`);
  run.note(
    'note: the app is unsigned, so the first time macOS may refuse to open it; ' +
      'right-click it, choose Open and confirm. You can also drag it to the Dock.'
  );
}

function uninstallMac(run) {
  run.remove(macAppPath());
}

// ---------------------------------------------------------------- Linux

// Desktop Entry spec, "The Exec key": an argument containing reserved characters
// goes inside double quotes, and within them ", `, $ and \ get a backslash.
// The value as a whole is a "string" value, whose own escape rules then turn every
// backslash into \\ again. A literal % must be written %% (field codes).
const EXEC_RESERVED = /[\s"'\\><~|&;$*?#()`]/;
function desktopExecArg(arg) {
  let s = String(arg);
  if (EXEC_RESERVED.test(s) || s === '') s = `"${s.replace(/(["`$\\])/g, '\\$1')}"`;
  s = s.replace(/%/g, '%%');
  return s.replace(/\\/g, '\\\\');
}

// Escapes for other string values (Name, Icon, Path...): \\ \n \t \r.
function desktopString(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/\t/g, '\\t').replace(/\r/g, '\\r');
}

// Pure: the .desktop file text.
function linuxDesktopEntry({ node, launch, root, png }) {
  return `[Desktop Entry]
Type=Application
Version=1.0
Name=${SHORTCUT_NAME}
Comment=${DESCRIPTION}
Exec=${desktopExecArg(node)} ${desktopExecArg(launch)}
Path=${desktopString(root)}
Icon=${desktopString(png)}
Terminal=false
Categories=Development;Utility;
StartupNotify=false
`;
}

function linuxDesktopDir() {
  if (process.env.DEPT_DESKTOP_DIR) return process.env.DEPT_DESKTOP_DIR;
  const out = tryRun('xdg-user-dir', ['DESKTOP']);
  const dir = out && out.trim();
  if (dir && path.isAbsolute(dir)) return dir;
  return path.join(os.homedir(), 'Desktop');
}

function linuxAppsDir() {
  const dataHome = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(dataHome, 'applications');
}

function linuxTargets() {
  return [path.join(linuxDesktopDir(), LINUX_FILE), path.join(linuxAppsDir(), LINUX_FILE)];
}

function installLinux(run, p) {
  checkIcon(run, p.png);
  const entry = linuxDesktopEntry(p);
  const [onDesktop, inMenu] = linuxTargets();
  run.write(onDesktop, entry, 0o755);
  if (run.dryRun) run.note(`would write ${inMenu} (same content)`);
  else run.write(inMenu, entry, 0o755);
  if (!run.dryRun) {
    // GNOME won't launch desktop files until they are marked trusted. Not fatal if
    // gio is missing or there's no session bus; the user can right-click → Allow Launching.
    if (tryRun('gio', ['set', onDesktop, 'metadata::trusted', 'true']) === null) {
      run.note('note: could not mark the desktop file trusted; if your desktop asks, right-click it and choose "Allow Launching"');
    }
    tryRun('update-desktop-database', [linuxAppsDir()]);
  }
}

function uninstallLinux(run) {
  for (const f of linuxTargets()) run.remove(f);
}

// ---------------------------------------------------------------- main

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(`error: ${err.message}`);
    process.exit(2);
  }
  if (opts.help) {
    console.log('usage: node department/scripts/install-shortcut.mjs [--uninstall] [--dry-run] [--platform win32|darwin|linux]');
    return;
  }

  const p = repoPaths();
  const run = makeRunner(opts.dryRun);
  const native = opts.platform === process.platform;
  const label = { win32: 'Windows', darwin: 'macOS', linux: 'Linux' }[opts.platform];
  const verb = opts.uninstall ? 'Removing' : 'Installing';
  console.log(`${verb} the "${SHORTCUT_NAME}" desktop shortcut for ${label}${opts.dryRun ? ' (dry run, nothing is written)' : ''}`);

  if (!opts.uninstall && !fs.existsSync(p.launch)) {
    run.warn(`${p.launch} does not exist yet; the shortcut will not work until it does`);
  }

  try {
    if (opts.platform === 'win32') opts.uninstall ? uninstallWindows(run, native) : installWindows(run, p, native);
    else if (opts.platform === 'darwin') opts.uninstall ? uninstallMac(run) : installMac(run, p);
    else opts.uninstall ? uninstallLinux(run) : installLinux(run, p);
  } catch (err) {
    printLog(run.log);
    console.error(`error: ${err.message}`);
    process.exit(1);
  }

  printLog(run.log);
  for (const w of run.warnings) console.warn(`warning: ${w}`);
  if (!opts.uninstall) {
    console.log(`Shortcut runs: ${p.node} ${p.launch}`);
    if (!opts.dryRun) console.log(`Done. Double-click "${SHORTCUT_NAME}" on your desktop to open the dashboard.`);
  } else if (!opts.dryRun) {
    console.log('Done.');
  }
}

function printLog(lines) {
  for (const entry of lines) for (const line of entry.split('\n')) console.log(`  ${line}`);
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
