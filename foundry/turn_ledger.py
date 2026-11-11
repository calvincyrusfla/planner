"""Token ledger: where every Claude token goes, turn by turn.

``claude -p --output-format json`` reports only totals per call, but Claude Code
also writes a JSONL transcript of every session under its config folder. This
module reads those transcripts, splits each session's cache reads into the fixed
prefix, re-read tool results (per tool) and the rest of the conversation, and
stores the result in two tables of the queue's own SQLite database.

Every ``*_est`` value is an estimate: transcripts do not say what a cache read
contained, and tool result sizes are taken as characters / 4.

``python -m foundry.turn_ledger report [--since ISO] [--db PATH]`` prints the
split per role.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundry.config import FoundryConfig
from foundry.orchestrator_queue import FoundryQueue

__all__ = [
    "BACKGROUND_TASKS",
    "ToolResult",
    "ToolUse",
    "Turn",
    "find_transcript",
    "main",
    "parse_tool_results",
    "parse_turns",
    "record_session",
    "report",
    "split_cache_reads",
]

log = logging.getLogger(__name__)

#: Strong references to running recordings, so the event loop cannot drop one midway.
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

#: The name a tool result is counted under when its tool_use line is missing.
UNKNOWN_TOOL = "unknown"

_TOP_TOOLS = 5
_CHARS_PER_TOKEN = 4
_USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS turn_sessions (
    session_id                   TEXT PRIMARY KEY,
    task_id                      TEXT,
    agent_id                     TEXT,
    role                         TEXT,
    recorded_at                  TEXT NOT NULL,
    turns                        INTEGER NOT NULL,
    input_tokens                 INTEGER NOT NULL,
    cache_creation_input_tokens  INTEGER NOT NULL,
    cache_read_input_tokens      INTEGER NOT NULL,
    output_tokens                INTEGER NOT NULL,
    prefix_est                   INTEGER NOT NULL,
    conversation_est             INTEGER NOT NULL,
    tool_reread_est              TEXT NOT NULL
)
"""

_CREATE_RECORDS = """
CREATE TABLE IF NOT EXISTS turn_records (
    session_id                   TEXT NOT NULL,
    turn                         INTEGER NOT NULL,
    input_tokens                 INTEGER NOT NULL,
    cache_creation_input_tokens  INTEGER NOT NULL,
    cache_read_input_tokens      INTEGER NOT NULL,
    output_tokens                INTEGER NOT NULL,
    tools                        TEXT NOT NULL,
    PRIMARY KEY (session_id, turn)
)
"""


@dataclass
class ToolUse:
    """One ``tool_use`` block of an assistant turn."""

    id: str
    name: str
    input: Any = None


@dataclass
class Turn:
    """One API message of a session; ``index`` starts at 1."""

    index: int
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    tool_uses: list[ToolUse] = field(default_factory=list)


@dataclass
class ToolResult:
    """One ``tool_result`` block; ``after_turn`` is how many turns came before it."""

    tool_use_id: str
    size_est: int
    after_turn: int


# --------------------------------------------------------------------------- finding


def find_transcript(session_id: str, config_dir: Path | None = None) -> Path | None:
    """The transcript of ``session_id``, or None when there is none.

    Looks for ``projects/*/<session_id>.jsonl`` under ``config_dir``, else
    ``CLAUDE_CONFIG_DIR``, else ``~/.claude``. The project folder is never built
    from the cwd because its name is Claude Code's own encoding of the path.
    """
    if not session_id or any(ch in session_id for ch in "/\\\x00"):
        return None
    root = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or Path.home() / ".claude")
    try:
        projects = sorted((root / "projects").iterdir())
    except OSError:
        return None
    for project in projects:
        candidate = project / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- parsing


def _count(value: object) -> int:
    """A token count from the transcript; anything that is not one counts as 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _result_chars(content: object) -> int:
    """Characters of a tool result's content: a string or a list of text blocks."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, str):
            total += len(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            total += len(block["text"])
    return total


def _lines(path: Path) -> Iterable[dict[str, Any]]:
    """Every JSON object of the file; other lines are skipped."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield record


def _merge_assistant(turn: Turn, usage: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
    """Fold one transcript line of a message into its turn."""
    for usage_field in _USAGE_FIELDS:
        setattr(turn, usage_field, max(getattr(turn, usage_field), _count(usage.get(usage_field))))
    seen = {use.id for use in turn.tool_uses}
    for block in blocks:
        tool_id, name = block.get("id"), block.get("name")
        if block.get("type") != "tool_use" or not isinstance(tool_id, str) or not isinstance(name, str):
            continue
        if tool_id not in seen:
            seen.add(tool_id)
            turn.tool_uses.append(ToolUse(id=tool_id, name=name, input=block.get("input")))


def _read_transcript(path: Path) -> tuple[list[Turn], list[ToolResult]]:
    """Read the turns and tool results of a transcript in one pass."""
    turns: dict[str, Turn] = {}
    results: dict[str, ToolResult] = {}
    for record in _lines(path):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if record.get("type") == "assistant":
            message_id, usage = message.get("id"), message.get("usage")
            if not isinstance(message_id, str) or not message_id or not isinstance(usage, dict) or not usage:
                continue
            turn = turns.setdefault(message_id, Turn(index=len(turns) + 1))
            _merge_assistant(turn, usage, _content_blocks(message))
        elif record.get("type") == "user":
            for block in _content_blocks(message):
                tool_use_id = block.get("tool_use_id")
                if block.get("type") != "tool_result" or not isinstance(tool_use_id, str):
                    continue
                size = _result_chars(block.get("content")) // _CHARS_PER_TOKEN
                results.setdefault(tool_use_id, ToolResult(tool_use_id, size, len(turns)))
    return list(turns.values()), list(results.values())


def parse_turns(path: Path) -> list[Turn]:
    """The assistant turns of a transcript, each ``message.id`` counted once."""
    return _read_transcript(path)[0]


def parse_tool_results(path: Path) -> list[ToolResult]:
    """The tool results of a transcript, for :func:`split_cache_reads`."""
    return _read_transcript(path)[1]


# --------------------------------------------------------------------------- the split


def split_cache_reads(turns: Sequence[Turn], results: Sequence[ToolResult]) -> dict[str, Any]:
    """Totals of a session and the estimated split of its cache reads.

    ``prefix_est`` is turn 1's context times the number of turns,
    ``tool_reread_est`` maps each tool to its results' size times the turns after
    them, and ``conversation_est`` is the remaining cache reads, floored at 0.
    """
    count = len(turns)
    cache_reads = sum(turn.cache_read_input_tokens for turn in turns)
    prefix_est = 0
    if turns:
        first = turns[0]
        prefix_est = (
            first.input_tokens + first.cache_creation_input_tokens + first.cache_read_input_tokens
        ) * count
    names = {use.id: use.name for turn in turns for use in turn.tool_uses}
    tool_reread_est: dict[str, int] = {}
    for result in results:
        name = names.get(result.tool_use_id, UNKNOWN_TOOL)
        tool_reread_est[name] = tool_reread_est.get(name, 0) + result.size_est * (count - result.after_turn)
    return {
        "turns": count,
        "input_tokens": sum(turn.input_tokens for turn in turns),
        "cache_creation_input_tokens": sum(turn.cache_creation_input_tokens for turn in turns),
        "cache_read_input_tokens": cache_reads,
        "output_tokens": sum(turn.output_tokens for turn in turns),
        "prefix_est": prefix_est,
        "tool_reread_est": tool_reread_est,
        "conversation_est": max(cache_reads - prefix_est - sum(tool_reread_est.values()), 0),
    }


# --------------------------------------------------------------------------- storage


def _utc_text(value: str) -> str:
    """An ISO-8601 time as UTC text in one fixed format, so strings compare in time order."""
    text = value.strip()
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_tables(queue: FoundryQueue) -> None:
    with queue.transaction() as conn:
        conn.execute(_CREATE_SESSIONS)
        conn.execute(_CREATE_RECORDS)


def _store(
    queue: FoundryQueue,
    task_id: str,
    agent_id: str,
    role: str,
    session_id: str,
    turns: Sequence[Turn],
    split: dict[str, Any],
) -> None:
    """Write one session and its turns, replacing an earlier record of the same session."""
    with queue.transaction() as conn:
        _ensure_tables(queue)
        conn.execute("DELETE FROM turn_records WHERE session_id=?", (session_id,))
        conn.execute(
            "INSERT OR REPLACE INTO turn_sessions (session_id, task_id, agent_id, role, recorded_at, turns,"
            " input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens,"
            " prefix_est, conversation_est, tool_reread_est) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, task_id, agent_id, role, _utc_text(queue.now()), split["turns"],
                split["input_tokens"], split["cache_creation_input_tokens"],
                split["cache_read_input_tokens"], split["output_tokens"], split["prefix_est"],
                split["conversation_est"], json.dumps(split["tool_reread_est"], sort_keys=True),
            ),
        )
        conn.executemany(
            "INSERT INTO turn_records (session_id, turn, input_tokens, cache_creation_input_tokens,"
            " cache_read_input_tokens, output_tokens, tools) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    session_id, turn.index, turn.input_tokens, turn.cache_creation_input_tokens,
                    turn.cache_read_input_tokens, turn.output_tokens,
                    json.dumps([use.name for use in turn.tool_uses]),
                )
                for turn in turns
            ],
        )


def record_session(
    queue: FoundryQueue,
    task_id: str,
    agent_id: str,
    role: str,
    session_id: str,
    config_dir: Path | None = None,
) -> bool:
    """Find, parse, split and store one session; any problem returns False, never raises."""
    try:
        path = find_transcript(session_id, config_dir)
        if path is None:
            log.debug("turn ledger: no transcript for session %s", session_id)
            return False
        turns, results = _read_transcript(path)
        if not turns:
            log.debug("turn ledger: no assistant turns in %s", path)
            return False
        _store(queue, task_id, agent_id, role, session_id, turns, split_cache_reads(turns, results))
        return True
    except Exception:
        log.debug("turn ledger: could not record session %s", session_id, exc_info=True)
        return False


# --------------------------------------------------------------------------- report


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _role_lines(role: str, rows: Sequence[Any]) -> list[str]:
    sessions = len(rows)
    cache_reads = sum(int(row["cache_read_input_tokens"]) for row in rows)
    prefix = sum(int(row["prefix_est"]) for row in rows)
    conversation = sum(int(row["conversation_est"]) for row in rows)
    tools: dict[str, int] = {}
    for row in rows:
        for name, tokens in json.loads(row["tool_reread_est"]).items():
            tools[name] = tools.get(name, 0) + int(tokens)
    top = sorted(tools.items(), key=lambda item: (-item[1], item[0]))[:_TOP_TOOLS]
    detail = ", ".join(f"{name} {_share(tokens, cache_reads)}" for name, tokens in top)
    average_turns = sum(int(row["turns"]) for row in rows) / sessions
    return [
        f"{role}: {sessions} sessions, {average_turns:.1f} avg turns, "
        f"{cache_reads / sessions:,.0f} avg cache reads",
        f"  prefix_est: {_share(prefix, cache_reads)}",
        f"  tool_reread_est: {_share(sum(tools.values()), cache_reads)}" + (f" ({detail})" if detail else ""),
        f"  conversation_est: {_share(conversation, cache_reads)}",
    ]


def report(queue: FoundryQueue, since: str | None = None) -> str:
    """Per role: sessions, average turns and cache reads, and the estimated split.

    ``since`` is an ISO-8601 time (UTC when it has no offset); older sessions are
    left out. Raises ValueError when it cannot be parsed.
    """
    sql = "SELECT * FROM turn_sessions"
    args: list[str] = []
    if since is not None:
        sql += " WHERE recorded_at >= ?"
        args.append(_utc_text(since))
    _ensure_tables(queue)
    with queue.lock:
        rows = queue.conn.execute(sql + " ORDER BY role, rowid", args).fetchall()
    if not rows:
        return "No turn ledger sessions recorded."
    by_role: dict[str, list[Any]] = {}
    for row in rows:
        by_role.setdefault(str(row["role"]), []).append(row)
    lines = ["Share of cache reads per role (*_est are estimates and can add up to more than 100%)"]
    for role, role_rows in by_role.items():
        lines += ["", *_role_lines(role, role_rows)]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m foundry.turn_ledger report [--since ISO] [--db PATH]``."""
    parser = argparse.ArgumentParser(prog="python -m foundry.turn_ledger", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser("report", help="print the cache read split per role")
    report_parser.add_argument("--since", help="only sessions recorded at or after this ISO time")
    report_parser.add_argument("--db", type=Path, help="queue database (default: FOUNDRY_DB_PATH)")
    args = parser.parse_args(argv)
    db_path: Path = args.db or FoundryConfig.from_env().db_path
    if not db_path.is_file():
        print(f"no queue database at {db_path}", file=sys.stderr)
        return 1
    with FoundryQueue(db_path) as queue:
        try:
            print(report(queue, args.since))
        except ValueError:
            print(f"--since is not an ISO-8601 time: {args.since}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
