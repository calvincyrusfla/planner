"""Types, constants and small helpers shared by the Floor Manager and its Foreman actions.

Split out of :mod:`foundry.floor_manager`, which imports these names back, so
:mod:`foundry.floor_actions` needs no import of the manager.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ACTION_LOG_MAX",
    "ActionOutcome",
    "InfrastructureError",
    "PeerReviewerGrant",
]


class InfrastructureError(Exception):
    """A failure of Foundry's own plumbing, not of the builder's work.

    Raised e.g. by :class:`WorkspaceGates` when the scratch copy of the
    workspace cannot be made. Workers treat it as a *counted* infrastructure
    failure (D1/E1: release the ticket, no attempt spent; quarantine after the
    configured number of consecutive failures), never as a gate failure.
    """


@dataclass
class ActionOutcome:
    """What the Floor Manager did for one escalation."""

    alert_id: str
    action: str
    applied: bool
    detail: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class PeerReviewerGrant:
    """An idle builder slot temporarily acting as a reviewer."""

    team_id: str
    slot: int
    agent_id: str
    until: float  # monotonic deadline

    def active(self, now: float | None = None) -> bool:
        """True until the grant expires."""
        return (time.monotonic() if now is None else now) < self.until


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


#: kv key of the persisted Foreman action log (``{"seq": n, "actions": [...]}``).
_ACTIONS_KEY = "floor_manager.actions"
# Limits on WIDEN_CONTEXT per task: time since its last widen, and since a widen of it was undone.
_WIDEN_COOLDOWN_S = 10 * 60.0
_WIDEN_UNDO_SUPPRESS_S = 2 * 3600.0
#: How many action log entries are kept (oldest dropped first).
ACTION_LOG_MAX = 500
_TERMINAL = {"complete", "quarantined", "failed"}


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string (``...Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(stamp: Any) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime (UTC assumed); ``None`` when absent or unreadable."""
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)
