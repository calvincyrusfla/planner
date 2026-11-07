#!/usr/bin/env node
// Desktop-shortcut launcher for the Agent Department dashboard. No dependencies.
//   node department/launch.mjs [--port 4300] [--browser-tab]
// Starts server.mjs in the background if it isn't already running, opens the
// dashboard in its own app window (Edge/Chrome/Chromium --app mode; set
// DEPT_BROWSER=<exe> to pick the browser, or --browser-tab for a normal tab in
// the default browser), then exits. Safe to run any number of times:
// a running dashboard is reused, never duplicated. Works from any cwd and without
// a shell PATH (GUI launchers), since it only relies on process.execPath.
import fs from 'node:fs';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const argPort = args.includes('--port') ? Number(args[args.indexOf('--port') + 1]) : null;
const PORT = argPort || Number(process.env.PORT) || 4300;
const URL_BASE = `http://127.0.0.1:${PORT}`;
// Keep the log next to the state file, so DEPT_DATA test runs don't litter the repo.
const LOG_DIR = process.env.DEPT_DATA ? path.dirname(path.resolve(process.env.DEPT_DATA)) : path.join(here, 'data');
const LOG_FILE = path.join(LOG_DIR, 'server.log');

// Absolute System32 paths on Windows, since a shortcut's environment may lack a useful PATH.
const winBin = (rel) => path.win32.join(process.env.SystemRoot || 'C:\\Windows', 'System32', rel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 'dashboard' if our server answers, 'other' if something else holds the port, 'free' if nothing answers.
async function probe() {
  try {
    const res = await fetch(`${URL_BASE}/api/state`, { signal: AbortSignal.timeout(1000) });
    const body = await res.json().catch(() => null);
    return body && body.ok === true ? 'dashboard' : 'other';
  } catch (err) {
    // Refused means nothing is listening; a hang means something is, but not us.
    return err.name === 'TimeoutError' ? 'other' : 'free';
  }
}

// Fire-and-forget child process; a missing program must never crash the launcher.
function detach(cmd, cmdArgs, opts = {}) {
  try {
    const child = spawn(cmd, cmdArgs, { stdio: 'ignore', detached: true, windowsHide: true, ...opts });
    child.on('error', () => {});
    child.unref();
    return child;
  } catch {
    return null;
  }
}

function fail(message) {
  console.error(`Agent Department: ${message}`);
  const title = 'Agent Department';
  const run = (cmd, cmdArgs) => {
    try {
      spawnSync(cmd, cmdArgs, { stdio: 'ignore', windowsHide: true, timeout: 60000 });
    } catch {}
  };
  if (process.platform === 'darwin') {
    run('osascript', ['-e', `display alert ${JSON.stringify(title)} message ${JSON.stringify(message)} as critical`]);
  } else if (process.platform === 'win32') {
    const ps = (s) => `'${s.replace(/'/g, "''")}'`;
    run(winBin('WindowsPowerShell\\v1.0\\powershell.exe'), [
      '-NoProfile',
      '-Command',
      `Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show(${ps(message)},${ps(title)})`,
    ]);
  } else {
    const r = spawnSync('notify-send', ['-u', 'critical', title, message], { stdio: 'ignore' });
    if (r.error || r.status !== 0) run('zenity', ['--error', `--title=${title}`, `--text=${message}`]);
  }
  process.exit(1);
}

function startServer() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const log = fs.openSync(LOG_FILE, 'a');
  fs.writeSync(log, `\n--- ${new Date().toISOString()} launch.mjs starting server on port ${PORT}\n`);
  const child = spawn(process.execPath, [path.join(here, 'server.mjs'), '--port', String(PORT)], {
    cwd: path.dirname(here),
    env: { ...process.env, PORT: String(PORT) },
    stdio: ['ignore', log, log],
    detached: true,
    windowsHide: true,
  });
  child.on('error', () => {});
  child.unref();
  fs.closeSync(log);
}

function openBrowserTab(url) {
  if (process.platform === 'darwin') detach('open', [url]);
  // Verbatim args so the empty `start` title stays "" instead of being escaped to \"\".
  else if (process.platform === 'win32') detach(winBin('cmd.exe'), ['/d', '/s', '/c', 'start', '""', url], { windowsVerbatimArguments: true });
  else detach('xdg-open', [url]);
}

// Where Edge, then Chrome, usually live on Windows. Pure, so it can be checked with a fake env.
export function windowsBrowserCandidates(env) {
  const roots = [env['ProgramFiles(x86)'], env.ProgramFiles, env.LOCALAPPDATA || env.LocalAppData].filter(Boolean);
  const apps = ['Microsoft\\Edge\\Application\\msedge.exe', 'Google\\Chrome\\Application\\chrome.exe'];
  return apps.flatMap((app) => roots.map((root) => path.win32.join(root, app)));
}

const MAC_BROWSERS = [
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
];
const LINUX_BROWSERS = ['microsoft-edge', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];

function onPath(name) {
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    const full = dir && path.join(dir, name);
    if (full && fs.existsSync(full)) return full;
  }
  return null;
}

function findAppBrowser() {
  if (process.env.DEPT_BROWSER) return fs.existsSync(process.env.DEPT_BROWSER) ? process.env.DEPT_BROWSER : null;
  if (process.platform === 'win32') return windowsBrowserCandidates(process.env).find((p) => fs.existsSync(p)) || null;
  if (process.platform === 'darwin') return MAC_BROWSERS.find((p) => fs.existsSync(p)) || null;
  for (const name of LINUX_BROWSERS) {
    const found = onPath(name);
    if (found) return found;
  }
  return null;
}

// Chromium app mode: its own window and taskbar button, no tabs or address bar.
// Uses the normal browser profile (no --user-data-dir), so no first-run screens.
function openAppWindow(url) {
  const browser = args.includes('--browser-tab') ? null : findAppBrowser();
  if (!browser) return openBrowserTab(url);
  // No windowsHide here: the browser window has to be visible.
  detach(browser, [`--app=${url}`, '--window-size=1400,900'], { windowsHide: false });
}

async function main() {
  let status = await probe();
  if (status === 'other') {
    fail(`Port ${PORT} is in use by another program, not the dashboard. Free the port or set PORT to another number.`);
  }
  if (status === 'free') {
    startServer();
    for (let waited = 0; status !== 'dashboard' && waited < 10000; waited += 250) {
      await sleep(250);
      status = await probe();
      if (status === 'other') fail(`Port ${PORT} is in use by another program, not the dashboard.`);
    }
    if (status !== 'dashboard') fail(`The dashboard did not start on port ${PORT} within 10 seconds. See ${LOG_FILE}`);
  }

  console.log(`Agent Department dashboard: ${URL_BASE}`);
  openAppWindow(URL_BASE);
  process.exit(0);
}

// Run only when executed directly, so tests can import windowsBrowserCandidates.
// Compare real paths (case-insensitive on Windows) so symlinks or drive-letter case don't matter.
function isMain() {
  try {
    const norm = (p) => (process.platform === 'win32' ? p.toLowerCase() : p);
    return norm(fs.realpathSync(process.argv[1])) === norm(fs.realpathSync(fileURLToPath(import.meta.url)));
  } catch {
    return false;
  }
}
if (isMain()) await main();
