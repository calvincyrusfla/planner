# Foundry

Foundry is a Python "factory" that runs coding work for the Agent Department.

- The **Floor Manager** (`floor_manager.py`) is the planning tier. It takes work orders, has the Shop Foreman size them, queues one ticket per allocated builder, carries out escalations and drafts the PR text when a task is finished. It makes no polling Claude calls.
- The **Shop Foreman** (`shop_foreman.py`) is a local **Gemma 4** model served by **Ollama** (OpenAI-compatible API at `http://localhost:11434/v1`). It sizes tasks (S = 1 builder, M = 2, L/XL = 5), prunes context, condenses tracebacks into one-line fix hints, drafts commit/PR text, answers questions through read-only tools and raises structured `ForemanEscalation`s. Every Foreman hook has a deterministic fallback, so the factory keeps working with Gemma offline.
- **Builders** (up to 5 per team) and **reviewers** (1 per team, `<team>-6`) are **Claude**. They run as headless Claude Code (`claude -p --output-format json`) on your PC under your Claude subscription. Foundry uses no API keys anywhere.
- **Deterministic gates** (`validation_gates.py`: syntax, lint, tests, schema) run before any reviewer. A failure skips the reviewer and sends the builder a one-line fix hint. A task gets at most 2 retries, then it is quarantined.
- Underneath: a SQLite (WAL) queue with crash resume (`orchestrator_queue.py`), a token-bucket rate limiter with jittered backoff (`rate_limiter.py`), telemetry (`telemetry.py`) and an exact-match solution cache (`solution_cache.py`).

Foundry **feeds the existing Agent Department dashboard**; it does not replace it (see [Board bridge](#how-it-feeds-the-agent-department-dashboard)).

## Phase 1 vs phase 2

| | Phase 1 (this code) | Phase 2 (next) |
|---|---|---|
| Claude | `SimulatedClaudeClient` (scripted, counts calls per agent) | `ClaudeCodeClient` runs headless Claude Code on Windows |
| Gemma | `SimulatedGemmaClient` (scripted) | `OllamaGemmaClient` talks to a real Gemma 4 in Ollama |
| Real clients | exist, unit-tested against a fake `claude` executable and a fake HTTP server | used for real |
| Dashboard | ops posted to `/api/op` (dry mode in tests and the demo) | plus a Q&A box wired to `ShopForeman.answer_question` |
| Solution cache | exact match only | fuzzy/semantic matching |

Every phase-1 test and the demo run with simulated Claude and Gemma. `--real` exists but **has not been run against real Claude Code or Ollama yet**.

## Install

Run every command in this README **from the repository root** (the folder that holds `foundry\` and `department\`).

Windows (PowerShell or cmd):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r foundry\requirements.txt
.venv\Scripts\python -m pip install -r foundry\requirements-dev.txt   # only needed to run the tests
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r foundry/requirements-dev.txt
```

You need Python 3.11+. The only runtime dependency is `pydantic>=2.7`; everything else uses the standard library.

## Run the simulated demo

```powershell
.venv\Scripts\python -m foundry.main --simulate
```

```bash
.venv/bin/python -m foundry.main --simulate
```

The demo takes well under a second and needs no network, Claude or Ollama. It prints:

1. **sizing**: three work orders become S (1 builder, team alpha), M (2, bravo) and L (5, charlie);
2. **selective dispatch**: Claude calls per builder slot. Idle slots show `0` because a slot above the allocation never gets a ticket;
3. **gates**: `bravo-1`'s first diff has a syntax error. You see the one-line hint (`[syntax] shop/strings.py:16: ... '(' was never closed`) and the retry prompt that carries it;
4. **review**: the diff-only reviewer verdicts;
5. **escalation**: the Foreman's line check (here a `TOKEN_VELOCITY_SPIKE`) and the Floor Manager's action (`THROTTLE_DISPATCH` on the rate limiter);
6. **PR summaries**: commit messages and a PR body drafted by the Foreman, plus the `.patch` file written for each task;
7. **telemetry and board**: call counts by role, tokens saved and the board ops it would have sent.

Options:

- `--workdir DIR` keeps the demo repo and `foundry.db` instead of using a temp dir.
- `--json` prints a machine-readable summary at the end.
- `--board auto` sends the demo's ops to your local dashboard. The default, `dry`, only records them.

Time in the demo is simulated. The queue, telemetry and limiter share a virtual clock, so rate-limit waits advance it instead of sleeping.

## Tests

```powershell
.venv\Scripts\python -m pytest foundry\tests -q
```

```bash
.venv/bin/python -m pytest foundry/tests -q
```

`tests/test_orchestration.py` covers the required end-to-end list. It wires the real modules (including the real `ShopForeman`) and swaps in only the simulated clients:

1. dynamic sizing (an S task: exactly 1 builder calls Claude, the other slots 0)
2. context pruning
3. traceback condensation
4. gate short-circuit
5. retry limit
6. rate limiter under load
7. escalation validation and every Floor Manager action
8. `foreman_probe` offline and online

It also runs the demo itself. Each module also has its own `test_<area>.py`.

## Setting up the Shop Foreman (phase 2)

**Windows** (run from the repo root):

```bat
foundry\setup_foreman.bat
```

**macOS / Linux:**

```bash
./foundry/setup_foreman.sh
```

The scripts are idempotent and need no admin rights. They:

1. check that `ollama` is on PATH (otherwise they print https://ollama.com/download);
2. report the GPU and VRAM;
3. pick the model;
4. start `ollama serve` if the API isn't answering;
5. run `ollama pull` for the model;
6. finish with the probe.

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `FOREMAN_MODEL` | `gemma4:e4b` | model tag; `gemma4:e2b` is suggested below 8 GB VRAM |
| `FOREMAN_OLLAMA_URL` | `http://localhost:11434` | Ollama server root |
| `FOREMAN_DRY_RUN=1` | off | print the planned commands without running anything |
| `FOREMAN_WAIT_S` | `30` | seconds to wait for `ollama serve` |

To check the Foreman on its own:

```powershell
.venv\Scripts\python -m foundry.foreman_probe [--json]
```

Two variables point at Ollama, one per tool:

- `FOREMAN_OLLAMA_URL` is the server root (`http://localhost:11434`) used by the **setup scripts**.
- `FOUNDRY_GEMMA_BASE_URL` is the OpenAI-compatible API (`http://localhost:11434/v1`, note the `/v1`) used by **Foundry itself** (`main.py`, `OllamaGemmaClient`, the probe).

If you move Ollama, set both.

Tip: Ollama's default context window is small. For long sizing and PR prompts, start the server with a larger window, for example `set OLLAMA_CONTEXT_LENGTH=8192` (cmd), `$env:OLLAMA_CONTEXT_LENGTH=8192` (PowerShell) or `OLLAMA_CONTEXT_LENGTH=8192 ollama serve`. If you use the Ollama tray app, quit and restart it after setting the variable.

### Real mode (phase 2, untested against live services)

```powershell
.venv\Scripts\python -m foundry.main --real --repo C:\path\to\repo --title "Add clamp helper" --files shop/mathx.py
```

Before doing anything, real mode runs two preflight checks. If either fails it prints how to fix it and exits with code **2**:

1. **Claude Code.** It looks up the executable (`claude.exe` or `claude.cmd` on Windows, or `FOUNDRY_CLAUDE_BIN`). It then builds the launch command once (`validate_launch()`), so flags that can't be passed (for example a `"` or `%` through `claude.cmd`) fail now rather than on every ticket.
2. **Gemma.** It probes the Shop Foreman: is Gemma reachable, and is the model installed?

It then sizes the order and runs the teams with `ClaudeCodeClient` and `OllamaGemmaClient`. Claude Code must be logged in under your subscription.

When the run ends, real mode prints each task's state. As applicable, it also prints the quarantine reason and the last infrastructure error. For a task that isn't finished it prints the real cause:

- `paused until 15:00 (usage limit) - rerun the same command to resume`. The weekday and date are added when the pause doesn't end today, e.g. `Sun 8 Nov 15:00`;
- `Claude Code is not logged in - run claude and /login, then rerun the same command`;
- `Claude Code was not found - install it or set FOUNDRY_CLAUDE_BIN, ...`;
- otherwise, still in progress.

The factory is always shut down cleanly, even after `--timeout`.

| Exit code | Meaning |
|---|---|
| `0` | done; the patch was written |
| `1` | quarantined or failed (the reason is printed) |
| `2` | preflight failed (Claude Code or Gemma not ready) |
| `3` | still in progress: `--timeout` was reached, or the line is paused at the usage limit |

**Resuming.** Run the same command again. An unfinished task with the same `--title`, `--files` and `--repo` is resumed from the database, not submitted a second time.

**Tasks belong to a repository.** Each task records the `--repo` it was submitted for, and `--real` only dispatches tasks of the current `--repo`. At startup it lists every other unfinished task with the command to resume it, for example `F-001 (repo C:\work\A) is unfinished - rerun with --repo "C:\work\A" to resume it`. Repo names match case-insensitively on Windows. Tasks from older databases have no recorded repo; they are listed as `unknown repo` and are never dispatched by `--real`.

**Your working tree is never modified:**

- Claude runs with `cwd` = the target repo, but only as a diff-only builder or reviewer.
- Gates apply each diff to a scratch copy of the repo. `.git`, `node_modules`, `.venv` and caches are left out, and symlinks are copied as links, never followed.
- Each approved task ends up as two files in the patches folder. By default that is `patches\` **next to the database** (`foundry\data\patches\` with the default `FOUNDRY_DB_PATH`); `FOUNDRY_PATCHES_DIR` overrides it:
  - `<task>.patch`: the combined approved diff, one hunk set per subtask. A subtask approved again after a resume replaces its earlier diff instead of being added twice;
  - `<task>.md`: the commit message and PR body.

  When git is installed, Foundry first runs `git apply --check` on the combined patch against a scratch copy of the task's repo. If the subtasks' diffs conflict, the patch is saved as `<task>.patch.rejected` for a manual merge, and a warning is logged.

  A task's files never lie:
  - writing `<task>.patch` deletes a stale `<task>.patch.rejected` from an earlier attempt, and the reverse;
  - a file of the same name with *different* content (another database, a reset) is never overwritten. Foundry writes `<task>-2.patch` (the next free number) and prints the exact file name it used.

Review the patch, then apply it yourself from the target repo's root:

```powershell
git apply --check C:\path\to\planner\foundry\data\patches\F-001.patch
git apply --index C:\path\to\planner\foundry\data\patches\F-001.patch
git commit -F C:\path\to\planner\foundry\data\patches\F-001.md
```

**Claude flags.** Every Claude Code call gets the safe defaults from `FoundryConfig.claude_extra_args`:

- `--max-turns 3`;
- `--disallowedTools "Bash Edit Write NotebookEdit Task Agent WebFetch WebSearch"`;
- `--strict-mcp-config`;
- `--append-system-prompt` with `claude_append_prompt`. That prompt says the agent is a Foundry builder or reviewer whose only output is the diff or JSON, and that it must ignore repository instructions (such as this repo's `CLAUDE.md`) to register tasks or spawn agents.

Flags in `FOUNDRY_CLAUDE_ARGS` are **appended** to these defaults. Set `FOUNDRY_CLAUDE_ARGS_REPLACE=1` to use only your flags instead. `FOUNDRY_CLAUDE_APPEND_PROMPT` replaces the role prompt; `none` turns it off.

The value is split the same way on every platform:

- whitespace separates arguments;
- single or double quotes group words and are removed;
- backslashes are always literal, so `--mcp-config "C:\Users\John Smith\mcp.json"` works as written;
- `#` is not a comment;
- an unbalanced quote is an error.

A JSON value must be wrapped in single quotes, for example `--settings '{"model": "sonnet"}'`. A literal `"` (or `%`) can never reach Claude through `claude.cmd`, because Windows cmd can't pass it safely. The `--real` preflight refuses such arguments; use `claude.exe` (the native installer) or put the JSON in a file.

### What happens at the Claude usage limit

A subscription usage limit is not the builder's fault, so **it never uses up a ticket attempt**. Messages such as "You've hit your limit · resets 3pm (Europe/Berlin)" or "usage limit reached|<epoch>" are recognised.

1. The worker releases its ticket; the attempt count stays the same.
2. The rate limiter pauses all dispatch until the reset time. Foundry reads it from the message and honours the time zone in parentheses.
3. If the reset time can't be read, the pause lasts `FOUNDRY_LIMIT_COOLDOWN_S` (default 1200 s).
4. After the pause, work resumes where it stopped, with no burst of catch-up calls.

If the run stops while paused, real mode exits with code 3 and prints the local time the pause ends. Rerun the same command after that time to continue.

Problems that affect the whole account also pause the line and never count against a ticket: usage limits, "please run /login" and a missing `claude`.

Other infrastructure failures are counted per ticket. These include:

- a timeout;
- Claude exiting without its JSON result;
- a launch error;
- a reviewer error reply;
- a failed scratch copy of the workspace.

Each failure releases the ticket without spending an attempt and logs one WARNING line (error type plus a short excerpt; never the prompt). A successful Claude call resets the count. After `FOUNDRY_INFRA_FAILURE_CAP` (default 3) consecutive failures on one ticket, the task is quarantined with the reason `infrastructure: <error>`, so a broken setup can't loop forever.

Only real outcomes count as attempts: a failed gate, a REVISE verdict or an unusable reply.

### Restarts

Foundry is restart-safe on the same database:

- On start, `build_factory` calls `queue.resume()`, which reopens tickets a crashed worker left claimed.
- The Floor Manager reloads its state from the queue's key-value table: directives, orders, allocations, board task ids, PR drafts, reported quarantines and applied alert ids.
- New work orders continue numbering after the highest `F-###` in the database.
- `--real` resumes an unfinished task with the same title, files and repo, and only dispatches tasks of its own `--repo`.
- Escalations are idempotent per `alert_id`: an alert that was already applied is not applied or posted again.

## How it feeds the Agent Department dashboard

`board_bridge.py` (`BoardBridge(mode="auto" | "dry" | "off")`) maps Foundry events onto the department's own operations (`department/lib/ops.mjs`). It sends them to the local dashboard as `POST http://127.0.0.1:4300/api/op {type, args}`. When the dashboard isn't running it falls back to `node department/dept.mjs ...`. If neither works it drops the op and logs one warning.

| Foundry event | Board op |
|---|---|
| work order sized | `addTask {title, description, size: "small", builders: <allocation>}`, then team fix-ups (see below) |
| builder approved, retries, notes | `reportProgress {taskId, teamId, agentId, message, status}` |
| Foreman escalation | `reportProgress` with message `[Foreman] <severity> <category>: <diagnosis> -> <action>` |
| reviewer verdict | `updateTask {status: "review"}` + one `reviewTask {verdict: approved/changes, by: <team>-6}` per team on the board task |
| task complete | `updateTask {status: "done"}` (after the PR summary is posted as progress) |
| task quarantined | `updateTask {status: "paused"}` + a progress note with the reason |

The bridge never blocks or crashes the factory:

- In `auto` mode ops go onto an in-memory queue, and one daemon thread sends them with short timeouts.
- Every error is caught.
- Board task ids (`T-0xx`) are resolved in that thread.

Every board task is registered as `small` with `builders` = the Foundry allocation; a `large` task would take every free team. Then the bridge reads the board's state.

- **The Foundry team is free on the board** (or the board already picked it): the bridge assigns it and unassigns the other teams the board just auto-assigned. Those teams were free a moment earlier.
- **The Foundry team is busy on another open board task**, for example one of your own (or the board state can't be read): **the bridge never moves it**. The board's own assignment stays as it is. At review time Foundry posts a verdict for *every* team on that board task, so `done` is never refused.

In that busy case the verdicts Foundry posts are *Foundry's* review result, recorded under each board team's reviewer id (`<team>-6`). They are not a review by those teams. Progress for that task is posted as plain messages: no agent, team or status, so it never changes the busy team's agents. Your own board work is never paused or left without a team by Foundry.

Run `npm run dashboard` to watch Foundry tasks appear live. If a shared board (hub) is configured, the dashboard forwards to it like any other client. In tests use `mode="dry"`: it records ops in `bridge.ops` and makes no I/O.

Foundry teams and agent ids (`alpha-1` … `alpha-5`, reviewer `alpha-6`) match the department roster.

## Configuration

`FoundryConfig.from_env()` (`config.py`) reads the following variables. Empty values count as unset.

| Variable | Default |
|---|---|
| `FOUNDRY_DB_PATH` | `foundry/data/foundry.db` |
| `FOUNDRY_GEMMA_BASE_URL` | `http://localhost:11434/v1` |
| `FOUNDRY_GEMMA_MODEL` | `gemma4:e4b` (falls back to `FOREMAN_MODEL`) |
| `FOUNDRY_CLAUDE_BIN` | `claude` |
| `FOUNDRY_RPM` / `FOUNDRY_TPM` / `FOUNDRY_MAX_CONCURRENT` | `20` / `100000` / `3` |
| `FOUNDRY_REVIEW_DWELL_S` | `600` (REVIEW_BOTTLENECK → PROMOTE_PEER_REVIEWER) |
| `FOUNDRY_STALL_S` | `900` (AGENT_STALLED → RESET_BUILDER_CONTEXT) |
| `FOUNDRY_GATE_FAIL_QUARANTINE` | `2` (LINE_JAM → QUARANTINE_TASK) |
| `FOUNDRY_TPM_SPIKE_RATIO` | `0.9` (TOKEN_VELOCITY_SPIKE → THROTTLE_DISPATCH) |
| `FOUNDRY_TEAMS` | `alpha,bravo,charlie` |
| `FOUNDRY_BUILDERS_PER_TEAM` | `5` |
| `FOUNDRY_BOARD_BRIDGE_MODE` | `auto` (`auto`, `dry`, `off`) |
| `FOUNDRY_TRUST_CACHE` | `false`: a cache hit skips only the builder's Claude call; the gates and the reviewer still run. `true` also skips the reviewer |
| `FOUNDRY_LIMIT_COOLDOWN_S` | `1200` (pause after a usage limit whose reset time can't be parsed) |
| `FOUNDRY_CLAUDE_ARGS` | extra flags **appended** to the safe defaults above |
| `FOUNDRY_CLAUDE_ARGS_REPLACE` | `1` = `FOUNDRY_CLAUDE_ARGS` replaces the defaults instead |
| `FOUNDRY_INFRA_FAILURE_CAP` | `3` consecutive infrastructure failures on one ticket before it is quarantined |
| `FOUNDRY_CLAUDE_APPEND_PROMPT` | the Foundry role prompt; `none` = no `--append-system-prompt` |
| `FOUNDRY_PATCHES_DIR` | `patches/` next to the database (`foundry/data/patches` by default) |

## Escalation actions (Floor Manager)

| Action | What the manager does |
|---|---|
| `PROMOTE_PEER_REVIEWER` | runs an idle builder slot (above the task's current allocation, with no claimed ticket) as a temporary extra reviewer for `minutes`; it never reviews its own slot's work. If no slot is idle or the pool doesn't start the reviewer, the action reports `applied=False` |
| `QUARANTINE_TASK` | `queue.quarantine(task, reason)` and pauses the task on the board (reported once per task) |
| `RESET_BUILDER_CONTEXT` | clears the builder's scratch and releases its claimed ticket (`queue.release_ticket`: attempt unchanged); the next claim starts with a fresh prompt. Every reset is logged as a `builder_reset` event |
| `RESIZE_ALLOCATION` | `queue.resize(task, builders)`; the manager's allocation view is updated as well |
| `THROTTLE_DISPATCH` | `limiter.throttle(factor, seconds)` |
| `NONE` | nothing (still recorded and posted) |

Every escalation is validated as a `ForemanEscalation`, stored in the queue and posted to the board. A failing action is logged and reported as `applied=False`; it never takes the line down.

## Honest limits

- **Nothing has run against real Claude Code or a real Gemma yet.** The real clients are unit-tested only against fakes (a fake `claude` script and a local fake HTTP server).
- **The solution cache matches exactly.** It hits only on identical criteria, files and context; fuzzy matching is phase 2.
- **The context engine uses AST and regex, not semantics.** Python is pruned with `ast` (signatures, fields, docstring first lines) and TS/JS with signature regexes. Snippet relevance comes from keyword overlap, which Gemma can re-rank.
- **The default lint is light.** Without `ruff` installed, the linter gate is a small AST check (unused imports, bare `except`, and so on).
- **Patches are not applied for you.** Foundry writes `.patch` files; you apply them with `git apply`. A patch can conflict if the repo changed since the run. Without git installed, the `--check` step is skipped.
- **Everything runs in one process.** The queue is safe across processes (WAL, `BEGIN IMMEDIATE`), but teams run as asyncio tasks in one process. A crash is recovered with `queue.resume()`.
- **The bridge is fire-and-forget.** If the board refuses an op (for example the team is busy on another task), the bridge only logs it; Foundry state stays authoritative in SQLite. Through the CLI fallback, the ops go wherever `dept.mjs` is configured to send them, which may be the shared hub.
- **Demo timing is simulated.** The demo's TPM spike comes from realistic per-call session overhead on the simulated client, and its waits use virtual time.
