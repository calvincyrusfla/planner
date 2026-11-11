"""Quarantine report: what each attempt of a quarantined piece did, and its exact last error.

The :class:`QuarantineReporter` follows Foundry's ``quarantined`` events. For each one it
writes ``foundry/data/reports/<task_id>.md`` (per attempt: model/agent, turns, tokens,
files changed, why it was sent back; then the last error's exact lines) and records a
``quarantine_report`` event holding the path and a 1,500-character summary.

Gemma only picks which lines of the last error to quote (``error_excerpt``); without it
the last 1,500 characters are used. The last event id seen is kept in
``queue.kv_set("quarantine_report:last_event", id)``. Not wired into ``serve.py`` yet.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from foundry import prompt_archive
from foundry.gemma_excerpts import error_excerpt

log = logging.getLogger("foundry.quarantine_report")

__all__ = ["DEFAULT_POLL_S", "SUMMARY_CHARS", "TAIL_CHARS", "QuarantineReporter"]

DEFAULT_POLL_S = 15.0
SUMMARY_CHARS = 1500
TAIL_CHARS = 1500
GEMMA_TIMEOUT_S = 60.0
_CURSOR_KEY = "quarantine_report:last_event"
_REPORTS_DIR = Path(__file__).resolve().parent / "data" / "reports"


def _text(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


class QuarantineReporter:
    """Writes a Markdown report for each quarantined task; never raises out of a round."""

    def __init__(
        self,
        service: Any,
        *,
        reports_dir: Path | str | None = None,
        interval: float = DEFAULT_POLL_S,
    ) -> None:
        self.service = service
        self.reports_dir = Path(reports_dir) if reports_dir is not None else _REPORTS_DIR
        self.interval = float(interval) if interval and interval > 0 else DEFAULT_POLL_S
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.rounds = 0
        self.reported = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.round()
            await asyncio.sleep(self.interval)

    # ------------------------------------------------------------------ one round

    async def round(self) -> None:
        """Report every new ``quarantined`` event. Never raises (cancellation aside)."""
        async with self._lock:
            self.rounds += 1
            try:
                await self._round()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reporting must never take the service down
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("quarantine report: round failed: %s", self.last_error)

    async def _round(self) -> None:
        queue = getattr(getattr(self.service, "factory", None), "queue", None)
        if queue is None:
            return
        try:
            last = int(queue.kv_get(_CURSOR_KEY, 0) or 0)
        except (TypeError, ValueError):
            last = 0
        for event in queue.events(since_id=last, kind="quarantined"):
            try:
                await self._report(queue, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("quarantine report for %s failed: %s", event.get("task_id"), exc)
            queue.kv_set(_CURSOR_KEY, int(event["id"]))

    # ------------------------------------------------------------------ the report

    async def _report(self, queue: Any, event: dict[str, Any]) -> None:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            return
        reason = str((event.get("payload") or {}).get("reason") or "")
        events = [e for e in queue.events(task_id=task_id) if int(e["id"]) <= int(event["id"])]
        try:
            calls = prompt_archive.calls_for(queue, task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("quarantine report: archive unavailable: %s", exc)
            calls = []
        attempts = self._attempts(events, calls)
        raw_error = self._last_error(queue, task_id, events, reason)
        excerpt = await self._excerpt(raw_error)
        markdown = self._markdown(task_id, reason, attempts, excerpt)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{task_id}.md"
        path.write_text(markdown, encoding="utf-8")
        queue.record_event(
            "quarantine_report", task_id, {"path": str(path), "summary": markdown[:SUMMARY_CHARS]}
        )
        self.reported += 1

    @staticmethod
    def _attempts(events: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per attempt, in order: calls, files and the reasons it was sent back."""
        by_number: dict[int, dict[str, Any]] = {}

        def entry(number: int) -> dict[str, Any]:
            return by_number.setdefault(
                number, {"n": number, "calls": [], "files": [], "reasons": []}
            )

        current = 1
        for ev in events:
            kind, payload = ev.get("kind"), ev.get("payload") or {}
            item = entry(current)
            if kind == "submission":
                for name in payload.get("files") or []:
                    if str(name) not in item["files"]:
                        item["files"].append(str(name))
            elif kind == "gate_result" and not payload.get("passed", True):
                item["reasons"].append(f"gate `{payload.get('check_type')}` failed")
            elif kind == "verdict" and str(payload.get("verdict", "")).lower() not in ("approve", "approved"):
                item["reasons"].append(f"reviewer {payload.get('reviewer_id')}: {payload.get('verdict')}")
            elif kind == "help_requested":
                item["reasons"].append(f"help requested: {_text(payload.get('question') or payload.get('reason'))}")
            elif kind == "ticket_reopened":
                if payload.get("fix_hint"):
                    item["reasons"].append(f"sent back: {_text(payload['fix_hint'])}")
                current = int(payload.get("attempt") or current + 1)
            elif kind == "retry_exhausted" and payload.get("fix_hint"):
                item["reasons"].append(f"sent back: {_text(payload['fix_hint'])}")
        for call in calls:
            number = call.get("attempt")
            entry(int(number) if number is not None else 1)["calls"].append(call)
        return [by_number[n] for n in sorted(by_number)]

    @staticmethod
    def _last_error(queue: Any, task_id: str, events: list[dict[str, Any]], reason: str) -> str:
        """The exact text of the last failure: the last failed gate's output, else the hint/reason."""
        try:
            with queue.lock:
                row = queue.conn.execute(
                    "SELECT stdout, stderr FROM gate_results WHERE task_id=? AND passed=0 ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            if row is not None:
                text = "\n".join(p for p in (str(row["stderr"] or ""), str(row["stdout"] or "")) if p.strip())
                if text.strip():
                    return text
        except Exception:  # noqa: BLE001 - fall back to the events
            pass
        for ev in reversed(events):
            hint = (ev.get("payload") or {}).get("fix_hint")
            if ev.get("kind") in ("ticket_reopened", "retry_exhausted") and hint:
                return str(hint)
        return reason

    async def _excerpt(self, error: str) -> str:
        gemma = getattr(self.service, "gemma", None)
        if gemma is None:
            gemma = getattr(getattr(self.service, "factory", None), "gemma", None)
        if gemma is not None and error.strip():
            picked = await error_excerpt(gemma, error, timeout=GEMMA_TIMEOUT_S, max_chars=TAIL_CHARS)
            if picked:
                return picked
        return error[-TAIL_CHARS:]

    @staticmethod
    def _markdown(task_id: str, reason: str, attempts: list[dict[str, Any]], excerpt: str) -> str:
        out = [f"# Quarantine report: {task_id}", "", f"Reason: {_text(reason) or 'unknown'}", ""]
        for att in attempts:
            calls = att["calls"]
            turns = len(calls)
            tokens = sum(int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0) for c in calls)
            models = sorted({str(c.get("model") or c.get("agent_id") or "?") for c in calls})
            out.append(f"## Attempt {att['n']}")
            out.append(f"- Model: {', '.join(models) if models else 'unknown'}")
            out.append(f"- Turns: {turns}")
            out.append(f"- Tokens: {tokens}")
            out.append(f"- Files changed: {', '.join(att['files']) if att['files'] else 'none'}")
            if att["reasons"]:
                out.append("- Sent back because:")
                out.extend(f"  - {r}" for r in att["reasons"])
            out.append("")
        out.extend(["## Last error", "", "```text", excerpt or "(no error text)", "```", ""])
        return "\n".join(out)
