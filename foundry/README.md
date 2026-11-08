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
| Dashboard | ops posted to `/api/op` (dry mode in tests and the demo) | plus the [Foundry service](#run-foundry-in-the-background): status chip, Send to Foundry and Ask the Foreman |
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
6. finish with the probe;
7. run the speed check, `python -m foundry.gemma_doctor` (see [Make Gemma fast](#make-gemma-fast)). A slow Gemma or one on the CPU is reported but doesn't fail the setup: Foundry still works and falls back to its built-in sizing when Gemma is too slow. Without a `.venv` it runs with the system Python (the check needs only the standard library).

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

### Make Gemma fast

Gemma sizes every Foundry task and answers Ask the Foreman. If it runs on the CPU it can take minutes per answer, and sizing often gives up and uses the built-in heuristic instead. The steps below are for Windows; run them from the repo root.

1. **Run the speed check.**

   ```bat
   .venv\Scripts\python -m foundry.gemma_doctor
   ```

   It lists the installed models, asks Ollama how much of Gemma is loaded on the GPU, times a tiny answer and ends with a plain verdict, for example `Gemma is running 100% on the GPU at 42 tokens/s - good.` or `Gemma is running on the CPU (0% GPU) at 3 tokens/s - sizing will often fall back`. It counts as fast when at least 90% of the model is on the GPU and it writes 10 tokens/s or more. It exits with 0 when Gemma is fast, 1 when it is slow or (partly) on the CPU, and 2 when Ollama isn't answering or the model isn't installed. When it is slow it also suggests what to change, such as a smaller Gemma tag. Add `--json` for a machine-readable report, or `--model` / `--base-url` to check another tag or server (by default it uses `FOUNDRY_GEMMA_MODEL` / `FOREMAN_MODEL` and `FOUNDRY_GEMMA_BASE_URL` / `FOREMAN_OLLAMA_URL`).

2. **Read the GPU %.** It is the share of the model that sits in the graphics card's memory (`ollama ps` shows the same figure in its `PROCESSOR` column).
   - `100% GPU`: as fast as this card goes.
   - Something like `40%/60% CPU/GPU`: the model doesn't fit in the card's memory, so part of it runs on the much slower CPU. Use a smaller tag (step 4) or free GPU memory (step 5).
   - `100% CPU` / `0% GPU`: Ollama isn't using the card at all. Update Ollama and the driver (step 3).

3. **Update Ollama and the GPU driver.** Install the latest Ollama from https://ollama.com/download (the tray app updates itself; restart it when it offers to). Update the graphics driver from NVIDIA (GeForce Experience / NVIDIA app) or AMD (Adrenalin). Restart Ollama afterwards and run the speed check again.

4. **Pick a smaller Gemma.** `gemma4:e2b` is about half the size of the default `gemma4:e4b` and fits cards with less than 8 GB of memory. Pull it with the setup script, then tell Foundry to use it:

   ```bat
   set FOREMAN_MODEL=gemma4:e2b
   foundry\setup_foreman.bat
   setx FOUNDRY_GEMMA_MODEL gemma4:e2b
   ```

   `setx` keeps the setting for new windows, so restart `foundry\start_foundry.bat` afterwards. (Foundry reads `FOUNDRY_GEMMA_MODEL` and falls back to `FOREMAN_MODEL`.)

5. **Leave room on the card.** Close games, video editors and other AI apps while Foundry runs; they hold GPU memory and push part of Gemma onto the CPU.

6. **Don't make the context window bigger than needed.** `OLLAMA_CONTEXT_LENGTH` (see the tip above) costs GPU memory. `8192` is enough for Foundry; if a larger value pushes the GPU % below 100, lower it again.

7. **Tune the time limits if Gemma is still slow.** These are a safety net, not a speed-up:
   - `FOUNDRY_GEMMA_TIMEOUT_S` (default `180`): how long one sizing or review call may take before Foundry uses its heuristic.
   - `FOUNDRY_ASK_TIMEOUT_S` (default `90`): how long Ask the Foreman waits for Gemma. When the time runs out you get the built-in status answer instead of an error. Keep it below 110 seconds, the service's own limit for one answer (`ASK_CHUNK_TIMEOUT_S` in `serve_api.py`).

   For example `setx FOUNDRY_GEMMA_TIMEOUT_S 300`, then restart Foundry.

### Gemma pre-review

Before a builder's diff goes to the Claude reviewer, Gemma takes a quick look at it. The order is: Claude builder reply → the deterministic gates (syntax, lint) → **Gemma pre-review** → Claude reviewer. When Gemma spots an obvious problem, the diff goes back to the builder and a Claude review call is saved.

Gemma is a small model and can be wrong, so it may only send work back for **obvious, objective problems**:

- the diff doesn't touch the files or do the job the builder was asked for;
- unfinished code where the work should be (`TODO`, placeholders, empty stubs);
- breakage the gates can't catch;
- debug leftovers (stray prints, breakpoints);
- secrets or credentials in the diff;
- edits to files outside the builder's allowed files.

Style and taste never count as blocking. Gemma answers `pass` or `send_back`, with a list of issues (each `blocking` or `minor`) and a confidence from 0 to 1.

Set the mode with `FOUNDRY_PRE_REVIEW`:

| Mode | What happens |
|---|---|
| `gate` (default) | The diff goes back to the builder, with the hint `[pre-review] <notes>`, only when Gemma says `send_back`, lists at least one blocking issue **and** its confidence is at least `FOUNDRY_PRE_REVIEW_MIN_CONFIDENCE` (default `0.7`). Otherwise it goes on to the Claude reviewer with Gemma's notes attached. |
| `advise` | Nothing is sent back. Gemma's notes are added to the Claude reviewer's prompt as "Gemma pre-review notes (may be wrong): …". |
| `off` | No pre-review. |

Limits that keep a wrong Gemma cheap:

- **Once per ticket.** Gemma can send a ticket back at most once. The next submission of that ticket always goes to the Claude reviewer, with Gemma's notes attached. The cap is stored in the queue, so it holds across restarts.
- **A send-back counts as a rejected attempt**, like a gate failure, so the usual retry and quarantine limits still apply.
- **Gemma never blocks on errors.** If Gemma is off, times out or gives an unusable answer, the diff passes on to the Claude reviewer. That doesn't use up an attempt.
- **Never on the last attempt.** On a ticket's last allowed attempt Gemma never sends the diff back, because a send-back there would quarantine work no Claude reviewer has seen. The diff goes to the Claude reviewer with Gemma's notes attached.
- **Pause after repeated failures.** After 3 Gemma failures (timeouts or errors) in a row, the pre-review is skipped for 10 minutes and diffs go straight to the Claude reviewer. These count as `skipped`. One usable Gemma answer resets the count.

Counters for passed, sent back, advised, skipped and fallback, plus `claude_reviews_avoided` (one per send-back), are in `/api/status` under `foreman.pre_review`. The dashboard's Foundry tasks summary line shows them as "Pre-review: N sent back (≈N Claude reviews saved)".

### Sizing that learns

When a task finishes (approved and the patch is written) or is quarantined, Foundry stores one row in the `sizing_outcomes` table of the queue database. Each row has:

- the task, its repo, title and files;
- the size Gemma or the heuristic predicted (S/M/L/XL), and which of them sized it;
- the builders allocated and the number of subtasks;
- the effort it took: builder attempts, gate failures, pre-review send-backs, reviewer rejections, Claude calls, tokens (input + output) and wall time;
- the final state.

Each task is recorded once. From the effort, Foundry works out the size the task **actually** was and compares it with the prediction (see `foundry/sizing_outcomes.py`: `actual_complexity` and `sizing_accuracy`, which counts exact, under and over, also per predicted size). The effort score is:

`effort = Claude calls + retries`

- **Claude calls** are the builder and reviewer calls recorded for the task. When none were recorded (for example in simulated runs), Foundry counts 2 calls per builder attempt: one build plus one review.
- **Retries** are builder attempts beyond the first per subtask.
- **Wall time is stored but doesn't count.** It reflects queueing, rate limits and the PC as much as the task.

A score up to 3 is **S**, up to 7 **M**, up to 20 **L**, and above that **XL**. A clean run of each size (2 calls per subtask, no retries) scores within its own size: S = 2, M = 4, L = 10. L and XL get the same crew of 5 builders, so effort can't tell them apart. L-versus-XL counts as a match.

Quarantined tasks:

- **The retry cap ran out.** The task counts as at least L, because it needed more than it got.
- **Quarantined for another reason** (an escalation, a sizing problem). The row is kept but left out of accuracy and calibration, because it says nothing about the size.

Each row also keeps two predictions:

- the size that was actually applied;
- the Foreman's raw size, from Gemma or the heuristic, before any calibration shift.

The shift rule groups tasks by the raw size, so calibration never learns from its own corrections.

The next sizing uses this in two ways. Both look only at the same repo's tasks; accuracy and shift rules are worked out per repo.

1. **Examples.** Before Gemma sizes a new order, the Foreman picks up to 5 recent, similar finished tasks (shared files first, then shared title words) and adds them to the prompt, for example "Past task Add clamp helper (files shop/mathx.py): you predicted M, it actually took L (3 attempts, 9 Claude calls)".
2. **Shift rule.** Look at the repo's finished tasks with the same raw predicted size (up to the 20 most recent). The rule applies once there are at least 8:
   - if at least 60 % of them actually turned out bigger, the new prediction moves up one size;
   - if at least 60 % turned out smaller, it moves down one size.

   It never moves more than one step. The rule applies whether Gemma or the heuristic sized the task. The shift is logged, and the rationale says "calibrated from N past tasks".

`/api/status` has the summary under `foreman.calibration` (`n`, `accuracy_pct`, `under`, `over`, `shift_rules`). The dashboard's Foundry tasks summary line shows it as "Sizing accuracy: X % over N tasks".

### Real mode (phase 2, untested against live services)

```powershell
.venv\Scripts\python -m foundry.main --real --repo C:\path\to\repo --title "Add clamp helper" --files shop/mathx.py
```

Before doing anything, real mode runs two preflight checks. If either fails it prints how to fix it and exits with code **2**:

1. **Claude Code.** It looks up the executable (`claude.exe` or `claude.cmd` on Windows, or `FOUNDRY_CLAUDE_BIN`). It then builds the launch command once (`validate_launch()`), so flags that can't be passed (for example a `"` or `%` through `claude.cmd`) fail now rather than on every ticket.
2. **Gemma.** It probes the Shop Foreman: is Gemma reachable, and is the model installed?

It then sizes the order and runs the teams with `ClaudeCodeClient` and `OllamaGemmaClient`. Claude Code must be logged in under your subscription.

When the reviewer has approved, real mode waits (up to 10 minutes) for the Shop Foreman to draft the commit message and write the patch, then shuts the line down. It no longer stops halfway: earlier versions left before a real Gemma draft finished, so nothing was written and only the task state was printed.

For a finished task it prints the commit message's first line, the full paths of the patch and the `.md`, and the exact commands to run next. For example, on Windows:

```text
F-001: S, 1 builder(s) on alpha
F-001: approved - the Shop Foreman is writing the commit message and the patch...
F-001: complete (S, 1 builder(s) on alpha)

Commit message: feat(shop): add clamp helper
Patch:          C:\path\to\planner\foundry\data\patches\F-001.patch
Message + PR:   C:\path\to\planner\foundry\data\patches\F-001.md

Your repository has not been changed yet. Open a terminal in the repository folder
  C:\path\to\repo
and run:
  1. look at the change:  type "C:\path\to\planner\foundry\data\patches\F-001.patch"
  2. apply it:            git apply "C:\path\to\planner\foundry\data\patches\F-001.patch"
  3. commit it:           git add -A
                          git commit -F "C:\path\to\planner\foundry\data\patches\F-001.md"
```

(On Linux and macOS step 1 uses `cat`.) If the combined patch does not apply cleanly to your repository, the report says the patch was **REJECTED** and prints the path of `<task>.patch.rejected` for a manual merge, with no `git apply` step. An empty approved change is reported as "nothing to apply".

For a task that isn't finished it prints the state and, as applicable, the quarantine reason and the last infrastructure error, plus the real cause:

- `paused until 15:00 (usage limit) - rerun the same command to resume`. The weekday and date are added when the pause doesn't end today, e.g. `Sun 8 Nov 15:00`;
- `Claude Code is not logged in - run claude and /login, then rerun the same command`;
- `Claude Code was not found - install it or set FOUNDRY_CLAUDE_BIN, ...`;
- `approved, but the patch was not written within 600s (the Shop Foreman is still drafting) - rerun the same command to finish it`;
- otherwise, still in progress.

The factory is always shut down cleanly, even after `--timeout`.

| Exit code | Meaning |
|---|---|
| `0` | done; the patch (or, if it doesn't apply, the `.patch.rejected`) and the `.md` were written |
| `1` | quarantined or failed (the reason is printed) |
| `2` | preflight failed (Claude Code or Gemma not ready) |
| `3` | still in progress: `--timeout` was reached, the line is paused at the usage limit, or the patch isn't written yet |

**Resuming.** Run the same command again. An unfinished task with the same `--title`, `--files` and `--repo` is resumed from the database, not submitted a second time. That includes a task that was approved but whose patch was never written (for example a run from before this fix that printed only `F-001: complete`): the rerun writes the patch and prints the report above.

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

## Run Foundry in the background

`--real` (above) runs one task and stops. The **Foundry service** keeps the factory running for one repository, so you can send it work from the dashboard at any time.

### Start it (Windows)

1. Do the [Install](#install) steps once, and set up the [Shop Foreman](#setting-up-the-shop-foreman-phase-2) if you want Gemma.
2. Make sure Claude Code is logged in: open a terminal, run `claude`, then `/login` if it asks.
3. Double-click `foundry\start_foundry.bat` in File Explorer, or run it from the repo root:

   ```bat
   foundry\start_foundry.bat
   ```

   A window titled **Foundry** opens and prints `Foundry is running for <repo> on http://127.0.0.1:4310 - press Ctrl+C to stop`. That window *is* the service. By default it works on this repository (the folder that holds `foundry\`). To work on another repository, pass its folder (`--repo` is optional):

   ```bat
   foundry\start_foundry.bat C:\path\to\repo
   foundry\start_foundry.bat --repo C:\path\to\repo
   ```

4. Leave the window open. You can minimise it.

The dashboard's desktop shortcut (`node department/launch.mjs`) also starts Foundry this way when it isn't running yet, minimised as a **Foundry** window on the taskbar; close that window or press Ctrl+C in it to stop it. Set `DEPT_FOUNDRY=off` to turn this off. On macOS and Linux it runs `foundry/start_foundry.sh` in the background with its output in `department/data/foundry.log`. If Foundry wasn't started (for example, no `.venv` yet), `department/data/launch.log` says why.

If something is missing, the window says so and waits for a key instead of closing:

- `No Python virtual environment found`: do the [Install](#install) steps;
- `Claude Code is not ready: ...`: install Claude Code or set `FOUNDRY_CLAUDE_BIN`, then start again;
- `Repository folder not found`: check the path you passed.

Gemma is not required to start: without it the Foreman uses its built-in fallbacks, but **Ask the Foreman** can't answer.

What the service does while it runs:

- it listens on `http://127.0.0.1:4310` (only this PC can reach it);
- it keeps the builder and reviewer pools started for that repository;
- every 60 seconds the Shop Foreman checks the line and the Floor Manager carries out any escalation (the same actions as in [Escalation actions](#escalation-actions-floor-manager));
- on start it picks up the unfinished tasks of that repository from the database, so nothing is lost after a restart;
- finished tasks are written to the patches folder as `.patch` and `.md` files, exactly as in real mode. Your working tree is never changed; you apply the patch yourself.

### Stop it

Click in the **Foundry** window and press **Ctrl+C**. It prints `Foundry is stopping (finishing in-flight work)...`, sends its last board updates, shuts the factory down and prints `Foundry stopped.`. The window then says `Press any key to continue . . .`; press a key or close the window. (If Windows asks `Terminate batch job (Y/N)?`, answer `N` so the window shows the stop message, or `Y` to close at once.)

You can also simply close the window. That is safe too: unfinished work is resumed the next time you start it.

### Without the .bat file

The `.bat` file runs this command from the repo root. Without `--repo`, `foundry.serve` uses the current folder.

```powershell
.venv\Scripts\python -m foundry.serve --repo C:\path\to\repo
```

On macOS and Linux, `foundry/start_foundry.sh` does the same in the terminal (stop it with Ctrl+C):

```bash
./foundry/start_foundry.sh [--repo] [/path/to/repo]
.venv/bin/python -m foundry.serve --repo /path/to/repo
```

Options of `foundry.serve`: `--port N` (instead of `FOUNDRY_SERVE_PORT`), `--monitor S` (instead of `FOUNDRY_MONITOR_S`) and `--board auto|dry|off` (instead of `FOUNDRY_BOARD_BRIDGE_MODE`). `FOUNDRY_DRY_RUN=1` makes either start script print the command instead of running it.

| Variable | Default | Meaning |
|---|---|---|
| `FOUNDRY_SERVE_PORT` | `4310` | port of the service. The dashboard reads the same variable, so if you change it, set it for both (before starting each) |
| `FOUNDRY_MONITOR_S` | `60` | seconds between the Foreman's line checks |

All the variables in [Configuration](#configuration) apply as well.

### The service's API

The dashboard uses these endpoints; you don't need them yourself. They answer only on `127.0.0.1` and only to requests addressed to a local host name. There is no CORS, so web pages from other sites can't call them.

| Endpoint | What it does |
|---|---|
| `GET /api/health` | `{ok, repo, model, gemma_ok, claude_ok, version, gemma_speed, pr_ready, pr_reason}` (`pr_reason` says why PRs can't be opened) |
| `GET /api/status` | the tasks (`id, title, state, team, builders, patch, md, rejected, reason, pr`), a Foreman telemetry summary, `paused_until` and `last_account_error` |
| `POST /api/orders` | `{title, description?, files?, size?}` submits a work order for the service's repository and returns `{ok, task_id}` at once; the Shop Foreman then sizes it in the background (on a CPU this can take a minute), and `/api/status` shows the task as `sizing` until then. An order whose sizing was interrupted is sized again on the next start. A bad order gets HTTP 400 |
| `POST /api/ask` | `{question}`: the Foreman's answer, streamed as `text/event-stream` (`data: {"text": "..."}` chunks, then `event: done`) |
| `POST /api/tasks/<id>/pr/retry` | opens the pull request again for a task whose PR failed (see [Automatic pull requests](#automatic-pull-requests)) |
| `GET /api/actions` | `{ok, actions}`: the Foreman's actions, newest first (see [The Foreman fixes things itself](#the-foreman-fixes-things-itself)) |
| `POST /api/actions/<id>/undo` | undoes one action; `{ok: false, reason}` when it can't be undone |

## Automatic pull requests

When the service runs for a repository, **every approved task gets its own branch and pull request** on GitHub, with no clicks from you. The Shop Foreman (Gemma) writes the PR title and description. The `.patch` and `.md` files are still written to the patches folder as before, so nothing is lost if the PR can't be opened.

### Set it up once (Windows)

1. Install the GitHub CLI. In PowerShell or a Command Prompt:

   ```powershell
   winget install --id GitHub.cli
   ```

   Then **open a new window** so `gh` is on the PATH. (Without winget, download the installer from https://cli.github.com/.)
2. Log in once:

   ```powershell
   gh auth login
   ```

   Pick **GitHub.com**, **HTTPS**, and **Login with a web browser**. Answer **Yes** when it asks to use your GitHub credentials for git, so Foundry's `git push` uses the same login.
3. Check it: `foundry\setup_foreman.bat` now ends with a GitHub check that says whether `gh` is installed and logged in (`./foundry/setup_foreman.sh` on macOS/Linux). It only reports; it never fails the setup. You can also run `gh auth status` yourself.

The repository you start Foundry with (`--repo`) must be a git repository with an `origin` remote on GitHub that you can push to.

When the service starts it checks git, the `origin` remote and `gh` once. If something is missing it logs a clear warning, and `/api/health` reports `pr_ready: false` with the reason in `pr_reason`. Tasks still run; only the PR step is skipped or fails.

### What Foundry does for each approved task

1. Gemma drafts the PR title and description (the same draft that goes into `<task>.md`). The description ends with `Opened by Foundry (task F-012)`.
2. Foundry makes a **temporary git worktree** under `foundry\data\worktrees\`, starting from the base branch (`FOUNDRY_PR_BASE`, else the repository's default branch, else the branch you are on).
3. It creates the branch `foundry/<task id>-<short title>`, in lower case, for example `foundry/f-012-add-clamp-helper`. If that branch already exists it uses `-2`, `-3` and so on.
4. It applies the patch, commits it (as you: the name and e-mail from your git config; your git hooks run as usual), and pushes the branch to `origin`.
5. It opens the pull request with `gh pr create` and records the link.
6. It removes the temporary worktree.

**Your working files and your current branch are never touched.** Everything happens in the separate worktree, which is deleted afterwards. Foundry never force-pushes, and never pushes to the base branch. After a successful publish the local `foundry/*` branch is kept in your repository (it isn't checked out); you can delete it safely, for example `git branch -D foundry/f-012-add-clamp-helper`.

You'll find the link in three places: the Foundry tasks list on the dashboard (**PR #12**, opens in your browser), a progress note on the board task, and the task's `pr` field in `/api/status` (`state` is `pending`, `opened`, `failed` or `skipped`, with `url`, `branch`, `error` and `stage`).

A restart never opens a second PR for the same task: an opened PR is remembered, a failed one is not retried by itself, and one that was still pending when the service stopped is tried once more.

### Settings

| Variable | Default | What it does |
|---|---|---|
| `FOUNDRY_AUTO_PR` | `true` | `0` / `false` turns automatic PRs off; Foundry then only writes the patch files |
| `FOUNDRY_PR_BASE` | unset | the branch PRs are opened against (unset: the repository's default branch, else your current branch) |
| `FOUNDRY_PR_DRAFT` | `false` | `1` / `true` opens every PR as a draft |

### When a PR fails

The Foundry tasks list then shows **PR failed** with a **Retry** button; hover over **PR failed** for the reason and the step that failed. Fix the cause and click **Retry** (or `POST /api/tasks/<id>/pr/retry`). When the branch was already pushed and only the PR step failed, Retry opens the PR for that same branch; it doesn't push a second one. The patch and `.md` files are always in the patches folder, so you can also [apply the change by hand](#real-mode-phase-2-untested-against-live-services).

| Stage | What it means and what to do |
|---|---|
| `no_git` | git is not installed or not on the PATH. Install Git for Windows (`winget install --id Git.Git`), then restart Foundry |
| `not_a_repo` | `--repo` is not a git repository. Start Foundry with the repository's folder |
| `no_remote` | the repository has no `origin` remote. Add it: `git remote add origin https://github.com/<you>/<repo>.git` |
| `apply_failed` | the patch doesn't apply to the base branch: either the base changed since the task ran, or your task depended on changes you haven't pushed (Foundry applies the patch to `origin/<base>`, not to your local checkout). Push those changes first and Retry, merge `<task>.patch` by hand, or resubmit the task |
| `push_failed` | git couldn't push, usually because it isn't logged in or you can't write to the repository. Run `gh auth login` (answer Yes to git credentials), or `gh auth setup-git` |
| `gh_missing` | the GitHub CLI is not installed: `winget install --id GitHub.cli`, open a new window, `gh auth login`, then restart Foundry |
| `gh_not_logged_in` | run `gh auth login` |
| `no_base` | Foundry couldn't tell which branch to open the PR against. Set `FOUNDRY_PR_BASE`, for example `main` |
| `fetch_failed` | `git fetch` of the base branch failed: check your network and that the branch exists on `origin` |
| `worktree_failed`, `commit_failed` | git couldn't create the temporary worktree or the commit (for example a commit hook refused it). Read the error, fix it, then Retry |
| `no_patch` | the task has no patch to publish (an empty change, or the combined patch doesn't apply); the PR is `skipped` |
| `pr_failed` | the branch was pushed but `gh pr create` failed (for example a PR for that branch already exists, or the base branch name is wrong). Read the error, fix it, then Retry, or open the PR from the pushed branch on GitHub |

## Dashboard integration

Start the dashboard as usual (`npm run dashboard`, or the desktop shortcut) and start Foundry with `foundry\start_foundry.bat`. The order doesn't matter. The dashboard talks to Foundry through its own server (`/api/foundry/...` is passed on to `http://127.0.0.1:4310/api/...`), so the browser only ever talks to the dashboard.

### The Foundry chip

In the dashboard header a **Foundry** chip shows whether the service is running. It is checked every 10 seconds.

- **Foundry · running · *repo*** with a green dot: the service is up, and the chip names the repository it works on.
- **Foundry · running · *repo* · Gemma off** with an amber dot: the service is up but Gemma (the Shop Foreman) isn't answering. Tasks still run with the built-in fallbacks; Ask the Foreman won't work until Gemma is back.
- **Foundry · not running**: start `foundry\start_foundry.bat`. The chip changes within 10 seconds; you don't need to reload the page.

Hover over the chip for details.

### Send to Foundry

The new-task form has a **Send to Foundry** switch.

1. Type the title and description as usual, and pick Small or Large.
2. Turn **Send to Foundry** on. A **Files** box appears and the button changes to **Send to Foundry**.
3. Optionally list the files the change is about, separated by commas, for example `shop/mathx.py, shop/strings.py`. This helps Foundry give the builders the right context.
4. Click **Send to Foundry**. A message confirms it, for example `Sent to Foundry as F-004; it appears on the board shortly`.

With the switch on, the task goes to Foundry instead of straight onto the board. Foundry sizes it and picks the team and the number of builders itself (the Builders picker is not used), then registers it on the board, so it shows up as a normal task a moment later and you can follow its progress, reviews and completion there. With the switch off, the form works exactly as before. If Foundry isn't running, the form says so and nothing is added.

When the task is done, the patch and the commit message are in the patches folder (see [Real mode](#real-mode-phase-2-untested-against-live-services) for how to apply them).

### Ask the Foreman

The **Ask the Foreman** panel lets you ask the Shop Foreman a question in plain words, for example "Why is F-003 stuck?" or "Which tasks are waiting for review?". Type it and click **Ask**. The answer appears piece by piece as Gemma writes it; **Stop** ends it early. The Foreman can only read: it looks at the queue, the telemetry and the repository, and it never changes anything.

When Foundry isn't running, the box and the button are disabled and the panel says `Foundry isn't running. Start it with foundry\start_foundry.bat to ask questions.` The answers need Gemma, so set up the [Shop Foreman](#setting-up-the-shop-foreman-phase-2) first.

### Is Gemma doing the work?

The dashboard shows which tasks Gemma really sized:

- While Foundry is running, the Ask the Foreman card has a **Foundry tasks** list (click it to open it; the summary line counts how many were sized by Gemma and how many by the fallback). Each row shows the task's id, title, state and team, and a badge:
  - **Gemma · 41 s**: Gemma sized the task, and it took 41 seconds.
  - **Fallback**: Gemma was too slow or didn't answer, so the built-in rules sized it (with the time sizing took, when known). The task still runs normally; the rules are just less precise about the crew size.
  - **Sizing…**: the task is still being sized.
- Hover over the **Foundry** chip to see Gemma's recent speed, for example `Gemma: avg 38 s per call, 2 fallbacks (last 41 s, 12 calls)`.

Many Fallback badges or a high average mean Gemma is too slow on this PC; see [Make Gemma fast](#make-gemma-fast). The same figures are in the service's API: each task row in `/api/status` has `sizing_source` (`gemma` or `heuristic`) and `sizing_ms`, `/api/status` has `foreman.gemma` (call counts, fallbacks, timeouts, average and last time, per hook), and `/api/health` has `gemma_speed` (`avg_ms`, `last_ms`, `fallbacks`, `calls`).

### When something doesn't work

- **The chip says not running but the Foundry window is open.** Look at the window for an error. Check that nothing else uses port 4310, and that `FOUNDRY_SERVE_PORT` is either unset or the same for Foundry and the dashboard.
- **"Foundry is not running"** when you submit or ask: the service was stopped or crashed. Start it again; unfinished tasks are resumed.
- **A task stays paused.** Claude hit the usage limit; Foundry carries on by itself when the limit resets (see [What happens at the Claude usage limit](#what-happens-at-the-claude-usage-limit)).

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
- **The Foundry team is busy on another open board task**, for example one of your own (or the board state can't be read): **the bridge never moves it**. The board's own assignment stays as it is.

At review time the bridge reads the **live** board and posts a verdict for *every* team on that board task right then, so `done` is never refused. This includes a team the board's dispatcher gave a queued task after it was added. A board task that is still queued with no team gets no review step; it is marked done directly. Only when the board state can't be read does the bridge fall back to the teams it saw when the task was added.

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
| `FOUNDRY_GEMMA_TIMEOUT_S` | `180` seconds one Gemma request may take before the Foreman falls back to its heuristic (sizing runs in the background, so a slow CPU Gemma is fine) |
| `FOUNDRY_ASK_TIMEOUT_S` | `90` seconds Ask the Foreman waits for Gemma before it gives the built-in status answer (keep it below the service's 110 s per-answer limit) |
| `FOUNDRY_PRE_REVIEW` | `gate` (`off`, `advise`, `gate`; see [Gemma pre-review](#gemma-pre-review)) |
| `FOUNDRY_PRE_REVIEW_MIN_CONFIDENCE` | `0.7` (how sure Gemma must be before `gate` mode sends a diff back) |
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
| `FOUNDRY_AUTO_PR` | `true`: every approved task gets its own branch and pull request (see [Automatic pull requests](#automatic-pull-requests)) |
| `FOUNDRY_PR_BASE` | unset: PRs target the repository's default branch, else the current branch |
| `FOUNDRY_PR_DRAFT` | `false`: `true` opens PRs as drafts |

## Escalation actions (Floor Manager)

| Action | What the manager does |
|---|---|
| `PROMOTE_PEER_REVIEWER` | runs an idle builder slot (above the task's current allocation, with no claimed ticket) as a temporary extra reviewer for `minutes`; it never reviews its own slot's work. If no slot is idle or the pool doesn't start the reviewer, the action reports `applied=False` |
| `QUARANTINE_TASK` | `queue.quarantine(task, reason)` and pauses the task on the board (reported once per task) |
| `RESET_BUILDER_CONTEXT` | clears the builder's scratch and releases its claimed ticket (`queue.release_ticket`: attempt unchanged); the next claim starts with a fresh prompt. Every reset is logged as a `builder_reset` event |
| `RESIZE_ALLOCATION` | `queue.resize(task, builders)`; the manager's allocation view is updated as well |
| `THROTTLE_DISPATCH` | `limiter.throttle(factor, seconds)` |
| `REASSIGN_TEAM` | moves a stuck task's open tickets to an idle team's pool |
| `RETRY_TASK` | re-opens a task quarantined for infrastructure reasons with fresh attempts (once per task) |
| `NONE` | nothing (still recorded and posted) |

Every escalation is validated as a `ForemanEscalation`, stored in the queue and posted to the board. A failing action is logged and reported as `applied=False`; it never takes the line down. Every applied action is also written to the action log with what is needed to undo it (`manager.actions()`, `manager.undo_action(action_id)`); see [The Foreman fixes things itself](#the-foreman-fixes-things-itself).

## The Foreman fixes things itself

The Shop Foreman watches the line all the time. When it spots a problem (a stuck task, a review queue that isn't moving, a task that keeps failing, tokens running out too fast) it **acts on its own**, for every kind of action, including quarantine. You don't have to approve anything. Instead, every action is logged and almost every one can be undone with one click.

### The Foreman actions log

The Ask the Foreman card on the dashboard has a **Foreman actions** log, newest first. Each line says when it happened and what the Foreman did, in plain words, for example `Moved F-012 to Team Bravo (stuck 14 min)`. An action that failed is shown as failed. Actions that can be undone have an **Undo** button; once undone the line is tagged **Undone**. The same log is in the service's API: `GET /api/actions` and `POST /api/actions/<id>/undo`.

### What each action does, and what Undo does

| Action | What the Foreman did | What Undo does |
|---|---|---|
| Change the crew size (`RESIZE_ALLOCATION`) | gave a task more or fewer builders | puts back the previous number of builders |
| Slow down dispatch (`THROTTLE_DISPATCH`) | slowed down how fast new Claude calls start, to stay under the usage limit | lifts the slow-down at once |
| Extra reviewer (`PROMOTE_PEER_REVIEWER`) | ran an idle builder as an extra reviewer for a while | stops the extra reviewer |
| Quarantine (`QUARANTINE_TASK`) | took a task that keeps failing off the line and paused it | puts the task back on the line with the attempts it had before |
| Move to another team (`REASSIGN_TEAM`) | moved a stuck or slow task's open work from a busy team to an idle one | moves the work that is still open back to the first team |
| Try again (`RETRY_TASK`) | put a task that was quarantined for a passing problem (a timeout, a crash, not a real build failure) back on the line with fresh attempts; at most once per task | quarantines it again |
| Fresh start for a builder (`RESET_BUILDER_CONTEXT`) | cleared a stuck builder's scratch so its next try starts with a fresh prompt | **can't be undone**: the cleared scratch is gone. The log says so, and there is no Undo button |

Undo is safe to click twice: the second click does nothing. If things have moved on since the action (for example the moved work is already finished, or the task was already quarantined again), Undo refuses and says why, and nothing changes.

The Foreman never retries the same task twice. Every action is also posted to the board task as a progress note.

## Honest limits

- **Nothing has run against real Claude Code or a real Gemma yet.** The real clients are unit-tested only against fakes (a fake `claude` script and a local fake HTTP server).
- **The solution cache matches exactly.** It hits only on identical criteria, files and context; fuzzy matching is phase 2.
- **The context engine uses AST and regex, not semantics.** Python is pruned with `ast` (signatures, fields, docstring first lines) and TS/JS with signature regexes. Snippet relevance comes from keyword overlap, which Gemma can re-rank.
- **The default lint is light.** Without `ruff` installed, the linter gate is a small AST check (unused imports, bare `except`, and so on).
- **Patches are not applied to your working tree.** The service pushes each approved task to its own branch and opens a PR ([Automatic pull requests](#automatic-pull-requests)); `--real` and the patch files leave applying to you, with `git apply`. A patch can conflict if the repo changed since the run. Without git installed, the `--check` step is skipped.
- **Everything runs in one process.** The queue is safe across processes (WAL, `BEGIN IMMEDIATE`), but teams run as asyncio tasks in one process. A crash is recovered with `queue.resume()`.
- **The bridge is fire-and-forget.** If the board refuses an op (for example the team is busy on another task), the bridge only logs it; Foundry state stays authoritative in SQLite. Through the CLI fallback, the ops go wherever `dept.mjs` is configured to send them, which may be the shared hub.
- **Demo timing is simulated.** The demo's TPM spike comes from realistic per-call session overhead on the simulated client, and its waits use virtual time.
