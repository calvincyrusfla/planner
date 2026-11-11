"""The code library: call numbers, ranges, signatures and lookups."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.code_library import Library, build_library, main, open_library

SERVICE = '''import os


def helper(x):
    return x + 1


class Service:
    """A service."""

    @staticmethod
    def run(
        a: int,
        b: int = 2,
    ) -> int:
        def inner():
            return 1

        return helper(a) + inner()

    async def stop(self):
        return None


def helper_two():
    return 2
'''

USER = '''from service import helper


def use():
    return helper(3)
'''

TOOLS = """export function checkOrder(order) {
  return order.ok;
}

export const ENV = 'x';

const make = async (a, b) => {
  return checkOrder(a);
};

class Box {
  open() {
    return 1;
  }
}
"""

TEST_PY = """from service import helper


def test_helper():
    assert helper(1) == 2
"""

TEST_MJS = """import { checkOrder } from './tools.mjs';
checkOrder({});
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "service.py").write_text(SERVICE, encoding="utf-8")
    (root / "pkg" / "user.py").write_text(USER, encoding="utf-8")
    (root / "tools.mjs").write_text(TOOLS, encoding="utf-8")
    (root / "tests" / "test_service.py").write_text(TEST_PY, encoding="utf-8")
    (root / "tools.test.mjs").write_text(TEST_MJS, encoding="utf-8")
    return root


@pytest.fixture()
def lib(repo: Path, tmp_path: Path) -> Library:
    db = tmp_path / "out" / "lib.sqlite"
    info = build_library(repo, db)
    assert info["files"] == 5
    return open_library(db)


def test_entries_ranges_and_signatures(lib: Library) -> None:
    out = {e["call"]: e for e in lib.outline("pkg/service.py")}
    assert list(out) == [
        "pkg/service.py::helper",
        "pkg/service.py::Service",
        "pkg/service.py::Service.run",
        "pkg/service.py::Service.stop",
        "pkg/service.py::helper_two",
    ]
    assert out["pkg/service.py::helper"]["kind"] == "function"
    assert out["pkg/service.py::Service"]["kind"] == "class"
    assert out["pkg/service.py::Service.run"]["kind"] == "method"
    run = out["pkg/service.py::Service.run"]
    assert (run["start"], run["end"]) == (11, 19)  # decorator included
    assert run["signature"] == "def run( a: int, b: int = 2, ) -> int:"
    assert out["pkg/service.py::Service.stop"]["signature"] == "async def stop(self):"
    assert out["pkg/service.py::Service"]["signature"] == "class Service:"
    assert not any("inner" in c for c in out)


def test_js_entries(lib: Library) -> None:
    out = {e["call"]: e for e in lib.outline("tools.mjs")}
    assert set(out) == {
        "tools.mjs::checkOrder",
        "tools.mjs::ENV",
        "tools.mjs::make",
        "tools.mjs::Box",
    }
    assert (out["tools.mjs::checkOrder"]["start"], out["tools.mjs::checkOrder"]["end"]) == (1, 3)
    assert out["tools.mjs::checkOrder"]["signature"] == "export function checkOrder(order)"
    assert (out["tools.mjs::make"]["start"], out["tools.mjs::make"]["end"]) == (7, 9)
    assert out["tools.mjs::Box"]["kind"] == "class"
    assert out["tools.mjs::Box"]["end"] == 15


def test_find_ordering(lib: Library) -> None:
    calls = [e["call"] for e in lib.find("helper")]
    assert calls == ["pkg/service.py::helper", "pkg/service.py::helper_two", "tests/test_service.py::test_helper"]
    assert [e["call"] for e in lib.find("HELP")] == calls
    assert [e["call"] for e in lib.find("order")] == ["tools.mjs::checkOrder"]
    assert lib.find("helper", limit=1)[0]["call"] == "pkg/service.py::helper"
    assert lib.find("nothing_here") == []
    assert set(lib.find("helper")[0]) == {"call", "kind", "path", "start", "end", "signature"}


def test_show_with_line_numbers_and_context(lib: Library) -> None:
    assert lib.show("pkg/service.py::helper") == "4| def helper(x):\n5|     return x + 1"
    wide = lib.show("pkg/service.py::helper", context=1)
    assert wide is not None
    assert wide.splitlines()[0] == "3| "
    assert wide.splitlines()[-1] == "6| "
    clipped = lib.show("tools.mjs::checkOrder", context=50)
    assert clipped is not None
    assert clipped.splitlines()[0].startswith("1| export function")
    assert lib.show("pkg/service.py::missing") is None


def test_callers(lib: Library) -> None:
    sites = lib.callers("pkg/service.py::helper")
    assert {(s["path"], s["line"]) for s in sites} == {("pkg/service.py", 19), ("pkg/user.py", 5), ("tests/test_service.py", 5)}
    assert any(s["text"] == "return helper(3)" for s in sites)
    assert lib.callers("pkg/service.py::helper", limit=1).__len__() == 1
    js = lib.callers("tools.mjs::checkOrder")
    assert [(s["path"], s["line"]) for s in js] == [("tools.mjs", 8), ("tools.test.mjs", 2)]
    assert lib.callers("nope::x") == []


def test_tests_for(lib: Library) -> None:
    hits = lib.tests_for("pkg/service.py::helper")
    assert [(h["path"], h["line"]) for h in hits] == [("tests/test_service.py", 1), ("tests/test_service.py", 5)]
    assert lib.tests_for("pkg/service.py::helper_two") == []
    assert {h["path"] for h in lib.tests_for("tools.mjs::checkOrder")} == {"tools.test.mjs"}
    assert lib.tests_for("pkg/service.py::helper", limit=1).__len__() == 1


def test_outline_unknown_file(lib: Library) -> None:
    assert lib.outline("nope.py") == []


def test_rebuild_replaces_db(repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "lib.sqlite"
    build_library(repo, db)
    (repo / "pkg" / "extra.py").write_text("def brand_new():\n    pass\n", encoding="utf-8")
    (repo / "pkg" / "user.py").unlink()
    info = build_library(repo, db)
    lib = open_library(db)
    assert info["entries"] > 0 and "seconds" in info
    assert lib.find("brand_new")[0]["call"] == "pkg/extra.py::brand_new"
    assert lib.find("use") == []
    assert [p.name for p in tmp_path.iterdir() if "tmp" in p.name] == []


def test_skips_huge_binary_and_skipped_dirs(repo: Path, tmp_path: Path) -> None:
    (repo / "huge.py").write_text("def huge():\n    pass\n" + "# x\n" * 600_000, encoding="utf-8")
    (repo / "bad.py").write_bytes(b"def bad():\n    return '\xff\xfe'\n")
    (repo / "nul.py").write_bytes(b"def nul():\n    pass\n\0")
    for d in ("node_modules", ".hidden", "data", "dist", "build", "__pycache__", ".venv", "venv"):
        (repo / d).mkdir()
        (repo / d / "skipme.py").write_text("def skipme():\n    pass\n", encoding="utf-8")
    db = tmp_path / "lib.sqlite"
    info = build_library(repo, db)
    assert info["files"] == 5
    lib = open_library(db)
    for name in ("huge", "bad", "nul", "skipme"):
        assert lib.find(name) == []


def test_syntax_error_file_has_no_entries(repo: Path, tmp_path: Path) -> None:
    (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    db = tmp_path / "lib.sqlite"
    build_library(repo, db)
    assert open_library(db).outline("broken.py") == []


def test_open_missing_library(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_library(tmp_path / "none.sqlite")


def test_cli(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "cli.sqlite")
    assert main(["build", "--repo", str(repo), "--db", db]) == 0
    assert "5 files" in capsys.readouterr().out
    assert main(["find", "helper", "--db", db]) == 0
    assert "pkg/service.py::helper" in capsys.readouterr().out
    assert main(["show", "pkg/service.py::helper", "--db", db]) == 0
    assert "4| def helper(x):" in capsys.readouterr().out
    assert main(["show", "pkg/service.py::nope", "--db", db]) == 1
    capsys.readouterr()
    assert main(["callers", "pkg/service.py::helper", "--db", db]) == 0
    assert "pkg/user.py:5" in capsys.readouterr().out
    assert main(["tests", "pkg/service.py::helper", "--db", db]) == 0
    assert "tests/test_service.py:5" in capsys.readouterr().out
    assert main(["outline", "tools.mjs", "--db", db]) == 0
    assert "tools.mjs::Box" in capsys.readouterr().out
