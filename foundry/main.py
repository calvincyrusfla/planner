"""Foundry entry point: ``python -m foundry.main --simulate`` runs the end-to-end demo.

The simulated demo (phase 1) needs no Claude, no Ollama and no network. It:

1. builds a small throwaway Python repo and three work orders (S, M, L);
2. lets the Shop Foreman size them (S=1, M=2, L=5 builders) and the Floor
   Manager queue one ticket per allocated builder slot;
3. runs all three teams (5 builder slots + 1 reviewer each) against a
   ``SimulatedClaudeClient`` and prints how many Claude calls each builder made
   (idle slots: 0);
4. forces one deterministic gate failure (a syntax error), prints the Foreman's
   one-line fix hint, the retry and the reviewer verdicts;
5. has the Foreman evaluate the line, raises an escalation and lets the Floor
   Manager execute its action;
6. prints the drafted commit message / PR summary and the board ops that would
   go to the Agent Department dashboard (``--board dry``, the default).

Time is simulated: rate-limiter waits advance a virtual clock instead of
sleeping, so the demo finishes in a couple of seconds.

``--real`` (phase 2) probes the Gemma Shop Foreman first (exit code 2 when it is
not ready), then runs one work order with headless Claude Code
(``claude -p --output-format json``) and Ollama. It has not been exercised
against the real services in phase 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from foundry.board_bridge import BoardBridge
from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, WorkOrder, build_factory
from foundry.gemma_client import GemmaReply, GemmaRequest, SimulatedGemmaClient
from foundry.schemas import ClaudeResult

__all__ = [
    "VirtualClock", "DEMO_FILES", "DEMO_ORDERS", "demo_claude", "demo_gemma", "run_demo", "run_real",
    "explain_task", "completion_report", "task_outputs", "main",
]

#: Claude Code carries a large system prompt into every headless session; the
#: simulated client reports it so token burn looks like a real session.
SESSION_OVERHEAD_TOKENS = 1500


class VirtualClock:
    """Real time plus a virtual offset; ``sleep`` advances the offset instantly.

    The queue (datetime), telemetry (epoch) and rate limiter (monotonic) share
    it, so simulated waits are consistent everywhere and cost no wall time.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self._t0 = time.time()
        self._m0 = time.monotonic()

    def epoch(self) -> float:
        """Seconds since the epoch, virtual."""
        return self._t0 + (time.monotonic() - self._m0) + self.offset

    def monotonic(self) -> float:
        """Monotonic seconds, virtual."""
        return time.monotonic() + self.offset

    def utc(self) -> datetime:
        """Aware UTC datetime, virtual."""
        return datetime.fromtimestamp(self.epoch(), timezone.utc)

    def advance(self, seconds: float) -> None:
        """Move virtual time forward."""
        self.offset += max(0.0, float(seconds))

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time and yield once."""
        self.advance(seconds)
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- demo repo

def _module(name: str, funcs: Sequence[str]) -> str:
    lines = [f'"""{name} module of the demo shop."""', ""]
    for f in funcs:
        lines += ["", f"def {f}(value):", f'    """Return {f} of value."""', "    result = value", "    return result", ""]
    return "\n".join(lines).rstrip() + "\n"


DEMO_FILES: dict[str, str] = {
    "shop/mathx.py": _module("mathx", ["double", "halve", "square"]),
    "shop/strings.py": _module("strings", ["shout", "whisper"]),
    "shop/slug.py": _module("slug", ["slugify_basic"]),
    "shop/inventory.py": _module("inventory", ["count_items", "restock", "reserve", "release"]),
    "shop/pricing.py": _module("pricing", ["net_price", "gross_price"]),
    "shop/orders.py": _module("orders", ["open_order", "close_order"]),
    "shop/report.py": _module("report", ["daily_report"]),
    "shop/cli.py": _module("cli", ["parse_args"]),
}

DEMO_ORDERS: list[dict[str, Any]] = [
    {
        "title": "Add clamp helper",
        "description": "Add clamp(x, lo, hi) to the math helpers.",
        "files": ["shop/mathx.py"],
        "acceptance_criteria": ["clamp returns x limited to the range lo..hi"],
    },
    {
        "title": "Add string casing helpers",
        "description": "Add title-case and kebab-case helpers next to the existing string utilities.",
        "files": ["shop/strings.py", "shop/slug.py"],
        "acceptance_criteria": ["title_case capitalises each word", "kebab_case joins words with dashes"],
    },
    {
        "title": "Rewrite inventory pipeline end-to-end",
        "description": "Rewrite the inventory, pricing, orders, report and CLI modules so stock flows end-to-end.",
        "files": ["shop/inventory.py", "shop/pricing.py", "shop/orders.py", "shop/report.py", "shop/cli.py"],
        "acceptance_criteria": ["each module gains a pipeline_step function"],
    },
]

#: The builder whose first attempt is broken on purpose (M task, team bravo).
FAILING_BUILDER = "bravo-1"


def write_demo_repo(root: Path) -> Path:
    """Write the demo repo under ``root`` and return it."""
    for rel, text in DEMO_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def append_diff(path: str, original: str, new_lines: Sequence[str]) -> str:
    """Unified diff that appends ``new_lines`` to ``original`` (last line as context)."""
    lines = original.splitlines()
    n = len(lines)
    body = [f"--- a/{path}", f"+++ b/{path}", f"@@ -{n},1 +{n},{1 + len(new_lines)} @@", f" {lines[-1]}"]
    body += [f"+{ln}" for ln in new_lines]
    return "\n".join(body) + "\n"


_FILES_RE = re.compile(r"## (?:Files you may modify|Your files)\n((?:- .+\n)+)")
_BUILDER_RE = re.compile(r"You are builder (\S+) on team")
_SUBTASK_RE = re.compile(r"subtask (\S+), attempt")


def demo_claude(repo: Path, *, fail_first: str | None = FAILING_BUILDER) -> SimulatedClaudeClient:
    """A scripted Claude: builders append a function per allowed file, reviewers approve.

    ``fail_first``'s first attempt contains a syntax error so the gates fail.
    """
    failed: set[str] = set()

    def script(prompt: str, agent_id: str) -> ClaudeResult:
        if prompt.startswith("You are reviewer") or "## Unified diff" in prompt:
            text = json.dumps({"verdict": "APPROVED", "directives": []})
        else:
            m = _FILES_RE.search(prompt)
            files = [ln[2:].strip() for ln in m.group(1).splitlines()] if m else []
            diffs = []
            # a stacked ticket (earlier subtasks already applied) adds its own helper name
            sub = _SUBTASK_RE.search(prompt) if "## Already done on this piece" in prompt else None
            suffix = "_" + re.sub(r"\W", "_", sub.group(1)).lower() if sub else ""
            for rel in files:
                src = (repo / rel).read_text(encoding="utf-8")
                fn = re.sub(r"\W", "_", Path(rel).stem) + suffix
                if agent_id == fail_first and agent_id not in failed:
                    new = ["", "", f"def {fn}_helper(value:", "    return value"]
                else:
                    new = ["", "", f"def {fn}_helper(value):", f'    """Helper added by {agent_id}."""', "    return value"]
                diffs.append(append_diff(rel, src, new))
            if agent_id == fail_first:
                failed.add(agent_id)
            text = "Here is the change.\n\n```diff\n" + "".join(diffs) + "```\n"
        return ClaudeResult(
            text=text,
            input_tokens=-(-len(prompt) // 4) + SESSION_OVERHEAD_TOKENS,
            output_tokens=-(-len(text) // 4),
            duration_seconds=0.0,
        )

    return SimulatedClaudeClient(script)


def demo_gemma() -> SimulatedGemmaClient:
    """A scripted Gemma that writes PR drafts and diagnoses; other hooks fall back."""

    def responder(req: GemmaRequest) -> Any:
        system = str(req.messages[0].get("content", "")) if req.messages else ""
        if "Conventional Commit" in system:
            user = str(req.messages[-1].get("content", ""))
            title = user.split("\n", 1)[0].removeprefix("Task: ").strip() or "update"
            subject = re.sub(r"\s+", " ", title.lower())[:50]
            return {
                "commit_message": f"feat(shop): {subject}",
                "pr_body": f"## Summary\n{title}.\n\nDrafted by the Gemma Shop Foreman from the approved diffs.",
            }
        if "diagnosis" in system:
            return GemmaReply(text="Claude token burn is above the safe share of the budget. Slow dispatch briefly.")
        return "{}" if req.json_schema is not None else ""

    return SimulatedGemmaClient(responder)


# --------------------------------------------------------------------------- demo run

def _p(out: list[str], line: str = "") -> None:
    out.append(line)
    print(line, flush=True)


async def run_demo(
    *,
    workdir: Path | None = None,
    board: str = "dry",
    quiet: bool = False,
) -> dict[str, Any]:
    """Run the simulated end-to-end demo; returns a summary dict (used by tests)."""
    out: list[str] = []
    say = (lambda line="": out.append(line)) if quiet else (lambda line="": _p(out, line))
    tmp_ctx = tempfile.TemporaryDirectory(prefix="foundry-demo-") if workdir is None else None
    root = Path(tmp_ctx.name) if tmp_ctx else Path(workdir)  # type: ignore[union-attr]
    started = time.monotonic()
    try:
        repo = write_demo_repo(root / "repo")
        clock = VirtualClock()
        config = FoundryConfig(
            db_path=root / "foundry.db",
            rpm=60,
            tpm=20_000,
            max_concurrent=3,
            board_bridge_mode=board,  # type: ignore[arg-type]
        )
        from foundry.rate_limiter import RateLimiter

        limiter = RateLimiter(config.rpm, config.tpm, config.max_concurrent, clock=clock.monotonic, sleep=clock.sleep)
        claude = demo_claude(repo)
        gemma = demo_gemma()
        bridge = BoardBridge(board)  # type: ignore[arg-type]
        factory = build_factory(
            config,
            claude=claude,
            gemma=gemma,
            repo_root=repo,
            workspace=repo,
            bridge=bridge,
            limiter=limiter,
            queue_clock=clock.utc,
            telemetry_clock=clock.epoch,
            patches_dir=root / "patches",
        )
        say("Foundry phase 1 demo: simulated Claude builders/reviewers, simulated Gemma Shop Foreman")
        say(f"  teams {', '.join(config.teams)} x {config.builders_per_team} builder slots + 1 reviewer; "
            f"budget {config.rpm} rpm / {config.tpm} tpm, {config.max_concurrent} Claude calls at once")
        say()
        result = await _run_line(factory, clock, say)
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        say()
        say(f"Done in {result['elapsed_s']}s wall time ({clock.offset:.0f}s of simulated waiting).")
        result["output"] = "\n".join(out)
        factory.close()
        return result
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


async def _run_line(f: Factory, clock: VirtualClock, say: Any) -> dict[str, Any]:
    manager, queue, claude = f.manager, f.queue, f.claude
    say("1. Sizing (Shop Foreman) and dispatch (Floor Manager)")
    task_ids: list[str] = []
    for order in DEMO_ORDERS:
        tid = await manager.submit(WorkOrder(**order))
        d = manager.directives[tid]
        task_ids.append(tid)
        say(f"   {tid} '{order['title']}' -> {d.complexity}: {d.allocated_builders} builder(s) on team {d.team_id} "
            f"[{f.foreman.last_source.get('size_task_scope', '?')}]")

    async with f.running():
        states = await f.wait(task_ids, timeout=30)
    say()
    say("2. Selective dispatch: Claude calls per builder slot (idle slots never call Claude)")
    for team in f.config.teams:
        row = "  ".join(f"{a}={claude.calls.get(a, 0)}" for a in f.config.builder_ids(team))
        say(f"   {team:<8} {row}  | reviewer {f.config.reviewer_id(team)}={claude.calls.get(f.config.reviewer_id(team), 0)}")

    say()
    say("3. Deterministic gates, fix hint and retry")
    retries = [e for e in queue.events() if e["kind"] == "retry"]
    for e in retries:
        p = e["payload"]
        say(f"   {e['task_id']} {p.get('ticket_id')}: gate failed -> hint: {p.get('fix_hint')}")
        say(f"   {e['task_id']}: ticket reopened for attempt {p.get('attempt', 0) + 1} (max 3)")
    if not retries:
        say("   (no gate failures)")
    failing_prompts = claude.prompts_for(FAILING_BUILDER)
    if len(failing_prompts) > 1:
        say(f"   {FAILING_BUILDER} retry prompt carries the hint: "
            f"{'## Fix required' in failing_prompts[1]}")

    say()
    say("4. Review (diff-only payloads)")
    for tid in task_ids:
        verdicts = queue.verdicts(tid)
        summary = ", ".join(f"{v.get('reviewer_id')}:{v.get('verdict')}" for v in verdicts)
        say(f"   {tid} [{states[tid]}] {summary}")

    say()
    say("5. Shop Foreman line check -> escalation -> Floor Manager action")
    alert = await f.foreman.evaluate_line_anomaly()
    action = None
    if alert is None:
        say("   line healthy, no escalation")
    else:
        say(f"   {alert.alert_id} {alert.severity.value} {alert.category.value}: {alert.diagnosis}")
        say(f"   metrics: tpm={alert.metrics_snapshot.current_tpm:.0f} queue_depth={alert.metrics_snapshot.queue_depth}")
        action = await manager.apply_escalation(alert)
        say(f"   Floor Manager -> {action.action}: {action.detail} (applied={action.applied})")

    say()
    say("6. PR summaries (drafted by the Shop Foreman)")
    for tid in task_ids:
        s = manager.pr_summaries.get(tid)
        if s:
            say(f"   {tid}: {s.get('commit_message')}")
    last = manager.pr_summaries.get(task_ids[-1], {})
    body = " / ".join(ln.strip() for ln in last.get("pr_body", "").splitlines() if ln.strip())
    say(f"   {task_ids[-1]} PR body ({f.foreman.last_source.get('draft_pr_summary', '?')}): {body[:160]}")
    for tid in task_ids:
        patch = manager.patch_paths.get(tid)
        if patch:
            say(f"   {tid} patch: {patch.name} ({len(patch.read_text(encoding='utf-8').splitlines())} lines; apply with git apply)")

    snap = f.telemetry.snapshot()
    saved = f.telemetry.tokens_saved_by_source()
    say()
    say("7. Telemetry and board")
    say(f"   Claude calls: {claude.total_calls} (builders {claude.role_calls.get('builder', 0)}, "
        f"reviewers {claude.role_calls.get('reviewer', 0)}, manager {claude.role_calls.get('manager', 0)})")
    say(f"   tokens saved: {json.dumps(saved)}; peak in flight {f.limiter.peak_in_flight}/{f.config.max_concurrent}")
    ops = getattr(f.bridge, "ops", [])
    counts: dict[str, int] = {}
    for op in ops:
        counts[op["type"]] = counts.get(op["type"], 0) + 1
    say(f"   board ops ({f.bridge.mode}): {json.dumps(counts)}")
    escal = [op["args"]["message"] for op in ops if op["type"] == "reportProgress" and op["args"]["message"].startswith("[Foreman]")]
    for m in escal:
        say(f"   board <- {m}")

    return {
        "task_ids": task_ids,
        "states": states,
        "directives": {t: manager.directives[t].model_dump() for t in task_ids},
        "calls": dict(claude.calls),
        "retries": len(retries),
        "alert": alert.model_dump(mode="json") if alert else None,
        "action": action.__dict__ if action else None,
        "pr_summaries": dict(manager.pr_summaries),
        "patches": {t: str(p) for t, p in manager.patch_paths.items()},
        "board_ops": list(ops),
        "telemetry": snap,
    }


# --------------------------------------------------------------------------- real mode (phase 2)

def claude_args(config: FoundryConfig) -> list[str]:
    """Extra ``claude -p`` flags for diff-only Foundry roles (from the config).

    ``config.claude_extra_args`` holds the safe defaults (few turns, no
    Bash/Edit/Write/agent tools); ``config.claude_append_prompt`` is passed as
    ``--append-system-prompt`` unless the args already carry one.
    """
    args = [str(a) for a in (getattr(config, "claude_extra_args", None) or [])]
    prompt = getattr(config, "claude_append_prompt", None)
    if prompt and "--append-system-prompt" not in args:
        args += ["--append-system-prompt", str(prompt)]
    return args


#: Exit codes of ``--real``: done / failed or quarantined / still in progress (rerun to resume).
EXIT_DONE, EXIT_FAILED, EXIT_IN_PROGRESS = 0, 1, 3


_TERMINAL_STATES = ("complete", "quarantined", "failed")


def _state(task: Any) -> str:
    raw = (task or {}).get("state", "unknown")
    return str(getattr(raw, "value", raw))


def _same_repo(a: str | None, b: Path) -> bool:
    """Same repository? (case-insensitive on Windows, like the queue's repo_root matching)."""
    if not a:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.abspath(str(p))))  # noqa: E731
    return norm(a) == norm(b)


def _needs_finishing(factory: Factory, tid: str) -> bool:
    """Approved (``complete``) but the commit message / patch were never written (run cut short)."""
    return _state(factory.queue.get_task(tid)) == "complete" and tid not in factory.manager.pr_summaries


def _find_open_order(factory: Factory, order: WorkOrder, repo: Path) -> str | None:
    """A not-yet-finished task of *this* repo from an earlier run with the same title and files.

    A task that was approved but whose patch was never written (the earlier run
    stopped while the Shop Foreman was still drafting) counts as unfinished.
    """
    for tid, old in factory.manager.orders.items():
        if old.title == order.title and sorted(old.files) == sorted(order.files):
            if not _same_repo(factory.manager.task_repo(tid), repo):
                continue
            if _state(factory.queue.get_task(tid)) not in _TERMINAL_STATES or _needs_finishing(factory, tid):
                return tid
    return None


def other_repo_tasks(factory: Factory, repo: Path) -> list[str]:
    """Lines naming every unfinished task that belongs to another (or an unknown) repository."""
    lines = []
    for task in factory.queue.all_tasks():
        if _state(task) in _TERMINAL_STATES:
            continue
        tid = task["task_id"]
        other = factory.manager.task_repo(tid)
        if other is None:
            lines.append(f"{tid} (unknown repo) is unfinished and is not dispatched by --real")
        elif not _same_repo(other, repo):
            lines.append(f"{tid} (repo {other}) is unfinished - rerun with --repo \"{other}\" to resume it")
    return lines


def format_local_time(when: datetime, now: datetime | None = None) -> str:
    """``15:00`` today, otherwise ``Sun 8 Nov 15:00``."""
    now = now or datetime.now()
    if when.date() == now.date():
        return when.strftime("%H:%M")
    return f"{when:%a} {when.day} {when:%b %H:%M}"


def _account_error(factory: Factory) -> dict[str, Any] | None:
    fn = getattr(factory.queue, "kv_get", None)
    err = fn("last_account_error") if callable(fn) else None
    return err if isinstance(err, dict) else None


def explain_task(factory: Factory, tid: str) -> tuple[str, list[str]]:
    """Return ``(state, lines)`` describing where a task stands, for the end of ``--real``."""
    task = factory.queue.get_task(tid) or {}
    state = _state(task)
    d = factory.manager.directives.get(tid)
    lines = [f"{tid}: {state}" + (f" ({d.complexity}, {d.allocated_builders} builder(s) on {d.team_id})" if d else "")]
    if state in ("quarantined", "failed") and task.get("quarantine_reason"):
        lines.append(f"  reason: {task['quarantine_reason']}")
    errors = [t.infra_last_error for t in factory.queue.tickets_for_task(tid) if getattr(t, "infra_last_error", None)]
    if errors:
        lines.append(f"  last infrastructure error: {errors[-1][:160]}")
    if state in _TERMINAL_STATES:
        return state, lines
    limiter = factory.limiter
    remaining = float(limiter.pause_remaining()) if hasattr(limiter, "pause_remaining") else 0.0
    err = _account_error(factory)
    kind = str((err or {}).get("type", ""))
    detail = str((err or {}).get("message", ""))[:160]
    if "Auth" in kind:
        lines.append("  Claude Code is not logged in - run claude and /login, then rerun the same command")
    elif "NotFound" in kind:
        lines.append("  Claude Code was not found - install it or set FOUNDRY_CLAUDE_BIN, then rerun the same command")
    elif remaining > 0:
        until = format_local_time(datetime.now() + timedelta(seconds=remaining))
        cause = "usage limit" if (not kind or "RateLimited" in kind) else f"{kind}: {detail}"
        lines.append(f"  paused until {until} ({cause}) - rerun the same command to resume")
    else:
        lines.append("  still in progress - rerun the same command to resume where it stopped")
    return state, lines


#: Upper bound (seconds) on waiting for the Shop Foreman's commit message and the
#: patch file after the reviewer approved. Gemma requests time out after 60 s
#: and the Foreman then falls back to a heuristic draft, so this is generous.
FINISH_TIMEOUT_S = 600.0


def _abs(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - defensive
        return Path(os.path.abspath(str(path)))


def task_outputs(factory: Factory, tid: str) -> dict[str, Path | None]:
    """Absolute paths of a task's output files: ``patch``, ``rejected`` and ``md`` (None when absent).

    Uses what this run wrote; otherwise looks up the file stem the manager
    recorded (``F-001`` or ``F-001-2``) in the patches folder, so a task
    finished by an earlier run is reported too.
    """
    m = factory.manager
    patch = m.patch_paths.get(tid)
    rejected = m.rejected_patches.get(tid)
    folder = getattr(m, "patches_dir", None)
    name = (getattr(m, "patch_names", None) or {}).get(tid)
    md = None
    if folder is not None and name:
        if patch is None and rejected is None:
            good, bad = Path(folder) / f"{name}.patch", Path(folder) / f"{name}.patch.rejected"
            patch = good if good.is_file() else None
            rejected = bad if (patch is None and bad.is_file()) else None
        md_path = Path(folder) / f"{name}.md"
        md = md_path if md_path.is_file() else None
    elif patch is not None or rejected is not None:
        md_path = Path(patch or rejected).with_name(f"{Path(patch or rejected).name.split('.patch')[0]}.md")  # type: ignore[arg-type]
        md = md_path if md_path.is_file() else None
    return {
        "patch": _abs(patch) if patch else None,
        "rejected": _abs(rejected) if rejected else None,
        "md": _abs(md) if md else None,
    }


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def completion_report(
    factory: Factory, tid: str, repo: Path, *, windows: bool | None = None
) -> list[str]:
    """Lines telling a ``complete`` task's user where the output is and exactly what to run next."""
    windows = (os.name == "nt") if windows is None else windows
    view = "type" if windows else "cat"
    files = task_outputs(factory, tid)
    summary = factory.manager.pr_summaries.get(tid) or {}
    subject = _first_line(summary.get("commit_message", ""))
    patch, rejected, md = files["patch"], files["rejected"], files["md"]
    lines = [""]
    if subject:
        lines.append(f"Commit message: {subject}")
    if patch is not None:
        empty = patch.stat().st_size == 0 if patch.is_file() else False
        lines.append(f"Patch:          {patch}")
        if md is not None:
            lines.append(f"Message + PR:   {md}")
        lines.append("")
        if empty:
            lines.append("The approved change is empty, so there is nothing to apply.")
            return lines
        lines.append("Your repository has not been changed yet. Open a terminal in the repository folder")
        lines.append(f"  {repo}")
        lines.append("and run:")
        lines.append(f"  1. look at the change:  {view} \"{patch}\"")
        lines.append(f"  2. apply it:            git apply \"{patch}\"")
        if md is not None:
            lines.append("  3. commit it:           git add -A")
            lines.append(f"                          git commit -F \"{md}\"")
        return lines
    if rejected is not None:
        lines.append("The patch was REJECTED: the approved changes do not apply cleanly to your repository,")
        lines.append("so there is nothing to apply automatically. It was saved for a manual merge as")
        lines.append(f"  {rejected}")
        if md is not None:
            lines.append(f"Message + PR:   {md}")
        lines.append(f"Look at it with:  {view} \"{rejected}\"")
        return lines
    folder = getattr(factory.manager, "patches_dir", None)
    lines.append("No patch file was written for this task" + (f" (looked in {_abs(Path(folder))})." if folder else "."))
    return lines


async def _finish_task(factory: Factory, tid: str, timeout: float, out: Any) -> bool:
    """Wait (bounded) until the commit message is drafted and the patch is written; True when it was."""
    manager = factory.manager
    if tid in manager.pr_summaries:
        return True
    out(f"{tid}: approved - the Shop Foreman is writing the commit message and the patch...")
    try:
        await asyncio.wait_for(manager.on_task_complete(tid), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return tid in manager.pr_summaries


async def run_real(
    args: argparse.Namespace,
    config: FoundryConfig,
    claude: Any,
    gemma: Any = None,
    *,
    out: Any = print,
    finish_timeout: float = FINISH_TIMEOUT_S,
) -> int:
    """Phase 2: one work order with headless Claude Code and Ollama (after the preflight).

    The target repo is only read: Claude runs in it (cwd) with diff-only output,
    gates patch scratch copies, and the approved result is written to
    ``config.patches_dir/<task>.patch`` for you to ``git apply``.

    After the reviewer approves, the run waits (up to ``finish_timeout``) for
    the Shop Foreman's commit message and the patch file before shutting the
    line down, then prints their absolute paths and the commands to view and
    apply the patch (see :func:`completion_report`).

    Rerunning the same command resumes an unfinished task with the same title
    and files instead of submitting it again (including an approved task whose
    patch was not written yet). Returns :data:`EXIT_DONE`, :data:`EXIT_FAILED`
    (quarantined/failed) or :data:`EXIT_IN_PROGRESS` (timed out, paused at the
    usage limit, or the patch is not written yet). The factory is always closed.
    """
    if gemma is None:
        from foundry.gemma_client import OllamaGemmaClient

        gemma = OllamaGemmaClient(config.gemma_base_url, config.gemma_model, timeout=config.gemma_timeout_s)
    repo = Path(args.repo).resolve()
    factory = build_factory(
        config, claude=claude, gemma=gemma, repo_root=repo, workspace=repo, dispatch_repo=repo
    )
    state = "unknown"
    try:
        forget = getattr(factory.queue, "kv_delete", None)
        if callable(forget):
            forget("last_account_error")  # only explain errors from this run
        for line in other_repo_tasks(factory, repo):
            out(line)
        order = WorkOrder(
            title=args.title, description=args.desc or "", files=list(args.files or []), repo_root=str(repo)
        )
        tid = _find_open_order(factory, order, repo)
        if tid:
            out(f"{tid}: resuming the unfinished task from the last run")
        else:
            tid = await factory.manager.submit(order)
        d = factory.manager.directives[tid]
        out(f"{tid}: {d.complexity}, {d.allocated_builders} builder(s) on {d.team_id}")
        finished = True
        async with factory.running():
            try:
                await factory.wait([tid], timeout=args.timeout)
            except asyncio.TimeoutError:
                out(f"{tid}: not finished after {args.timeout:g}s")
            if _state(factory.queue.get_task(tid)) == "complete":
                # The commit message and the patch are written by a callback that
                # can outlast the pools' shutdown grace (a real Gemma draft takes
                # longer than a few seconds); leaving now would cancel it.
                finished = await _finish_task(factory, tid, finish_timeout, out)
        state, lines = explain_task(factory, tid)
        for line in lines:
            out(line)
        if state == "complete":
            if finished:
                for line in completion_report(factory, tid, repo):
                    out(line)
            else:
                out(f"  approved, but the patch was not written within {finish_timeout:g}s "
                    "(the Shop Foreman is still drafting) - rerun the same command to finish it")
                state = "finishing"
    finally:
        factory.close()
    if state == "complete":
        return EXIT_DONE
    if state in ("quarantined", "failed"):
        return EXIT_FAILED
    return EXIT_IN_PROGRESS


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    p = argparse.ArgumentParser(prog="python -m foundry.main", description="Foundry factory orchestrator.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true", help="run the simulated end-to-end demo (default)")
    mode.add_argument("--real", action="store_true", help="phase 2: headless Claude Code + Ollama Gemma")
    p.add_argument("--board", choices=["dry", "auto", "off"], default=None,
                   help="board bridge mode (simulate default: dry; real default: FOUNDRY_BOARD_BRIDGE_MODE or auto)")
    p.add_argument("--workdir", help="keep demo files (repo, foundry.db) here instead of a temp dir")
    p.add_argument("--json", action="store_true", help="simulate: print the result summary as JSON at the end")
    p.add_argument("--repo", default=".", help="real: repository the builders work in")
    p.add_argument("--title", default="", help="real: work order title")
    p.add_argument("--desc", default="", help="real: work order description")
    p.add_argument("--files", nargs="*", help="real: files the work order may touch")
    p.add_argument("--timeout", type=float, default=3600.0, help="real: seconds to wait for the task")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if not args.real:
        workdir = Path(args.workdir) if args.workdir else None
        if workdir:
            workdir.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(run_demo(workdir=workdir, board=args.board or "dry"))
        if args.json:
            print(json.dumps({k: v for k, v in result.items() if k != "output"}, indent=2, default=str))
        return 0 if all(s in ("complete", "quarantined") for s in result["states"].values()) else 1

    from foundry.foreman_probe import EXIT_NOT_READY, probe

    config = FoundryConfig.from_env(**({"board_bridge_mode": args.board} if args.board else {}))
    if not args.title:
        print("--real needs --title (and usually --files)", file=sys.stderr)
        return EXIT_NOT_READY
    from foundry.claude_client import ClaudeCodeClient, ClaudeError, resolve_claude_bin

    try:
        claude = ClaudeCodeClient(resolve_claude_bin(config.claude_bin), extra_args=claude_args(config))
        validate = getattr(claude, "validate_launch", None)
        if callable(validate):
            validate()  # bad flags (e.g. quoting through claude.cmd) fail here, not on every ticket
    except ClaudeError as exc:
        print(f"Claude Code is not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    r = probe(config.gemma_base_url, config.gemma_model)
    if not r.ok:
        print(r.message, file=sys.stderr)
        return EXIT_NOT_READY
    return asyncio.run(run_real(args, config, claude))


if __name__ == "__main__":
    sys.exit(main())
