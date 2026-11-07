"""Tests for foundry.validation_gates: diff application and deterministic gates."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from foundry import validation_gates as vg
from foundry.schemas import BuilderSubmission, DeterministicGateResult
from foundry.validation_gates import (
    AppliedFile,
    DiffApplyError,
    GateOutcome,
    ValidationGates,
    apply_diff,
    builtin_lint,
    run_gates,
    run_gates_outcome,
)

HAS_NODE = shutil.which("node") is not None
HAS_RUFF = shutil.which("ruff") is not None


def submission(files: list[str], diff: str = "") -> BuilderSubmission:
    """A ready_for_review submission touching ``files``."""
    return BuilderSubmission(
        task_id="T-1",
        subtask_id="S-1",
        builder_id="bravo-1",
        status="ready_for_review",
        modified_files=files,
        diff=diff,
    )


def write(ws: Path, rel: str, text: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# apply_diff
# --------------------------------------------------------------------------

NEW_FILE_DIFF = """\
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+def hello() -> str:
+    return "hi"
"""


def test_apply_diff_creates_new_file(tmp_path: Path) -> None:
    changed = apply_diff(NEW_FILE_DIFF, tmp_path, allowed_files=["pkg/new.py"])
    assert changed == [AppliedFile("pkg/new.py", "added")]
    assert (tmp_path / "pkg/new.py").read_text() == 'def hello() -> str:\n    return "hi"\n'


def test_apply_diff_modifies_with_multiple_hunks_and_offset(tmp_path: Path) -> None:
    original = "".join(f"line{i}\n" for i in range(1, 21))
    write(tmp_path, "a.txt", "header\n" + original)  # shifted by one line
    diff = """\
diff --git a/a.txt b/a.txt
index 111..222 100644
--- a/a.txt
+++ b/a.txt
@@ -2,3 +2,3 @@
 line2
-line3
+LINE3
 line4
@@ -17,3 +17,4 @@
 line17
 line18
+inserted
 line19
"""
    changed = apply_diff(diff, tmp_path)
    assert changed == [AppliedFile("a.txt", "modified")]
    lines = (tmp_path / "a.txt").read_text().splitlines()
    assert lines[0] == "header"
    assert "LINE3" in lines and "line3" not in lines
    assert lines[lines.index("line18") + 1] == "inserted"
    assert len(lines) == 22


def test_apply_diff_deletes_file(tmp_path: Path) -> None:
    write(tmp_path, "old.py", "x = 1\ny = 2\n")
    diff = """\
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""
    assert apply_diff(diff, tmp_path) == [AppliedFile("old.py", "deleted")]
    assert not (tmp_path / "old.py").exists()


def test_apply_diff_multiple_files_and_no_newline_marker(tmp_path: Path) -> None:
    write(tmp_path, "b.txt", "one\ntwo")
    diff = """\
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
 one
-two
\\ No newline at end of file
+three
--- /dev/null
+++ b/c.txt
@@ -0,0 +1 @@
+no eol
\\ No newline at end of file
"""
    changed = apply_diff(diff, tmp_path)
    assert [c.action for c in changed] == ["modified", "added"]
    assert (tmp_path / "b.txt").read_text() == "one\nthree\n"
    assert (tmp_path / "c.txt").read_text() == "no eol"


def test_apply_diff_preserves_crlf(tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\n")
    diff = "--- a/w.txt\n+++ b/w.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    apply_diff(diff, tmp_path)
    assert (tmp_path / "w.txt").read_bytes() == b"a\r\nc\r\n"


def test_apply_diff_context_mismatch_is_atomic(tmp_path: Path) -> None:
    write(tmp_path, "good.txt", "keep\n")
    write(tmp_path, "bad.txt", "actual\n")
    diff = """\
--- a/good.txt
+++ b/good.txt
@@ -1 +1 @@
-keep
+changed
--- a/bad.txt
+++ b/bad.txt
@@ -1 +1 @@
-expected
+changed
"""
    with pytest.raises(DiffApplyError, match="does not apply"):
        apply_diff(diff, tmp_path)
    # Nothing written: the first file is untouched.
    assert (tmp_path / "good.txt").read_text() == "keep\n"


@pytest.mark.parametrize(
    "diff, message",
    [
        ("just some text\n", "no file changes"),
        (NEW_FILE_DIFF.replace("pkg/new.py", "../evil.py"), "escapes the workspace"),
        (NEW_FILE_DIFF.replace("b/pkg/new.py", "/etc/evil.py"), "absolute path"),
        (NEW_FILE_DIFF.replace("b/pkg/new.py", "C:\\evil.py"), "absolute path"),
        (NEW_FILE_DIFF.replace("pkg/new.py", "pkg/../../evil.py"), "escapes the workspace"),
        ("--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n", "renames"),
        ("--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-a\n+b\n", "does not exist"),
        ("Binary files a/x.png and b/x.png differ\n", "binary"),
    ],
)
def test_apply_diff_rejects_bad_input(tmp_path: Path, diff: str, message: str) -> None:
    with pytest.raises(DiffApplyError, match=message):
        apply_diff(diff, tmp_path)
    assert not (tmp_path.parent / "evil.py").exists()


def test_apply_diff_refuses_paths_outside_allowed_files(tmp_path: Path) -> None:
    with pytest.raises(DiffApplyError, match="not in allowed_files"):
        apply_diff(NEW_FILE_DIFF, tmp_path, allowed_files=["pkg/other.py"])
    with pytest.raises(DiffApplyError, match="not in allowed_files"):
        apply_diff(NEW_FILE_DIFF, tmp_path, allowed_files=[])
    assert not (tmp_path / "pkg/new.py").exists()


@pytest.mark.parametrize("allowed", [["pkg/*.py"], ["pkg/"], ["./pkg/new.py"], ["pkg\\new.py"]])
def test_apply_diff_allowed_patterns(tmp_path: Path, allowed: list[str]) -> None:
    apply_diff(NEW_FILE_DIFF, tmp_path, allowed_files=allowed)
    assert (tmp_path / "pkg/new.py").exists()


def test_apply_diff_refuses_symlink_escape(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    try:
        (ws / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    diff = NEW_FILE_DIFF.replace("pkg/new.py", "link/new.py")
    with pytest.raises(DiffApplyError, match="escapes the workspace"):
        apply_diff(diff, ws)
    assert not (outside / "new.py").exists()


def test_apply_diff_new_file_must_not_exist(tmp_path: Path) -> None:
    write(tmp_path, "pkg/new.py", "x = 1\n")
    with pytest.raises(DiffApplyError, match="already exists"):
        apply_diff(NEW_FILE_DIFF, tmp_path)


def test_apply_diff_partial_delete_rejected(tmp_path: Path) -> None:
    write(tmp_path, "d.txt", "a\nb\n")
    diff = "--- a/d.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    with pytest.raises(DiffApplyError, match="does not remove all"):
        apply_diff(diff, tmp_path)
    assert (tmp_path / "d.txt").exists()


# --------------------------------------------------------------------------
# syntax gate
# --------------------------------------------------------------------------


def test_syntax_gate_passes_and_fails_python(tmp_path: Path) -> None:
    write(tmp_path, "ok.py", "x = 1\n")
    [res] = run_gates(submission(["ok.py"]), tmp_path, checks=["syntax"])
    assert isinstance(res, DeterministicGateResult)
    assert res.passed and res.check_type == "syntax"
    assert (res.task_id, res.subtask_id) == ("T-1", "S-1")

    write(tmp_path, "bad.py", "def f(:\n    pass\n")
    [res] = run_gates(submission(["ok.py", "bad.py"]), tmp_path, checks=["syntax"])
    assert not res.passed
    assert "bad.py:1" in res.stderr and "SyntaxError" in res.stderr


def test_syntax_gate_json(tmp_path: Path) -> None:
    write(tmp_path, "good.json", '{"a": 1}')
    write(tmp_path, "bad.json", '{"a": 1,}')
    assert run_gates(submission(["good.json"]), tmp_path, checks=["syntax"])[0].passed
    [res] = run_gates(submission(["bad.json"]), tmp_path, checks=["syntax"])
    assert not res.passed and "bad.json:1" in res.stderr


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_syntax_gate_node_check(tmp_path: Path) -> None:
    write(tmp_path, "good.mjs", "export const x = 1;\n")
    write(tmp_path, "bad.js", "function (\n")
    assert run_gates(submission(["good.mjs"]), tmp_path, checks=["syntax"])[0].passed
    [res] = run_gates(submission(["bad.js"]), tmp_path, checks=["syntax"])
    assert not res.passed and "bad.js" in res.stderr


def test_syntax_gate_skips_js_without_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vg.shutil, "which", lambda name: None)
    write(tmp_path, "bad.js", "function (\n")
    [res] = run_gates(submission(["bad.js"]), tmp_path, checks=["syntax"])
    assert res.passed
    assert res.note is not None and "node not installed" in res.note


def test_syntax_gate_ignores_deleted_files_and_refuses_escaping_paths(tmp_path: Path) -> None:
    [res] = run_gates(submission(["gone.py"]), tmp_path, checks=["syntax"])
    assert res.passed
    [res] = run_gates(submission(["../outside.py"]), tmp_path, checks=["syntax"])
    assert not res.passed and "escapes the workspace" in res.stderr


# --------------------------------------------------------------------------
# linter gate
# --------------------------------------------------------------------------

LINT_BAD = "import os\n\ntry:\n    x = 1\nexcept:\n    x = 2\n"
LINT_OK = (
    "from __future__ import annotations\n"
    "import sys\n"
    "from typing import TYPE_CHECKING\n"
    "from pkg import exported as exported\n"
    "if TYPE_CHECKING:\n"
    "    from pathlib import Path\n"
    "def f(p: 'Path') -> int:\n"
    "    try:\n"
    "        return len(sys.argv)\n"
    "    except ValueError:\n"
    "        return 0\n"
)


def test_builtin_lint_detects_unused_import_and_bare_except() -> None:
    problems = builtin_lint(LINT_BAD, "m.py")
    assert any("F401" in p and "'os'" in p and p.startswith("m.py:1:") for p in problems)
    assert any("E722" in p and p.startswith("m.py:5:") for p in problems)
    assert builtin_lint(LINT_OK, "m.py") == []


def test_builtin_lint_compile_level_errors() -> None:
    # ast.parse accepts this; compile() rejects it.
    [problem] = builtin_lint("return 1\n", "m.py")
    assert "E999" in problem and "outside function" in problem


def test_builtin_lint_respects_all_and_init() -> None:
    assert builtin_lint("import os\n__all__ = ['os']\n", "m.py") == []
    assert builtin_lint("import os\n", "pkg/__init__.py") == []


@pytest.mark.parametrize(
    "mode",
    ["builtin", pytest.param("ruff", marks=pytest.mark.skipif(not HAS_RUFF, reason="no ruff"))],
)
def test_linter_gate(tmp_path: Path, mode: str) -> None:
    write(tmp_path, "bad.py", LINT_BAD)
    write(tmp_path, "ok.py", "import sys\nprint(sys.argv)\n")
    write(tmp_path, "notes.txt", "not python")
    [res] = run_gates(submission(["ok.py", "notes.txt"]), tmp_path, checks=["linter"], linter=mode)
    assert res.passed, res.stdout + res.stderr
    [res] = run_gates(submission(["bad.py"]), tmp_path, checks=["linter"], linter=mode)
    assert not res.passed
    assert "F401" in res.stdout and "E722" in res.stdout


def test_linter_auto_falls_back_without_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vg.shutil, "which", lambda name: None)
    write(tmp_path, "bad.py", LINT_BAD)
    [res] = run_gates(submission(["bad.py"]), tmp_path, checks=["linter"])
    assert not res.passed and "builtin linter" in res.stderr
    [res] = run_gates(submission(["bad.py"]), tmp_path, checks=["linter"], linter="ruff")
    assert not res.passed and "ruff requested but not installed" in res.stderr


# --------------------------------------------------------------------------
# tests gate
# --------------------------------------------------------------------------


def test_tests_gate_skipped_without_command(tmp_path: Path) -> None:
    [res] = run_gates(submission([]), tmp_path, checks=["tests"])
    assert res.passed and res.note and "skipped" in res.note


def test_tests_gate_runs_pytest_pass_and_fail(tmp_path: Path) -> None:
    write(tmp_path, "test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    write(tmp_path, "test_bad.py", "def test_bad():\n    assert 1 + 1 == 3\n")
    cmd = "python -m pytest -q -p no:cacheprovider test_ok.py"
    [res] = run_gates(submission([]), tmp_path, checks=["tests"], test_cmd=cmd)
    assert res.passed, res.stdout + res.stderr
    assert "1 passed" in res.stdout

    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_bad.py"]
    [res] = run_gates(submission([]), tmp_path, checks=["tests"], test_cmd=cmd)
    assert not res.passed
    assert "1 failed" in res.stdout and "test_bad.py" in res.stdout


def test_tests_gate_timeout(tmp_path: Path) -> None:
    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    [res] = run_gates(submission([]), tmp_path, checks=["tests"], test_cmd=cmd, test_timeout=0.3)
    assert not res.passed and "timed out" in res.stderr


def test_tests_gate_missing_executable(tmp_path: Path) -> None:
    [res] = run_gates(
        submission([]), tmp_path, checks=["tests"], test_cmd=["definitely-not-a-binary-xyz"]
    )
    assert not res.passed and "cannot run test command" in res.stderr


# --------------------------------------------------------------------------
# schema gate
# --------------------------------------------------------------------------

SCHEMA_SRC = """\
class Widget:
    size: int = 0

    def resize(self, width: int, *, height: int = 0) -> None:
        ...

async def fetch(url, *args, timeout=1.0, **kw):
    ...

VERSION = "1"
"""


def test_schema_gate_passes(tmp_path: Path) -> None:
    write(tmp_path, "mod.py", SCHEMA_SRC)
    targets = {
        "mod.py": [
            "Widget",
            "Widget.resize(self, width, *, height)",
            "Widget.size",
            "fetch(url, *args, timeout, **kw)",
            "fetch",
            "VERSION",
        ]
    }
    [res] = run_gates(submission([]), tmp_path, checks=["schema"], schema_targets=targets)
    assert res.passed, res.stderr


@pytest.mark.parametrize(
    "targets, message",
    [
        ({"mod.py": ["Gadget"]}, "missing Gadget"),
        ({"mod.py": ["Widget.shrink"]}, "missing Widget.shrink"),
        ({"mod.py": ["Widget.resize(self, width, height)"]}, "signature mismatch"),
        ({"mod.py": ["VERSION(x)"]}, "is not a function"),
        ({"mod.py": ["fetch.inner"]}, "missing fetch.inner"),
        ({"nope.py": ["X"]}, "file not found"),
        ({"../mod.py": ["X"]}, "escapes the workspace"),
        ({"mod.py": ["not valid!"]}, "bad schema target"),
    ],
)
def test_schema_gate_failures(tmp_path: Path, targets: dict[str, list[str]], message: str) -> None:
    write(tmp_path, "mod.py", SCHEMA_SRC)
    [res] = run_gates(submission([]), tmp_path, checks=["schema"], schema_targets=targets)
    assert not res.passed
    assert message in res.stderr


def test_schema_gate_skipped_without_targets(tmp_path: Path) -> None:
    [res] = run_gates(submission([]), tmp_path, checks=["schema"])
    assert res.passed and res.note


# --------------------------------------------------------------------------
# short-circuit + GateOutcome + ValidationGates
# --------------------------------------------------------------------------


def test_run_gates_all_pass(tmp_path: Path) -> None:
    write(tmp_path, "mod.py", SCHEMA_SRC)
    write(tmp_path, "test_mod.py", "from mod import Widget\n\ndef test_w():\n    assert Widget().size == 0\n")
    outcome = run_gates_outcome(
        submission(["mod.py", "test_mod.py"]),
        tmp_path,
        test_cmd="python -m pytest -q -p no:cacheprovider test_mod.py",
        schema_targets={"mod.py": ["Widget"]},
        linter="builtin",
    )
    assert outcome.passed and outcome.first_failure is None
    assert [r.check_type for r in outcome.results] == ["syntax", "linter", "tests", "schema"]
    assert outcome.summary().startswith("all gates passed")


def test_run_gates_short_circuits_on_first_failure(tmp_path: Path) -> None:
    write(tmp_path, "mod.py", "def broken(:\n")
    results = run_gates(
        submission(["mod.py"]),
        tmp_path,
        test_cmd=[sys.executable, "-c", "raise SystemExit('tests must not run')"],
        schema_targets={"mod.py": ["broken"]},
    )
    assert [r.check_type for r in results] == ["syntax"]
    outcome = GateOutcome.from_results(results)
    assert not outcome.passed
    assert outcome.first_failure is results[0]
    assert outcome.failed_check == "syntax"
    hint = outcome.summary()
    assert "\n" not in hint and len(hint) <= 160 and "mod.py:1" in hint


def test_run_gates_without_short_circuit_runs_everything(tmp_path: Path) -> None:
    write(tmp_path, "mod.py", LINT_BAD)
    results = run_gates(
        submission(["mod.py"]),
        tmp_path,
        schema_targets={"mod.py": ["Missing"]},
        linter="builtin",
        short_circuit=False,
    )
    assert [r.passed for r in results] == [True, False, True, False]
    outcome = GateOutcome.from_results(results)
    assert outcome.failed_check == "linter"


def test_run_gates_rejects_unknown_check(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown gate"):
        run_gates(submission([]), tmp_path, checks=["vibes"])  # type: ignore[list-item]


def test_validation_gates_validate_applies_diff_then_checks(tmp_path: Path) -> None:
    gates = ValidationGates(schema_targets={"pkg/new.py": ["hello()"]}, linter="builtin")
    sub = submission(["pkg/new.py"], NEW_FILE_DIFF)
    outcome = gates.validate(sub, tmp_path, allowed_files=["pkg/new.py"])
    assert outcome.passed, outcome.summary()
    assert (tmp_path / "pkg/new.py").exists()


def test_validation_gates_refused_diff_is_a_failure(tmp_path: Path) -> None:
    gates = ValidationGates()
    sub = submission(["pkg/new.py"], NEW_FILE_DIFF)
    outcome = gates.validate(sub, tmp_path, allowed_files=["other.py"])
    assert not outcome.passed
    assert outcome.first_failure is not None
    assert "not in allowed_files" in outcome.first_failure.stderr
    assert not (tmp_path / "pkg").exists()


def test_validation_gates_async(tmp_path: Path) -> None:
    gates = ValidationGates(linter="builtin")
    bad = NEW_FILE_DIFF.replace('    return "hi"', '    return "hi"(')
    outcome = asyncio.run(gates.validate_async(submission(["pkg/new.py"], bad), tmp_path))
    assert not outcome.passed and outcome.failed_check == "syntax"


def test_reexports_cache_and_retry_policy() -> None:
    from foundry import solution_cache

    assert vg.SolutionCache is solution_cache.SolutionCache
    assert vg.RetryPolicy is solution_cache.RetryPolicy
