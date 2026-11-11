"""The Gemma PR brief in the PR body (FloorManager._publish / pr_request)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.floor_manager import FloorManager, WorkOrder, build_factory
from foundry.orchestrator_queue import FoundryQueue

TID = "F-001"
PATCH = "--- a/shop/mathx.py\n+++ b/shop/mathx.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
HEADER = "## Brief (Gemma, advisory)"


class _Reply:
    def __init__(self, data: Any) -> None:
        self.data = data

    def parse_json(self) -> Any:
        return self.data


class _Gemma:
    def __init__(self, data: Any = None, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.data = data
        self.error = error
        self.delay = delay
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> _Reply:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _Reply(self.data)


class _Publisher:
    def __init__(self) -> None:
        self.prs: list[dict[str, Any]] = []

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any]) -> dict[str, Any]:
        self.prs.append(dict(pr))
        return {"ok": True, "branch": "foundry/x", "commit": "abc", "pr_url": "https://example.test/pr/1"}


GOOD = {"criteria": [{"index": 0, "files": ["shop/mathx.py"]}], "unusual": []}


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gemma: Any, **kw: Any) -> tuple[FloorManager, _Publisher]:
    monkeypatch.setenv("FOUNDRY_SUITE_GATE", "off")
    publisher = _Publisher()
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = FloorManager(FoundryQueue(str(tmp_path / "foundry.db")), SimpleNamespace(gemma=gemma), {}, None,
                           BoardBridge("dry"), publisher=publisher, patches_dir=tmp_path / "patches",
                           repo_root=repo, **kw)
    manager.orders[TID] = WorkOrder(title="Add y", description="Add y to x.", acceptance_criteria=["y exists"])
    patch = tmp_path / "patches" / f"{TID}.patch"
    patch.parent.mkdir()
    patch.write_text(PATCH, encoding="utf-8")
    manager.patch_paths[TID] = patch
    return manager, publisher


def test_off_makes_no_call_and_leaves_the_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gemma = _Gemma(GOOD)
    manager, publisher = _manager(tmp_path, monkeypatch, gemma)
    plain = manager.pr_request(TID)["body"]
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert gemma.calls == 0 and publisher.prs[0]["body"] == plain and HEADER not in plain


def test_no_gemma_on_the_foreman_makes_no_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, publisher = _manager(tmp_path, monkeypatch, None, gemma_notes=True)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert HEADER not in publisher.prs[0]["body"] and manager.pr_briefs == {}


def test_brief_is_in_the_body_above_the_footer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gemma = _Gemma(GOOD)
    manager, publisher = _manager(tmp_path, monkeypatch, gemma, gemma_notes=True)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    body = publisher.prs[0]["body"]
    assert gemma.calls == 1
    assert HEADER in body and "shop/mathx.py" in manager.pr_briefs[TID]
    assert body.index(HEADER) < body.index("---\nOpened by Foundry")
    assert body.rstrip().endswith(f"Opened by Foundry (task {TID})")
    # a retry of the PR step does not ask again
    asyncio.run(manager._publish(TID))
    assert gemma.calls == 1


@pytest.mark.parametrize("gemma", [_Gemma(error=RuntimeError("boom")), _Gemma(GOOD, delay=5.0)])
def test_failing_or_slow_gemma_still_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gemma: _Gemma) -> None:
    manager, publisher = _manager(tmp_path, monkeypatch, gemma, gemma_notes=True, gemma_notes_timeout_s=0.05)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert HEADER not in publisher.prs[0]["body"] and not manager.pr_briefs.get(TID)


def test_build_factory_passes_the_settings(tmp_path: Path) -> None:
    from foundry.main import demo_claude, demo_gemma, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")

    def build(**kw: Any) -> Any:
        cfg = FoundryConfig(db_path=tmp_path / f"{len(kw)}.db", teams=["alpha"], board_bridge_mode="dry", **kw)
        return build_factory(cfg, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(), repo_root=repo,
                             workspace=repo, bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches")

    f = build(gemma_notes=True, gemma_notes_timeout_s=12)
    assert f.manager.gemma_notes is True and f.manager.gemma_notes_timeout_s == 12.0
    f.close()
    g = build()
    assert g.manager.gemma_notes is False and g.manager.gemma_notes_timeout_s == 90.0
    g.close()
