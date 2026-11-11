"""Order consistency check at intake (foundry/order_consistency.py and its board_dispatch wiring)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from foundry import board_dispatch
from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.order_consistency import consistency_note, outside_paths, piece_findings
from foundry.serve import Service
from foundry.tests.test_board_dispatch import FakeBoard, _pump, _watcher


class _Reply:
    def __init__(self, data: Any) -> None:
        self._data = data

    def parse_json(self) -> Any:
        return self._data


class _Gemma:
    def __init__(self, data: Any = None, error: Exception | None = None, delay: float = 0.0) -> None:
        self.data = data
        self.error = error
        self.delay = delay
        self.calls = 0

    async def chat(self, messages: Any, **kw: Any) -> _Reply:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _Reply(self.data)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "pkg" / name).write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_outside_paths(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    sub = {
        "description": "Edit `pkg/a.py`, see pkg/b.py and pkg/c.py, also pkg/a.py again and pkg/missing.py.",
        "files": ["pkg/a.py"],
        "readFiles": ["pkg/b.py"],
    }
    assert outside_paths(sub, root) == ["pkg/c.py"]
    assert outside_paths(sub, None) == ["pkg/c.py", "pkg/missing.py"]


def test_outside_paths_capped(tmp_path: Path) -> None:
    desc = " ".join(f"d/f{i}.py" for i in range(15))
    assert len(outside_paths({"description": desc}, None)) == 10


def _findings(data: Any, candidates: list[str] | None = None) -> list[str]:
    sub = {"title": "T", "description": "D"}
    return asyncio.run(piece_findings(_Gemma(data), "2", sub, candidates or ["p/x.py", "p/y.py"], timeout=1))


def test_verdicts_map_to_templates() -> None:
    assert _findings({"verdict": "same", "changes": []}) == []
    assert _findings({"verdict": "different_change", "changes": []}) == [
        "#2: title and description describe different changes"]
    assert _findings({"verdict": "title_too_narrow", "changes": []}) == [
        "#2: the description asks for more than the title says"]


def test_changes_indexes_and_out_of_range_dropped() -> None:
    assert _findings({"verdict": "same", "changes": [1, 7, -1, 1]}) == [
        "#2: asks to change p/y.py, which is not in its files"]


@pytest.mark.parametrize("data", [None, "x", [], {"verdict": "bogus", "changes": []},
                                  {"verdict": "same", "changes": "0"}])
def test_garbage_gives_nothing(data: Any) -> None:
    assert _findings(data) == []


def test_raising_and_slow_gemma_give_nothing() -> None:
    sub = {"title": "T", "description": "D"}
    assert asyncio.run(piece_findings(_Gemma(error=RuntimeError("boom")), "1", sub, [], timeout=1)) == []
    slow = _Gemma({"verdict": "different_change", "changes": []}, delay=1.0)
    assert asyncio.run(piece_findings(slow, "1", sub, [], timeout=0.05)) == []


def test_note_none_gemma_and_empty() -> None:
    subs = [{"key": "1", "title": "T", "description": "D"}]
    assert asyncio.run(consistency_note(None, subs, None, timeout=1)) == ""
    assert asyncio.run(consistency_note(_Gemma({"verdict": "same", "changes": []}), subs, None, timeout=1)) == ""


def test_note_joined_and_capped() -> None:
    subs = [{"key": "1", "title": "T", "description": "D"}, {"key": "2", "title": "T", "description": "D"}]
    gemma = _Gemma({"verdict": "different_change", "changes": []})
    note = asyncio.run(consistency_note(gemma, subs, None, timeout=1))
    assert note == ("Order check (advisory): #1: title and description describe different changes; "
                    "#2: title and description describe different changes")
    many = [{"key": str(i), "title": "T", "description": "D"} for i in range(100)]
    assert len(asyncio.run(consistency_note(gemma, many, None, timeout=1))) == 2000


def test_total_timeout_keeps_earlier_findings() -> None:
    class Seq:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: Any, **kw: Any) -> _Reply:
            self.calls += 1
            if self.calls > 1:
                await asyncio.sleep(5)
            return _Reply({"verdict": "title_too_narrow", "changes": []})

    subs = [{"key": "1", "title": "T", "description": "D"}, {"key": "2", "title": "T", "description": "D"}]
    note = asyncio.run(consistency_note(Seq(), subs, None, timeout=10, total_timeout=0.2))
    assert note == "Order check (advisory): #1: the description asks for more than the title says"


# --------------------------------------------------------------------------- dispatcher


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: bool) -> tuple[FakeBoard, list[int], dict[str, Any]]:
    calls: list[int] = []

    async def fake_note(gemma: Any, subtasks: Any, repo_root: Any, *, timeout: float, **kw: Any) -> str:
        calls.append(len(subtasks))
        return "Order check (advisory): #1: title and description describe different changes"

    monkeypatch.setattr(board_dispatch, "consistency_note", fake_note)
    board = FakeBoard()
    b = board.add()
    out: dict[str, Any] = {}

    async def go() -> None:
        repo = write_demo_repo(tmp_path / "repo")
        cfg = FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                            teams=["alpha", "bravo"], board_bridge_mode="dry", gemma_notes=setting)
        svc = Service(cfg, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
                      monitor_s=3600, factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"})
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            out["tasks"] = sorted(w._record(b["id"])["tasks"])
        finally:
            await svc.stop()

    asyncio.run(go())
    out["note"] = board.get(b["id"])["note"]
    return board, calls, out


def test_dispatcher_sends_note_once_when_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board, calls, out = _run(tmp_path, monkeypatch, True)
    assert calls == [2]
    assert out["note"].startswith("Order check (advisory)")
    assert sum(1 for _, a in board.ops if a.get("note")) == 1
    assert out["tasks"] == ["1", "2"]


def test_dispatcher_sends_nothing_when_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board, calls, out = _run(tmp_path, monkeypatch, False)
    assert calls == []
    assert out["note"] == ""
    assert out["tasks"] == ["1", "2"]
