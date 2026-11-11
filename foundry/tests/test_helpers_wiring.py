"""The service starts and stops the batch 13 helpers; one failing never stops the others."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from foundry.serve import Service
from foundry.tests.test_serve import _service

MODULES = {
    "foundry.notifier": "Notifier",
    "foundry.help_desk": "HelpDesk",
    "foundry.governor": "CapacityGovernor",
    "foundry.quarantine_report": "QuarantineReporter",
}


def _fake(name: str, log: list[str], mode: str = "") -> type:
    class Fake:
        def __init__(self, service: Any) -> None:
            if mode == "fail_init":
                raise RuntimeError("init")
            self.service = service

        def start(self) -> None:
            if mode == "fail_start":
                raise RuntimeError("start")
            log.append(f"start:{name}")

        async def stop(self) -> None:
            log.append(f"stop:{name}")
            if mode == "fail_stop":
                raise RuntimeError("stop")

    Fake.__name__ = name
    return Fake


def _install(monkeypatch: pytest.MonkeyPatch, log: list[str], modes: dict[str, str] | None = None) -> None:
    modes = modes or {}
    for module, name in MODULES.items():
        mod = types.ModuleType(module)
        setattr(mod, name, _fake(name, log, modes.get(name, "")))
        monkeypatch.setitem(sys.modules, module, mod)


def _run(tmp_path: Path) -> tuple[list[str], dict[str, Any]]:
    async def go() -> tuple[list[str], dict[str, Any]]:
        svc: Service = _service(tmp_path)
        await svc.start()
        try:
            health = svc.health()
        finally:
            await svc.stop()
        return svc.health()["helpers"], health

    return asyncio.run(go())


def test_all_started_stopped_and_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _install(monkeypatch, log)
    after, health = _run(tmp_path)
    assert health["helpers"] == list(MODULES.values())
    assert after == []
    assert [e for e in log if e.startswith("stop:")] == [f"stop:{n}" for n in MODULES.values()]


@pytest.mark.parametrize("mode", ["fail_init", "fail_start"])
def test_one_failing_helper_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    log: list[str] = []
    _install(monkeypatch, log, {"HelpDesk": mode})
    _, health = _run(tmp_path)
    assert health["ok"] is True
    assert health["helpers"] == ["Notifier", "CapacityGovernor", "QuarantineReporter"]


def test_missing_module_and_failing_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _install(monkeypatch, log, {"Notifier": "fail_stop"})
    monkeypatch.setitem(sys.modules, "foundry.governor", None)  # import fails
    _, health = _run(tmp_path)
    assert health["helpers"] == ["Notifier", "HelpDesk", "QuarantineReporter"]
    assert "stop:HelpDesk" in log and "stop:QuarantineReporter" in log
