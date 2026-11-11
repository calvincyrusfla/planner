"""The codemod tools: file and symbol moves and deletions, alone and through the MCP server."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from foundry import codemod
from foundry.code_library import Library, build_library, open_library
from foundry.codemod import CodemodError
from foundry.library_mcp import Workspace, serve

A_PY = '''"""Doc."""
import os
from pathlib import Path

LIMIT = 3


def deco(f):
    return f


def helper(x):
    return x + LIMIT


@deco
def moved(p):
    return Path(p) / str(helper(1))


def stays():
    return moved("a")
'''
OTHER_PY = "from pkg.a import moved\n\nmoved(1)\n"
CLASS_PY = "class Box:\n    def m(self):\n        return 1\n\n\ndef tail():\n    return 2\n"
JS = "export function one() {\n  return 1;\n}\n\nexport function two() {\n  return one();\n}\n"


def _repo(tmp_path: Path, **extra: str) -> tuple[Path, Library]:
    root = tmp_path / "repo"
    files = {"pkg/a.py": A_PY, "pkg/other.py": OTHER_PY, "pkg/box.py": CLASS_PY, "js/x.js": JS}
    files.update(extra)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return root, _library(root, tmp_path)


def _library(root: Path, tmp_path: Path) -> Library:
    build_library(root, tmp_path / "lib.db")
    return open_library(tmp_path / "lib.db")


def _text(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


# ----------------------------------------------------------------- files


def test_delete_file(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path)
    report = codemod.delete_file(root, "pkg/other.py")
    assert not (root / "pkg/other.py").exists()
    assert "Deleted pkg/other.py" in report and "Still to do" in report


def test_move_file_creates_folders_and_keeps_content(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path)
    report = codemod.move_file(root, "pkg/other.py", "deep/er/other.py")
    assert not (root / "pkg/other.py").exists()
    assert _text(root, "deep/er/other.py") == OTHER_PY
    assert "pkg/other.py to deep/er/other.py" in report


def test_file_refusals(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    for bad in ("../outside.txt", "pkg/../../outside.txt", str(outside), "/etc/passwd", "", "."):
        with pytest.raises(CodemodError):
            codemod.delete_file(root, bad)
    assert outside.read_text(encoding="utf-8") == "keep"
    with pytest.raises(CodemodError, match="outside"):
        codemod.move_file(root, "pkg/a.py", "../moved.py")
    with pytest.raises(CodemodError, match="outside"):
        codemod.move_file(root, "../outside.txt", "x.txt")
    with pytest.raises(CodemodError, match="overwrite"):
        codemod.move_file(root, "pkg/a.py", "pkg/other.py")
    assert _text(root, "pkg/other.py") == OTHER_PY
    with pytest.raises(CodemodError, match="No such file"):
        codemod.delete_file(root, "nope.py")
    with pytest.raises(CodemodError, match="Not a file"):
        codemod.delete_file(root, "pkg")


# ----------------------------------------------------------------- delete_symbol


def test_delete_symbol_takes_decorators_and_trailing_blanks(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.delete_symbol(root, lib, "pkg/a.py::moved")
    after = _text(root, "pkg/a.py")
    assert "@deco\ndef moved" not in after and "def moved" not in after
    assert after.endswith("    return x + LIMIT\n\n\ndef stays():\n    return moved(\"a\")\n")
    assert "Deleted pkg/a.py::moved" in report
    assert "pkg/a.py:17  " in report  # shifted for the cut
    assert "pkg/other.py:3" in report


def test_delete_last_symbol_leaves_no_trailing_blank_lines(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    codemod.delete_symbol(root, lib, "pkg/a.py::stays")
    assert _text(root, "pkg/a.py").endswith("str(helper(1))\n")


def test_delete_symbol_without_callers_says_so(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.delete_symbol(root, lib, "pkg/a.py::stays")
    assert "No other callers" in report


def test_delete_symbol_keeps_crlf(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, **{"pkg/crlf.py": "def a():\r\n    pass\r\n\r\n\r\ndef b():\r\n    pass\r\n"})
    lib = _library(root, tmp_path)
    codemod.delete_symbol(root, lib, "pkg/crlf.py::a")
    assert _text(root, "pkg/crlf.py") == "def b():\r\n    pass\r\n"


def test_delete_symbol_js_removes_its_lines(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.delete_symbol(root, lib, "js/x.js::one")
    after = _text(root, "js/x.js")
    assert "function one" not in after and "function two" in after
    assert "js/x.js" in report


def test_delete_symbol_refuses_what_would_break_python(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    with pytest.raises(CodemodError, match="invalid Python"):
        codemod.delete_symbol(root, lib, "pkg/box.py::Box.m")
    assert _text(root, "pkg/box.py") == CLASS_PY
    with pytest.raises(CodemodError, match="Unknown call number"):
        codemod.delete_symbol(root, lib, "pkg/a.py::missing")
    with pytest.raises(CodemodError, match="Not a call number"):
        codemod.delete_symbol(root, lib, "pkg/a.py")
    with pytest.raises(CodemodError, match="outside"):
        codemod.delete_symbol(root, lib, "../x.py::f")


# ----------------------------------------------------------------- move_symbol


def test_move_symbol_to_a_new_file_reports_used_names(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.move_symbol(root, lib, "pkg/a.py::moved", "pkg/b.py")
    assert _text(root, "pkg/b.py") == "@deco\ndef moved(p):\n    return Path(p) / str(helper(1))\n"
    after = _text(root, "pkg/a.py")
    assert "def moved" not in after and "@deco" not in after
    assert after.endswith("    return x + LIMIT\n\n\ndef stays():\n    return moved(\"a\")\n")
    used = [line.split()[0] for line in report.splitlines() if line.startswith("  ")]
    assert used == ["Path", "deco", "helper"]
    assert "imported (line 3) in pkg/a.py" in report
    assert "pkg/other.py:3" in report
    assert "from pkg.b import" not in after


def test_move_symbol_appends_with_one_blank_line(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path, **{"pkg/b.py": "X = 1\n\n\n"})
    codemod.move_symbol(root, lib, "pkg/a.py::stays", "pkg/b.py")
    assert _text(root, "pkg/b.py") == 'X = 1\n\ndef stays():\n    return moved("a")\n'


def test_move_symbol_with_reexport_imports_the_name_back(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.move_symbol(root, lib, "pkg/a.py::moved", "pkg/sub/b.py", reexport=True)
    after = _text(root, "pkg/a.py")
    assert after.splitlines()[:4] == [
        '"""Doc."""',
        "import os",
        "from pathlib import Path",
        "from pkg.sub.b import moved",
    ]
    assert (root / "pkg/sub/b.py").is_file()
    assert "added `from pkg.sub.b import moved`" in report
    compile(after, "a.py", "exec")


def test_move_symbol_reexport_without_imports_goes_after_the_docstring(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path, **{"pkg/c.py": '"""Doc."""\n\n\ndef f():\n    return 1\n'})
    codemod.move_symbol(root, lib, "pkg/c.py::f", "pkg/d.py", reexport=True)
    assert _text(root, "pkg/c.py") == '"""Doc."""\n\nfrom pkg.d import f\n'


def test_move_class_and_nothing_used(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path)
    report = codemod.move_symbol(root, lib, "pkg/box.py::Box", "pkg/new.py")
    assert _text(root, "pkg/new.py") == "class Box:\n    def m(self):\n        return 1\n"
    assert _text(root, "pkg/box.py") == "def tail():\n    return 2\n"
    assert "uses no names" in report


def test_move_symbol_refusals(tmp_path: Path) -> None:
    root, lib = _repo(tmp_path, **{"pkg/b.py": "def moved():\n    pass\n"})
    before = {rel: _text(root, rel) for rel in ("pkg/a.py", "pkg/b.py", "pkg/box.py", "js/x.js")}
    with pytest.raises(CodemodError, match="method"):
        codemod.move_symbol(root, lib, "pkg/box.py::Box.m", "pkg/new.py")
    with pytest.raises(CodemodError, match="not supported"):
        codemod.move_symbol(root, lib, "js/x.js::one", "js/y.js")
    with pytest.raises(CodemodError, match="not supported"):
        codemod.move_symbol(root, lib, "pkg/a.py::moved", "pkg/notes.txt")
    with pytest.raises(CodemodError, match="outside"):
        codemod.move_symbol(root, lib, "pkg/a.py::moved", "../escape.py")
    with pytest.raises(CodemodError, match="already defines"):
        codemod.move_symbol(root, lib, "pkg/a.py::moved", "pkg/b.py")
    with pytest.raises(CodemodError, match="same file"):
        codemod.move_symbol(root, lib, "pkg/a.py::moved", "pkg/a.py")
    with pytest.raises(CodemodError, match="Unknown call number"):
        codemod.move_symbol(root, lib, "pkg/a.py::nope", "pkg/new.py")
    assert {rel: _text(root, rel) for rel in before} == before
    assert not (root / "pkg/new.py").exists() and not (tmp_path / "escape.py").exists()


# ----------------------------------------------------------------- MCP end to end


def _serve(workspace: Workspace | None, backend: Any, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    serve(backend, stdin, out, workspace)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _tool(n: int, name: str, **arguments: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _workspace(tmp_path: Path) -> tuple[Path, Workspace]:
    root, lib = _repo(tmp_path)

    def rebuild() -> Library:
        return _library(root, tmp_path)

    return root, Workspace(str(root), lib, rebuild)


def test_tools_list_has_the_edit_tools_only_on_a_workspace(tmp_path: Path) -> None:
    root, ws = _workspace(tmp_path)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    (with_ws,) = _serve(ws, ws.backend, request)
    (without,) = _serve(None, ws.backend, request)
    names = [t["name"] for t in with_ws["result"]["tools"]]
    assert names == ["find", "show", "callers", "tests_for", "outline",
                     "delete_file", "move_file", "delete_symbol", "move_symbol"]
    assert [t["name"] for t in without["result"]["tools"]] == names[:5]
    for tool in with_ws["result"]["tools"][5:]:
        assert "EDITS the workspace" in tool["description"]
    (refused,) = _serve(None, ws.backend, _tool(2, "delete_file", path="pkg/a.py"))
    assert refused["error"]["code"] == -32602
    assert (root / "pkg/a.py").exists()


def test_move_symbol_over_mcp_rebuilds_the_library(tmp_path: Path) -> None:
    root, ws = _workspace(tmp_path)
    found = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "find", "arguments": {"name": "moved"}}}
    moved, after = _serve(
        ws, ws.backend, _tool(1, "move_symbol", call="pkg/a.py::moved", to_path="pkg/b.py", reexport=True), found
    )
    assert moved["result"]["isError"] is False
    assert "pkg/b.py" in moved["result"]["content"][0]["text"]
    lines = after["result"]["content"][0]["text"].splitlines()
    assert any(line.startswith("pkg/b.py::moved") for line in lines)
    assert not any(line.startswith("pkg/a.py::moved") for line in lines)


def test_delete_and_move_file_over_mcp(tmp_path: Path) -> None:
    root, ws = _workspace(tmp_path)
    moved, deleted, outline = _serve(
        ws,
        ws.backend,
        _tool(1, "move_file", src="pkg/box.py", dst="pkg/new/box.py"),
        _tool(2, "delete_file", path="pkg/other.py"),
        _tool(3, "outline", path="pkg/new/box.py"),
    )
    assert moved["result"]["isError"] is False and deleted["result"]["isError"] is False
    assert not (root / "pkg/other.py").exists() and (root / "pkg/new/box.py").is_file()
    assert "pkg/new/box.py::Box" in outline["result"]["content"][0]["text"]


def test_delete_symbol_over_mcp(tmp_path: Path) -> None:
    root, ws = _workspace(tmp_path)
    deleted, outline = _serve(
        ws, ws.backend, _tool(1, "delete_symbol", call="pkg/a.py::helper"), _tool(2, "outline", path="pkg/a.py")
    )
    assert deleted["result"]["isError"] is False
    assert "def helper" not in _text(root, "pkg/a.py")
    assert "helper" not in outline["result"]["content"][0]["text"]


def test_mcp_errors_are_results_and_the_server_keeps_going(tmp_path: Path) -> None:
    root, ws = _workspace(tmp_path)
    replies = _serve(
        ws,
        ws.backend,
        _tool(1, "delete_file", path="../outside.txt"),
        _tool(2, "move_file", src="pkg/a.py", dst="pkg/other.py"),
        _tool(3, "move_symbol", call="pkg/box.py::Box.m", to_path="pkg/z.py"),
        _tool(4, "move_symbol", call="js/x.js::one", to_path="js/y.js"),
        _tool(5, "delete_symbol", call="pkg/a.py::missing"),
        _tool(6, "move_symbol", call="pkg/a.py::moved", to_path="pkg/z.py", reexport="yes"),
        _tool(7, "delete_file"),
        _tool(8, "delete_file", path="pkg/other.py"),
    )
    assert [r["result"]["isError"] for r in replies] == [True] * 7 + [False]
    assert (root / "pkg/a.py").exists() and not (root / "pkg/other.py").exists()
    assert "must be a boolean" in replies[5]["result"]["content"][0]["text"]
    assert "Nothing changed" in replies[0]["result"]["content"][0]["text"]
