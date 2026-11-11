from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry import order_compiler as oc
from foundry.code_library import Library, build_library

A_PY = """def alpha(x):
    return x + 1


class Box:
    def open(self):
        return alpha(1)

    def close(self):
        return 2
"""
B_PY = """def beta(y):
    return y * 2


def alpha(z):
    return z
"""
APP_PY = """from pkg.a import Box
from pkg.b import beta


def run():
    return beta(3) + Box().open()
"""
TEST_B = """from pkg.b import beta


def test_beta():
    assert beta(2) == 4
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "a.py").write_text(A_PY, encoding="utf-8")
    (root / "pkg" / "b.py").write_text(B_PY, encoding="utf-8")
    (root / "app.py").write_text(APP_PY, encoding="utf-8")
    (root / "tests" / "test_b.py").write_text(TEST_B, encoding="utf-8")
    return root


def make_lib(root: Path, tmp_path: Path) -> Library:
    db = tmp_path / "lib.db"
    build_library(root, db)
    return Library(db)


def sub(description: str, files: list[str], **extra: Any) -> dict[str, Any]:
    return {"title": "t", "description": description, "files": files, **extra}


def compile_subs(repo: Path, tmp_path: Path, subs: list[dict[str, Any]]) -> dict[str, Any]:
    return oc.compile_batch({"title": "b", "subtasks": subs}, make_lib(repo, tmp_path))


def test_extract_names() -> None:
    text = "Use `beta()`, `Box.open`, `ab`, `pkg/a.py`, `index.html`, `beta` and `x y`."
    assert oc.extract_names(text) == ["beta", "Box.open"]


def test_resolved_ambiguous_unresolved(repo: Path, tmp_path: Path) -> None:
    rep = compile_subs(repo, tmp_path, [sub("Call `beta`, `alpha`, `Box.open` and `missing_fn`.", ["app.py"])])
    status = {n["name"]: n for n in rep["subtasks"][0]["names"]}
    assert status["beta"]["status"] == "resolved"
    assert status["beta"]["calls"] == ["pkg/b.py::beta"]
    assert status["Box.open"]["calls"] == ["pkg/a.py::Box.open"]
    assert status["alpha"]["status"] == "ambiguous"
    assert sorted(status["alpha"]["calls"]) == ["pkg/a.py::alpha", "pkg/b.py::alpha"]
    assert status["missing_fn"]["status"] == "unresolved"
    findings = "\n".join(rep["subtasks"][0]["findings"])
    assert "`missing_fn` not found; fine if this piece creates it" in findings


def test_fact_sheet_and_read_files(repo: Path, tmp_path: Path) -> None:
    rep = compile_subs(repo, tmp_path, [sub("Change `beta`.", ["pkg/a.py"])])
    piece = rep["subtasks"][0]
    assert "pkg/b.py::beta  pkg/b.py:1-2  def beta(y)" in piece["facts"]
    assert "app.py:6" in piece["facts"]
    assert "tests/test_b.py" in piece["facts"]
    assert piece["suggestedReadFiles"] == ["pkg/b.py"]


def test_read_files_skips_listed(repo: Path, tmp_path: Path) -> None:
    rep = compile_subs(repo, tmp_path, [sub("Change `beta`.", ["pkg/a.py"], readFiles=["pkg/b.py"])])
    assert rep["subtasks"][0]["suggestedReadFiles"] == []


def test_fact_sheet_cap(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    names = [f"function_number_{i:03d}" for i in range(40)]
    (root / "many.py").write_text("".join(f"def {n}():\n    return 1\n\n\n" for n in names), encoding="utf-8")
    (root / "use.py").write_text("".join(f"{n}()\n" for n in names) * 4, encoding="utf-8")
    text = " ".join(f"`{n}`" for n in names)
    rep = compile_subs(root, tmp_path, [sub(text, ["other.py"])])
    facts = rep["subtasks"][0]["facts"]
    assert len(facts) <= oc.FACT_CAP
    assert "cut at 3000" in facts
    assert len(rep["subtasks"][0]["suggestedReadFiles"]) <= oc.MAX_READ_FILES


def test_conflict_nested_entries(repo: Path, tmp_path: Path) -> None:
    subs = [sub("Edit `Box.open`.", ["pkg/a.py"]), sub("Edit `Box`.", ["pkg/a.py"])]
    rep = compile_subs(repo, tmp_path, subs)
    assert any("#1 and #2 both change" in c for c in rep["conflicts"])
    assert not any(" share " in c for c in rep["conflicts"])


def test_conflict_weak_without_line_overlap(repo: Path, tmp_path: Path) -> None:
    subs = [sub("Edit `Box.open`.", ["pkg/a.py"]), sub("Edit `Box.close`.", ["pkg/a.py"])]
    rep = compile_subs(repo, tmp_path, subs)
    assert rep["conflicts"] == [
        "#1 and #2 share pkg/a.py; keep their edits apart and tell each where its imports go"
    ]


def test_conflict_skipped_with_depends_on(repo: Path, tmp_path: Path) -> None:
    subs = [sub("Edit `Box.open`.", ["pkg/a.py"]), sub("Edit `Box`.", ["pkg/a.py"], dependsOn=["1"])]
    assert compile_subs(repo, tmp_path, subs)["conflicts"] == []


def test_no_conflict_for_disjoint_files(repo: Path, tmp_path: Path) -> None:
    subs = [sub("Edit `Box`.", ["pkg/a.py"]), sub("Edit `beta`.", ["pkg/b.py"])]
    assert compile_subs(repo, tmp_path, subs)["conflicts"] == []


def test_size_flag(tmp_path: Path) -> None:
    root = tmp_path / "huge"
    root.mkdir()
    body = "".join(f"    x{i} = {i}\n" for i in range(700))
    (root / "huge.py").write_text(f"def giant():\n{body}    return 1\n", encoding="utf-8")
    rep = compile_subs(root, tmp_path, [sub("Rewrite `giant`.", ["huge.py"])])
    assert any(
        "#1 touches ~702 lines of existing code; consider a codemod or splitting" in f
        for f in rep["subtasks"][0]["findings"]
    )


def test_callers_outside_on_rename(repo: Path, tmp_path: Path) -> None:
    rep = compile_subs(repo, tmp_path, [sub("Rename `beta` to gamma. Keep the rest.", ["pkg/b.py"])])
    found = [f for f in rep["subtasks"][0]["findings"] if "changes pkg/b.py::beta" in f]
    assert len(found) == 1
    assert "callers outside its files:" in found[0]
    assert "app.py:6" in found[0]


def test_no_callers_finding_without_change_word(repo: Path, tmp_path: Path) -> None:
    rep = compile_subs(repo, tmp_path, [sub("Use `beta` in a new helper.", ["pkg/b.py"])])
    assert not any("callers outside" in f for f in rep["subtasks"][0]["findings"])


def test_write_leaves_input_untouched(repo: Path, tmp_path: Path) -> None:
    batch: dict[str, Any] = {"title": "b", "subtasks": [sub("Change `beta`.", ["pkg/a.py"])]}
    src = tmp_path / "in.json"
    src.write_text(json.dumps(batch), encoding="utf-8")
    before = src.read_text(encoding="utf-8")
    out = tmp_path / "out.json"
    rep = oc.compile_batch(batch, make_lib(repo, tmp_path))
    oc.write_batch(batch, rep, out)
    assert src.read_text(encoding="utf-8") == before
    assert batch["subtasks"][0]["description"] == "Change `beta`."
    written = json.loads(out.read_text(encoding="utf-8"))
    desc = written["subtasks"][0]["description"]
    assert desc.startswith("Change `beta`.")
    assert "## Facts (from the code library, " in desc
    assert "pkg/b.py::beta" in desc
    assert written["subtasks"][0]["readFiles"] == ["pkg/b.py"]


def test_cli(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    batch = {"title": "b", "subtasks": [sub("Change `beta` and `nothing_here`.", ["pkg/a.py"])]}
    src = tmp_path / "batch.json"
    src.write_text(json.dumps(batch), encoding="utf-8")
    out = tmp_path / "out.json"
    assert oc.main([str(src), "--repo", str(repo), "--write", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["subtasks"][0]["names"][0]["status"] == "resolved"
    assert out.exists()
    assert oc.main([str(src), "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "`nothing_here` not found; fine if this piece creates it" in text
