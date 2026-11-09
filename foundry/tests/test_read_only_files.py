"""Read-only files: what builders are shown, what they may not edit, and the tests auto-added."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.board_dispatch import BatchWatcher
from foundry.builder_worker import (
    BuilderWorker,
    _path_covered,
    _read_only_path,
    _read_only_touched,
    build_builder_prompt,
)
from foundry.context_engine import ContextEngine
from foundry.floor_manager import FloorManager, WorkOrder, _read_only_files


def _stub(**fields: Any) -> Any:
    """A stand-in for an object only some of whose attributes the code under test reads."""
    return SimpleNamespace(**fields)


class _Partial(SimpleNamespace):
    """A stub whose unset public attributes read as None, like a model's optional fields."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return None


def _assignment(allowed: list[str], read_files: list[str], excerpts: list[dict[str, Any]] | None = None) -> Any:
    context = _Partial(objective="Do the thing.", acceptance_criteria=["works"], snippets=[])
    return _Partial(
        builder_id="bravo-1",
        team_id="bravo",
        task_id="F-1",
        subtask_id="F-1.1",
        attempt=0,
        context=context,
        acceptance_criteria=["works"],
        allowed_files=allowed,
        fix_hint=None,
        read_files=read_files,
        read_only_excerpts=excerpts or [],
    )


def _write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _engine(root: Path) -> Any:
    return ContextEngine(root)


def test_prompt_lists_read_only_files_apart_from_editable_ones() -> None:
    excerpt = {"path": "foundry/b.py", "start_line": 1, "end_line": 2, "text": "1| first\n2| second\n"}
    prompt = build_builder_prompt(_assignment(["foundry/a.py", "foundry/b.py"], ["foundry/b.py"], [excerpt]))
    editable, read_only = prompt.split("## Read-only files")
    assert "- foundry/a.py" in editable
    assert "- foundry/b.py" not in editable
    assert "- foundry/b.py" in read_only
    assert "### foundry/b.py (lines 1-2)" in read_only
    assert "2| second" in read_only


def test_prompt_has_no_read_only_section_without_read_only_files() -> None:
    assert "## Read-only files" not in build_builder_prompt(_assignment(["a.py"], []))


def test_prompt_says_so_when_every_allowed_file_is_read_only() -> None:
    assert "- (none: every listed file is read-only)" in build_builder_prompt(_assignment(["a.py"], ["a.py"]))


def test_read_only_path_uses_forward_slashes() -> None:
    assert _read_only_path("foundry\\tests\\test_a.py") == "foundry/tests/test_a.py"


def test_path_covered_matches_exact_paths_and_globs() -> None:
    assert _path_covered("foundry/a.py", ["foundry/a.py"])
    assert _path_covered("foundry/tests/test_a.py", ["foundry/tests/*.py"])
    assert not _path_covered("foundry/b.py", ["foundry/a.py", "docs/*.md"])


def test_read_only_touched_reports_protected_files_only() -> None:
    assignment = _stub(read_files=["foundry\\b.py"])
    assert _read_only_touched(["foundry/a.py", "foundry/b.py"], assignment) == ["foundry/b.py"]
    assert _read_only_touched(["foundry/a.py"], _stub()) == []


def test_read_files_drops_task_files_the_ticket_may_edit() -> None:
    worker = _stub(read_files_for=lambda _task: ["foundry/tests/test_a.py", "foundry/a.py"], _help_answer=lambda _ticket: {})
    ticket = _stub(task_id="F-1", read_files=["docs\\spec.md"])
    assert BuilderWorker._read_files(worker, ticket, ["foundry/a.py"]) == ["docs/spec.md", "foundry/tests/test_a.py"]


def test_read_files_keeps_the_tickets_own_list_even_when_allowed() -> None:
    ticket = _stub(task_id="F-1", read_files=["foundry/a.py"])
    assert BuilderWorker._read_files(_stub(read_files_for=None, _help_answer=lambda _ticket: {}), ticket, ["foundry/a.py"]) == ["foundry/a.py"]


def test_read_only_excerpts_query_carries_the_ticket_text() -> None:
    seen: dict[str, Any] = {}

    def build(paths: list[str], text: str) -> list[dict[str, Any]]:
        seen.update(paths=paths, text=text)
        return [{"path": paths[0]}]

    worker = _stub(
        _stack_engine=lambda: _stub(read_only_excerpts=build),
        _work_order=lambda _task: {"description": "order text"},
        _requested_symbols=lambda _ticket: ["asked_for"],
    )
    ticket = _stub(task_id="F-1", description="ticket text")
    info = {"title": "Title", "acceptance_criteria": ["criterion"], "read_files": ["a.py"]}
    assert BuilderWorker._read_only_excerpts(worker, ticket, info) == [{"path": "a.py"}]
    assert seen["paths"] == ["a.py"]
    for expected in ("Title", "criterion", "ticket text", "order text", "asked_for"):
        assert expected in seen["text"]


def test_read_only_excerpts_empty_when_engine_cannot_build_them() -> None:
    worker = _stub(_stack_engine=lambda: _stub())
    assert BuilderWorker._read_only_excerpts(worker, _stub(), {}) == []


def test_small_file_is_shown_whole(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def helper():\n    return 1\n")
    excerpts = ContextEngine.read_only_excerpts(_engine(tmp_path), ["mod.py"], "anything")
    assert [(e["path"], e["start_line"], e["end_line"]) for e in excerpts] == [("mod.py", 1, 2)]
    assert "return 1" in excerpts[0]["text"]


def test_large_file_shows_only_what_the_query_names(tmp_path: Path) -> None:
    fillers = "".join(f"def filler_{i}():\n    return {i}\n\n" for i in range(100))
    _write(tmp_path, "big.py", fillers + "def wanted_symbol():\n    return 'found'\n")
    engine = _engine(tmp_path)
    text = ContextEngine.read_only_excerpts(engine, ["big.py"], "Change `wanted_symbol` here")[0]["text"]
    assert "return 'found'" in text
    assert "def filler_0" not in text


def test_large_file_falls_back_to_symbol_first_lines(tmp_path: Path) -> None:
    fillers = "".join(f"def filler_{i}():\n    return {i}\n\n" for i in range(100))
    _write(tmp_path, "big.py", fillers)
    text = ContextEngine.read_only_excerpts(_engine(tmp_path), ["big.py"], "nothing relevant")[0]["text"]
    assert "def filler_0" in text
    assert "return 0" not in text


def test_files_outside_the_repo_or_missing_are_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(tmp_path, "outside.py")
    engine = _engine(repo)
    assert ContextEngine.read_only_excerpts(engine, ["../outside.py", "missing.py"], "anything") == []


def test_excerpt_line_budget_is_shared_between_files(tmp_path: Path) -> None:
    _write(tmp_path, "one.py", "a = 1\nb = 2\nc = 3\n")
    _write(tmp_path, "two.py", "d = 4\n")
    excerpts = ContextEngine.read_only_excerpts(_engine(tmp_path), ["one.py", "two.py"], "x", max_lines=2)
    assert [(e["path"], e["end_line"]) for e in excerpts] == [("one.py", 2)]


def test_work_order_read_files_reach_the_manager_lookup() -> None:
    manager = _stub(orders={"F-1": WorkOrder(title="t", read_files=["a.py"])})
    assert FloorManager.read_files_for(manager, "F-1") == ["a.py"]
    assert FloorManager.read_files_for(manager, "F-2") == []


def test_read_only_files_adds_existing_tests_of_editable_files(tmp_path: Path) -> None:
    _write(tmp_path, "foundry/tests/test_a.py")
    _write(tmp_path, "tests/test_a.py")
    found = _read_only_files(["foundry/a.py", "foundry/b.py"], ["./docs\\spec.md"], str(tmp_path))
    assert found == ["docs/spec.md", "foundry/tests/test_a.py", "tests/test_a.py"]


def test_read_only_files_never_protects_what_the_task_may_edit(tmp_path: Path) -> None:
    _write(tmp_path, "foundry/tests/test_a.py")
    assert _read_only_files(["foundry/a.py", "foundry/tests/*.py"], ["foundry/a.py"], str(tmp_path)) == []


def test_read_only_files_without_a_root_adds_no_tests(tmp_path: Path) -> None:
    _write(tmp_path, "foundry/tests/test_a.py")
    assert _read_only_files(["foundry/a.py"], [], None) == []


def _related(root: Path, files: list[str]) -> list[str]:
    watcher = _stub(
        service=_stub(repo=str(root)),
        _MAX_RELATED_TESTS=BatchWatcher._MAX_RELATED_TESTS,
        _RELATED_TESTS_SKIP=BatchWatcher._RELATED_TESTS_SKIP,
        _imported_modules=BatchWatcher._imported_modules,
    )
    return BatchWatcher._related_tests(watcher, files)


def test_related_tests_finds_importers_with_matching_name_first(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_other.py", "import foundry.gates\n")
    _write(tmp_path, "foundry/tests/test_gates.py", "from foundry import gates\n")
    _write(tmp_path, "tests/test_unrelated.py", "import os\n")
    assert _related(tmp_path, ["foundry/gates.py"]) == ["foundry/tests/test_gates.py", "tests/test_other.py"]


def test_related_tests_caps_the_list_and_skips_editable_tests(tmp_path: Path) -> None:
    for n in range(5):
        _write(tmp_path, f"tests/test_{n}.py", "import foundry.gates\n")
    found = _related(tmp_path, ["foundry/gates.py", "tests/test_0.py"])
    assert found == ["tests/test_1.py", "tests/test_2.py", "tests/test_3.py"]


def test_related_tests_ignores_non_python_files(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_other.py", "import foundry.gates\n")
    assert _related(tmp_path, ["docs/guide.md"]) == []


def test_excerpt_budget_goes_first_to_files_the_query_names(tmp_path: Path) -> None:
    _write(tmp_path, "other.py", "a = 1\nb = 2\nc = 3\n")
    _write(tmp_path, "named.py", "def wanted_symbol():\n    return 'found'\n")
    engine = _engine(tmp_path)
    query = "Change `wanted_symbol` here"
    excerpts = ContextEngine.read_only_excerpts(engine, ["other.py", "named.py"], query, max_lines=2)
    assert [e["path"] for e in excerpts] == ["named.py"]
    assert "return 'found'" in excerpts[0]["text"]


def test_whole_file_that_does_not_fit_shrinks_to_its_named_lines(tmp_path: Path) -> None:
    fillers = "".join(f"def filler_{i}():\n    return {i}\n\n" for i in range(34))
    _write(tmp_path, "mid.py", fillers + "def wanted_symbol():\n    return 'found'\n")
    engine = _engine(tmp_path)
    query = "Change `wanted_symbol` here"
    text = ContextEngine.read_only_excerpts(engine, ["mid.py"], query, max_lines=50)[0]["text"]
    assert "return 'found'" in text
    assert "def filler_0" not in text


def test_ticket_read_file_shows_named_test_lines_and_refuses_edits(tmp_path: Path) -> None:
    rel = "foundry/tests/test_x.py"
    fillers = "".join(f"def filler_{i}():\n    return {i}\n\n" for i in range(100))
    _write(tmp_path, rel, fillers + "def wanted_symbol():\n    return 'found'\n")
    query = "Change `wanted_symbol` here"
    excerpts = ContextEngine.read_only_excerpts(_engine(tmp_path), [rel], query)
    assignment = _assignment(["foundry/x.py", rel], [rel], excerpts)
    editable, read_only = build_builder_prompt(assignment).split("## Read-only files")
    assert f"- {rel}" not in editable
    assert f"- {rel}" in read_only
    assert "return 'found'" in read_only
    assert "def filler_0" not in read_only
    assert _read_only_touched(["foundry/x.py", rel], assignment) == [rel]


def test_editing_a_module_adds_at_most_three_of_its_tests_as_read_only(tmp_path: Path) -> None:
    _write(tmp_path, "foundry/tests/test_x.py", "from foundry.x import thing\n")
    for name in ("a", "b", "c"):
        _write(tmp_path, f"tests/test_{name}.py", "import foundry.x\n")
    _write(tmp_path, "tests/test_other.py", "import foundry.y\n")
    found = _related(tmp_path, ["foundry/x.py"])
    assert found == ["foundry/tests/test_x.py", "tests/test_a.py", "tests/test_b.py"]
    assert set(_read_only_files(["foundry/x.py"], found, str(tmp_path))) == set(found)


def test_read_files_include_the_floor_manager_answer_files() -> None:
    worker = _stub(
        read_files_for=lambda _task: [],
        _help_answer=lambda _ticket: {"note": "n", "read_files": ["docs/guide.md", "foundry/a.py"]},
    )
    ticket = _stub(task_id="F-1", read_files=[])
    assert BuilderWorker._read_files(worker, ticket, ["foundry/a.py"]) == ["docs/guide.md"]
