// Opens a visible terminal on this computer running Claude Code in the repo folder,
// with a fixed prompt. Modelled on foundryLaunchSpec in ../launch.mjs. The command line
// is built only from the constant below, the repo root and the operator's own
// environment: nothing from a request may ever reach it.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

export const HELP_PROMPT = 'answer the dashboard help request';

// In order of preference, with the flag that makes each one run a command.
const TERMINALS = [
  ['x-terminal-emulator', ['-e']],
  ['gnome-terminal', ['--']],
  ['konsole', ['-e']],
  ['xterm', ['-e']],
];

const UNSAFE_BIN = 'unsafe claude path';
const unsafeBin = (p) => /["%^&|<>\r\n]/.test(p);
// cmd.exe would expand %VAR% or end the quoted path early at a ", so such paths are refused
// (same rule as launch.mjs).
const unsafeForCmd = (p) => /["%\r\n]/.test(p);
// A single quote would end the shell quoting below.
const unsafeForShell = (p) => /['\r\n]/.test(p);
const shQuote = (p) => `'${p}'`;
// A plain word stays as it is; anything else (spaces, $, ...) is quoted for the shell.
const shWord = (p) => (/^[\w@+=:,./-]+$/.test(p) ? p : shQuote(p));

function onPath(name) {
  for (const dir of String(process.env.PATH || '').split(path.delimiter).filter(Boolean)) {
    const file = path.join(dir, name);
    try {
      fs.accessSync(file, fs.constants.X_OK);
      if (fs.statSync(file).isFile()) return file;
    } catch {}
  }
  return null;
}

// Pure: how to open Claude on this platform, or {spec: null, reason}. `which` is injected for tests.
export function claudeLaunchSpec({ platform, repoRoot, env = {}, which = onPath }) {
  if (String(env.DEPT_CLAUDE_LAUNCH || '').toLowerCase() === 'off') return { spec: null, reason: 'disabled (DEPT_CLAUDE_LAUNCH=off)' };
  const bin = env.DEPT_CLAUDE_BIN || env.FOUNDRY_CLAUDE_BIN || 'claude';
  if (unsafeBin(bin)) return { spec: null, reason: UNSAFE_BIN };

  if (platform === 'win32') {
    if (unsafeForCmd(repoRoot)) return { spec: null, reason: `path not safe to quote for cmd.exe: ${repoRoot}` };
    const cmd = path.win32.join(env.SystemRoot || 'C:\\Windows', 'System32', 'cmd.exe');
    return {
      spec: {
        cmd,
        // The outer pair of quotes around the /k command is required. With more than two
        // quote characters, cmd /k removes the first and the last one (see cmd /?), so
        // `"claude" "prompt"` alone would run the broken command `claude" "prompt`.
        args: ['/d', '/s', '/c', 'start', '"Claude - help request"', '/d', `"${repoRoot}"`, 'cmd', '/k', `""${bin}" "${HELP_PROMPT}""`],
        // Not detached: that adds CREATE_NEW_PROCESS_GROUP, which disables Ctrl+C in the
        // new window, and the user must be able to stop Claude there. The `start`ed window
        // does not depend on this process anyway.
        opts: { cwd: repoRoot, windowsHide: true, windowsVerbatimArguments: true, stdio: 'ignore' },
      },
      reason: 'starting',
    };
  }

  if (unsafeForShell(bin)) return { spec: null, reason: UNSAFE_BIN };
  if (unsafeForShell(repoRoot)) return { spec: null, reason: `path not safe to quote for the shell: ${repoRoot}` };
  const command = `cd ${shQuote(repoRoot)} && ${shWord(bin)} "${HELP_PROMPT}"`;
  const opts = { cwd: repoRoot, detached: true, stdio: 'ignore' };

  if (platform === 'darwin') {
    const script = `tell application "Terminal" to do script "${command.replace(/[\\"]/g, '\\$&')}"`;
    return { spec: { cmd: 'osascript', args: ['-e', script], opts }, reason: 'starting' };
  }

  const terminal = TERMINALS.find(([name]) => which(name));
  if (!terminal) return { spec: null, reason: 'no terminal emulator found' };
  const [cmd, flag] = terminal;
  return { spec: { cmd, args: [...flag, 'bash', '-lc', `${command}; exec bash`], opts }, reason: 'starting' };
}

// Starts a spec from claudeLaunchSpec without waiting for it. Never throws.
export function launchClaude(spec, run = spawn) {
  try {
    const child = run(spec.cmd, spec.args, spec.opts);
    child.on('error', () => {});
    child.unref();
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err && typeof err.message === 'string' ? err.message : 'Could not start Claude' };
  }
}
