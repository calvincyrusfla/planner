"""PR digest: a plain-code summary of a task for its PR description (no model call)."""
from __future__ import annotations

import logging
import re
from typing import Any

from foundry.risk_tier import classify, paths_from_diff

__all__ = ["MAX_DIGEST_CHARS", "build_digest"]

log = logging.getLogger(__name__)

MAX_DIGEST_CHARS = 2500
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", re.IGNORECASE)


def _events(manager: Any, task_id: str) -> list[dict[str, Any]]:
    try:
        return list(manager.queue.events(task_id=task_id))
    except Exception as exc:  # advisory only
        log.debug("pr digest: events failed: %s", exc)
        return []


def _token_rows(manager: Any, task_id: str) -> list[tuple[Any, ...]]:
    telemetry = getattr(manager, "telemetry", None)
    query = getattr(telemetry, "_query", None)
    if not callable(query):
        return []
    try:
        return list(query(
            "SELECT role, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) "
            "FROM telemetry_tokens WHERE task_id=? GROUP BY role ORDER BY role",
            (task_id,),
        ))
    except Exception as exc:  # advisory only
        log.debug("pr digest: tokens failed: %s", exc)
        return []


def build_digest(manager: Any, task_id: str, patch_text: str) -> str:
    """Plain-code digest of a task: tier, diffstat, tests, gates, attempts, sessions, tokens (<= 2,500 chars)."""
    paths = paths_from_diff(patch_text)
    info = classify(paths, patch_text)
    lines = [f"- Risk tier: {info['tier']} ({'; '.join(str(r) for r in info['reasons'])})"]
    lines.append(f"- Diffstat: {info['files']} file(s), +{info['added']} -{info['removed']}")
    tests = [p for p in paths if _TEST_PATH.search(p)]
    lines.append("- Tests: " + (", ".join(f"`{p}`" for p in tests) if tests else "none added or changed"))

    events = _events(manager, task_id)
    passed: list[str] = []
    failed: list[str] = []
    for ev in events:
        if ev.get("kind") not in ("gate", "gate_result"):
            continue
        pl = ev.get("payload") or {}
        check = str(pl.get("check_type") or "gate")
        bucket = passed if pl.get("passed") else failed
        if check not in bucket:
            bucket.append(check)
    ok = [c for c in passed if c not in failed]
    lines.append("- Gates passed: " + (", ".join(ok) if ok else "none recorded"))

    sessions = [ev.get("payload") or {} for ev in events if ev.get("kind") == "builder_session"]
    attempts = {pl.get("attempt") for pl in sessions if pl.get("attempt") is not None}
    lines.append(f"- Attempts: {len(attempts)}; builder sessions: {len(sessions)}")

    rows = _token_rows(manager, task_id)
    if rows:
        parts = [f"{r[0]} in {int(r[1] or 0)} / out {int(r[2] or 0)} / cache {int(r[3] or 0)}" for r in rows]
        lines.append("- Tokens: " + "; ".join(parts))
    else:
        lines.append("- Tokens: none recorded")

    text = "\n".join(lines)
    if len(text) > MAX_DIGEST_CHARS:
        text = text[: MAX_DIGEST_CHARS - 3].rstrip() + "..."
    return text
