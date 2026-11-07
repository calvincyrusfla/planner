# Agent Department

A local dashboard for a department of 3 agent teams (Alpha, Bravo, Charlie), each with 5 builder agents. Use it to see which team is on which task and how far along each task is. You can also reassign teams or take them off a task.

No dependencies: it needs only Node 18 or newer.

## Start the dashboard

```bash
npm run dashboard
```

This opens http://127.0.0.1:4300. Use `node department/server.mjs --port 5000` to pick another port. The state is saved to `department/data/state.json`, which is git-ignored and survives restarts.

## Desktop shortcut

You can start the dashboard from a desktop icon instead of a terminal. The icon is a small network: a hub linked to nodes in the three team colors.

### Windows

You need Node 18 or newer, which includes npm. Open PowerShell or Command Prompt in the repo folder and run this once:

```bash
npm run shortcut
```

A shortcut named **Agent Department** with the network icon appears on your Desktop (also when your Desktop is in OneDrive). Double-click it. If the server isn't running yet, it starts in the background (its log goes to `department/data/server.log`). Then the dashboard opens in its own window, with no tabs or address bar and its own taskbar button. This uses Microsoft Edge's app mode, which every Windows 10 and 11 PC has. If Edge is missing, Chrome is used the same way. If neither is found, the dashboard opens in a normal tab of your default browser.

To pin it to the taskbar, right-click the shortcut and choose **Pin to taskbar** (on Windows 11, **Show more options** first).

Closing the window doesn't stop the server; it keeps running in the background. There is no stop button. To stop it without touching other Node programs, run this in PowerShell:

```powershell
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object CommandLine -like '*department*server.mjs*' | Invoke-CimMethod -Name Terminate
```

### macOS and Linux

Run `npm run shortcut` from the repo folder.

- **macOS:** creates `Agent Department.app` on `~/Desktop`. The app isn't signed, so the first time, right-click it, choose **Open**, then confirm.
- **Linux:** creates `agent-department.desktop` on your Desktop and in `~/.local/share/applications`, so it also shows up in your app menu.

To stop the background server:

```bash
pkill -f department/server.mjs
```

### All systems

Start the dashboard the same way from a terminal, without the shortcut:

```bash
npm start
```

To open it in a normal browser tab instead of its own window:

```bash
npm start -- --browser-tab
```

To use a specific browser, set `DEPT_BROWSER` to the path of its program file (for example `msedge.exe` or `chrome.exe`) before running `npm start`.

Remove the shortcut:

```bash
npm run shortcut:remove
```

The shortcut points at this folder and at the Node you ran it with. If you move the folder or switch Node versions, run `npm run shortcut` again.

The icons live in `department/assets/`. To regenerate them after editing `department/assets/icon.svg` (needs Playwright):

```bash
npm run icons
```

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
- The dashboard always uses a dark theme.

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
