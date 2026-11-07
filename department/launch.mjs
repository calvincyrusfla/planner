#!/usr/bin/env node
// Desktop-shortcut launcher for the Agent Department dashboard. No dependencies.
//   node department/launch.mjs [--port 4300]
// Starts server.mjs in the background if it isn't already running, opens the
// dashboard in the default browser, then exits. Safe to run any number of times:
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

function openBrowser(url) {
  if (process.platform === 'darwin') detach('open', [url]);
  // Verbatim args so the empty `start` title stays "" instead of being escaped to \"\".
  else if (process.platform === 'win32') detach(winBin('cmd.exe'), ['/d', '/s', '/c', 'start', '""', url], { windowsVerbatimArguments: true });
  else detach('xdg-open', [url]);
}

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
openBrowser(URL_BASE);
process.exit(0);
