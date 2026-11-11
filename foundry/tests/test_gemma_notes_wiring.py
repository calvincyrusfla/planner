"""Pitfall notes in the builder prompt (advisory, behind config.gemma_notes)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import BuilderWorker, build_builder_prompt
from foundry.schemas import ContextSlice, TaskAssignment

HEADING = "## Watch out (advisory)"
INTRO = "Found by a search of the repo's tests. Check each against the code; ignore any that do not apply."


def _assignment() -> TaskAssignment:
    return TaskAssignment(
        task_id="T-1",
        team_id="alpha",
        builder_id="alpha-1",
        subtask_id="T-1.1",
        acceptance_criteria=["it works"],
        allowed_files=["pkg/mod.py"],
        context=ContextSlice(objective="Change things."),
    )


class _Reply:
    def __init__(self, data: Any) -> None:
        self._data = data

    def parse_json(self) -> Any:
        return self._data


class _Gemma:
    def __init__(self, data: Any = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.calls = 0

    async def chat(self, messages: Any, **kw: Any) -> _Reply:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _Reply(self.data)


def _worker(repo: Path, gemma: Any, enabled: bool = True) -> BuilderWorker:
    worker = object.__new__(BuilderWorker)
    worker.agent_id = "alpha-1"
    worker.config = SimpleNamespace(gemma_notes=enabled, gemma_notes_timeout_s=5)  # type: ignore[attr-defined]
    worker.context_engine = SimpleNamespace(gemma=gemma, repo_root=repo)
    return worker


def _repo(tmp_path: Path, with_test: bool = True) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def foo_bar():\n    return 1\n", encoding="utf-8")
    if with_test:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_mod.py").write_text(
            'def test_x():\n    assert names() == ["foo_bar", "baz"]\n', encoding="utf-8"
        )
    return tmp_path


META = {"description": "Change `foo_bar` so it returns more."}
NOTE_DATA = {"notes": [{"path": "tests/test_mod.py", "line": 2, "note": "exact list must be updated"}]}


def _notes(worker: BuilderWorker) -> list[str]:
    ticket = {"ticket_id": "K-1", "task_id": "T-1"}
    return asyncio.run(worker._pitfall_notes(ticket, _assignment(), META))


def test_prompt_section_position() -> None:
    a = _assignment()
    prompt = build_builder_prompt(a, order_description="the order", pitfall_notes=["a.py:1: one", "b.py:2: two"])
    assert f"\n{HEADING}\n{INTRO}\n- a.py:1: one\n- b.py:2: two\n" in prompt
    assert prompt.index("## Order details") < prompt.index(HEADING) < prompt.index("## Files you may modify")


def test_prompt_unchanged_without_notes() -> None:
    a = _assignment()
    old = build_builder_prompt(a, order_description="the order")
    assert build_builder_prompt(a, order_description="the order", pitfall_notes=None) == old
    assert build_builder_prompt(a, order_description="the order", pitfall_notes=[]) == old
    assert HEADING not in old


def test_no_notes_when_setting_off(tmp_path: Path) -> None:
    gemma = _Gemma(NOTE_DATA)
    assert _notes(_worker(_repo(tmp_path), gemma, enabled=False)) == []
    assert gemma.calls == 0


def test_no_notes_without_gemma(tmp_path: Path) -> None:
    assert _notes(_worker(_repo(tmp_path), None)) == []


def test_no_gemma_call_when_nothing_found(tmp_path: Path) -> None:
    gemma = _Gemma(NOTE_DATA)
    assert _notes(_worker(_repo(tmp_path, with_test=False), gemma)) == []
    assert gemma.calls == 0


def test_grounded_note_reaches_prompt(tmp_path: Path) -> None:
    gemma = _Gemma(NOTE_DATA)
    notes = _notes(_worker(_repo(tmp_path), gemma))
    assert notes == ["tests/test_mod.py:2: exact list must be updated"]
    prompt = build_builder_prompt(_assignment(), pitfall_notes=notes)
    assert "- tests/test_mod.py:2: exact list must be updated" in prompt


def test_failing_gemma_gives_no_notes_and_second_call_is_cached(tmp_path: Path) -> None:
    gemma = _Gemma(error=RuntimeError("boom"))
    worker = _worker(_repo(tmp_path), gemma)
    assert _notes(worker) == []
    assert gemma.calls == 1
    assert _notes(worker) == []
    assert gemma.calls == 1


def test_success_is_cached_per_ticket(tmp_path: Path) -> None:
    gemma = _Gemma(NOTE_DATA)
    worker = _worker(_repo(tmp_path), gemma)
    first = _notes(worker)
    assert first and _notes(worker) == first
    assert gemma.calls == 1
