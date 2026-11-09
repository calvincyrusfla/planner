"""Tests for ``_save_prompt`` and its call from the builder."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import builder_worker
from foundry.builder_worker import BuilderWorker, _save_prompt
from foundry.schemas import ContextSlice, TaskAssignment


@pytest.fixture
def prompt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "prompts"
    monkeypatch.setenv("FOUNDRY_PROMPT_DIR", str(directory))
    monkeypatch.delenv("FOUNDRY_SAVE_PROMPTS", raising=False)
    return directory


def test_writes_the_prompt(prompt_dir: Path) -> None:
    _save_prompt("the prompt\n", "T-1", 2)
    assert (prompt_dir / "T-1_attempt2.txt").read_text(encoding="utf-8") == "the prompt\n"


def test_default_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_PROMPT_DIR", raising=False)
    monkeypatch.delenv("FOUNDRY_SAVE_PROMPTS", raising=False)
    monkeypatch.setattr(builder_worker, "_DEFAULT_PROMPT_DIR", tmp_path / "default")
    _save_prompt("x", "T-1", 0)
    assert (tmp_path / "default" / "T-1_attempt0.txt").is_file()


def test_ticket_id_is_sanitized(prompt_dir: Path) -> None:
    _save_prompt("x", "../../evil ticket/1", 0)
    assert [p.name for p in prompt_dir.iterdir()] == ["evil_ticket_1_attempt0.txt"]


@pytest.mark.parametrize("value", ["0", "false", "No", " off "])
def test_flag_disables_saving(prompt_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_SAVE_PROMPTS", value)
    _save_prompt("x", "T-1", 0)
    assert not prompt_dir.exists()


def test_keeps_only_the_newest_200(prompt_dir: Path) -> None:
    prompt_dir.mkdir()
    (prompt_dir / "notes.md").write_text("keep me", encoding="utf-8")
    for i in range(205):
        old = prompt_dir / f"old-{i}.txt"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    _save_prompt("new", "T-1", 0)
    assert len(list(prompt_dir.glob("*.txt"))) == 200
    assert (prompt_dir / "T-1_attempt0.txt").is_file()
    assert (prompt_dir / "notes.md").is_file()
    assert not (prompt_dir / "old-5.txt").exists()
    assert (prompt_dir / "old-6.txt").is_file()


def test_unwritable_directory_is_harmless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_PROMPT_DIR", str(blocker))
    monkeypatch.delenv("FOUNDRY_SAVE_PROMPTS", raising=False)
    _save_prompt("x", "T-1", 0)
    assert blocker.read_text(encoding="utf-8") == "x"


def test_builder_saves_the_prompt_it_sends(prompt_dir: Path) -> None:
    assignment = TaskAssignment(
        task_id="T",
        team_id="alpha",
        builder_id="alpha-1",
        subtask_id="T.1",
        context=ContextSlice(objective="Do it."),
        attempt=2,
    )

    async def build_assignment(_ticket: Any) -> TaskAssignment:
        return assignment

    async def call_claude(_prompt: str, *, task_id: str) -> Any:
        raise RuntimeError(f"stop after the prompt of {task_id}")

    async def infra_release(_ticket: Any, _exc: BaseException) -> str:
        return "released"

    worker: Any = SimpleNamespace(
        scratch={},
        cache=None,
        max_retries=3,
        build_assignment=build_assignment,
        _call_claude=call_claude,
        _infra_release=infra_release,
        _task_state=lambda _task_id: "building",
        _ticket_changed=lambda _task_id: None,
        _work_order=lambda _task_id: {"title": "t", "description": ""},
        _already_done=list,
        _focus_files=lambda _ticket, _assignment: [],
        _tm=lambda *_args: None,
    )
    ticket = {"ticket_id": "T-1", "task_id": "T"}
    assert asyncio.run(BuilderWorker._build_ticket(worker, ticket)) == "released"
    saved = (prompt_dir / "T-1_attempt3.txt").read_text(encoding="utf-8")
    assert saved == worker.scratch["last_prompt"]
