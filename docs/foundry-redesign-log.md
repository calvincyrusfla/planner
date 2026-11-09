# Foundry redesign log

Lessons from running the prototype (Nov 2026). Each entry: what happened, what it cost, what v2 should do. Newest at the bottom of each section.

## Highest priority

### 1. Never reword the order — pass the exact details (cost: thousands of tokens)
- **What happened:** the Foreman rewrote every piece in its own words and builders saw only a 2-sentence summary. Exact names, data formats and strings to search for were lost again and again.
- **Cost:** several batches failed for this alone (S-011/S-012 guessed function names; S-015/S-017 dashboard piece couldn't find its lines; S-023 F-046 blocked on a "missing" contract that was in the order). Each failure burned up to 4 attempts × builder + reviewer calls — **thousands of Claude tokens per failure** to save a few hundred tokens of prompt.
- **v2:** the Foreman sizes, splits and picks context; it never rewrites the spec. Every builder and reviewer gets the original order verbatim plus a one-line note for its own part.

### 2. Build from a clean copy of the publish branch
- Windows `core.autocrlf=true` and a working folder on another branch made patches that could never publish (F-012, F-016). v2: always build, check and publish from a pinned snapshot of the target commit.

### 3. Builders must see what they change
- Outline-only context and raw unified diffs failed on real files (S-002: hidden bodies, duplicate functions). v2: full view of edit targets, inventory of existing symbols, text windows for non-Python files, structured edits ("replace this function", "change this exact text") that work in every language Foundry edits (Python, JS/TS; HTML via text).

### 4. Builders need the evidence, not just instructions
- Builders that couldn't see a test or the module to call guessed or refused (S-011, S-017 F-036, S-023 F-046). v2: batches carry read-only reference files; related tests are added automatically.

## Architecture

- **One builder per piece, many pieces in parallel.** Splitting one piece across parallel builders mostly caused failures (S-003). Idle builders should take any ready work.
- **One safety policy, not overlapping ones.** Retries, Foreman quarantines, pre-review, the infra cap and gates fought each other (Foreman quarantined after 2 failures while 4 attempts were allowed — S-004).
- **One owner per piece of data.** Board vs Foundry copies of the same state caused many sync bugs.
- **PRs built from the same base can clash when merged** (#19 + #20 both added `_added_lines`; #17 + #20 conflicted in config.py). v2: re-test each PR against the latest merged code before it is "ready", or merge through a queue.
- **Gates should judge only the change.** Whole-file type checks failed unrelated changes on pre-existing errors (S-005).
- **Setup in one place.** Environment variables, restarts, sign-outs and stale shells caused repeated confusion (FOUNDRY_TYPECHECK). v2: one settings screen, applied without restarts.

## Operations
- Measure cost from day one: Claude tokens per task, per failure, saved by Gemma — visible on the dashboard.
- Mobile remote access: phone-friendly dashboard via the cloud board, push notifications, safe one-tap controls, real logins (consider Supabase for the board).
- Never probe the live board with real batches (S-022 built a junk piece); validate by reading.

## Cost (measured 9 Nov 2026)
- **Foundry was not cheaper than agent-team builds so far.** 52 tasks, 332 Claude calls: 10.2M fresh input tokens plus 3.93M cache reads. A successful piece averaged ~250k fresh tokens (median ~200k); counting failures, each opened PR cost ~535k. Agent-team features cost 340k–800k each.
- **Failures were more than half of all spend.** Most were builders missing information (no reference file, no exact text, reworded order) and retrying blind up to 4 times.
- **Gemma's "saved" figure (7.75M from pruning) is an estimate**, not a measured saving. v2: measure fresh vs cached tokens per call and per task, and report real savings only.
- **Prompts grew huge on wide context** (130k+ characters at level 3). v2: cache the stable prompt prefix and keep the default context narrow.

## Contracts between parts
- **One name per field across the whole system.** The board stored `readFiles` and Foundry read `read_files`, so every reference file in every batch was silently dropped (fixed in #39). v2: one shared schema, generated for both JavaScript and Python, with a test that sends a real batch end to end.
- **Check a PR against the plan, not just the tests.** #38 merged with the help answer in a different shape from the plan and its board operation never registered, so the feature could not be used. v2: the reviewer checks every point of the order's contract.
- **Keep what the builder actually saw.** Without saved prompts we could only guess why builders said information was missing (#40 now saves them). v2: every prompt and reply is kept and linked from the task.

## Agency Warehouse (v2) ideas
- **Answer help requests from the phone.** The PC dashboard's "Answer with Claude" button opens Claude Code on the PC. v2: the online board gets the same button, opening a claude.ai Claude Code session with the prompt filled in (or a notification plus a copy-the-prompt button if pre-filled links are not supported).
