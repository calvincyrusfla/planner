"""Exact-match solution cache and retry policy for the Foundry gates.

The :class:`SolutionCache` remembers diffs that a reviewer has **APPROVED** so
that an identical work order can be served without another Claude call. It
lives in its own ``solution_cache`` table inside the same SQLite file as the
:class:`~foundry.orchestrator_queue.FoundryQueue`, but opens its **own**
connection (WAL mode, ``busy_timeout``) so it never contends for the queue's
connection lock.

Phase 1: exact match only
-------------------------
The cache key is a SHA-256 over a *normalized* view of the assignment:

* the work order's intent — task title and description (optional keyword
  arguments, since :class:`TaskAssignment` does not carry them) and the
  context slice's objective, each whitespace-collapsed and case-folded. Two
  tasks with the same files and criteria but a different goal must miss;
* acceptance criteria — whitespace collapsed, case-folded, de-duplicated and
  sorted (so reordering or re-spacing criteria still hits);
* allowed files — path separators normalized to ``/`` and sorted;
* context snippets — each snippet's path, start line and a SHA-256 of its
  text, sorted.

Anything that only describes *who* or *when* (task id, team id, builder id,
attempt, fix hint) is deliberately left out of the key.

Semantic / fuzzy matching (embedding similarity of criteria, tolerant snippet
matching) is **phase 2**. Exact-only is safe because a hit requires the same
requirements *and* byte-identical source context for the same set of files:
the stored diff was approved against exactly that code, so replaying it can
not silently apply a solution written for different code or different
requirements. A fuzzy hit could return a diff that looks right but solves a
neighbouring problem, and it would skip the Claude builder that would have
noticed. Replayed diffs still go through the deterministic gates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.schemas import TaskAssignment

__all__ = ["CACHE_KEY_VERSION", "SolutionCache", "RetryPolicy", "cache_key", "normalize_criterion"]

#: Bump when the normalization changes so old rows can never be hit by mistake.
CACHE_KEY_VERSION = "v2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS solution_cache (
    key         TEXT PRIMARY KEY,
    diff        TEXT NOT NULL,
    task_id     TEXT,
    subtask_id  TEXT,
    approved_at TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT
)
"""


def _utc_now() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def normalize_criterion(text: str) -> str:
    """Collapse whitespace and case-fold one acceptance criterion."""
    return " ".join(text.split()).casefold()


def _normalize_path(path: str) -> str:
    """Normalize a repo-relative path (``\\`` → ``/``, strip ``./`` and spaces)."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _normalize_text(text: str | None) -> str:
    """Collapse whitespace and case-fold free text (``None`` → ``""``)."""
    return " ".join((text or "").split()).casefold()


def _key_material(
    assignment: TaskAssignment, *, title: str | None = None, description: str | None = None
) -> dict[str, Any]:
    """Build the normalized, order-independent view that the key hashes."""
    criteria = sorted({normalize_criterion(c) for c in assignment.acceptance_criteria if c.strip()})
    files = sorted({_normalize_path(f) for f in assignment.allowed_files if f.strip()})
    snippets: list[list[Any]] = []
    context = getattr(assignment, "context", None)
    for snip in getattr(context, "snippets", None) or []:
        digest = hashlib.sha256(snip.text.encode("utf-8")).hexdigest()
        snippets.append([_normalize_path(snip.path), int(snip.start_line), digest])
    snippets.sort()
    intent = {
        "title": _normalize_text(title),
        "description": _normalize_text(description),
        "objective": _normalize_text(getattr(context, "objective", None)),
    }
    return {"v": CACHE_KEY_VERSION, "intent": intent, "criteria": criteria, "files": files, "snippets": snippets}


def cache_key(
    assignment: TaskAssignment, *, title: str | None = None, description: str | None = None
) -> str:
    """Return the deterministic exact-match cache key for ``assignment``.

    Pass the work order's ``title`` and ``description`` when known; ``None``
    and ``""`` are equivalent. Callers must pass them consistently to
    :meth:`SolutionCache.lookup` and :meth:`SolutionCache.store`.
    """
    material = json.dumps(_key_material(assignment, title=title, description=description), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SolutionCache:
    """SQLite-backed exact-match cache of reviewer-approved diffs.

    Thread-safe: one connection per instance guarded by a lock; several
    instances (or processes) may share the file thanks to WAL and
    ``busy_timeout``.
    """

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        """Open (or create) the cache table in ``db_path``."""
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=busy_timeout_ms / 1000, isolation_level=None
        )
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._lookups = 0
        self._hits = 0
        self._stores = 0

    # -- key -----------------------------------------------------------------
    @staticmethod
    def key_for(
        assignment: TaskAssignment, *, title: str | None = None, description: str | None = None
    ) -> str:
        """Return the cache key for ``assignment`` (see :func:`cache_key`)."""
        return cache_key(assignment, title=title, description=description)

    # -- operations ----------------------------------------------------------
    def lookup(
        self, assignment: TaskAssignment, *, title: str | None = None, description: str | None = None
    ) -> str | None:
        """Return the approved diff for an identical assignment, or ``None``.

        A hit increments the row's ``hits`` counter and ``last_hit_at``.
        """
        key = cache_key(assignment, title=title, description=description)
        with self._lock:
            self._lookups += 1
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT diff FROM solution_cache WHERE key = ?", (key,)).fetchone()
                if row is not None:
                    self._conn.execute(
                        "UPDATE solution_cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
                        (_utc_now(), key),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if row is None:
                return None
            self._hits += 1
            return str(row[0])

    def store(
        self,
        assignment: TaskAssignment,
        diff: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> str:
        """Remember ``diff`` for ``assignment``; returns the key.

        Call this **only** after the reviewer returned ``APPROVED``. Storing
        the same key again replaces the diff and refreshes ``approved_at`` but
        keeps the hit count.
        """
        if not diff.strip():
            raise ValueError("refusing to cache an empty diff")
        key = cache_key(assignment, title=title, description=description)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO solution_cache (key, diff, task_id, subtask_id, approved_at, hits)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(key) DO UPDATE SET
                    diff = excluded.diff,
                    task_id = excluded.task_id,
                    subtask_id = excluded.subtask_id,
                    approved_at = excluded.approved_at
                """,
                (key, diff, getattr(assignment, "task_id", None), getattr(assignment, "subtask_id", None), _utc_now()),
            )
            self._stores += 1
        return key

    def get_entry(self, key: str) -> dict[str, Any] | None:
        """Return the raw row for ``key`` (diff, approved_at, hits, ...) or ``None``."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, diff, task_id, subtask_id, approved_at, hits, last_hit_at FROM solution_cache WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d[0] for d in cur.description], row))

    def stats(self) -> dict[str, Any]:
        """Telemetry snapshot: persistent totals plus this instance's counters."""
        with self._lock:
            entries, total_hits = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM solution_cache"
            ).fetchone()
        lookups = self._lookups
        return {
            "entries": int(entries),
            "total_hits": int(total_hits),
            "lookups": lookups,
            "hits": self._hits,
            "misses": lookups - self._hits,
            "stores": self._stores,
            "hit_rate": (self._hits / lookups) if lookups else 0.0,
        }

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SolutionCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class RetryPolicy:
    """Gate/review retry budget: attempts are numbered ``0..max_retries``.

    With the default ``max_retries=2`` a ticket is built at most three times
    (attempt 0, 1, 2); a failure on attempt 2 means quarantine.
    """

    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")

    def should_retry(self, attempt: int) -> bool:
        """True if a failure on ``attempt`` may be retried."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        return attempt < self.max_retries

    def next_attempt(self, attempt: int) -> int | None:
        """Attempt number to run after a failure on ``attempt``, or ``None`` (quarantine)."""
        return attempt + 1 if self.should_retry(attempt) else None

    def exhausted(self, attempt: int) -> bool:
        """True if a failure on ``attempt`` must quarantine the task."""
        return not self.should_retry(attempt)
