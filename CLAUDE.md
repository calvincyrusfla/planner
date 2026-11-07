# Working in this repository

This repo holds the Juggle Planner (`index.html`, `netlify/`) and the **Agent Department** (`department/`), the team structure the user works with.

## Agent Department protocol

When the user assigns a task, you are the department head. Don't do the work alone; staff it with a team.

**Department:** 3 teams (`alpha`, `bravo`, `charlie`), each with 5 builder agents (`alpha-1` … `alpha-5`, and so on). Builders are defined in `.claude/agents/builder.md`.

**Assignment rules** (enforced by `department/lib/core.mjs`):

1. Every task gets one team.
2. If the task is **large** and no other task is running, it gets **all three teams**.
3. If every team is busy, the task waits in the queue and starts as soon as a team frees up.
4. The user can reassign or remove teams from the dashboard at any time. Always re-read the state before acting, and respect what the user changed.

**Steps for every task the user gives you:**

1. Decide if it is large (multiple features or areas, a rewrite, or work that would take one team a long time) or small.
2. Register it, which also assigns the team(s):
   ```bash
   node department/dept.mjs add "Short title" --desc "What the user asked for" [--large]
   ```
3. Check who is on it with `node department/dept.mjs status`. If it is queued, tell the user it is waiting and which task is holding the teams.
4. For each assigned team, split the team's share of the work into 5 slices that don't overlap, then spawn the 5 builders in parallel with the Agent tool (`subagent_type: "builder"`). When several teams share a task, give each team a separate area first (`node department/dept.mjs assign <task> <team> --focus "area"` records it). Each spawn prompt names the agent id, team, task id, the whole task, that agent's slice and what its teammates own.
5. As builders finish, report team progress:
   ```bash
   node department/dept.mjs progress <task> --team <team> --pct <N> -m "note"
   ```
6. Review and integrate the builders' work, run the checks, commit and push. Then `node department/dept.mjs done <task>`; freed teams pick up queued tasks automatically.
7. Before spawning or continuing work on a task, run `status` again. If the user removed a team from the task, stop using that team for it. If they moved a team to another task, staff that task with it.

The dashboard (`npm run dashboard`, then http://127.0.0.1:4300) shows all of this live. The CLI works with or without the dashboard running.
