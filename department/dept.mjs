#!/usr/bin/env node
// Command line for the agent department. Talks to the dashboard server when it
// is running (so the dashboard updates live) and edits the state file directly
// when it is not.
import * as core from './lib/core.mjs';
import fs from 'node:fs';
import os from 'node:os';
import { load, save, DATA_FILE } from './lib/store.mjs';
import { applyOp } from './lib/ops.mjs';
import { hubConfig, saveHubConfig, clearHubConfig, hubGet, hubOp, HUB_FILE } from './lib/hub.mjs';

// Node's built-in fetch ignores HTTPS_PROXY unless NODE_USE_ENV_PROXY is set
// (Node 22.21+). Behind a proxy, as in Claude cloud sessions, re-run this
// command with it so the shared board is reachable. Local addresses stay
// direct through NO_PROXY. On a PC without a proxy nothing changes.
if ((process.env.HTTPS_PROXY || process.env.https_proxy) && !process.env.NODE_USE_ENV_PROXY) {
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, ['--disable-warning=UNDICI-EHPA', ...process.execArgv, ...process.argv.slice(1)], {
    stdio: 'inherit',
    env: { ...process.env, NODE_USE_ENV_PROXY: '1' },
  });
  process.exit(r.status ?? 1);
}

const BASE = process.env.DEPT_URL || `http://127.0.0.1:${process.env.PORT || 4300}`;

const HELP = `Agent Department CLI

  node department/dept.mjs status                      Teams, agents and open tasks
  node department/dept.mjs add "Title" [--large] [--desc "..."]
                                                       New task; auto-assigns a team
                                                       (all teams if large and nothing else runs)
  node department/dept.mjs assign <task> <team> [--focus "..."]
                                                       Put a team on a task (moves it off its old one)
  node department/dept.mjs unassign <task> <team>      Remove a team from a task
  node department/dept.mjs progress <task> [--team alpha] [--agent alpha-2]
                       [--pct 40] [--status working|blocked|done] [-m "note"]
                                                       Report progress
  node department/dept.mjs set <task> [--status queued|review|paused|done|cancelled] [--pct N]
                       [--title "..."] [--size small|large]
  node department/dept.mjs review <task>               Send a task to its teams' reviewers (In review)
  node department/dept.mjs verdict <task> --team <id> approve|changes [-m "note"] [--issues N]
                                                       Record a team reviewer's verdict; changes
                                                       sends the task back to the builders
  node department/dept.mjs done <task> [--force]       Finish a task and free its teams; needs every
                                                       team's reviewer sign-off unless --force
  node department/dept.mjs delete <task>
  node department/dept.mjs dispatch                    Hand free teams to queued tasks
  node department/dept.mjs auto on|off                 Toggle automatic dispatch
  node department/dept.mjs times [task]                Time per team (total, tasks done, average)
                                                       and finished tasks; or one task's breakdown
  node department/dept.mjs usage                       Claude usage meter: 5-hour and weekly limits,
                                                       when they reset, age and source of the report
  node department/dept.mjs usage report [--five <pct>] [--five-reset <iso|epoch>]
                       [--week <pct>] [--week-reset <iso|epoch>] [--source X]
                                                       Store a usage report by hand (testing); the
                                                       status line (npm run usage-meter) does this
  node department/dept.mjs json                        Dump the full state
  node department/dept.mjs reset                       Wipe all tasks and reset the teams

Shared board (one board for this PC and Claude sessions, stored on the Netlify site):
  node department/dept.mjs hub connect <https://site> [--key K]
                                                       Use the shared board (key also from DEPT_HUB_KEY)
  node department/dept.mjs hub push [file]             Copy this machine's board into the shared one
  node department/dept.mjs hub status                  Mode, URL, rev, task count, reachable or not
  node department/dept.mjs hub disconnect              Back to this machine's board only
  Env DEPT_HUB_URL (+ DEPT_HUB_KEY) selects a shared board without hub connect.
`;

function parse(argv) {
  const pos = [];
  const opt = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-m') opt.message = argv[++i];
    else if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--') || next === '-m') opt[key] = true;
      else opt[key] = argv[++i];
    } else pos.push(a);
  }
  return { pos, opt };
}

async function viaServer(method, path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: { 'content-type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(3000),
    });
  } catch {
    return { offline: true };
  }
  let json;
  try {
    json = await res.json();
  } catch {
    throw new core.DeptError(`Unexpected answer from the dashboard server at ${BASE} (HTTP ${res.status})`);
  }
  if (!json.ok) throw new core.DeptError(json.error, res.status);
  return { result: json.result };
}

// Transport order: the local dashboard server if it is up (it forwards to the
// shared board in hub mode), else the shared board if one is configured, else
// the local state file. `op` is {type, args} as understood by lib/ops.mjs.
async function run(method, path, body, op) {
  const r = await viaServer(method, path, body);
  if (!r.offline) return r.result;
  if (typeof op === 'function') op = { local: op }; // older call style: run(..., (state) => ...)
  const cfg = op.type ? hubConfig() : null;
  if (cfg) return (await hubOp(cfg, op)).result;
  const state = load();
  const result = op.local ? op.local(state) : applyOp(state, op);
  save(state);
  return result;
}

async function currentState() {
  const r = await viaServer('GET', '/api/state');
  if (!r.offline) return r.result;
  const cfg = hubConfig();
  if (cfg) return (await hubGet(cfg)).state;
  return load();
}

const OPEN = core.TASK_STATUSES.filter((st) => st !== 'done' && st !== 'cancelled');
const countOpen = (s) => s.tasks.filter((t) => OPEN.includes(t.status)).length;

// Per-team review state for the current round. Uses core.reviewStatus when the
// core has it; otherwise reads task.reviews directly (same contract).
function reviewState(s, task) {
  if (typeof core.reviewStatus === 'function') return core.reviewStatus(s, task);
  const round = task.reviewRound || 0;
  const out = { round, approved: [], changes: [], pending: [] };
  for (const team of s.teams.filter((x) => x.taskId === task.id)) {
    const r = (task.reviews || {})[team.id];
    if (r && r.round === round && r.verdict === 'approved') out.approved.push(team.id);
    else if (r && r.round === round && r.verdict === 'changes') out.changes.push(team.id);
    else out.pending.push(team.id);
  }
  return out;
}

function reviewLine(s, task) {
  const rs = reviewState(s, task);
  const mark = (id) => (rs.approved.includes(id) ? `${id} ✓` : rs.changes.includes(id) ? `${id} ✗` : `${id} …`);
  const ids = s.teams.filter((x) => x.taskId === task.id).map((x) => x.id);
  return `in review (round ${rs.round}): ${ids.map(mark).join(', ') || '—'}`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// Would marking this task done skip missing reviewer sign-off? Uses
// core.reviewGate when present; otherwise: every team on it approved this round.
function signOffMissing(s, task) {
  if (typeof core.reviewGate === 'function') return !core.reviewGate(s, task).ok;
  const on = s.teams.filter((x) => x.taskId === task.id);
  if (!on.length) return false;
  const rs = reviewState(s, task);
  return rs.approved.length < on.length;
}

// ---------------------------------------------------------------- usage meter

const USAGE_STALE_MS = 15 * 60 * 1000;
const USAGE_HELP = 'Usage: usage report [--five <pct>] [--five-reset <iso|epoch>] [--week <pct>] [--week-reset <iso|epoch>] [--source X]';

function usageWindowArg(opt, pctKey, resetKey) {
  const pct = opt[pctKey];
  const reset = opt[resetKey];
  if (pct === undefined) {
    if (reset !== undefined) throw new core.DeptError(`--${resetKey} needs --${pctKey} <pct> too`);
    return null;
  }
  if (typeof pct !== 'string' || pct.trim() === '' || !Number.isFinite(Number(pct))) {
    throw new core.DeptError(`--${pctKey} must be a percentage (0-100)`);
  }
  if (reset !== undefined && typeof reset !== 'string') throw new core.DeptError(`--${resetKey} needs a time (ISO or epoch seconds)`);
  return { usedPct: Number(pct), resetsAt: reset ?? null };
}

function clockTime(ms, at) {
  const d = new Date(ms);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (Math.abs(ms - at) < 20 * 3600 * 1000 && d.getDate() === new Date(at).getDate()) return time;
  return `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
}

function usageRow(label, win, at) {
  if (!win) return `  ${label.padEnd(9)} no data`;
  const reset = ts(win.resetsAt);
  if (Number.isFinite(reset) && reset <= at) {
    return `  ${label.padEnd(9)} reset at ${clockTime(reset, at)}; will update on next report (was ${win.usedPct}%)`;
  }
  const pct = Number(win.usedPct) || 0;
  const level = pct >= 90 ? '  HIGH' : pct >= 70 ? '  getting high' : '';
  const when = Number.isFinite(reset) ? `  resets in ${dur(reset - at)} (${clockTime(reset, at)})` : '';
  const filled = Math.max(0, Math.min(20, Math.round((pct / 100) * 20)));
  return `  ${label.padEnd(9)} ${'█'.repeat(filled)}${'░'.repeat(20 - filled)} ${`${pct}%`.padStart(6)}${when}${level}`;
}

function printUsage(s) {
  const u = s.usage;
  if (!u || (!u.fiveHour && !u.sevenDay)) {
    console.log('Usage meter not connected: run `npm run usage-meter` on your PC, then restart Claude Code.');
    return;
  }
  const at = core.clockNow();
  console.log('Claude usage');
  console.log(usageRow('5-hour', u.fiveHour, at));
  console.log(usageRow('Weekly', u.sevenDay, at));
  const reported = ts(u.reportedAt);
  const age = Number.isFinite(reported) ? Math.max(0, at - reported) : NaN;
  const from = u.source ? ` from ${u.source}` : '';
  console.log(`  Updated ${Number.isFinite(age) ? `${dur(age)} ago` : 'at an unknown time'}${from}`);
  if (!Number.isFinite(age) || age > USAGE_STALE_MS) {
    console.log('  No Claude Code session is reporting; numbers may be out of date.');
  }
}

async function usageCommand(sub, opt) {
  if (sub === undefined || sub === 'show' || sub === 'status') return printUsage(await currentState());
  if (sub !== 'report') throw new core.DeptError(`Unknown usage command ${sub}; use \`usage\` or \`usage report ...\``);
  const fiveHour = usageWindowArg(opt, 'five', 'five-reset');
  const sevenDay = usageWindowArg(opt, 'week', 'week-reset');
  if (!fiveHour && !sevenDay) throw new core.DeptError(USAGE_HELP);
  const source = typeof opt.source === 'string' ? opt.source : os.hostname();
  const args = { fiveHour, sevenDay, reportedAt: new Date(core.clockNow()).toISOString(), source };
  const r = await run('POST', '/api/op', { type: 'reportUsage', args }, { type: 'reportUsage', args });
  if (r && r.stored === false) console.log('Not stored: the board already has a newer usage report.');
  else console.log('Usage report stored.');
  const at = core.clockNow();
  const u = r?.usage;
  if (u) {
    console.log(usageRow('5-hour', u.fiveHour, at));
    console.log(usageRow('Weekly', u.sevenDay, at));
  }
}

const VERDICTS = { approve: 'approved', approved: 'approved', ok: 'approved', changes: 'changes', change: 'changes', reject: 'changes' };

async function hubCommand(sub, pos, opt) {
  switch (sub) {
    case 'connect': {
      const url = pos[0];
      if (!url || !/^https?:\/\//.test(url)) throw new core.DeptError('Usage: hub connect https://<site>.netlify.app [--key <passcode>]');
      const key = typeof opt.key === 'string' ? opt.key : process.env.DEPT_HUB_KEY || '';
      const cfg = { url: url.replace(/\/+$/, ''), key };
      const { state, rev } = await hubGet(cfg); // verify before saving
      saveHubConfig(cfg);
      console.log(`Connected to the shared board at ${cfg.url} (saved in ${HUB_FILE})`);
      console.log(`  rev ${rev}, ${state.tasks.length} tasks (${countOpen(state)} open)`);
      const local = load();
      if (local.tasks.length) {
        const backup = DATA_FILE.replace(/\.json$/, '') + '.before-hub.json';
        if (!fs.existsSync(backup)) fs.copyFileSync(DATA_FILE, backup);
        console.log(`  This PC's board has ${local.tasks.length} tasks (backup: ${backup}).`);
        console.log('  Run `node department/dept.mjs hub push` once to copy them to the shared board.');
      }
      if (process.env.DEPT_HUB_URL) console.log('  Note: DEPT_HUB_URL is set in the environment and takes precedence over the saved config.');
      return;
    }
    case 'push': {
      const cfg = hubConfig();
      if (!cfg) throw new core.DeptError('No shared board configured; run `hub connect <url> --key <passcode>` first');
      const file = pos[0] || DATA_FILE;
      if (!fs.existsSync(file)) throw new core.DeptError(`Nothing to push: ${file} does not exist`);
      const local = load(file);
      // importState skips a task only if id AND createdAt match, but it renumbers
      // what it adds, so a second push would duplicate. Drop tasks the hub already
      // has under another id (same createdAt + title), and their log entries.
      const remote = (await hubGet(cfg)).state;
      const there = new Set(remote.tasks.map((t) => `${t.createdAt}\u0000${t.title}`));
      const sameId = new Set(remote.tasks.map((t) => `${t.id}\u0000${t.createdAt}`));
      const dropped = new Set(local.tasks.filter((t) => there.has(`${t.createdAt}\u0000${t.title}`) && !sameId.has(`${t.id}\u0000${t.createdAt}`)).map((t) => t.id));
      if (dropped.size) {
        local.tasks = local.tasks.filter((t) => !dropped.has(t.id));
        local.log = (local.log || []).filter((e) => !dropped.has(e.taskId));
        for (const team of local.teams || []) if (dropped.has(team.taskId)) team.taskId = null;
      }
      const { result, state, rev } = await hubOp(cfg, { type: 'importState', args: { state: local } });
      console.log(`Pushed ${file} to ${cfg.url}: ${result.added} added, ${result.skipped + dropped.size} already there`);
      for (const [from, to] of Object.entries(result.idMap || {})) if (from !== to) console.log(`  ${from} → ${to}`);
      console.log(`Shared board now: rev ${rev}, ${state.tasks.length} tasks (${countOpen(state)} open)`);
      return;
    }
    case 'status':
    case undefined: {
      const cfg = hubConfig();
      if (!cfg) {
        console.log(`Mode: local (this machine only, ${DATA_FILE})`);
        console.log('No shared board configured. Connect with `hub connect https://<site>.netlify.app --key <passcode>` or set DEPT_HUB_URL.');
        return;
      }
      console.log(`Mode: shared board (${cfg.source === 'env' ? 'from DEPT_HUB_URL' : `from ${HUB_FILE}`})`);
      console.log(`URL:  ${cfg.url}`);
      console.log(`Key:  ${cfg.key ? 'set' : "empty (sends 'proxy'; for Claude cloud sessions)"}`);
      try {
        const { state, rev } = await hubGet(cfg);
        console.log(`Reachable: yes · rev ${rev} · ${state.tasks.length} tasks (${countOpen(state)} open)`);
      } catch (err) {
        console.log(`Reachable: no · ${err.message}`);
        process.exitCode = 1;
      }
      return;
    }
    case 'disconnect': {
      const removed = clearHubConfig();
      console.log(removed ? `Disconnected; removed ${HUB_FILE}. Commands now use this machine's board.` : 'No saved shared board config.');
      if (process.env.DEPT_HUB_URL) console.log('Note: DEPT_HUB_URL is still set in the environment, so the shared board is still used.');
      return;
    }
    default:
      throw new core.DeptError(`Unknown hub command ${sub}; use connect, push, status or disconnect`);
  }
}

function bar(pct, width = 20) {
  const n = Math.round((pct / 100) * width);
  return '█'.repeat(n) + '░'.repeat(width - n) + ` ${String(pct).padStart(3)}%`;
}

const dur = (ms) => core.formatDuration(ms || 0);
const ts = (iso) => (iso ? Date.parse(iso) : NaN);

function printStatus(s) {
  const at = core.clockNow();
  console.log(`Auto-dispatch: ${s.settings.autoDispatch ? 'on' : 'off'}\n`);
  for (const team of s.teams) {
    const task = team.taskId ? s.tasks.find((t) => t.id === team.taskId) : null;
    console.log(`${team.name.padEnd(13)} ${task ? `${task.id} ${task.title}` : '(free)'}${team.focus ? ` — focus: ${team.focus}` : ''}`);
    const total = `total ${dur(core.teamTotalMs(team, at))}`;
    if (task) console.log(`  ${bar(team.progress)}  on task ${dur(core.taskTeamMs(s, task, at)[team.id])} · ${total}`);
    else console.log(`  ${total}`);
    for (const a of team.agents) {
      if (a.role === 'reviewer') console.log(`  ★ ${`${a.id} reviewer`.padEnd(19)} ${a.status.padEnd(8)} ${a.work}`);
      else console.log(`  · ${a.id.padEnd(19)} ${a.status.padEnd(8)} ${a.work}`);
    }
    if (team.issuesFound) console.log(`  ${plural(team.issuesFound, 'issue')} caught by review`);
  }
  const open = s.tasks.filter((t) => OPEN.includes(t.status));
  console.log(`\nOpen tasks (${open.length})`);
  for (const t of open) {
    const teams = s.teams.filter((x) => x.taskId === t.id).map((x) => x.id).join(', ') || '—';
    console.log(`  ${t.id} [${t.status}] ${t.size === 'large' ? '(large) ' : ''}${t.title}\n        ${bar(t.progress)}  teams: ${teams}  active: ${dur(core.taskActiveMs(t, at))}`);
    if (t.status === 'review') console.log(`        ${reviewLine(s, t)}`);
    else if (t.reviewRound) {
      const back = Object.entries(t.reviews || {}).filter(([, r]) => r.round === t.reviewRound && r.verdict === 'changes').map(([id]) => id);
      console.log(`        review round ${t.reviewRound}${back.length ? `: changes requested by ${back.join(', ')}` : ''}`);
    }
    if (t.issuesFound) console.log(`        ${plural(t.issuesFound, 'issue')} caught`);
  }
}

function teamBreakdown(s, task, at) {
  const per = core.taskTeamMs(s, task, at);
  const parts = Object.entries(per)
    .filter(([, ms]) => ms > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([id, ms]) => `${id} ${dur(ms)}`);
  return parts.join(', ') || '—';
}

function wallMs(task, at) {
  const start = ts(task.createdAt);
  const end = task.finishedAt ? ts(task.finishedAt) : at;
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : 0;
}

function printTask(s, task, at) {
  console.log(`${task.id} [${task.status}] ${task.size === 'large' ? '(large) ' : ''}${task.title}`);
  console.log(`  active time  ${dur(core.taskActiveMs(task, at))}${task.activeSince ? ' (running)' : ''}`);
  console.log(`  wall time    ${dur(wallMs(task, at))}${task.finishedAt ? '' : ' (so far)'}`);
  console.log(`  created      ${task.createdAt || '—'}`);
  if (task.finishedAt) console.log(`  finished     ${task.finishedAt}`);
  if (task.reviewRound) {
    console.log(`  review       round ${task.reviewRound}, ${plural(task.issuesFound || 0, 'issue')} caught`);
    for (const [id, r] of Object.entries(task.reviews || {})) {
      console.log(`    ${id.padEnd(11)} ${r.verdict === 'approved' ? '✓ approved' : '✗ changes'} (round ${r.round}${r.issues ? `, ${plural(r.issues, 'issue')}` : ''})${r.note ? ` — ${r.note}` : ''}`);
    }
  }
  const per = core.taskTeamMs(s, task, at);
  const rows = Object.entries(per).filter(([, ms]) => ms > 0).sort((a, b) => b[1] - a[1]);
  console.log('  team time');
  if (!rows.length) console.log('    —');
  for (const [id, ms] of rows) {
    const team = s.teams.find((x) => x.id === id);
    const on = team && team.taskId === task.id ? ' (on it now)' : '';
    console.log(`    ${(team ? team.name : id).padEnd(13)} ${dur(ms)}${on}`);
  }
}

function printTimes(s, taskId) {
  const at = core.clockNow();
  if (taskId) {
    return printTask(s, core.getTask(s, taskId), at);
  }
  const cols = ['Team', 'Total', 'Done', 'Avg/done', 'Issues', 'Current task'];
  const rows = core.teamStats(s, at).map((st) => [
    st.name,
    dur(st.totalMs),
    String(st.tasksDone),
    st.tasksDone ? dur(st.avgDoneMs) : '—',
    String(st.issuesFound ?? s.teams.find((x) => x.id === st.id)?.issuesFound ?? 0),
    st.currentTaskId ? `${st.currentTaskId} (${dur(st.currentMs)})` : '(free)',
  ]);
  const widths = cols.map((c, i) => Math.max(c.length, ...rows.map((r) => r[i].length)));
  const line = (r) => '  ' + r.map((v, i) => v.padEnd(widths[i])).join('  ').trimEnd();
  console.log('Time by team');
  console.log(line(cols));
  console.log(line(widths.map((w) => '─'.repeat(w))));
  for (const r of rows) console.log(line(r));
  const done = s.tasks
    .filter((t) => t.status === 'done')
    .sort((a, b) => (ts(b.finishedAt) || 0) - (ts(a.finishedAt) || 0));
  console.log(`\nFinished tasks (${done.length})`);
  for (const t of done) {
    console.log(`  ${t.id} ${t.title}`);
    console.log(`        active ${dur(core.taskActiveMs(t, at))} · wall ${dur(wallMs(t, at))} · teams: ${teamBreakdown(s, t, at)}`);
  }
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const { pos, opt } = parse(rest);
  switch (cmd) {
    case undefined:
    case 'help':
    case '--help':
      return console.log(HELP);
    case 'status':
      return printStatus(await currentState());
    case 'times':
      return printTimes(await currentState(), pos[0]);
    case 'hub':
      return hubCommand(pos[0], pos.slice(1), opt);
    case 'usage':
      return usageCommand(pos[0], opt);
    case 'json':
      return console.log(JSON.stringify(await currentState(), null, 2));
    case 'add': {
      const body = { title: pos.join(' '), description: opt.desc || '', size: opt.large ? 'large' : opt.size || 'small' };
      const r = await run('POST', '/api/tasks', body, { type: 'addTask', args: body });
      console.log(`${r.task.id} created → ${r.assigned.length ? r.assigned.join(', ') : 'queued (no free team)'}`);
      return;
    }
    case 'assign': {
      const [task, teamId] = pos;
      const body = { teamId, focus: opt.focus || '' };
      const r = await run('POST', `/api/tasks/${task}/assign`, body, { type: 'assignTeam', args: { taskId: task, ...body } });
      console.log(`${r.team.name} → ${r.task.id}${r.previous && r.previous !== r.task.id ? ` (moved from ${r.previous})` : ''}`);
      return;
    }
    case 'unassign': {
      const [task, teamId] = pos;
      const r = await run('POST', `/api/tasks/${task}/unassign`, { teamId }, { type: 'unassignTeam', args: { taskId: task, teamId } });
      console.log(`${r.team.name} removed from ${r.task.id} (now ${r.task.status})`);
      return;
    }
    case 'progress': {
      const body = {
        taskId: pos[0],
        teamId: opt.team,
        agentId: opt.agent,
        progress: opt.pct,
        status: opt.status,
        work: opt.work,
        message: opt.message || '',
      };
      const r = await run('POST', '/api/progress', body, { type: 'reportProgress', args: body });
      console.log(`${r.task.id} at ${r.task.progress}%`);
      return;
    }
    case 'set': {
      const body = {};
      if (opt.status) body.status = opt.status;
      if (opt.pct !== undefined) body.progress = opt.pct;
      if (opt.title) body.title = opt.title;
      if (opt.size) body.size = opt.size;
      if (opt.desc) body.description = opt.desc;
      const r = await run('PATCH', `/api/tasks/${pos[0]}`, body, { type: 'updateTask', args: { id: pos[0], patch: body } });
      console.log(`${r.id} [${r.status}] ${r.progress}%`);
      return;
    }
    case 'review': {
      const id = pos[0];
      if (!id) throw new core.DeptError('Usage: review <task>');
      const patch = { status: 'review' };
      const r = await run('POST', '/api/op', { type: 'updateTask', args: { id, patch, by: 'cli' } }, { type: 'updateTask', args: { id, patch, by: 'cli' } });
      console.log(`${r.id} in review (round ${r.reviewRound ?? '?'})`);
      return;
    }
    case 'verdict': {
      const [taskId, word] = pos;
      const verdict = VERDICTS[String(word || (opt.approve ? 'approve' : opt.changes ? 'changes' : '')).toLowerCase()];
      if (!taskId || typeof opt.team !== 'string' || !verdict) {
        throw new core.DeptError('Usage: verdict <task> --team <id> approve|changes [-m "note"] [--issues N]');
      }
      let issues = 0;
      if (opt.issues !== undefined) {
        if (typeof opt.issues !== 'string') throw new core.DeptError('Usage: verdict <task> --team <id> approve|changes [-m "note"] [--issues N]');
        issues = Number(opt.issues);
        if (!Number.isInteger(issues) || issues < 0) throw new core.DeptError('--issues must be a whole number, 0 or more');
      }
      const args = { taskId, teamId: opt.team, verdict, note: typeof opt.message === 'string' ? opt.message : '', issues, by: typeof opt.by === 'string' ? opt.by : 'reviewer' };
      const r = await run('POST', '/api/op', { type: 'reviewTask', args }, { type: 'reviewTask', args });
      const id = r.task?.id || taskId;
      const name = r.team?.name || opt.team;
      console.log(`${name} ${verdict === 'changes' ? 'requested changes on' : 'approved'} ${id}${issues ? ` (${plural(issues, 'issue')})` : ''}`);
      const pending = r.pending || [];
      const changes = Array.isArray(r.changes) ? r.changes : [];
      const complete = typeof r.complete === 'boolean' ? r.complete : !pending.length;
      if (!complete) console.log(`  Waiting for: ${pending.join(', ') || '—'}`);
      else if (changes.length) console.log(`  Back to the builders: changes requested by ${changes.join(', ')} (${id} now ${r.task?.status || 'active'})`);
      else console.log(`  Signed off by all reviewers; ${id} is ready for done`);
      return;
    }
    case 'done': {
      // `done --force T-1` would read T-1 as the flag's value; take it back.
      if (typeof opt.force === 'string') pos.unshift(opt.force);
      const id = pos[0];
      if (!id) throw new core.DeptError('Usage: done <task> [--force]');
      const patch = opt.force ? { status: 'done', force: true } : { status: 'done' };
      let skipped = false;
      if (opt.force) {
        try {
          const s = await currentState();
          const task = s.tasks.find((t) => t.id === id);
          skipped = Boolean(task && signOffMissing(s, task));
        } catch {
          // Can't tell beforehand; the done itself reports any real problem.
        }
      }
      const r = await run('PATCH', `/api/tasks/${id}`, patch, { type: 'updateTask', args: { id, patch } });
      console.log(`${r.id} done${skipped ? ' (forced, without reviewer sign-off)' : ''}`);
      return;
    }
    case 'delete': {
      const r = await run('DELETE', `/api/tasks/${pos[0]}`, null, { type: 'deleteTask', args: { id: pos[0] } });
      console.log(`${r.id} deleted`);
      return;
    }
    case 'dispatch': {
      const r = await run('POST', '/api/dispatch', {}, { type: 'dispatch', args: {} });
      console.log(r.length ? r.map((a) => `${a.teamId} → ${a.taskId}`).join('\n') : 'Nothing to dispatch');
      return;
    }
    case 'auto': {
      const body = { autoDispatch: pos[0] !== 'off' };
      const r = await run('PATCH', '/api/settings', body, { type: 'updateSettings', args: body });
      console.log(`Auto-dispatch ${r.autoDispatch ? 'on' : 'off'}`);
      return;
    }
    case 'reset': {
      await run('POST', '/api/reset', {}, { type: 'reset', args: {} });
      console.log('Department reset');
      return;
    }
    default:
      console.error(`Unknown command ${cmd}\n`);
      console.log(HELP);
      process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exitCode = 1;
});
