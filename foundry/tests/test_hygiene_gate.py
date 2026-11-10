"""Tests for the hygiene gate: debugger leftovers and unfinished-work markers."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from foundry.schemas import BuilderSubmission, GateCheck
from foundry.validation_gates import ALL_CHECKS, _gate_hygiene, run_gates


def _diff(path: str, *added: str) -> str:
    """A unified diff creating ``path`` with ``added`` as its lines."""
    body = "".join(f"+{line}\n" for line in added)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(added)} @@\n{body}"


@pytest.mark.parametrize(
    "line",
    [
        "    breakpoint()",
        "    pdb.set_trace()",
        "import pdb",
        "    import pdb; pdb.set_trace()",
        "import ipdb",
        "    ipdb.set_trace()",
    ],
)
def test_python_debug_leftovers_fail_with_file_and_line(line: str) -> None:
    passed, out, err = _gate_hygiene(_diff("pkg/app.py", "x = 1", line))
    assert not passed and out == ""
    assert "pkg/app.py:2:" in err


@pytest.mark.parametrize("name", ["app.js", "app.mjs", "app.cjs", "index.html"])
def test_js_debugger_statement_fails(name: str) -> None:
    passed, _, err = _gate_hygiene(_diff(name, "const a = 1;", "  debugger;"))
    assert not passed
    assert f"{name}:2:" in err


@pytest.mark.parametrize(
    "line",
    [
        'const s = "debugger;";',
        "const s = 'debugger;';",
        "const s = `debugger;`;",
        "// debugger;",
        "foo(); // debugger;",
        "/* debugger; */",
        " * debugger;",
        "obj.debugger;",
        "const notdebugger;",
    ],
)
def test_debugger_in_string_or_comment_passes(line: str) -> None:
    passed, _, err = _gate_hygiene(_diff("app.js", line))
    assert passed, err


def test_debugger_inside_a_multi_line_comment_passes() -> None:
    passed, _, err = _gate_hygiene(_diff("app.js", "/*", "debugger;", "*/"))
    assert passed, err


def test_debugger_after_a_closed_comment_fails() -> None:
    passed, _, err = _gate_hygiene(_diff("app.js", "/*", "note", "*/ debugger;"))
    assert not passed and "app.js:3:" in err


@pytest.mark.parametrize("word", ["TODO", "FIXME", "XXX"])
@pytest.mark.parametrize(
    ("path", "template"),
    [("app.py", "# {}: finish"), ("app.js", "// {} later"), ("README.md", "{} write this")],
)
def test_markers_fail_with_file_and_line(word: str, path: str, template: str) -> None:
    passed, _, err = _gate_hygiene(_diff(path, "first", template.format(word)))
    assert not passed
    assert f"{path}:2:" in err and word in err


@pytest.mark.parametrize(
    "path",
    [
        "tests/helper.py",
        "foundry/tests/helper.py",
        "test/helper.js",
        "pkg/test_app.py",
        "app.test.mjs",
    ],
)
def test_markers_in_test_files_pass(path: str) -> None:
    passed, _, err = _gate_hygiene(_diff(path, "# TODO: more cases"))
    assert passed, err


def test_debug_leftovers_in_test_files_still_fail() -> None:
    passed, _, err = _gate_hygiene(_diff("tests/test_app.py", "breakpoint()"))
    assert not passed and "tests/test_app.py:1:" in err


@pytest.mark.parametrize(
    "line", ["TODOS = []", "todo_list = []", "# todo later", "TODO_LIST = 1", "XXXL = 2", "FIXMES = 3"]
)
def test_markers_match_whole_upper_case_words_only(line: str) -> None:
    passed, _, err = _gate_hygiene(_diff("app.py", line))
    assert passed, err


@pytest.mark.parametrize(
    ("path", "line"), [("app.py", "print('hello')"), ("app.js", "console.log('hello');")]
)
def test_print_and_console_log_pass(path: str, line: str) -> None:
    passed, out, err = _gate_hygiene(_diff(path, line))
    assert passed and err == "" and out


def test_debug_words_in_python_strings_and_comments_pass() -> None:
    passed, _, err = _gate_hygiene(_diff("app.py", 'x = "breakpoint()"', "# import pdb"))
    assert passed, err


def test_removed_and_context_lines_are_ignored() -> None:
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " # TODO: context stays\n"
        "-breakpoint()\n"
        "-# FIXME: removed\n"
        "+x = 1\n"
    )
    passed, _, err = _gate_hygiene(diff)
    assert passed, err


def test_header_paths_are_not_scanned() -> None:
    passed, _, err = _gate_hygiene(_diff("docs/TODO/XXX.md", "plain text"))
    assert passed, err


def test_line_numbers_follow_the_hunk_and_the_file() -> None:
    diff = (
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -10,1 +10,2 @@\n"
        " keep = 1\n"
        "+breakpoint()\n"
        "--- a/two.js\n"
        "+++ b/two.js\n"
        "@@ -4,0 +5,1 @@\n"
        "+debugger;\n"
    )
    passed, _, err = _gate_hygiene(diff)
    assert not passed
    assert "one.py:11:" in err and "two.js:5:" in err


def test_report_lists_at_most_ten_findings() -> None:
    passed, _, err = _gate_hygiene(_diff("app.py", *["# TODO"] * 13))
    assert not passed
    lines = err.splitlines()
    assert sum(":" in line and "marker" in line and "app.py" in line for line in lines) == 10
    assert lines[-1] == "... and 3 more"


def test_empty_diff_passes() -> None:
    assert _gate_hygiene("")[0]


def test_hygiene_is_registered_right_after_secrets() -> None:
    assert "hygiene" in get_args(GateCheck)
    assert ALL_CHECKS.index("hygiene") == ALL_CHECKS.index("secrets") + 1


def test_run_gates_dispatches_hygiene_and_short_circuits(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("breakpoint()\n", encoding="utf-8")
    submission = BuilderSubmission(
        task_id="F-1",
        subtask_id="F-1.1",
        builder_id="alpha-1",
        status="ready_for_review",
        modified_files=["app.py"],
        diff=_diff("app.py", "breakpoint()"),
    )
    results = run_gates(submission, tmp_path, linter="builtin")
    assert [r.check_type for r in results] == ["syntax", "secrets", "hygiene"]
    assert not results[-1].passed and "app.py:1:" in results[-1].stderr
