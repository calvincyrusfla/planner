"""Phone notifications through ntfy (https://ntfy.sh).

The :class:`Notifier` follows Foundry's own events and sends one short push when
something needs the user:

* a help request is still unanswered :data:`HELP_DELAY_S` after ``help_requested``
  (the help desk gets the first try);
* a piece is ``quarantined``;
* a PR is opened (``pr_opened``): one digest per batch, at most every
  :data:`PR_DIGEST_WINDOW_S`, listing the PRs;
* Foundry pauses for a usage limit. No event records this, so each round reads the
  limiter and the ``last_account_error`` entry the workers keep.

Settings come from the environment: ``FOUNDRY_NTFY_TOPIC`` (unset: the notifier is
off), ``FOUNDRY_NTFY_SERVER`` (default ``https://ntfy.sh``) and ``FOUNDRY_INBOX_URL``
(the link a tap on the notification opens).

Messages are fixed templates holding a piece id, its title and one line of reason.
A quarantine reason or help question can quote model output, so neither is ever
copied: the reason is mapped to a fixed sentence. A piece gets at most one
notification per kind, and a push that fails is logged and dropped.

State lives in the queue's kv store, so a restart neither repeats nor misses events:
``notifier:last_event`` holds the last event id seen of each kind. It is saved after
the round's pushes, and the per-piece ``notifier:sent:<kind>:<piece>`` marks stop a
re-read event from sending again. The first round ever starts at the newest event
rather than sending Foundry's whole history to the phone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

log = logging.getLogger("foundry.notifier")

__all__ = [
    "DEFAULT_NTFY_SERVER", "DEFAULT_POLL_S", "HELP_DELAY_S", "MAX_MESSAGE", "Notification", "Notifier",
    "PR_DIGEST_WINDOW_S", "SEND_TIMEOUT_S",
]

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_POLL_S = 15.0
#: How long a help request waits for the help desk before the phone is told (seconds).
HELP_DELAY_S = 600.0
#: A batch gets at most one PR digest per this many seconds.
PR_DIGEST_WINDOW_S = 120.0
#: Longest message body sent.
MAX_MESSAGE = 300
SEND_TIMEOUT_S = 10.0

_CURSOR_KEY = "notifier:last_event"
_PENDING_KEY = "notifier:pending"
_USAGE_KEY = "notifier:usage_paused"
_SENT_PREFIX = "notifier:sent:"
_MAX_TITLE = 80
_MAX_LINK = 120
#: The account error type of a usage limit (``claude_client.ClaudeRateLimited``).
_USAGE_ERROR = "ClaudeRateLimited"
#: The marker line of a batch order (``board_dispatch.marker``).
_BATCH_MARKER = re.compile(r"\(Foundry batch ([^\s,]+)(?: of [^,]+)?, subtask ")
#: Start of a quarantine reason -> the sentence sent in its place.
_QUARANTINE_REASONS = (
    ("no floor manager answer", "No answer to its help request in time."),
    ("infrastructure", "Repeated infrastructure failures."),
    ("retry limit", "Used up its build attempts."),
    ("reviewer rejected", "The reviewer rejected it."),
    ("sizing failed", "Sizing failed."),
    ("duplicate of", "Duplicate of another piece."),
    ("order could not be recovered", "Its order could not be recovered."),
)
_OTHER_QUARANTINE = "See the dashboard for the reason."


@dataclass(frozen=True)
class Notification:
    """One push. ``title``, ``tags``, ``priority`` and ``click`` become ntfy headers."""

    title: str
    message: str
    tags: str
    priority: str
    click: str | None = None


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _epoch(stamp: Any, default: float) -> float:
    """``stamp`` (ISO-8601) as seconds since the epoch; ``default`` when it can't be read."""
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _quarantine_line(reason: Any) -> str:
    text = " ".join(str(reason or "").lower().split())
    for start, sentence in _QUARANTINE_REASONS:
        if text.startswith(start):
            return sentence
    return _OTHER_QUARANTINE


class Notifier:
    """Sends Foundry's notifications to ntfy; not started by the service yet.

    Args:
        service: the :class:`foundry.serve.Service` (reads ``service.factory``).
        sender: delivers a :class:`Notification`, blocking; default POSTs to ntfy.
        clock: wall-clock seconds since the epoch; default :func:`time.time`.
        env: the settings; default :data:`os.environ`.
        interval: seconds between rounds.
    """

    def __init__(
        self,
        service: Any,
        *,
        sender: Callable[[Notification], Any] | None = None,
        clock: Callable[[], float] | None = None,
        env: Mapping[str, str] | None = None,
        interval: float = DEFAULT_POLL_S,
    ) -> None:
        settings = os.environ if env is None else env
        self.service = service
        self.topic = (settings.get("FOUNDRY_NTFY_TOPIC") or "").strip()
        self.server = ((settings.get("FOUNDRY_NTFY_SERVER") or "").strip() or DEFAULT_NTFY_SERVER).rstrip("/")
        self.click = (settings.get("FOUNDRY_INBOX_URL") or "").strip() or None
        self.interval = float(interval) if interval and interval > 0 else DEFAULT_POLL_S
        self._sender: Callable[[Notification], Any] = sender or self._post
        self._clock: Callable[[], float] = clock or time.time
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.rounds = 0
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> asyncio.Task[None] | None:
        if not self.enabled:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
            log.info("notifier: pushing to ntfy every %gs", self.interval)
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
        """Follow new events and send what is due. Never raises (cancellation aside)."""
        if not self.enabled:
            return
        async with self._lock:
            self.rounds += 1
            try:
                await self._round()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # notifications must never take the service down
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("notifier: round failed (retrying next round): %s", self.last_error)

    async def _round(self) -> None:
        factory = getattr(self.service, "factory", None)
        queue = getattr(factory, "queue", None)
        if queue is None:
            return
        cursors = self._cursors(queue)
        pending = self._pending(queue)
        for event in self._new_events(queue, "quarantined", cursors):
            await self._on_quarantined(queue, event)
        for event in self._new_events(queue, "help_requested", cursors):
            self._on_help_requested(queue, event, pending)
        for event in self._new_events(queue, "pr_opened", cursors):
            self._on_pr_opened(queue, event, pending)
        await self._flush_help(queue, pending)
        await self._flush_digests(queue, pending)
        self._save(queue, _PENDING_KEY, pending)
        self._save(queue, _CURSOR_KEY, cursors)
        await self._check_usage(queue, factory)

    # ------------------------------------------------------------------ state

    @staticmethod
    def _save(queue: Any, key: str, value: Any) -> None:
        if queue.kv_get(key) != value:
            queue.kv_set(key, value)

    @staticmethod
    def _cursors(queue: Any) -> dict[str, int]:
        stored = queue.kv_get(_CURSOR_KEY)
        return {str(kind): int(last) for kind, last in stored.items()} if isinstance(stored, Mapping) else {}

    @staticmethod
    def _pending(queue: Any) -> dict[str, dict[str, Any]]:
        stored = queue.kv_get(_PENDING_KEY)
        stored = stored if isinstance(stored, Mapping) else {}
        return {name: dict(stored.get(name) or {}) for name in ("help", "prs", "digest_at")}

    @staticmethod
    def _new_events(queue: Any, kind: str, cursors: dict[str, int]) -> list[dict[str, Any]]:
        """The events of ``kind`` since its cursor, which moves past them.

        Each kind keeps its own cursor: one id shared by separate reads could step
        over an event recorded between them. A kind seen for the first time starts
        at its newest event.
        """
        events: list[dict[str, Any]] = queue.events(since_id=cursors.get(kind, 0), kind=kind)
        first_time = kind not in cursors
        cursors[kind] = int(events[-1]["id"]) if events else cursors.get(kind, 0)
        return [] if first_time else events

    @staticmethod
    def _already_sent(queue: Any, kind: str, piece: str) -> bool:
        return bool(queue.kv_get(f"{_SENT_PREFIX}{kind}:{piece}", False))

    def _claim(self, queue: Any, kind: str, piece: str) -> bool:
        """Mark ``piece`` as notified for ``kind``; False when it already was."""
        if self._already_sent(queue, kind, piece):
            return False
        queue.kv_set(f"{_SENT_PREFIX}{kind}:{piece}", True)
        return True

    # ------------------------------------------------------------------ triggers

    @staticmethod
    def _piece_line(queue: Any, task_id: str) -> str:
        task = queue.get_task(task_id)
        title = _one_line(task.get("title"), _MAX_TITLE) if task else ""
        return f"{task_id} {title}".strip()

    async def _on_quarantined(self, queue: Any, event: Mapping[str, Any]) -> None:
        task_id = event.get("task_id")
        if not task_id or not self._claim(queue, "quarantined", str(task_id)):
            return
        reason = _quarantine_line((event.get("payload") or {}).get("reason"))
        message = f"{self._piece_line(queue, str(task_id))}\nQuarantined. {reason}"
        await self._push("Foundry: piece quarantined", message, "no_entry", "high")

    def _on_help_requested(self, queue: Any, event: Mapping[str, Any], pending: dict[str, dict[str, Any]]) -> None:
        task_id = event.get("task_id")
        if task_id and not self._already_sent(queue, "help_requested", str(task_id)):
            pending["help"][str(task_id)] = _epoch(event.get("ts"), self._clock())

    async def _flush_help(self, queue: Any, pending: dict[str, dict[str, Any]]) -> None:
        waiting = pending["help"]
        if not waiting:
            return
        parked = queue.help_requests()
        now = self._clock()
        for task_id, seen_at in list(waiting.items()):
            requests = [r for r in parked if str(r.get("task_id")) == task_id]
            if not requests:  # answered, or the piece ended
                del waiting[task_id]
                continue
            if now - min(_epoch(r.get("asked_at"), seen_at) for r in requests) < HELP_DELAY_S:
                continue
            del waiting[task_id]
            if self._claim(queue, "help_requested", task_id):
                minutes = int(HELP_DELAY_S // 60)
                message = f"{self._piece_line(queue, task_id)}\nHelp request unanswered for {minutes} minutes."
                await self._push("Foundry: help needed", message, "raising_hand", "high")

    @staticmethod
    def _batch_of(queue: Any, task_id: str) -> str:
        """The batch id in the task's order; a task outside any batch is its own batch."""
        task = queue.get_task(task_id)
        found = _BATCH_MARKER.findall(str(task.get("description") or "")) if task else []
        return str(found[-1]) if found else task_id

    def _on_pr_opened(self, queue: Any, event: Mapping[str, Any], pending: dict[str, dict[str, Any]]) -> None:
        task_id = event.get("task_id")
        if not task_id or self._already_sent(queue, "pr_opened", str(task_id)):
            return
        payload = event.get("payload") or {}
        items = pending["prs"].setdefault(self._batch_of(queue, str(task_id)), [])
        if all(item["task_id"] != str(task_id) for item in items):
            link = _one_line(payload.get("url") or payload.get("branch") or "no link", _MAX_LINK)
            items.append({"task_id": str(task_id), "link": link})

    async def _flush_digests(self, queue: Any, pending: dict[str, dict[str, Any]]) -> None:
        now = self._clock()
        stamps = {batch: at for batch, at in pending["digest_at"].items() if now - at < PR_DIGEST_WINDOW_S}
        pending["digest_at"] = stamps
        for batch, items in list(pending["prs"].items()):
            if batch in stamps:
                continue
            del pending["prs"][batch]
            fresh = [item for item in items if self._claim(queue, "pr_opened", item["task_id"])]
            if fresh:
                stamps[batch] = now
                await self._push("Foundry: PRs opened", self._digest(batch, fresh), "rocket", "default")

    @staticmethod
    def _digest(batch: str, items: list[dict[str, str]]) -> str:
        """The digest body: every PR that fits in :data:`MAX_MESSAGE`, then how many did not."""
        noun = "PR" if len(items) == 1 else "PRs"
        text = f"{batch}: {len(items)} {noun} opened"
        for index, item in enumerate(items):
            left_after = len(items) - index - 1
            line = f"\n{item['task_id']} {item['link']}"
            more = f"\n+{left_after} more" if left_after else ""
            if len(text) + len(line) + len(more) > MAX_MESSAGE:
                return f"{text}\n+{left_after + 1} more"
            text += line
        return text

    async def _check_usage(self, queue: Any, factory: Any) -> None:
        """One push per usage-limit pause; the mark clears when the limiter runs again."""
        remaining = getattr(getattr(factory, "limiter", None), "pause_remaining", None)
        left = float(remaining()) if callable(remaining) else 0.0
        marked = bool(queue.kv_get(_USAGE_KEY, False))
        if left <= 0:
            if marked:
                queue.kv_set(_USAGE_KEY, False)
            return
        error = queue.kv_get("last_account_error")
        if marked or not isinstance(error, Mapping) or error.get("type") != _USAGE_ERROR:
            return
        queue.kv_set(_USAGE_KEY, True)
        message = f"Foundry paused for a usage limit. Resumes in about {max(1, math.ceil(left / 60))} min."
        await self._push("Foundry: paused", message, "hourglass", "default")

    # ------------------------------------------------------------------ sending

    async def _push(self, title: str, message: str, tags: str, priority: str) -> None:
        """Send one notification; a failure is logged and dropped."""
        note = Notification(title, message[:MAX_MESSAGE], tags, priority, self.click)
        try:
            await asyncio.to_thread(self._sender, note)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed += 1
            log.warning("notifier: could not send %r (dropped): %s: %s", title, type(exc).__name__, exc)
        else:
            self.sent += 1

    def _post(self, note: Notification) -> None:
        """POST ``note`` to ``<server>/<topic>``. Header values stay ASCII; titles of pieces go in the body."""
        headers = {
            "Title": note.title, "Tags": note.tags, "Priority": note.priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if note.click:
            headers["Click"] = note.click
        request = urllib.request.Request(
            f"{self.server}/{urllib.parse.quote(self.topic, safe='')}",
            data=note.message.encode("utf-8"), headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_S):
            pass
