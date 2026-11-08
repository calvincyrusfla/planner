"""Baseline behaviour of the typecheck gate: only errors a change introduces fail it."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from foundry import validation_gates as vg

MARK = "# BAD:"
NEW_ONE = "mypy: 1 new type error(s) in changed files"
UNFILTERED_ONE = "mypy: 1 type error(s) in changed files"


def _scan(py_files: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Stand-in for mypy: every line holding ``MARK`` is an error with the text after it."""
    lines: list[str] = []
    for rel in py_files:
        text = (cwd / rel).read_bytes().decode("utf-8")
        for number, source in enumerate(text.splitlines(), 1):
            if MARK in source:
                message = source.split(MARK, 1)[1].strip()
                lines.append(f"{rel}:{number}:1: error: {message}  [misc]")
    return subprocess.CompletedProcess([], 1 if lines else 0, "\n".join(lines), "")


@pytest.fixture
def mypy_runs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace mypy with :func:`_scan`; the list collects each run's working directory."""
    runs: list[Path] = []

    def invoke(
        base: Sequence[str], py_files: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        runs.append(cwd)
        return _scan(py_files, cwd)

    monkeypatch.setattr(vg, "_mypy_command", lambda: [sys.executable, "-m", "mypy"])
    monkeypatch.setattr(vg, "_invoke_mypy", invoke)
    return runs


def _diff(*changes: tuple[str, str | None, str | None]) -> str:
    parts = [vg._file_diff(rel, old, new).rstrip("\n") for rel, old, new in changes]
    return "\n".join(parts) + "\n"


def _gate(
    root: Path, post: dict[str, str], diff: str, timeout: float = 30.0
) -> tuple[bool, str, str]:
    """Write the post-change ``post`` files to ``root`` and run the gate on them."""
    files: list[tuple[str, Path]] = []
    for rel, text in post.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        files.append((rel, path))
    return vg._gate_typecheck(files, root, "auto", timeout, diff)


def test_error_on_untouched_line_is_ignored(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\nb = 2\n"
    new = "a = 1  # BAD: old problem\nb = 3\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert passed
    assert err == ""
    assert out.splitlines() == [
        "mypy: no new type errors, ignored 1 pre-existing type error(s)",
        "pre-existing: mod.py:1:1: error: old problem  [misc]",
    ]
    assert len(mypy_runs) == 2


def test_error_moved_down_is_pre_existing(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\n"
    new = "import os\nimport sys\na = 1  # BAD: old problem\n"

    passed, out, _ = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert passed
    assert "pre-existing: mod.py:3:1: error: old problem  [misc]" in out


def test_message_line_reference_is_normalised(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: redefined, see line 1\n"
    new = "b = 2\na = 1  # BAD: redefined, see line 2\n"

    passed, _, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert passed
    assert err == ""


def test_only_new_errors_are_reported(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\n"
    new = "a = 1  # BAD: old problem\nb = 2  # BAD: fresh one\nc = 3  # BAD: fresh two\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert not passed
    assert out.splitlines() == [
        "mod.py:2:1: error: fresh one  [misc]",
        "mod.py:3:1: error: fresh two  [misc]",
    ]
    assert err == "mypy: 2 new type error(s) in changed files"


def test_extra_occurrence_blames_added_line(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: same\n"
    new = "b = 2  # BAD: same\na = 1  # BAD: same\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert not passed
    assert out == "mod.py:1:1: error: same  [misc]"
    assert err == NEW_ONE


def test_clean_result_needs_no_baseline(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\n"
    new = "a = 1\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert passed
    assert out == "mypy: clean"
    assert err == ""
    assert len(mypy_runs) == 1


def test_brand_new_file_errors_all_count(tmp_path: Path, mypy_runs: list[Path]) -> None:
    new = "a = 1  # BAD: fresh one\n"

    passed, out, err = _gate(tmp_path, {"fresh.py": new}, _diff(("fresh.py", None, new)))

    assert not passed
    assert out == "fresh.py:1:1: error: fresh one  [misc]"
    assert err == NEW_ONE
    assert len(mypy_runs) == 1


def test_clean_brand_new_file_passes(tmp_path: Path, mypy_runs: list[Path]) -> None:
    new = "a = 1\n"

    passed, out, _ = _gate(tmp_path, {"fresh.py": new}, _diff(("fresh.py", None, new)))

    assert passed
    assert out == "mypy: clean"


def test_baseline_is_per_file(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\n"
    new = "a = 1  # BAD: old problem\nb = 2\n"
    fresh = "c = 3  # BAD: old problem\n"
    diff = _diff(("mod.py", old, new), ("fresh.py", None, fresh))

    passed, out, err = _gate(tmp_path, {"mod.py": new, "fresh.py": fresh}, diff)

    assert not passed
    assert out == "fresh.py:1:1: error: old problem  [misc]"
    assert err == NEW_ONE


def test_renamed_file_keeps_its_baseline(tmp_path: Path, mypy_runs: list[Path]) -> None:
    new = "a = 1  # BAD: old problem\nb = 2\n"
    diff = "--- a/old.py\n+++ b/new.py\n@@ -1,1 +1,2 @@\n a = 1  # BAD: old problem\n+b = 2\n"

    passed, out, _ = _gate(tmp_path, {"new.py": new}, diff)

    assert passed
    assert "pre-existing: new.py:1:1: error: old problem  [misc]" in out


@pytest.mark.parametrize("diff", ["", "this is not a diff\n"])
def test_without_a_usable_diff_every_error_counts(
    tmp_path: Path, mypy_runs: list[Path], diff: str
) -> None:
    new = "a = 1  # BAD: old problem\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, diff)

    assert not passed
    assert out == "mod.py:1:1: error: old problem  [misc]"
    assert err == UNFILTERED_ONE


def test_baseline_runs_in_a_scratch_copy(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "a = 1  # BAD: old problem\nb = 2\n"
    new = "a = 1  # BAD: old problem\nb = 3\n"

    _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert [path.name for path in tmp_path.iterdir()] == ["mod.py"]
    assert (tmp_path / "mod.py").read_bytes().decode("utf-8") == new
    assert mypy_runs[0] == tmp_path
    assert mypy_runs[1] != tmp_path
    assert not mypy_runs[1].exists()


def test_pre_existing_listing_is_capped(tmp_path: Path, mypy_runs: list[Path]) -> None:
    old = "".join(f"v{i} = 1  # BAD: problem {i}\n" for i in range(25))
    new = old + "done = 1\n"
    limit = vg._MAX_PREEXISTING_LISTED

    passed, out, _ = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    lines = out.splitlines()
    assert passed
    assert lines[0] == "mypy: no new type errors, ignored 25 pre-existing type error(s)"
    assert len(lines) == limit + 2
    assert lines[-1] == f"pre-existing: ... and {25 - limit} more"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (subprocess.TimeoutExpired("mypy", 30.0), "baseline mypy timed out after 30s"),
        (OSError("no such file"), "baseline mypy could not start: no such file"),
        (subprocess.CompletedProcess([], 2, "", "boom"), "baseline mypy could not run: boom"),
    ],
)
def test_baseline_mypy_failure_skips_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mypy_runs: list[Path],
    failure: Exception | subprocess.CompletedProcess[str],
    reason: str,
) -> None:
    def invoke(
        base: Sequence[str], py_files: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if cwd == tmp_path:
            return _scan(py_files, cwd)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(vg, "_invoke_mypy", invoke)
    old = "a = 1  # BAD: old problem\nb = 2\n"
    new = "a = 1  # BAD: old problem\nb = 3\n"

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)))

    assert passed
    assert err == ""
    assert out.splitlines()[0] == f"typecheck skipped ({reason})"
    assert "mod.py:1:1: error: old problem" in out


def test_real_mypy_reports_only_the_new_error(tmp_path: Path) -> None:
    pytest.importorskip("mypy")
    old = 'a: int = "x"\n'
    new = 'a: int = "x"\nb: int = "y"\nc: int = 1\n'

    passed, out, err = _gate(tmp_path, {"mod.py": new}, _diff(("mod.py", old, new)), timeout=180.0)

    if "typecheck skipped" in out:
        pytest.skip(out)
    assert not passed
    assert err == NEW_ONE
    assert [line.split(":")[:2] for line in out.splitlines()] == [["mod.py", "2"]]
