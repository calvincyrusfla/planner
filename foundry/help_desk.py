"""Help desk: answer fact questions in a help request from the code library.

On each ``help_requested`` event the desk pulls identifiers out of the question,
looks them up in the code library built for the task's base snapshot, and, only
for a fact question whose every name resolves, posts the library's facts with the
board operation ``answerHelp``. Everything else is left for people. Each request
leaves a ``help_desk`` event saying what happened.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from foundry.code_library import Library, build_library, open_library

log = logging.getLogger("foundry.help_desk")

__all__ = ["HelpDesk", "help_desk_enabled"]

KV_KEY = "help_desk:last_event"
INTERVAL_S = 15.0
MAX_ANSWER_CHARS = 5500
HEADER = "Auto-answer from the code library (help desk):"
MAX_NAMES = 8

_FACT = re.compile(
    r"\bwhere\b|\bwhat(?:'s| is| are)?\b[^?]{0,40}\b(?:called|named|name|signature)\b|\bsignature\b"
    r"|\bwhich (?:file|test|module|function|class)\b|\bwhat file\b|\bdoes\b[^?]{0,60}\bexist\b"
    r"|\bis there (?:a|an|any)\b|\bexists?\b|\bcalled\b|\bwhat (?:are )?the (?:args|arguments|parameters)\b",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"\bshould (?:i|we)\b|\bwhich (?:approach|option|way)\b|\bor should\b|\bprefer\b|\bdecide\b|\bscope\b"
    r"|\b(?:allowed|permitted|ok|okay) to\b|\bmay i\b|\bcan i (?:also )?(?:change|edit|modify|touch)\b"
    r"|\bwould you rather\b|\bis it (?:ok|okay|fine)\b",
    re.IGNORECASE,
)
_BACKTICK = re.compile(r"`([^`\n]+)`")
_PATH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:py|mjs|cjs|js|ts|html|json|md|bat|sh))\b")
_CALL = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)\(")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_PATH_SUFFIXES = (".py", ".mjs", ".cjs", ".js", ".ts", ".html", ".json", ".md", ".bat", ".sh")


def help_desk_enabled() -> bool:
    """FOUNDRY_HELP_DESK (default on): 0/false/no/off turns the desk off."""
    raw = os.environ.get("FOUNDRY_HELP_DESK", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _is_fact_question(text: str) -> bool:
    return bool(_FACT.search(text)) and not _DECISION.search(text)


def extract_identifiers(text: str) -> list[str]:
    """Backticked names, ``Name.attr``, ``name(`` and repo-relative paths, in order, without repeats."""
    found: list[tuple[int, str]] = []
    for m in _BACKTICK.finditer(text):
        item = m.group(1).strip().rstrip("()").strip()
        if item and " " not in item:
            found.append((m.start(), item))
    for rx in (_PATH, _CALL, _DOTTED):
        for m in rx.finditer(text):
            found.append((m.start(1), m.group(1)))
    out: list[str] = []
    for _, name in sorted(found):
        if name not in out:
            out.append(name)
    return out


class HelpDesk:
    """Answers fact questions from the code library; leaves the rest for people."""

    def __init__(self, service: Any, *, interval_s: float = INTERVAL_S) -> None:
        self.service = service
        self.interval_s = interval_s
        self.rounds = 0
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._dir: Path | None = None
        self._libs: dict[str, Library] = {}

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        if not help_desk_enabled():
            return
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._libs.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_s)
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the desk must outlive a bad round
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("help desk: round failed: %s", self.last_error)

    # ------------------------------------------------------------------ round

    def _queue(self) -> Any:
        return getattr(getattr(self.service, "factory", None), "queue", None)

    async def round(self) -> None:
        """Handle every new ``help_requested`` event; never raises."""
        try:
            if not help_desk_enabled():
                return
            queue = self._queue()
            if queue is None:
                return
            self.rounds += 1
            last = queue.kv_get(KV_KEY, 0)
            last = last if isinstance(last, int) else 0
            for event in queue.events(since_id=last, kind="help_requested"):
                try:
                    await self._handle(queue, event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("help desk: event %s failed: %s", event.get("id"), self.last_error)
                    self._note(queue, event, False, f"error: {self.last_error}")
                queue.kv_set(KV_KEY, int(event["id"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("help desk: round failed: %s", self.last_error)

    def _note(self, queue: Any, event: dict[str, Any], answered: bool, reason: str) -> None:
        try:
            queue.record_event("help_desk", event.get("task_id"), {
                "event_id": event.get("id"),
                "ticket_id": (event.get("payload") or {}).get("ticket_id"),
                "answered": answered,
                "reason": reason,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("help desk: could not record the outcome: %s", exc)

    def _piece(self, queue: Any, task_id: str) -> tuple[str, str] | None:
        with queue.lock:
            row = queue.conn.execute(
                "SELECT batch_id, key FROM ledger_pieces WHERE foundry_task=? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return (str(row["batch_id"]), str(row["key"])) if row else None

    async def _library(self, task_id: str) -> Library | None:
        lib = self._libs.get(task_id)
        if lib is not None:
            return lib
        root = await self.service.factory.manager.task_base(task_id)
        if root is None:
            return None
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="foundry-help-desk-"))
        db = self._dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', task_id)}.sqlite"
        await asyncio.to_thread(build_library, root, db)
        lib = open_library(db)
        self._libs[task_id] = lib
        return lib

    @staticmethod
    def _resolve(lib: Library, name: str) -> list[dict[str, Any]]:
        """The entries ``name`` stands for: a path gives its outline, a name its exact matches."""
        if "/" in name or name.endswith(_PATH_SUFFIXES):
            try:
                return list(lib.outline(name))[:5]
            except Exception:  # noqa: BLE001
                return []
        if not _IDENT.match(name):
            return []
        out: list[dict[str, Any]] = []
        for e in lib.find(name, 20):
            qname = str(e["call"]).split("::", 1)[-1]
            if (qname if "." in name else qname.rsplit(".", 1)[-1]) == name:
                out.append(e)
        return out[:3]

    async def _handle(self, queue: Any, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        task_id = event.get("task_id")
        question = str(payload.get("question") or "")
        text = "\n".join([question, str(payload.get("context") or ""), *map(str, payload.get("options") or [])])
        if not task_id:
            return self._note(queue, event, False, "no task")
        if not _is_fact_question(question):
            return self._note(queue, event, False, "not a fact question")
        names = extract_identifiers(text)[:MAX_NAMES]
        if not names:
            return self._note(queue, event, False, "no identifiers in the question")
        piece = self._piece(queue, str(task_id))
        if piece is None:
            return self._note(queue, event, False, "no board subtask for the task")
        lib = await self._library(str(task_id))
        if lib is None:
            return self._note(queue, event, False, "no code library for the task")
        resolved: list[tuple[str, list[dict[str, Any]]]] = []
        missing: list[str] = []
        for name in names:
            entries = self._resolve(lib, name)
            if entries:
                resolved.append((name, entries))
            else:
                missing.append(name)
        if missing:
            return self._note(queue, event, False, "unresolved: " + ", ".join(missing))
        parts = [HEADER]
        for name, entries in resolved:
            for e in entries:
                shown = lib.show(e["call"]) or ""
                parts.append(f"{e['call']}  {e['path']}:{e['start']}-{e['end']}  {e['signature']}\n{shown}")
        answer = "\n\n".join(parts)
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[: MAX_ANSWER_CHARS - 15].rstrip() + "\n... (cut off)"
        batch_id, key = piece
        await asyncio.to_thread(
            self.service.batch_watcher.board.op, "answerHelp",
            {"id": batch_id, "key": key, "answer": answer, "by": "help-desk"},
        )
        self._note(queue, event, True, "answered: " + ", ".join(n for n, _ in resolved))
