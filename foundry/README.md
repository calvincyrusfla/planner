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

```bash
python -m venv .venv
.venv/bin/pip install -r foundry/requirements-dev.txt     # Windows: .venv\Scripts\pip ...
```

You need Python 3.11+. The only runtime dependency is `pydantic>=2.7`; everything else uses the standard library.

## Run the simulated demo

```bash
python -m foundry.main --simulate
```

The demo takes well under a second and needs no network, Claude or Ollama. It prints:

1. **sizing**: three work orders become S (1 builder, team alpha), M (2, bravo) and L (5, charlie);
2. **selective dispatch**: Claude calls per builder slot. Idle slots show `0` because a slot above the allocation never gets a ticket;
3. **gates**: `bravo-1`'s first diff has a syntax error. You see the one-line hint (`[syntax] shop/strings.py:16: ... '(' was never closed`) and the retry prompt that carries it;
4. **review**: the diff-only reviewer verdicts;
5. **escalation**: the Foreman's line check (here a `TOKEN_VELOCITY_SPIKE`) and the Floor Manager's action (`THROTTLE_DISPATCH` on the rate limiter);
6. **PR summaries**: commit messages and a PR body drafted by the Foreman;
7. **telemetry and board**: call counts by role, tokens saved and the board ops it would have sent.

Options:

- `--workdir DIR` keeps the demo repo and `foundry.db` instead of using a temp dir.
- `--json` prints a machine-readable summary at the end.
- `--board auto` sends the demo's ops to your local dashboard. The default, `dry`, only records them.

Time in the demo is simulated. The queue, telemetry and limiter share a virtual clock, so rate-limit waits advance it instead of sleeping.

## Tests

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

```bash
python -m foundry.foreman_probe [--json]
```

`python -m foundry.main --real ...` runs the probe first. If Gemma isn't reachable or the model is missing, it prints how to fix it and exits with code **2**.

### Real mode (phase 2, untested against live services)

```bash
python -m foundry.main --real --repo C:\path\to\repo --title "Add clamp helper" --files shop/mathx.py
```

Real mode sizes the order, runs the teams with `ClaudeCodeClient` and `OllamaGemmaClient`, and prints the final state and the PR draft. Claude Code must be installed and logged in (`claude` or `claude.cmd` on PATH, or set `FOUNDRY_CLAUDE_BIN`).

## How it feeds the Agent Department dashboard

`board_bridge.py` (`BoardBridge(mode="auto" | "dry" | "off")`) maps Foundry events onto the department's own operations (`department/lib/ops.mjs`). It sends them to the local dashboard as `POST http://127.0.0.1:4300/api/op {type, args}`. When the dashboard isn't running it falls back to `node department/dept.mjs ...`. If neither works it drops the op and logs one warning.

| Foundry event | Board op |
|---|---|
| work order sized | `addTask {title, description, size, builders}` (S/M → `small`, L/XL → `large`), then `assignTeam` |
| builder approved, retries, notes | `reportProgress {taskId, teamId, agentId, message, status}` |
| Foreman escalation | `reportProgress` with message `[Foreman] <severity> <category>: <diagnosis> -> <action>` |
| reviewer verdict | `updateTask {status: "review"}` + `reviewTask {verdict: approved/changes, by: <team>-6}` |
| task complete | `updateTask {status: "done"}` (after the PR summary is posted as progress) |
| task quarantined | `updateTask {status: "paused"}` + a progress note with the reason |

The bridge never blocks or crashes the factory:

- In `auto` mode ops go onto an in-memory queue, and one daemon thread sends them with short timeouts.
- Every error is caught.
- Board task ids (`T-0xx`) are resolved in that thread.

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

## Escalation actions (Floor Manager)

| Action | What the manager does |
|---|---|
| `PROMOTE_PEER_REVIEWER` | runs an idle builder slot as a temporary extra reviewer for `minutes` |
| `QUARANTINE_TASK` | `queue.quarantine(task, reason)` and pauses the task on the board |
| `RESET_BUILDER_CONTEXT` | clears the builder's scratch and reopens its claimed ticket with a fresh prompt (no fix hint) |
| `RESIZE_ALLOCATION` | `queue.resize(task, builders)` |
| `THROTTLE_DISPATCH` | `limiter.throttle(factor, seconds)` |
| `NONE` | nothing (still recorded and posted) |

Every escalation is validated as a `ForemanEscalation`, stored in the queue and posted to the board. A failing action is logged and reported as `applied=False`; it never takes the line down.

## Honest limits

- **Nothing has run against real Claude Code or a real Gemma yet.** The real clients are unit-tested only against fakes (a fake `claude` script and a local fake HTTP server).
- **Resetting a stalled builder costs it a retry.** `RESET_BUILDER_CONTEXT` reopens the ticket with `reopen_ticket`, which counts as an attempt, because the queue has no way to release one ticket without counting it.
- **The solution cache matches exactly.** It hits only on identical criteria, files and context; fuzzy matching is phase 2.
- **The context engine uses AST and regex, not semantics.** Python is pruned with `ast` (signatures, fields, docstring first lines) and TS/JS with signature regexes. Snippet relevance comes from keyword overlap, which Gemma can re-rank.
- **The default lint is light.** Without `ruff` installed, the linter gate is a small AST check (unused imports, bare `except`, and so on).
- **Everything runs in one process.** The queue is safe across processes (WAL, `BEGIN IMMEDIATE`), but teams run as asyncio tasks in one process. A crash is recovered with `queue.resume()`.
- **The bridge is fire-and-forget.** If the board refuses an op (for example the team is busy on another task), the bridge only logs it; Foundry state stays authoritative in SQLite. Through the CLI fallback, the ops go wherever `dept.mjs` is configured to send them, which may be the shared hub.
- **Demo timing is simulated.** The demo's TPM spike comes from realistic per-call session overhead on the simulated client, and its waits use virtual time.
