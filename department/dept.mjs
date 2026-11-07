#!/usr/bin/env node
// Command line for the agent department. Talks to the dashboard server when it
// is running (so the dashboard updates live) and edits the state file directly
// when it is not.
import * as core from './lib/core.mjs';
import { load, save } from './lib/store.mjs';

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
  node department/dept.mjs json                        Dump the full state
  node department/dept.mjs reset                       Wipe all tasks and reset the teams
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
  const json = await res.json();
  if (!json.ok) throw new core.DeptError(json.error);
  return { result: json.result };
}

// Runs against the server if it is up, otherwise against the file.
async function run(method, path, body, local) {
  const r = await viaServer(method, path, body);
  if (!r.offline) return r.result;
  const state = load();
  const result = local(state);
  save(state);
  return result;
}

async function currentState() {
  const r = await viaServer('GET', '/api/state');
  return r.offline ? load() : r.result;
}

function bar(pct, width = 20) {
  const n = Math.round((pct / 100) * width);
  return '█'.repeat(n) + '░'.repeat(width - n) + ` ${String(pct).padStart(3)}%`;
}

function printStatus(s) {
  console.log(`Auto-dispatch: ${s.settings.autoDispatch ? 'on' : 'off'}\n`);
  for (const team of s.teams) {
    const task = team.taskId ? s.tasks.find((t) => t.id === team.taskId) : null;
    console.log(`${team.name.padEnd(13)} ${task ? `${task.id} ${task.title}` : '(free)'}${team.focus ? ` — focus: ${team.focus}` : ''}`);
    if (task) console.log(`  ${bar(team.progress)}`);
    for (const a of team.agents) console.log(`  · ${a.id.padEnd(10)} ${a.status.padEnd(8)} ${a.work}`);
  }
  const open = s.tasks.filter((t) => ['queued', 'active', 'paused'].includes(t.status));
  console.log(`\nOpen tasks (${open.length})`);
  for (const t of open) {
    const teams = s.teams.filter((x) => x.taskId === t.id).map((x) => x.id).join(', ') || '—';
    console.log(`  ${t.id} [${t.status}] ${t.size === 'large' ? '(large) ' : ''}${t.title}\n        ${bar(t.progress)}  teams: ${teams}`);
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
    case 'json':
      return console.log(JSON.stringify(await currentState(), null, 2));
    case 'add': {
      const body = { title: pos.join(' '), description: opt.desc || '', size: opt.large ? 'large' : opt.size || 'small' };
      const r = await run('POST', '/api/tasks', body, (s) => core.addTask(s, body));
      console.log(`${r.task.id} created → ${r.assigned.length ? r.assigned.join(', ') : 'queued (no free team)'}`);
      return;
    }
    case 'assign': {
      const [task, teamId] = pos;
      const body = { teamId, focus: opt.focus || '' };
      const r = await run('POST', `/api/tasks/${task}/assign`, body, (s) => core.assignTeam(s, task, teamId, body));
      console.log(`${r.team.name} → ${r.task.id}${r.previous && r.previous !== r.task.id ? ` (moved from ${r.previous})` : ''}`);
      return;
    }
    case 'unassign': {
      const [task, teamId] = pos;
      const r = await run('POST', `/api/tasks/${task}/unassign`, { teamId }, (s) => core.unassignTeam(s, task, teamId));
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
      const r = await run('POST', '/api/progress', body, (s) => core.reportProgress(s, body));
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
      const r = await run('PATCH', `/api/tasks/${pos[0]}`, body, (s) => core.updateTask(s, pos[0], body));
      console.log(`${r.id} [${r.status}] ${r.progress}%`);
      return;
    }
    case 'done': {
      const r = await run('PATCH', `/api/tasks/${pos[0]}`, { status: 'done' }, (s) => core.updateTask(s, pos[0], { status: 'done' }));
      console.log(`${r.id} done`);
      return;
    }
    case 'delete': {
      const r = await run('DELETE', `/api/tasks/${pos[0]}`, null, (s) => core.deleteTask(s, pos[0]));
      console.log(`${r.id} deleted`);
      return;
    }
    case 'dispatch': {
      const r = await run('POST', '/api/dispatch', {}, (s) => core.dispatch(s));
      console.log(r.length ? r.map((a) => `${a.teamId} → ${a.taskId}`).join('\n') : 'Nothing to dispatch');
      return;
    }
    case 'auto': {
      const body = { autoDispatch: pos[0] !== 'off' };
      const r = await run('PATCH', '/api/settings', body, (s) => core.updateSettings(s, body));
      console.log(`Auto-dispatch ${r.autoDispatch ? 'on' : 'off'}`);
      return;
    }
    case 'reset': {
      await run('POST', '/api/reset', {}, (s) => Object.assign(s, core.createInitialState()));
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
