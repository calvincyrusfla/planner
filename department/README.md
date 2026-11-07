# Agent Department

A local dashboard for a department of 3 agent teams (Alpha, Bravo, Charlie), each with 5 builder agents. Use it to see which team is on which task and how far along each task is. You can also reassign teams or take them off a task.

No dependencies: it needs only Node 18 or newer.

## Start the dashboard

```bash
npm run dashboard
```

This opens http://127.0.0.1:4300. Use `node department/server.mjs --port 5000` to pick another port. The state is saved to `department/data/state.json`, which is git-ignored and survives restarts.

## Assignment rules

- Every new task gets one free team.
- A **large** task gets **all three teams** when no other task is running.
- When every team is busy, the task waits in the queue and starts as soon as a team frees up (oldest first).
- When you remove a task's last team, the task is **paused**. A paused task isn't handed out automatically. Click **Queue** to put it back in line, or add a team yourself.
- Turn off **Auto-dispatch** to stop free teams from picking up queued tasks on their own.

## What the dashboard does

- **Teams:** one card per team, showing its task, progress, focus area and what each of its 5 builders is doing (idle, assigned, working, blocked or done). Use **Reassign to…** to move the team to another task, or **Remove from task** to free it.
- **Tasks:** each open task, with its progress bar and the teams on it. Click **×** on a team to remove it, or use **+ Add team…** to add one (a busy team is moved off its old task). You can also pause, queue, mark done, cancel or delete a task.
- **Activity:** a live feed of assignments, moves and progress notes from the agents.
- Updates stream in live while agents report progress.

## CLI

Agents report through the CLI, and you can use it too. It goes through the dashboard when the dashboard is running, and edits the state file directly when it isn't.

```bash
node department/dept.mjs status
node department/dept.mjs add "Build CSV export" --desc "Export tasks per tab" [--large]
node department/dept.mjs assign T-002 bravo --focus "API"
node department/dept.mjs unassign T-002 bravo
node department/dept.mjs progress T-002 --agent bravo-3 -m "Endpoint done"
node department/dept.mjs progress T-002 --team bravo --pct 60
node department/dept.mjs done T-002
node department/dept.mjs help
```

## How the agents use it

`CLAUDE.md` at the repo root sets the protocol for Claude sessions in this repo. Each task is registered here, staffed with the assigned team's 5 builders (`.claude/agents/builder.md`), and progress is reported back as the work happens.

## Tests

```bash
npm run test:dept
```
