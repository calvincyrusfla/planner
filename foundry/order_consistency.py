"""Order consistency check at intake: advisory findings on a batch's pieces.

Plain code finds the files a piece's description mentions that are not in its
``files`` / ``readFiles``; one Gemma question per piece decides whether the title
and description agree and which of those files the description asks to change.
Findings always use Foundry's fixed wording, never Gemma's.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from foundry.gemma_notes import _ask_json

log = logging.getLogger(__name__)

__all__ = ["consistency_note", "outside_paths", "piece_findings"]

_PATH_RE = re.compile(r"`?((?:[\w.-]+/)+[\w.-]+\.(?:py|mjs|js|ts|json|md))`?")
MAX_CANDIDATES = 10
MAX_NOTE_CHARS = 2000
NOTE_PREFIX = "Order check (advisory): "

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["same", "different_change", "title_too_narrow"]},
        "changes": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["verdict", "changes"],
}


def outside_paths(subtask: Mapping[str, Any], repo_root: Path | None) -> list[str]:
    """Existing repo paths named in the description that are in neither ``files`` nor ``readFiles``."""
    description = str(subtask.get("description") or "")
    listed: set[str] = set()
    for field in ("files", "readFiles", "read_files"):
        value = subtask.get(field)
        if isinstance(value, (list, tuple)):
            listed.update(str(v).strip() for v in value)
    found: list[str] = []
    for path in _PATH_RE.findall(description):
        if path in listed or path in found:
            continue
        if repo_root is not None:
            try:
                if not (Path(repo_root) / path).exists():
                    continue
            except (OSError, ValueError):
                continue
        found.append(path)
        if len(found) >= MAX_CANDIDATES:
            break
    return found


async def piece_findings(gemma: Any, key: str, subtask: Mapping[str, Any], candidates: Sequence[str],
                         *, timeout: float) -> list[str]:
    """Findings for one piece from one Gemma question; any failure gives []."""
    try:
        numbered = "\n".join(f"[{i}] {path}" for i, path in enumerate(candidates)) or "(none)"
        system = (
            "You check a work order for internal mismatches. Decide (a) whether the title and the description "
            "describe the same change: answer 'same', 'different_change' (they describe different changes) or "
            "'title_too_narrow' (the description asks for more than the title says); and (b) which of the "
            "numbered files the description asks to CHANGE (not just read, run or mention): list their numbers "
            'in "changes". Answer with JSON: {"verdict": "same", "changes": [0]}.'
        )
        user = (
            f"Title:\n{subtask.get('title') or ''}\n\nDescription:\n{subtask.get('description') or ''}\n\n"
            f"Files:\n{numbered}"
        )
        data = await _ask_json(gemma, system, user, _SCHEMA, timeout)
        if not isinstance(data, dict):
            return []
        verdict = data.get("verdict")
        changes = data.get("changes", [])
        if verdict not in ("same", "different_change", "title_too_narrow") or not isinstance(changes, list):
            return []
        out: list[str] = []
        if verdict == "different_change":
            out.append(f"#{key}: title and description describe different changes")
        elif verdict == "title_too_narrow":
            out.append(f"#{key}: the description asks for more than the title says")
        seen: set[int] = set()
        for idx in changes:
            if isinstance(idx, bool) or not isinstance(idx, int) or idx in seen:
                continue
            if 0 <= idx < len(candidates):
                seen.add(idx)
                out.append(f"#{key}: asks to change {candidates[idx]}, which is not in its files")
        return out
    except Exception as exc:  # noqa: BLE001 - the check is advisory
        log.debug("order consistency unavailable for %s (%s)", key, str(exc) or type(exc).__name__)
        return []


async def consistency_note(gemma: Any, subtasks: Sequence[Mapping[str, Any]], repo_root: Path | None,
                           *, timeout: float, total_timeout: float = 60.0) -> str:
    """The batch note for all pieces; "" when there is nothing to say."""
    if gemma is None:
        return ""
    findings: list[str] = []

    async def run() -> None:
        for i, sub in enumerate(subtasks):
            key = str(sub.get("key") or i + 1)
            findings.extend(await piece_findings(gemma, key, sub, outside_paths(sub, repo_root), timeout=timeout))

    try:
        await asyncio.wait_for(run(), total_timeout)
    except asyncio.TimeoutError:
        pass
    if not findings:
        return ""
    return (NOTE_PREFIX + "; ".join(findings))[:MAX_NOTE_CHARS]
