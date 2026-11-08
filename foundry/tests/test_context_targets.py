"""Tests for edit targets in foundry.context_engine: full bodies, inventory, levels, extra symbols.

Reproduces the S-002 failure shape: a ~2,000-line module where the subtask
must change the inside of one function. The slice must show that function's
body verbatim with original line numbers (plus context), list every existing
symbol, and keep the rest as clearly marked outlines.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from foundry.context_engine import (
    DEFAULT_FULL_LINES,
    FULL_LINES_ENV,
    OUTLINE_MARKER,
    ContextEngine,
)
from foundry.schemas import MAX_SNIPPET_LINES, ContextSlice, SubtaskAllocation

N_FUNCS = 150


def _big_module() -> str:
    """~2,000 lines: many 12-line helpers, a class with methods, the target function in the middle."""
    parts = [
        '"""Shop floor helpers."""',
        "",
        "from __future__ import annotations",
        "",
        "from pkg.models import Reading, unused_name",
        "",
        "LIMIT = 3",
        "",
        "",
    ]
    for i in range(N_FUNCS // 2):
        parts += [
            f"def helper_{i}(value: int) -> int:",
            f'    """Helper {i}."""',
            f"    marker = 'HBODY_{i}'",
            "    total = 0",
            "    for k in range(value):",
            "        total += k",
            "        if total > 100:",
            "            break",
            "    return total",
            "",
            "",
        ]
    parts += [
        "class Ledger:",
        '    """Keeps a running summary."""',
        "",
        "    rows: list[int]",
        "",
        "    def summary(self) -> str:",
        '        """One-line summary."""',
        "        text = 'LEDGER_SUMMARY_BODY'",
        "        return text",
        "",
        "    def _claimable(self, row: int) -> bool:",
        "        return row > 0  # CLAIMABLE_BODY",
        "",
        "",
        "def evaluate_line_anomaly(reading: Reading, *, threshold: float = 2.0) -> bool:",
        '    """Decide whether a reading is anomalous.',
        "",
        "    The docstring goes on for a bit.",
        '    """',
        "    score = abs(reading.value - reading.mean)",
        "    # TARGET_BODY_LINE",
        "    if score > threshold * LIMIT:",
        "        return True",
        "    return False",
        "",
        "",
    ]
    for i in range(N_FUNCS // 2, N_FUNCS):
        parts += [
            f"def helper_{i}(value: int) -> int:",
            f'    """Helper {i}."""',
            f"    marker = 'HBODY_{i}'",
            "    total = 0",
            "    for k in range(value):",
            "        total += k",
            "        if total > 100:",
            "            break",
            "    return total",
            "",
            "",
        ]
    parts += ["def tail_function() -> None:", "    return None  # TAIL_BODY", ""]
    return "\n".join(parts)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "floor.py").write_text(_big_module(), encoding="utf-8")
    (tmp_path / "pkg" / "models.py").write_text(
        "class Reading:\n    \"\"\"A sensor reading.\"\"\"\n\n    value: float\n    mean: float\n\n"
        "    def delta(self) -> float:\n        return self.value - self.mean  # MODELS_BODY\n\n\n"
        "def unused_name() -> None:\n    return None\n\n\ndef not_imported() -> None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "small.py").write_text(
        "def tiny(a: int) -> int:\n    return a + 1  # SMALL_BODY\n", encoding="utf-8"
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.ts").write_text(
        "export function renderBoard(rows: string[]): string {\n"
        "  return rows.join('TS_RENDER_BODY');\n"
        "}\n"
        "export function otherThing(): void {\n"
        "  console.log('TS_OTHER_BODY');\n"
        "}\n" + "".join(f"export function pad{i}(): number {{\n  return {i};\n}}\n" for i in range(10)),
        encoding="utf-8",
    )
    return tmp_path


def _text(sl: ContextSlice) -> str:
    return "\n".join(s.text for s in sl.snippets)


def _source() -> list[str]:
    return _big_module().splitlines()


def _line_of(needle: str) -> int:
    return next(i for i, ln in enumerate(_source(), 1) if needle in ln)


def _sub(title: str, criteria: list[str] | None = None, files: list[str] | None = None) -> SubtaskAllocation:
    return SubtaskAllocation(
        subtask_id="S-002.1",
        title=title,
        acceptance_criteria=criteria or [],
        allowed_files=files or ["pkg/floor.py"],
    )


# --------------------------------------------------------------------------- full targets


def test_module_is_big_enough() -> None:
    assert len(_source()) >= 1600


def test_named_function_shown_in_full_with_line_numbers(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(
        _sub("Tighten evaluate_line_anomaly", ["`evaluate_line_anomaly` uses a stricter threshold"]),
        "Tighten the anomaly check.",
    )
    text = _text(sl)
    assert "TARGET_BODY_LINE" in text
    src = _source()
    start = _line_of("def evaluate_line_anomaly")
    end = _line_of("    return False")
    # Every line of the function, plus 3 lines of context each side, appears verbatim with its number.
    numbered: dict[int, set[str]] = {}
    for row in text.splitlines():
        m = re.match(r"^\s*(\d+)\| (.*)$", row)
        if m:
            numbered.setdefault(int(m.group(1)), set()).add(m.group(2))
    for n in range(start - 3, end + 4):
        assert src[n - 1] in numbered.get(n, set()), f"line {n} missing from the full view"
    stats = engine.last_stats
    assert stats is not None
    assert any(t["symbol"] == "evaluate_line_anomaly" and t["reason"] == "named" for t in stats.targets)
    assert 0 < stats.full_lines_shown <= stats.full_budget == DEFAULT_FULL_LINES
    # Other bodies stay hidden; the big file is never shown whole.
    assert "HBODY_3" not in text and "TAIL_BODY" not in text
    assert all(s.line_count <= MAX_SNIPPET_LINES for s in sl.snippets)
    assert sl.snippet_line_count < len(src) / 2


def test_full_snippet_ranges_match_their_numbers(repo: Path) -> None:
    sl = ContextEngine(repo).build_slice(_sub("Fix evaluate_line_anomaly"), "Fix it.")
    src = _source()
    full = [s for s in sl.snippets if "TARGET_BODY_LINE" in s.text]
    assert len(full) == 1
    snip = full[0]
    rows = [re.match(r"^\s*(\d+)\| (.*)$", r) for r in snip.text.splitlines()]
    assert all(rows)
    nums = [int(m.group(1)) for m in rows if m]
    assert nums == list(range(snip.start_line, snip.end_line + 1))
    for m in rows:
        assert m is not None and src[int(m.group(1)) - 1] == m.group(2)


def test_class_method_target_and_bare_method_names(repo: Path) -> None:
    engine = ContextEngine(repo)
    engine.build_slice(_sub("Change Ledger.summary and _claimable"), "Change them.")
    stats = engine.last_stats
    assert stats is not None
    names = {t["symbol"] for t in stats.targets}
    assert {"Ledger.summary", "Ledger._claimable"} <= names
    sl = engine.build_slice(_sub("Change Ledger.summary"), "Change it.")
    text = _text(sl)
    assert "LEDGER_SUMMARY_BODY" in text
    # The method is not repeated as an outline: the header appears once.
    assert text.count("def summary(self) -> str:") == 1


def test_outlines_marked_and_inventory_lists_everything(repo: Path) -> None:
    engine = ContextEngine(repo, full_lines=25)  # room for the target, not for helper_7
    sl = engine.build_slice(_sub("Tighten evaluate_line_anomaly", ["helper_7 stays the same"]), "Tighten it.")
    text = _text(sl)
    assert OUTLINE_MARKER in text  # helper_7 outline has its body elided and marked
    assert "HBODY_7" not in text
    inv = [s for s in sl.snippets if s.text.startswith("# inventory of pkg/floor.py")]
    assert inv, "inventory snippet missing"
    inv_text = "\n".join(s.text for s in inv)
    start = _line_of("def evaluate_line_anomaly")
    assert f"def evaluate_line_anomaly {start}-" in inv_text
    assert "class Ledger" in inv_text and "summary" in inv_text and "_claimable" in inv_text
    for i in (0, N_FUNCS - 1):
        assert f"helper_{i} " in inv_text
    assert "const LIMIT 7" in inv_text and "tail_function" in inv_text
    assert all(s.line_count <= MAX_SNIPPET_LINES for s in inv)
    assert engine.last_stats is not None and engine.last_stats.inventory_files == ["pkg/floor.py"]
    # Inventory comes first for its file.
    first_floor = next(s for s in sl.snippets if s.path == "pkg/floor.py")
    assert first_floor.text.startswith("# inventory of pkg/floor.py")


def test_inventory_can_be_disabled(repo: Path) -> None:
    sl = ContextEngine(repo, inventory=False).build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.")
    assert "# inventory of" not in _text(sl)


def test_budget_caps_full_lines(repo: Path) -> None:
    engine = ContextEngine(repo, full_lines=30)
    criteria = [f"helper_{i} gains a guard" for i in range(20)]
    engine.build_slice(_sub("Guard helpers", criteria), "Guard the helpers.")
    stats = engine.last_stats
    assert stats is not None
    assert stats.full_budget == 30
    assert stats.full_lines_shown <= 30 + 2 * 3  # gap filling may add a couple of lines
    assert 1 <= len(stats.targets) < 20


def test_env_budget_override(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FULL_LINES_ENV, "50")
    engine = ContextEngine(repo)
    assert engine.full_budget(1) == 50 and engine.full_budget(2) == 100 and engine.full_budget(3) == 150
    monkeypatch.setenv(FULL_LINES_ENV, "not-a-number")
    assert engine.full_budget(1) == DEFAULT_FULL_LINES


def test_full_lines_zero_is_outline_only(repo: Path) -> None:
    sl = ContextEngine(repo, full_lines=0).build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.")
    text = _text(sl)
    assert "TARGET_BODY_LINE" not in text
    assert "def evaluate_line_anomaly(" in text


def test_small_file_shown_whole(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(_sub("Adjust something", files=["pkg/small.py"]), "Adjust.")
    assert "SMALL_BODY" in _text(sl)
    assert engine.last_stats is not None
    assert any(t["reason"] == "file" for t in engine.last_stats.targets)


def test_typescript_named_function_in_full(repo: Path) -> None:
    sl = ContextEngine(repo, full_lines=8).build_slice(_sub("Fix renderBoard", files=["web/app.ts"]), "Fix.")
    text = _text(sl)
    assert "TS_RENDER_BODY" in text
    assert "return 9;" not in text  # far-away bodies stay hidden
    outline = _text(ContextEngine(repo, full_lines=0).build_slice(_sub("Fix renderBoard", files=["web/app.ts"]), "Fix."))
    assert "TS_RENDER_BODY" not in outline
    assert f"renderBoard(rows: string[]): string {{ ... }}  // {OUTLINE_MARKER}" in outline


# --------------------------------------------------------------------------- levels & extra symbols


def test_extra_symbols_always_full(repo: Path) -> None:
    engine = ContextEngine(repo, full_lines=0)
    sl = engine.build_slice(
        _sub("Something vague"), "Vague.", extra_symbols=["`tail_function`", "Ledger._claimable", "pkg/floor.py::helper_3"]
    )
    text = _text(sl)
    assert "TAIL_BODY" in text and "CLAIMABLE_BODY" in text and "HBODY_3" in text
    assert "HBODY_4" not in text
    assert engine.last_stats is not None
    assert {t["symbol"] for t in engine.last_stats.targets if t["reason"] == "extra"} == {
        "tail_function",
        "Ledger._claimable",
        "helper_3",
    }


def test_extra_symbols_unknown_are_ignored(repo: Path) -> None:
    sl = ContextEngine(repo).build_slice(_sub("Vague"), "Vague.", extra_symbols=["does_not_exist", "", "x.y.z"])
    assert sl.snippets


def test_level_two_doubles_budget_and_adds_import_outlines(repo: Path) -> None:
    engine = ContextEngine(repo, full_lines=40)
    sl1 = engine.build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.")
    s1 = engine.last_stats
    assert s1 is not None and s1.level == 1 and s1.full_budget == 40 and s1.import_outline_files == []
    assert "class Reading" not in _text(sl1)
    sl2 = engine.build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.", level=2)
    s2 = engine.last_stats
    assert s2 is not None and s2.level == 2 and s2.full_budget == 80
    assert s2.import_outline_files == ["pkg/models.py"]
    text2 = _text(sl2)
    assert "class Reading:" in text2 and "def delta(self) -> float:" in text2
    assert "MODELS_BODY" not in text2 and "not_imported" not in text2
    engine.build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.", level=3)
    assert engine.last_stats is not None and engine.last_stats.full_budget == 120
    engine.build_slice(_sub("Fix evaluate_line_anomaly"), "Fix.", level=9)
    assert engine.last_stats is not None and engine.last_stats.level == 3


def test_description_names_targets(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(_sub("Small fix"), "Small fix.", description="The bug is inside tail_function.")
    assert "TAIL_BODY" in _text(sl)


def test_long_objective_still_names_targets(repo: Path) -> None:
    sl = ContextEngine(repo).build_slice(_sub("Small fix"), "One. Two. The third names tail_function.")
    assert "TAIL_BODY" in _text(sl)


def test_abuild_slice_same_signature(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = asyncio.run(
        engine.abuild_slice(_sub("Vague"), "Vague.", level=2, extra_symbols=["evaluate_line_anomaly"])
    )
    assert "TARGET_BODY_LINE" in _text(sl)
    assert engine.last_stats is not None and engine.last_stats.level == 2


def test_default_api_still_works_without_new_args(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(allowed_files=["pkg/floor.py"], objective="Touch evaluate_line_anomaly.")
    assert "TARGET_BODY_LINE" in _text(sl)
    sl2, stats = engine.build_slice_with_stats(allowed_files=["pkg/floor.py"], objective="x.")
    assert stats.level == 1 and isinstance(sl2, ContextSlice)


def test_siblings_sharing_words_stay_outlines(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(_sub("Make helper_5 faster", ["helper_5 returns early"]), "Speed up helper_5.")
    text = _text(sl)
    assert "HBODY_5'" in text
    assert "HBODY_6'" not in text and "HBODY_40'" not in text
