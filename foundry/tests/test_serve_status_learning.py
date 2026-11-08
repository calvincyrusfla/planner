"""/api/status ``foreman.pre_review`` + ``foreman.calibration`` (T-018; owner charlie-1).

Simulated Claude/Gemma, dry bridge, temp database. Both fields are read
defensively: missing or failing sources give ``{}`` and never break status().
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.serve import Service


def _service(tmp_path: Path) -> Service:
    repo = write_demo_repo(tmp_path / "repo")
    cfg = FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                        teams=["alpha", "bravo"], board_bridge_mode="dry")
    return Service(cfg, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
                   factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"})


def _run(tmp_path: Path, check: Any) -> None:
    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            check(svc)
        finally:
            await svc.stop()

    asyncio.run(go())


def test_status_carries_pre_review_and_calibration(tmp_path: Path) -> None:
    def check(svc: Service) -> None:
        f = svc.factory
        fm = svc.status()["foreman"]
        assert fm["pre_review"]["sent_back"] == 0 and fm["pre_review"]["claude_reviews_avoided"] == 0
        if hasattr(f.telemetry, "record_pre_review"):  # alpha-2's telemetry
            for outcome in ("passed", "passed", "sent_back", "advised", "fallback"):
                f.telemetry.record_pre_review("T-1", outcome)
            assert svc.status()["foreman"]["pre_review"] == {
                "passed": 2, "sent_back": 1, "advised": 1, "skipped": 0, "fallback": 1, "claude_reviews_avoided": 1}
        f.foreman.calibration_summary = lambda: {  # type: ignore[attr-defined]  # old no-arg signature
            "n": 9, "accuracy_pct": 66.666, "under": 2, "over": 1, "shift_rules": [{"predicted": "M", "to": "L"}]}
        assert svc.status()["foreman"]["calibration"] == {
            "n": 9, "accuracy_pct": 66.7, "exact": 0, "under": 2, "over": 1, "shift_rules": [{"predicted": "M", "to": "L"}]}
        json.dumps(svc.status())  # JSON-able

    _run(tmp_path, check)


def test_pre_review_without_snapshot_key_uses_counts_method(tmp_path: Path) -> None:
    def check(svc: Service) -> None:
        f = svc.factory
        real = f.telemetry.snapshot
        f.telemetry.snapshot = lambda: {k: v for k, v in real().items() if k != "pre_review"}  # type: ignore[method-assign]
        f.telemetry.pre_review_counts = lambda: {"sent_back": 3, "skipped": "x"}  # type: ignore[attr-defined]
        pr = svc.status()["foreman"]["pre_review"]
        assert pr["sent_back"] == 3 and pr["claude_reviews_avoided"] == 3 and pr["skipped"] == 0

    _run(tmp_path, check)


def test_failing_sources_never_break_status(tmp_path: Path) -> None:
    def check(svc: Service) -> None:
        f = svc.factory

        def boom() -> Any:
            raise RuntimeError("nope")

        real = f.telemetry.snapshot
        f.telemetry.snapshot = lambda: {k: v for k, v in real().items() if k != "pre_review"}  # type: ignore[method-assign]
        f.telemetry.pre_review_counts = boom  # type: ignore[attr-defined]
        f.foreman.calibration_summary = boom  # type: ignore[attr-defined]
        body = svc.status()
        assert body["ok"] is True
        assert body["foreman"]["pre_review"] == {} and body["foreman"]["calibration"] == {}
        f.foreman.calibration_summary = lambda: "garbage"  # type: ignore[attr-defined]
        assert svc.status()["foreman"]["calibration"] == {}

    _run(tmp_path, check)


def test_real_calibration_summary_is_passed_through(tmp_path: Path) -> None:
    def check(svc: Service) -> None:
        cal = svc.status()["foreman"]["calibration"]
        if not hasattr(svc.factory.foreman, "calibration_summary"):
            assert cal == {}
            return
        assert cal["n"] >= 0
        assert cal["accuracy_pct"] is None or isinstance(cal["accuracy_pct"], float)
        assert isinstance(cal["shift_rules"], list)
        json.dumps(cal)

    _run(tmp_path, check)


def test_calibration_summary_is_scoped_to_the_repo(tmp_path: Path) -> None:
    def check(svc: Service) -> None:
        seen: list[Any] = []

        def summary(repo: str | None = None) -> dict[str, Any]:
            seen.append(repo)
            return {"n": 1, "accuracy_pct": 100, "exact": 1, "under": 0, "over": 0, "shift_rules": []}

        svc.factory.foreman.calibration_summary = summary  # type: ignore[attr-defined]
        assert svc.status()["foreman"]["calibration"]["n"] == 1
        assert seen == [str(svc.repo)]

    _run(tmp_path, check)
