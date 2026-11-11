"""The fixed PR description template (FloorManager.pr_request with pr_template)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.floor_manager import FloorManager, WorkOrder, build_factory
from foundry.orchestrator_queue import FoundryQueue

TID = "F-001"
PATCH = (
    "--- a/shop/mathx.py\n+++ b/shop/mathx.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
    "--- /dev/null\n+++ b/shop/new.py\n@@ -0,0 +1,1 @@\n+z\n"
)


def _manager(tmp_path: Path, *, template: bool, criteria: list[str] | None = None,
             patch: bool = True, brief: str | None = None) -> FloorManager:
    m = FloorManager(FoundryQueue(str(tmp_path / "foundry.db")), None, {}, None, BoardBridge("dry"),
                     patches_dir=tmp_path / "patches", pr_template=template)
    m.orders[TID] = WorkOrder(title="Add y", acceptance_criteria=["y exists"] if criteria is None else criteria)
    m.pr_summaries[TID] = {"commit_message": "Add y\n\nbody line", "pr_body": "Adds y."}
    if patch:
        p = tmp_path / "patches" / f"{TID}.patch"
        p.parent.mkdir()
        p.write_text(PATCH, encoding="utf-8")
        m.patch_paths[TID] = p
    if brief:
        m.pr_briefs[TID] = brief
    return m


def test_off_gives_todays_body(tmp_path: Path) -> None:
    m = _manager(tmp_path, template=False)
    assert m.pr_request(TID)["body"] == f"Adds y.\n\n---\nOpened by Foundry (task {TID})\n"


def test_on_gives_sections_in_order(tmp_path: Path) -> None:
    m = _manager(tmp_path, template=True, brief="Look here.")
    req = m.pr_request(TID)
    body = req["body"]
    heads = ["## Summary", "## Acceptance criteria", "## Files changed", "## Brief (Gemma, advisory)",
             f"---\nOpened by Foundry (task {TID})"]
    idx = [body.index(h) for h in heads]
    assert idx == sorted(idx)
    assert "Adds y." in body and "- y exists" in body
    assert "- `shop/mathx.py`" in body and "- `shop/new.py`" in body and "/dev/null" not in body
    assert req["title"] == "Add y" and req["draft"] is False


def test_empty_sections_left_out(tmp_path: Path) -> None:
    m = _manager(tmp_path, template=True, criteria=[], patch=False)
    body = m.pr_request(TID)["body"]
    assert "## Acceptance criteria" not in body and "## Files changed" not in body
    assert "## Brief" not in body and body.startswith("## Summary")


def test_summary_falls_back_to_title(tmp_path: Path) -> None:
    m = _manager(tmp_path, template=True)
    m.pr_summaries[TID] = {}
    assert "## Summary\n\nAdd y" in m.pr_request(TID)["body"]


def test_build_factory_passes_the_setting(tmp_path: Path) -> None:
    from foundry.main import demo_claude, demo_gemma, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")

    def build(**kw: Any) -> Any:
        cfg = FoundryConfig(db_path=tmp_path / f"{len(kw)}.db", teams=["alpha"], board_bridge_mode="dry", **kw)
        return build_factory(cfg, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(), repo_root=repo,
                             workspace=repo, bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches")

    f = build(pr_template=True)
    assert f.manager.pr_template is True
    f.close()
    g = build()
    assert g.manager.pr_template is False
    g.close()


def test_config_reads_env() -> None:
    assert FoundryConfig().pr_template is False
    assert FoundryConfig.from_env({"FOUNDRY_PR_TEMPLATE": "true"}).pr_template is True
