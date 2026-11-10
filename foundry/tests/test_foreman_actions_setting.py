"""FOUNDRY_FOREMAN_ACTIONS: the Foreman's monitor can report its actions without carrying them out.

Real queue/manager wired by ``build_factory`` with the simulated Claude client
and a dry board bridge; nothing calls Claude, Ollama or the board.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.board_bridge import BoardBridge
from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import ActionOutcome, Factory, WorkOrder, build_factory
from foundry.main import write_demo_repo
from foundry.schemas import ForemanEscalation
from foundry.serve import Service

_SEQ = itertools.count(1)
_LOGGED_ONLY = "logged only (FOUNDRY_FOREMAN_ACTIONS=log): QUARANTINE_TASK"


def _factory(tmp_path: Path) -> Factory:
    repo = tmp_path / "repo"
    if not repo.exists():
        write_demo_repo(repo)
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=["alpha", "bravo"], board_bridge_mode="dry", auto_pr=False,
    )
    return build_factory(
        config, claude=SimulatedClaudeClient(), gemma=None, repo_root=repo, workspace=repo,
        bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches",
    )


def _quarantine_alert(task_id: str) -> ForemanEscalation:
    return ForemanEscalation.model_validate({
        "alert_id": f"ALT-20261110-{next(_SEQ):03d}",
        "timestamp": "2026-11-10T12:00:00Z",
        "severity": "HIGH",
        "category": "LINE_JAM",
        "target_entity": {"task_id": task_id},
        "metrics_snapshot": {"queue_depth": 1, "dwell_seconds": 0.0, "gate_failure_count": 0, "current_tpm": 0.0},
        "diagnosis": "Test alert.",
        "prescribed_action": "QUARANTINE_TASK",
        "action_parameters": {"reason": "jam"},
    })


def _order() -> WorkOrder:
    return WorkOrder(title="q me", description="d", files=["shop/pricing.py"], team_id="alpha")


# --------------------------------------------------------------------------- config


def test_default_is_act() -> None:
    assert FoundryConfig().foreman_actions == "act"
    assert FoundryConfig.from_env({}).foreman_actions == "act"


@pytest.mark.parametrize("raw", ["log", "LOG", " Log "])
def test_env_log_parses(raw: str) -> None:
    assert FoundryConfig.from_env({"FOUNDRY_FOREMAN_ACTIONS": raw}).foreman_actions == "log"


def test_env_bogus_is_refused() -> None:
    with pytest.raises(ValidationError, match="foreman_actions"):
        FoundryConfig.from_env({"FOUNDRY_FOREMAN_ACTIONS": "bogus"})


# --------------------------------------------------------------------------- floor manager


def test_act_false_reports_without_quarantining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue
    posted: list[Any] = []
    monkeypatch.setattr(m.bridge, "escalation", posted.append)

    async def go() -> None:
        tid = await m.submit(_order())
        state = q.get_task(tid)["state"]
        alert = _quarantine_alert(tid)

        out = await m.apply_escalation(alert, act=False)
        assert out.applied is False and out.detail == _LOGGED_ONLY
        assert out.action == "QUARANTINE_TASK"
        assert q.get_task(tid)["state"] == state and state != "quarantined"
        assert posted == [alert]

        [entry] = m.actions()
        assert entry["action"] == "QUARANTINE_TASK" and entry["applied"] is False
        assert entry["detail"] == _LOGGED_ONLY and entry["undoable"] is False

        # the same alert again: the earlier outcome, nothing acted on, posted or logged
        assert await m.apply_escalation(alert, act=False) is out
        assert await m.apply_escalation(alert) is out
        assert q.get_task(tid)["state"] == state
        assert posted == [alert] and len(m.actions()) == 1

        # the stored outcome survives lost manager state, and still does not act
        m.applied_alerts.clear()
        again = await m.apply_escalation(alert)
        assert again.applied is False and again.detail == _LOGGED_ONLY
        assert q.get_task(tid)["state"] == state
        assert posted == [alert]

    asyncio.run(go())
    f.close()


def test_act_true_quarantines_as_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue
    posted: list[Any] = []
    monkeypatch.setattr(m.bridge, "escalation", posted.append)

    async def go() -> None:
        tid = await m.submit(_order())
        alert = _quarantine_alert(tid)
        out = await m.apply_escalation(alert, act=True)
        assert out.applied is True and out.detail == f"{tid} quarantined"
        assert q.get_task(tid)["state"] == "quarantined"
        assert posted == [alert]
        assert m.actions()[0]["undoable"] is True

    asyncio.run(go())
    f.close()


# --------------------------------------------------------------------------- monitor


def _monitored(tmp_path: Path, mode: str | None) -> tuple[Service, list[dict[str, Any]]]:
    """A service on a fake factory; the list records the keywords of each apply_escalation call."""
    calls: list[dict[str, Any]] = []

    async def evaluate() -> Any:
        return {"alert_id": "ALT-20261110-900"}

    async def apply(alert: Any, **kwargs: Any) -> ActionOutcome:
        calls.append(kwargs)
        return ActionOutcome("ALT-20261110-900", "QUARANTINE_TASK", False, "x", {})

    factory = SimpleNamespace(
        manager=SimpleNamespace(report_quarantines=lambda: None, apply_escalation=apply),
        foreman=SimpleNamespace(evaluate_line_anomaly=evaluate),
    )
    config = SimpleNamespace() if mode is None else SimpleNamespace(foreman_actions=mode)
    return Service(config, repo=tmp_path, factory=factory), calls


@pytest.mark.parametrize(("mode", "expected"), [(None, {}), ("act", {}), ("log", {"act": False})])
def test_monitor_passes_act_only_in_log_mode(tmp_path: Path, mode: str | None, expected: dict[str, Any]) -> None:
    svc, calls = _monitored(tmp_path, mode)
    assert asyncio.run(svc.monitor_once()) is not None
    assert svc.last_monitor_error is None
    assert calls == [expected]
