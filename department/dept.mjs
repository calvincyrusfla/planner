#!/usr/bin/env node
// Command line for the agent department. Talks to the dashboard server when it
// is running (so the dashboard updates live) and edits the state file directly
// when it is not.
import * as core from './lib/core.mjs';
import fs from 'node:fs';
import { load, save, DATA_FILE } from './lib/store.mjs';
import { applyOp } from './lib/ops.mjs';
import { hubConfig, saveHubConfig, clearHubConfig, hubGet, hubOp, HUB_FILE } from './lib/hub.mjs';

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
  node department/dept.mjs set <task> [--status queued|paused|done|cancelled] [--pct N]
                       [--title "..."] [--size small|large]
  node department/dept.mjs done <task>                 Finish a task and free its teams
  node department/dept.mjs delete <task>
  node department/dept.mjs dispatch                    Hand free teams to queued tasks
  node department/dept.mjs auto on|off                 Toggle automatic dispatch
  node department/dept.mjs times [task]                Time per team (total, tasks done, average)
                                                       and finished tasks; or one task's breakdown
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

const countOpen = (s) => s.tasks.filter((t) => ['queued', 'active', 'paused'].includes(t.status)).length;

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
      const { result, state, rev } = await hubOp(cfg, { type: 'importState', args: { state: local } });
      console.log(`Pushed ${file} to ${cfg.url}: ${result.added} added, ${result.skipped} already there`);
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
    for (const a of team.agents) console.log(`  · ${a.id.padEnd(10)} ${a.status.padEnd(8)} ${a.work}`);
  }
  const open = s.tasks.filter((t) => ['queued', 'active', 'paused'].includes(t.status));
  console.log(`\nOpen tasks (${open.length})`);
  for (const t of open) {
    const teams = s.teams.filter((x) => x.taskId === t.id).map((x) => x.id).join(', ') || '—';
    console.log(`  ${t.id} [${t.status}] ${t.size === 'large' ? '(large) ' : ''}${t.title}\n        ${bar(t.progress)}  teams: ${teams}  active: ${dur(core.taskActiveMs(t, at))}`);
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
  const cols = ['Team', 'Total', 'Done', 'Avg/done', 'Current task'];
  const rows = core.teamStats(s, at).map((st) => [
    st.name,
    dur(st.totalMs),
    String(st.tasksDone),
    st.tasksDone ? dur(st.avgDoneMs) : '—',
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
    case 'done': {
      const r = await run('PATCH', `/api/tasks/${pos[0]}`, { status: 'done' }, { type: 'updateTask', args: { id: pos[0], patch: { status: 'done' } } });
      console.log(`${r.id} done`);
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
