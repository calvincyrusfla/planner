"""Tests for foundry.edits: structured builder edits turned into unified diffs."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from foundry.edits import (
    Edit,
    EditError,
    apply_edits,
    extract_edits_block,
    has_edits_block,
    list_symbols,
    parse_edits,
)
from foundry.validation_gates import apply_diff

MODULE = '''"""Sample module."""

import os


def alpha(x):
    """Return x plus one."""
    y = x + 1
    return y


@decorator
def beta():
    return "beta"


class Shop:
    """A shop."""

    def open(self):
        return True

    @property
    def name(self):
        return "shop"

    def close(self):
        return False


def gamma():
    return os.sep
'''

GIT = shutil.which("git")


def _ws(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "ws"
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        target.write_bytes(data)
    root.mkdir(exist_ok=True)
    return root


def _apply_via_gates(root: Path, diff: str, tmp_path: Path) -> Path:
    """Apply ``diff`` to a copy of ``root`` with the gates' apply_diff; return the copy."""
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    apply_diff(diff, copy)
    return copy


def _git_apply_check(root: Path, diff: str, tmp_path: Path) -> None:
    if GIT is None:
        pytest.skip("git not installed")
    copy = tmp_path / "gitcopy"
    shutil.copytree(root, copy)
    patch = tmp_path / "change.patch"
    patch.write_bytes(diff.encode("utf-8"))
    subprocess.run([GIT, "init", "-q"], cwd=copy, check=True)
    result = subprocess.run(
        [GIT, "apply", "--check", "--verbose", str(patch)],
        cwd=copy,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + "\n" + diff
    subprocess.run([GIT, "apply", str(patch)], cwd=copy, check=True)


def _run(tmp_path: Path, edits: list[dict], files: dict | None = None, allowed=None):
    root = _ws(tmp_path, files if files is not None else {"pkg/mod.py": MODULE})
    diff, errors = apply_edits(root, {"edits": edits}, allowed)
    return root, diff, errors


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_extract_edits_block_from_reply():
    payload = {"edits": [{"op": "append", "file": "a.py", "code": "x = 1\n"}]}
    reply = "Here you go.\n\n```edits\n" + json.dumps(payload, indent=2) + "\n```\n\nDone."
    assert has_edits_block(reply)
    assert json.loads(extract_edits_block(reply) or "") == payload
    assert extract_edits_block("```diff\n--- a\n```") is None
    assert not has_edits_block("")
    edits = parse_edits(reply)
    assert edits == [Edit(op="append", file="a.py", code="x = 1\n")]


def test_parse_edits_accepts_list_mapping_and_aliases():
    edits = parse_edits([{"op": "create_file", "path": "n.py", "content": "x = 1\n"}])
    assert edits[0].file == "n.py" and edits[0].code == "x = 1\n"
    single = parse_edits({"op": "delete_symbol", "file": "m.py", "symbol": "f"})
    assert single[0].symbol == "f"


@pytest.mark.parametrize(
    "payload, needle",
    [
        ("{not json", "not valid JSON"),
        ('{"other": 1}', '{"edits": [...]}'),
        ('{"edits": []}', "empty"),
        ('{"edits": [{"file": "a.py"}]}', 'missing "op"'),
        ('{"edits": [{"op": "rename", "file": "a.py"}]}', "unknown op 'rename'"),
        ('{"edits": [{"op": "append", "code": "x"}]}', 'missing "file"'),
        ('{"edits": [{"op": "replace_symbol", "file": "a.py", "code": "x"}]}', 'missing "symbol"'),
        ('{"edits": [{"op": "append", "file": "a.py", "code": "  "}]}', 'empty "code"'),
        ('{"edits": [{"op": "search_replace", "file": "a.py", "new": "x"}]}', '"old"'),
        ('{"edits": [{"op": "search_replace", "file": "a.py", "old": "x"}]}', 'missing "new"'),
        ('{"edits": [3]}', "must be a JSON object"),
    ],
)
def test_parse_edits_errors_are_precise(payload, needle):
    with pytest.raises(EditError, match=None) as info:
        parse_edits(payload)
    assert needle in str(info.value)
    assert "\n" not in str(info.value)


def test_apply_edits_reports_parse_errors(tmp_path):
    root = _ws(tmp_path, {"a.py": "x = 1\n"})
    diff, errors = apply_edits(root, "```edits\n{bad\n```", None)
    assert diff == "" and len(errors) == 1 and "not valid JSON" in errors[0]


def test_list_symbols_includes_methods_and_decorators():
    symbols = {name: (start, end) for name, start, end in list_symbols(MODULE)}
    assert symbols["beta"][0] == MODULE.splitlines().index("@decorator") + 1
    assert "Shop.open" in symbols and "Shop.name" in symbols
    assert symbols["alpha"] == (6, 9)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def test_replace_symbol_top_level(tmp_path):
    code = 'def alpha(x):\n    """Return x plus two."""\n    return x + 2\n'
    root, diff, errors = _run(tmp_path, [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "alpha", "code": code}])
    assert errors == []
    assert diff.startswith("diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@")
    copy = _apply_via_gates(root, diff, tmp_path)
    text = (copy / "pkg/mod.py").read_text()
    assert "return x + 2" in text and "y = x + 1" not in text
    ast.parse(text)
    assert (root / "pkg/mod.py").read_text() == MODULE  # workspace untouched
    _git_apply_check(root, diff, tmp_path)


def test_replace_symbol_includes_decorators(tmp_path):
    code = "@decorator\ndef beta():\n    return 'BETA'\n"
    root, diff, errors = _run(tmp_path, [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "beta", "code": code}])
    assert errors == []
    text = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_text()
    assert text.count("@decorator") == 1 and "'BETA'" in text


def test_replace_method_reindents_code(tmp_path):
    # Builder sends the method unindented; Foundry re-indents it into the class.
    code = "def open(self):\n    return 'open'\n"
    root, diff, errors = _run(tmp_path, [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "Shop.open", "code": code}])
    assert errors == []
    text = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_text()
    assert "    def open(self):\n        return 'open'\n" in text
    tree = ast.parse(text)
    shop = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    assert [n.name for n in shop.body if isinstance(n, ast.FunctionDef)] == ["open", "name", "close"]
    _git_apply_check(root, diff, tmp_path)


def test_replace_symbol_requires_matching_definition(tmp_path):
    _, diff, errors = _run(
        tmp_path,
        [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "alpha", "code": "def other():\n    pass\n"}],
    )
    assert diff == ""
    assert errors == ["edit 1 (replace_symbol pkg/mod.py:alpha): code defines 'other' but the symbol is 'alpha'"]

    _, _, errors = _run(
        tmp_path / "b",
        [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "alpha", "code": "def alpha():\n    pass\n\ndef extra():\n    pass\n"}],
    )
    assert "must define exactly 'alpha'" in errors[0]


def test_unknown_symbol_suggests_close_names(tmp_path):
    _, _, errors = _run(tmp_path, [{"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "alpah"}])
    assert "symbol 'alpah' not found" in errors[0] and "did you mean alpha" in errors[0]
    _, _, errors = _run(tmp_path / "b", [{"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "Shop.zzz"}])
    assert "Shop members: open, name, close" in errors[0]


def test_insert_after_and_before_symbol(tmp_path):
    root, diff, errors = _run(
        tmp_path,
        [
            {"op": "insert_after_symbol", "file": "pkg/mod.py", "symbol": "alpha", "code": "def alpha2():\n    return 2\n"},
            {"op": "insert_before_symbol", "file": "pkg/mod.py", "symbol": "Shop.close", "code": "def peek(self):\n    return 1\n"},
        ],
    )
    assert errors == []
    text = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_text()
    names = [name for name, _, _ in list_symbols(text)]
    assert names.index("alpha2") == names.index("alpha") + 1
    assert names.index("Shop.peek") == names.index("Shop.close") - 1
    assert "    return y\n\n\ndef alpha2():\n    return 2\n\n\n@decorator" in text
    assert "    def peek(self):\n        return 1\n\n    def close(self):" in text
    _git_apply_check(root, diff, tmp_path)


def test_delete_symbol_tidies_blank_lines(tmp_path):
    root, diff, errors = _run(
        tmp_path,
        [
            {"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "beta"},
            {"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "Shop.name"},
            {"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "gamma"},
        ],
    )
    assert errors == []
    text = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_text()
    assert "beta" not in text and "def name" not in text and "gamma" not in text
    assert "\n\n\n\n" not in text and text.endswith("return False\n")
    assert "    return y\n\n\nclass Shop:" in text
    _git_apply_check(root, diff, tmp_path)


def test_append_and_search_replace(tmp_path):
    root, diff, errors = _run(
        tmp_path,
        [
            {"op": "search_replace", "file": "pkg/mod.py", "old": "    y = x + 1\n", "new": "    y = x + 10\n"},
            {"op": "append", "file": "pkg/mod.py", "code": "def delta():\n    return 4\n"},
        ],
    )
    assert errors == []
    text = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_text()
    assert "y = x + 10" in text and text.endswith("    return os.sep\n\n\ndef delta():\n    return 4\n")
    _git_apply_check(root, diff, tmp_path)


def test_search_replace_must_match_exactly_once(tmp_path):
    _, _, errors = _run(tmp_path, [{"op": "search_replace", "file": "pkg/mod.py", "old": "return", "new": "yield"}])
    assert "occurs 6 times" in errors[0] and "unique" in errors[0]
    _, _, errors = _run(tmp_path / "b", [{"op": "search_replace", "file": "pkg/mod.py", "old": "y  =  x + 1", "new": "z"}])
    assert "occurs 0 times" in errors[0] and "whitespace" in errors[0]


def test_search_replace_works_on_non_python(tmp_path):
    root, diff, errors = _run(
        tmp_path,
        [{"op": "search_replace", "file": "web/app.js", "old": "const a = 1;", "new": "const a = 2;"}],
        files={"web/app.js": "// app\nconst a = 1;\n"},
    )
    assert errors == []
    assert (_apply_via_gates(root, diff, tmp_path) / "web/app.js").read_text() == "// app\nconst a = 2;\n"


def test_symbol_ops_refuse_non_python(tmp_path):
    _, _, errors = _run(
        tmp_path,
        [{"op": "delete_symbol", "file": "web/app.js", "symbol": "a"}],
        files={"web/app.js": "const a = 1;\n"},
    )
    assert "only works on .py files" in errors[0]


def test_create_file(tmp_path):
    root, diff, errors = _run(
        tmp_path, [{"op": "create_file", "file": "pkg/new_mod.py", "code": "VALUE = 1\n"}]
    )
    assert errors == []
    assert "new file mode 100644\n--- /dev/null\n+++ b/pkg/new_mod.py\n" in diff
    copy = _apply_via_gates(root, diff, tmp_path)
    assert (copy / "pkg/new_mod.py").read_text() == "VALUE = 1\n"
    _git_apply_check(root, diff, tmp_path)

    _, _, errors = _run(tmp_path / "b", [{"op": "create_file", "file": "pkg/mod.py", "code": "x = 1\n"}])
    assert "file already exists" in errors[0]


def test_missing_file_and_allowed_files(tmp_path):
    _, _, errors = _run(tmp_path, [{"op": "append", "file": "pkg/nope.py", "code": "x = 1\n"}])
    assert "file does not exist (use create_file" in errors[0]
    _, _, errors = _run(
        tmp_path / "b", [{"op": "append", "file": "pkg/mod.py", "code": "x = 1\n"}], allowed=["other.py"]
    )
    assert "path not in allowed_files: pkg/mod.py" in errors[0]
    _, diff, errors = _run(
        tmp_path / "c", [{"op": "append", "file": "pkg/mod.py", "code": "x = 1\n"}], allowed=["pkg/"]
    )
    assert errors == [] and diff


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "C:/x.py"])
def test_paths_cannot_escape(tmp_path, path):
    _, _, errors = _run(tmp_path, [{"op": "create_file", "file": path, "code": "x = 1\n"}])
    assert errors and ("escapes" in errors[0] or "absolute" in errors[0])


# --------------------------------------------------------------------------
# Safety checks
# --------------------------------------------------------------------------


def test_append_redefining_existing_function_is_rejected(tmp_path):
    # S-002 / F-008: a builder re-wrote a function it saw only as an outline.
    _, diff, errors = _run(
        tmp_path, [{"op": "append", "file": "pkg/mod.py", "code": "def alpha(x):\n    return x\n"}]
    )
    assert diff == ""
    assert errors == ["edit 1 (append pkg/mod.py): 'alpha' already defined at line 6 - use replace_symbol"]


def test_inserting_duplicate_method_is_rejected(tmp_path):
    _, _, errors = _run(
        tmp_path,
        [{"op": "insert_after_symbol", "file": "pkg/mod.py", "symbol": "Shop.close", "code": "def open(self):\n    return 0\n"}],
    )
    assert "'Shop.open' already defined at line" in errors[0] and "use replace_symbol" in errors[0]


def test_existing_duplicates_and_property_setters_are_tolerated(tmp_path):
    files = {"m.py": "def f():\n    pass\n\n\ndef f():\n    pass\n"}
    _, diff, errors = _run(tmp_path, [{"op": "append", "file": "m.py", "code": "def g():\n    pass\n"}], files=files)
    assert errors == [] and diff
    setter = "@name.setter\ndef name(self, value):\n    self._n = value\n"
    _, diff, errors = _run(
        tmp_path / "b",
        [{"op": "insert_after_symbol", "file": "pkg/mod.py", "symbol": "Shop.name", "code": setter}],
    )
    assert errors == [] and diff


def test_result_must_parse(tmp_path):
    _, diff, errors = _run(
        tmp_path, [{"op": "search_replace", "file": "pkg/mod.py", "old": "    return y\n", "new": "    return (y\n"}]
    )
    assert diff == "" and "result does not parse: line" in errors[0]


def test_errors_reported_for_every_failing_edit(tmp_path):
    _, diff, errors = _run(
        tmp_path,
        [
            {"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "nope"},
            {"op": "append", "file": "pkg/mod.py", "code": "def ok():\n    pass\n"},
            {"op": "append", "file": "pkg/mod.py", "code": "def gamma():\n    pass\n"},
        ],
    )
    assert diff == ""
    assert len(errors) == 2
    assert errors[0].startswith("edit 1 (delete_symbol pkg/mod.py:nope): symbol 'nope' not found")
    assert errors[1].startswith("edit 3 (append pkg/mod.py): 'gamma' already defined")


def test_no_change_is_an_error(tmp_path):
    _, diff, errors = _run(
        tmp_path, [{"op": "search_replace", "file": "pkg/mod.py", "old": "import os\n", "new": "import os\n"}]
    )
    assert diff == "" and errors == ["edits made no changes"]


# --------------------------------------------------------------------------
# Diff fidelity
# --------------------------------------------------------------------------


def test_crlf_file_keeps_line_endings(tmp_path):
    crlf = MODULE.replace("\n", "\r\n").encode()
    root, diff, errors = _run(
        tmp_path,
        [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "gamma", "code": "def gamma():\n    return 3\n"}],
        files={"pkg/mod.py": crlf},
    )
    assert errors == []
    data = (_apply_via_gates(root, diff, tmp_path) / "pkg/mod.py").read_bytes()
    assert b"def gamma():\r\n    return 3\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    _git_apply_check(root, diff, tmp_path)


def test_file_without_trailing_newline(tmp_path):
    files = {"m.py": "def f():\n    return 1"}
    root, diff, errors = _run(
        tmp_path, [{"op": "search_replace", "file": "m.py", "old": "return 1", "new": "return 2"}], files=files
    )
    assert errors == [] and "\\ No newline at end of file" in diff
    assert (_apply_via_gates(root, diff, tmp_path) / "m.py").read_text() == "def f():\n    return 2"
    _git_apply_check(root, diff, tmp_path)

    root, diff, errors = _run(
        tmp_path / "b", [{"op": "append", "file": "m.py", "code": "def g():\n    pass\n"}], files=files
    )
    assert errors == []
    assert (_apply_via_gates(root, diff, tmp_path / "b") / "m.py").read_text().endswith("    return 1\n\n\ndef g():\n    pass\n")
    _git_apply_check(root, diff, tmp_path / "b")


def test_large_module_function_body_change(tmp_path):
    # S-002 / F-006: change the inside of one function in a ~2,500-line module.
    chunks = [f"def helper_{i}(value):\n    total = value * {i}\n    return total\n" for i in range(800)]
    target = "def evaluate_line_anomaly(line):\n    \"\"\"Score a line.\"\"\"\n    score = len(line)\n    return score\n"
    source = "\n\n".join(chunks[:400] + [target] + chunks[400:])
    assert source.count("\n") > 2000
    code = "def evaluate_line_anomaly(line):\n    \"\"\"Score a line.\"\"\"\n    score = len(line.strip())\n    return score * 2\n"
    root, diff, errors = _run(
        tmp_path,
        [{"op": "replace_symbol", "file": "foundry/shop_foreman.py", "symbol": "evaluate_line_anomaly", "code": code}],
        files={"foundry/shop_foreman.py": source},
        allowed=["foundry/shop_foreman.py"],
    )
    assert errors == []
    assert diff.count("@@ -") == 1
    text = (_apply_via_gates(root, diff, tmp_path) / "foundry/shop_foreman.py").read_text()
    assert "return score * 2" in text and text.count("def evaluate_line_anomaly") == 1
    _git_apply_check(root, diff, tmp_path)


def test_write_true_updates_workspace(tmp_path):
    root = _ws(tmp_path, {"pkg/mod.py": MODULE})
    diff, errors = apply_edits(
        root,
        [{"op": "create_file", "file": "pkg/sub/new.py", "code": "A = 1\n"},
         {"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "gamma"}],
        ["pkg/"],
        write=True,
    )
    assert errors == [] and diff
    assert (root / "pkg/sub/new.py").read_text() == "A = 1\n"
    assert "gamma" not in (root / "pkg/mod.py").read_text()

    before = (root / "pkg/mod.py").read_text()
    _, errors = apply_edits(root, [{"op": "delete_symbol", "file": "pkg/mod.py", "symbol": "nope"}], None, write=True)
    assert errors and (root / "pkg/mod.py").read_text() == before


def test_multiple_files_in_one_diff(tmp_path):
    root, diff, errors = _run(
        tmp_path,
        [
            {"op": "append", "file": "a.py", "code": "B = 2\n"},
            {"op": "create_file", "file": "b.py", "code": "C = 3\n"},
        ],
        files={"a.py": "A = 1\n"},
    )
    assert errors == [] and diff.count("diff --git") == 2
    copy = _apply_via_gates(root, diff, tmp_path)
    assert (copy / "a.py").read_text() == "A = 1\n\n\nB = 2\n"
    _git_apply_check(root, diff, tmp_path)
