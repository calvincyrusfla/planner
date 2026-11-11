"""The file map: where to look, in the builder's start packet (advisory)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import BuilderWorker, build_builder_prompt
from foundry.file_map import Symbol, build_index, file_map
from foundry.schemas import ContextSlice, TaskAssignment

HEADING = "## Where to look (advisory)"
INTRO = (
    "Functions and classes this piece likely needs, with line ranges in the base. "
    "Read these first; your tools reach the rest."
)

PY = '''import os


class Box:
    def open(self):
        return 1

    def close(self):
        return 2


@deco
def helper():
    return 3
'''
MJS = "export function alpha() {\n  return 1;\n}\nclass Beta {\n}\nexport const gamma = 1;\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(PY, encoding="utf-8")
    (tmp_path / "b.mjs").write_text(MJS, encoding="utf-8")
    return tmp_path


class _Reply:
    def __init__(self, data: Any) -> None:
        self._data = data

    def parse_json(self) -> Any:
        return self._data


class _Gemma:
    def __init__(self, data: Any = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error

    async def chat(self, messages: Any, **kw: Any) -> _Reply:
        if self.error is not None:
            raise self.error
        return _Reply(self.data)


def _assignment() -> TaskAssignment:
    return TaskAssignment(
        task_id="T-1", team_id="alpha", builder_id="alpha-1", subtask_id="T-1.1",
        acceptance_criteria=["ok"], allowed_files=["a.py", "b.mjs"],
        context=ContextSlice(objective="Change things."),
    )


def test_build_index(tmp_path: Path) -> None:
    index = build_index(_repo(tmp_path), ["a.py", "b.mjs", "missing.py"])
    got = {(s.path, s.name): (s.start, s.end) for s in index}
    assert got[("a.py", "Box")] == (4, 8)
    assert got[("a.py", "Box.open")] == (5, 6)
    assert got[("a.py", "Box.close")] == (8, 9)
    assert got[("a.py", "helper")] == (12, 14)
    assert got[("b.mjs", "alpha")] == (1, 3)
    assert got[("b.mjs", "Beta")] == (4, 5)
    assert got[("b.mjs", "gamma")] == (6, 6)


INDEX = [Symbol("a.py", "Box.open", 5, 6), Symbol("a.py", "helper", 12, 14), Symbol("b.mjs", "alpha", 1, 3)]


def test_backtick_matches_without_gemma() -> None:
    out = asyncio.run(file_map(None, "Use `open` and `helper` here", INDEX, timeout=1))
    assert out == ["a.py:5-6 Box.open", "a.py:12-14 helper"]
    assert asyncio.run(file_map(None, "nothing", INDEX, timeout=1)) == []


def test_gemma_picks_validated_deduped_capped() -> None:
    gemma = _Gemma({"picks": [2, 2, 99, -1, "x", 1]})
    out = asyncio.run(file_map(gemma, "order `open`", INDEX, timeout=1))
    assert out == ["a.py:5-6 Box.open", "a.py:12-14 helper", "b.mjs:1-3 alpha"]
    out = asyncio.run(file_map(gemma, "order `open`", INDEX, timeout=1, max_entries=2))
    assert len(out) == 2


def test_raising_gemma_keeps_backtick_matches() -> None:
    out = asyncio.run(file_map(_Gemma(error=RuntimeError("boom")), "`helper`", INDEX, timeout=1))
    assert out == ["a.py:12-14 helper"]


def test_prompt_section_position_and_absence() -> None:
    a = _assignment()
    prompt = build_builder_prompt(a, order_description="the order", pitfall_notes=["x.py:1: n"],
                                  file_map=["a.py:1-2 foo"])
    assert f"\n{HEADING}\n{INTRO}\n- a.py:1-2 foo\n" in prompt
    assert prompt.index("## Files you may modify") < prompt.index("## Watch out (advisory)") < prompt.index(HEADING)
    assert prompt.index(HEADING) < prompt.index("## This attempt")
    base = build_builder_prompt(a, order_description="the order")
    assert build_builder_prompt(a, order_description="the order", file_map=None) == base
    assert build_builder_prompt(a, order_description="the order", file_map=[]) == base
    assert HEADING not in base


def _worker(repo: Path, enabled: bool) -> BuilderWorker:
    worker = object.__new__(BuilderWorker)
    worker.agent_id = "alpha-1"
    worker.config = SimpleNamespace(gemma_notes=enabled, gemma_notes_timeout_s=5)  # type: ignore[attr-defined]
    worker.context_engine = SimpleNamespace(gemma=_Gemma({"picks": [2]}), repo_root=repo)
    return worker


def test_worker_passes_map_only_with_setting_on(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ticket = {"ticket_id": "K-1", "task_id": "T-1"}
    meta = {"description": "Change `helper`."}
    on = asyncio.run(_worker(repo, True)._file_map(ticket, _assignment(), meta))
    assert on == ["a.py:8-9 Box.close", "a.py:12-14 helper"]
    assert asyncio.run(_worker(repo, False)._file_map(ticket, _assignment(), meta)) == []
