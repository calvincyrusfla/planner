"""Re-run a saved builder attempt on its original base commit, to measure a change.

Run only by hand, because every replay spends real Claude usage::

    python -m foundry.replay TICKET_ID [--attempt N] [--prompt-file PATH] [--model NAME]
        [--extra-args "..."] [--repo PATH] [--db PATH] [--dry-run]

The prompt comes from the archive :func:`foundry.builder_worker._save_prompt` writes
(the newest attempt unless ``--attempt`` says otherwise) or from ``--prompt-file``.
The base commit comes from the queue's ``base_repinned`` / ``base_pinned`` events.
Claude runs in a throwaway checkout of that commit. Tokens, turns, duration and
changes are printed and stored in the ``replays`` table of the queue database.

A replay never opens a PR, never writes to the board and never edits the user's
folder. The base snapshot stays in Foundry's snapshot folder for the next replay;
Foundry prunes it like any other. ``--dry-run`` prints the plan and calls nothing.

Exit codes: 0 the replay ran cleanly (or a dry run), 1 the replay ended in an
error (still stored), 2 the replay was refused before anything ran.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from foundry.base_snapshot import BaseSnapshots
from foundry.builder_worker import _DEFAULT_PROMPT_DIR
from foundry.claude_client import ClaudeClient, ClaudeCodeClient
from foundry.config import FoundryConfig, split_claude_args
from foundry.main import builder_claude_args
from foundry.orchestrator_queue import FoundryQueue
from foundry.piece_workspace import WorkspaceChanges, collect_changes, make_workspace, remove_workspace
from foundry.schemas import ClaudeResult

__all__ = [
    "ReplayDeps", "ReplayError", "ReplayPlan", "ReplayRecord", "base_sha_for", "build_plan",
    "diffstat", "execute", "find_prompt", "main", "store_replay",
]

_AGENT_ID = "replay"
#: The same limit builders run under (``claude_timeout`` in foundry/builder_worker.py).
_CLAUDE_TIMEOUT = 600.0
_ERROR_EXCERPT = 500
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

_REPLAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS replays (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    ticket_id          TEXT NOT NULL,
    attempt            INTEGER,
    variant            TEXT NOT NULL,
    base_sha           TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    num_turns          INTEGER,
    session_id         TEXT,
    duration_seconds   REAL NOT NULL DEFAULT 0,
    changed_files      TEXT NOT NULL DEFAULT '[]',
    outside_files      TEXT NOT NULL DEFAULT '[]',
    diffstat           TEXT NOT NULL DEFAULT '',
    is_error           INTEGER NOT NULL DEFAULT 0,
    error              TEXT
)
"""


class ReplayError(Exception):
    """The replay cannot start; the message says why."""


class SnapshotSource(Protocol):
    """The part of :class:`~foundry.base_snapshot.BaseSnapshots` a replay needs."""

    async def checkout(self, sha: str) -> Path:
        """A clean checkout of exactly ``sha``."""
        ...


def _real_client(config: FoundryConfig, args: list[str]) -> ClaudeClient:
    env: dict[str, str] | None = None
    if getattr(config, "builder_mode", "diff") == "edit" and getattr(config, "builder_tests", False):
        # builders that run tests need `python` to be Foundry's own interpreter, as in foundry/serve.py
        env = {"PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
    return ClaudeCodeClient(config.claude_bin, extra_args=args, env=env)


@dataclass(frozen=True)
class ReplayDeps:
    """Everything a replay reaches outside this module; tests pass fakes."""

    config: Callable[[], FoundryConfig] = FoundryConfig.from_env
    snapshots: Callable[[Path], SnapshotSource] = BaseSnapshots
    make_workspace: Callable[[Path, Path], Awaitable[Path]] = make_workspace
    collect_changes: Callable[[Path, Sequence[str]], Awaitable[WorkspaceChanges]] = collect_changes
    remove_workspace: Callable[[Path], Awaitable[None]] = remove_workspace
    client: Callable[[FoundryConfig, list[str]], ClaudeClient] = _real_client


@dataclass(frozen=True)
class ReplayPlan:
    """One fully resolved replay, ready to print or run."""

    ticket_id: str
    attempt: int | None
    prompt: str
    variant: str
    base_sha: str
    repo: Path
    allowed_files: list[str]
    claude_args: list[str]
    config: FoundryConfig


@dataclass
class ReplayRecord:
    """The outcome of one replay: one row of the ``replays`` table."""

    ticket_id: str
    attempt: int | None
    variant: str
    base_sha: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int | None = None
    session_id: str | None = None
    duration_seconds: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    outside_files: list[str] = field(default_factory=list)
    diffstat: str = ""
    is_error: bool = False
    error: str | None = None


def _safe_ticket(ticket_id: str) -> str:
    """``ticket_id`` as ``_save_prompt`` writes it in file names."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(ticket_id)).strip("._")[:100] or "ticket"


def find_prompt(ticket_id: str, attempt: int | None, directory: Path) -> tuple[Path, int]:
    """The saved prompt of ``attempt`` (None: the newest) and its attempt number."""
    safe = _safe_ticket(ticket_id)
    if attempt is not None:
        path = directory / f"{safe}_attempt{attempt}.txt"
        if not path.is_file():
            raise ReplayError(f"no saved prompt for attempt {attempt} of {ticket_id}: {path} does not exist")
        return path, attempt
    pattern = re.compile(re.escape(safe) + r"_attempt(\d+)\.txt")
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        names = []
    found = [int(match.group(1)) for match in map(pattern.fullmatch, names) if match]
    if not found:
        raise ReplayError(f"no saved prompt for {ticket_id} in {directory}; use --prompt-file")
    newest = max(found)
    return directory / f"{safe}_attempt{newest}.txt", newest


def _latest_sha(queue: FoundryQueue, task_id: str, kind: str, key: str) -> str | None:
    for event in reversed(queue.events(task_id=task_id, kind=kind)):
        sha = event["payload"].get(key)
        if isinstance(sha, str) and sha:
            return sha
    return None


def base_sha_for(queue: FoundryQueue, task_id: str) -> str:
    """The commit the task was built on: the latest re-pin, else the latest pin."""
    sha = _latest_sha(queue, task_id, "base_repinned", "new_sha") or _latest_sha(queue, task_id, "base_pinned", "sha")
    if sha is None:
        raise ReplayError(f"task {task_id} has no pinned base commit (no base_repinned or base_pinned "
                          "event), so it cannot be replayed on its original base")
    return sha


def _read_prompt(path: Path) -> str:
    try:
        prompt = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplayError(f"could not read the prompt {path}: {exc}") from exc
    if not prompt.strip():
        raise ReplayError(f"the prompt {path} is empty")
    return prompt


def build_plan(queue: FoundryQueue, args: argparse.Namespace, config: FoundryConfig) -> ReplayPlan:
    """Look up everything the replay needs; raises :class:`ReplayError` when something is missing."""
    ticket = queue.get_ticket(args.ticket)
    if ticket is None:
        raise ReplayError(f"no ticket {args.ticket} in {queue.path}")
    base_sha = base_sha_for(queue, ticket.task_id)
    attempt: int | None = args.attempt
    if args.prompt_file:
        path = Path(args.prompt_file)
    else:
        directory = Path(os.environ.get("FOUNDRY_PROMPT_DIR") or _DEFAULT_PROMPT_DIR)
        path, attempt = find_prompt(ticket.ticket_id, attempt, directory)
    prompt = _read_prompt(path)
    try:
        extra = split_claude_args(args.extra_args or "")
    except ValueError as exc:
        raise ReplayError(f"could not read --extra-args: {exc}") from exc
    if args.model:
        config = config.model_copy(update={"builder_model": args.model})
    task = queue.get_task(ticket.task_id) or {}
    variant = (f"prompt={path.name} model={config.builder_model or 'default'} "
               f"args={' '.join(extra) or 'none'}")
    return ReplayPlan(
        ticket_id=ticket.ticket_id,
        attempt=attempt,
        prompt=prompt,
        variant=variant,
        base_sha=base_sha,
        repo=Path(args.repo or task.get("repo_root") or ".").resolve(),
        allowed_files=list(ticket.allowed_files),
        claude_args=builder_claude_args(config) + extra,
        config=config,
    )


def diffstat(diff: str) -> str:
    """A one-line summary of a git diff. Hunk-aware, so a removed ``-- x`` line is counted."""
    files = added = removed = old = new = 0
    for line in diff.split("\n"):
        if old > 0 or new > 0:
            if line.startswith("+"):
                added += 1
                new -= 1
            elif line.startswith("-"):
                removed += 1
                old -= 1
            elif not line.startswith("\\"):
                old -= 1
                new -= 1
            continue
        if line.startswith("diff --git "):
            files += 1
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            old, new = int(hunk.group(1) or 1), int(hunk.group(2) or 1)
    return f"{files} file(s) changed, +{added} -{removed}"


async def execute(plan: ReplayPlan, deps: ReplayDeps) -> ReplayRecord:
    """Run the replay in a throwaway checkout. Failures end up in the record; the checkout always goes."""
    record = ReplayRecord(plan.ticket_id, plan.attempt, plan.variant, plan.base_sha)
    holder = Path(tempfile.mkdtemp(prefix="foundry-replay-"))
    dest = holder / "workspace"
    result: ClaudeResult | None = None
    started = time.monotonic()
    try:
        snapshot = await deps.snapshots(plan.repo).checkout(plan.base_sha)
        workspace = await deps.make_workspace(snapshot, dest)
        client = deps.client(plan.config, plan.claude_args)
        result = await client.run(plan.prompt, agent_id=_AGENT_ID, role="builder", cwd=str(workspace),
                                  timeout=_CLAUDE_TIMEOUT)
        record.input_tokens = result.input_tokens
        record.output_tokens = result.output_tokens
        record.cache_read_tokens = result.cache_read_tokens
        record.num_turns = result.num_turns
        record.session_id = result.session_id
        record.duration_seconds = result.duration_seconds
        record.is_error = result.is_error
        record.error = result.error
        changes = await deps.collect_changes(workspace, plan.allowed_files)
        record.changed_files = list(changes.changed)
        record.outside_files = list(changes.outside)
        record.diffstat = diffstat(changes.diff)
    except Exception as exc:  # noqa: BLE001 - a failed replay is still a result worth storing
        record.is_error = True
        record.error = f"{type(exc).__name__}: {exc}"[:_ERROR_EXCERPT]
    finally:
        await deps.remove_workspace(dest)
        shutil.rmtree(holder, ignore_errors=True)
    if result is None:
        record.duration_seconds = time.monotonic() - started
    return record


def store_replay(queue: FoundryQueue, record: ReplayRecord) -> int:
    """Add ``record`` to the ``replays`` table (created on first use); returns its id."""
    with queue.transaction() as conn:
        conn.execute(_REPLAYS_SCHEMA)
        cursor = conn.execute(
            "INSERT INTO replays (ts, ticket_id, attempt, variant, base_sha, input_tokens, output_tokens,"
            " cache_read_tokens, num_turns, session_id, duration_seconds, changed_files, outside_files,"
            " diffstat, is_error, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (queue.now(), record.ticket_id, record.attempt, record.variant, record.base_sha,
             record.input_tokens, record.output_tokens, record.cache_read_tokens, record.num_turns,
             record.session_id, record.duration_seconds, json.dumps(record.changed_files),
             json.dumps(record.outside_files), record.diffstat, int(record.is_error), record.error),
        )
        return int(cursor.lastrowid or 0)


def _attempt_text(attempt: int | None) -> str:
    return "from --prompt-file" if attempt is None else str(attempt)


def _print_plan(plan: ReplayPlan) -> None:
    print(f"dry run of {plan.ticket_id}: nothing is called, stored or changed")
    print(f"  attempt: {_attempt_text(plan.attempt)}")
    print(f"  variant: {plan.variant}")
    print(f"  base: {plan.base_sha}")
    print(f"  repo: {plan.repo}")
    print(f"  prompt: {len(plan.prompt)} characters")
    print(f"  allowed files: {', '.join(plan.allowed_files) or 'none'}")
    print(f"  command: {shlex.join([plan.config.claude_bin, '-p', '--output-format', 'json', *plan.claude_args])}")


def _print_record(record: ReplayRecord) -> None:
    print(f"replay of {record.ticket_id}")
    print(f"  attempt: {_attempt_text(record.attempt)}")
    print(f"  variant: {record.variant}")
    print(f"  base: {record.base_sha}")
    print(f"  tokens: input {record.input_tokens}, output {record.output_tokens}, "
          f"cache read {record.cache_read_tokens}")
    print(f"  turns: {'unknown' if record.num_turns is None else record.num_turns}")
    print(f"  session: {record.session_id or 'unknown'}")
    print(f"  duration: {record.duration_seconds:.1f}s")
    print(f"  changed files: {', '.join(record.changed_files) or 'none'}")
    print(f"  outside allowed files: {', '.join(record.outside_files) or 'none'}")
    print(f"  diffstat: {record.diffstat or 'none'}")
    print(f"  is_error: {str(record.is_error).lower()}")
    if record.error:
        print(f"  error: {record.error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m foundry.replay", allow_abbrev=False,
        description="Re-run a saved builder attempt on its original base commit. Spends real Claude usage.")
    parser.add_argument("ticket", metavar="TICKET_ID", help="the ticket to replay")
    parser.add_argument("--attempt", type=int, help="saved attempt to replay (default: the newest)")
    parser.add_argument("--prompt-file", help="use this prompt instead of the saved one")
    parser.add_argument("--model", help="builder model for this replay")
    parser.add_argument("--extra-args", help="extra claude flags, as one quoted string")
    parser.add_argument("--repo", help="repository holding the base commit (default: the task's)")
    parser.add_argument("--db", help="queue database (default: the configured one)")
    parser.add_argument("--dry-run", action="store_true", help="print what would run and call nothing")
    return parser


def _bind_extra_args(argv: Sequence[str]) -> list[str]:
    """Join ``--extra-args VALUE`` into one item: argparse rejects a value like ``--verbose``."""
    bound: list[str] = []
    items = iter(argv)
    for item in items:
        value = next(items, None) if item == "--extra-args" else None
        bound.append(item if value is None else f"{item}={value}")
    return bound


def main(argv: Sequence[str] | None = None, *, deps: ReplayDeps | None = None) -> int:
    """Command-line entry point; returns the exit code."""
    args = _parser().parse_args(_bind_extra_args(sys.argv[1:] if argv is None else argv))
    deps = deps or ReplayDeps()
    config = deps.config()
    db = Path(args.db) if args.db else Path(config.db_path)
    if not db.is_file():  # FoundryQueue would create an empty database here
        print(f"replay: no queue database at {db}", file=sys.stderr)
        return 2
    with FoundryQueue(db) as queue:
        try:
            plan = build_plan(queue, args, config)
        except ReplayError as exc:
            print(f"replay: {exc}", file=sys.stderr)
            return 2
        if args.dry_run:
            _print_plan(plan)
            return 0
        record = asyncio.run(execute(plan, deps))
        store_replay(queue, record)
    _print_record(record)
    return 1 if record.is_error else 0


if __name__ == "__main__":
    sys.exit(main())
