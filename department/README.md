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
- **Time by team:** one card per team with its total time, tasks done, average time per done task and the task it's on now.
- The dashboard always uses a dark theme.

## Timers

Every task and team keeps time. The numbers tick live on the dashboard.

- **Active time:** how long a task has been active, meaning at least one team was on it. Time while it's queued or paused doesn't count.
- **Start to finish:** for a finished task, the time from when it was added to when it was done or cancelled, including any waiting.
- **Team time:** on a task, how long each team has spent on it. A team that was moved off and back again keeps adding to the same number.
- **Team totals:** in **Time by team**, each team's time across all tasks (deleted tasks too), how many done tasks it worked on, and its average time per done task.

Time only counts while a team is on the task. Removing a team, moving it, or pausing, finishing or cancelling the task stops its clock.

To see the same numbers in the terminal:

```bash
node department/dept.mjs times
```

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
node department/dept.mjs times
node department/dept.mjs help
```

## Shared board

By default each machine keeps its own board, so tasks Claude registers in a cloud session don't show up on your PC. The shared board fixes that: the board is stored on your Netlify site, and the dashboard on your PC and every Claude session read and write the same one.

### Setup

1. The board lives in a function that deploys with the Netlify site, so it works once this is on the branch Netlify deploys.
2. The passcode is the same `EDIT_KEY` the planner uses (see "One-time setup in Netlify" in the root `README.md`). Without it, nobody can read or change the board.
3. On your PC, open PowerShell in the repo folder and connect to your site:

   ```powershell
   node department/dept.mjs hub connect https://<your-site>.netlify.app --key <passcode>
   ```

4. Copy the tasks already on your PC to the shared board. Do this once; tasks that are already there are skipped:

   ```powershell
   node department/dept.mjs hub push
   ```

5. Restart the dashboard. Close its window, stop the server with the PowerShell command under "Desktop shortcut" above, then double-click the shortcut again.

The header now says **Live · Shared board**. Without a shared board it says **Live · This PC only**.

The connection is saved in `department/data/hub.json` (git-ignored). To check it:

```powershell
node department/dept.mjs hub status
```

To go back to a board on this PC only:

```powershell
node department/dept.mjs hub disconnect
```

### When the site can't be reached

The dashboard keeps showing the last copy it got, and the header says **Shared board offline · showing last copy** in yellow (hover over it for the reason). Changes are refused until the site is back, then it catches up on its own within a few seconds.

## How the agents use it

`CLAUDE.md` at the repo root sets the protocol for Claude sessions in this repo. Each task is registered here, staffed with the assigned team's 5 builders (`.claude/agents/builder.md`), and progress is reported back as the work happens.

## Tests

```bash
npm run test:dept
```
