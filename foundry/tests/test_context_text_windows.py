"""Named-text windows for non-Python files in the context engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.context_engine import (
    _CONTEXT_PAD,
    _MAX_TEXT_HITS,
    _MAX_TEXT_WINDOWS,
    _TEXT_WINDOW,
    ContextEngine,
    _text_hits,
    _text_names,
)
from foundry.context_text import request_terms, spread_hits

HIT = 500
MARK = '<div id="weekPanel">'


def _page(size: int, marks: dict[int, str]) -> str:
    lines = [f"<p>filler {n}</p>" for n in range(1, size + 1)]
    for number, text in marks.items():
        lines[number - 1] = text
    return "\n".join(lines) + "\n"


def _window(line: int) -> set[int]:
    reach = _TEXT_WINDOW + _CONTEXT_PAD
    return set(range(line - reach, line + reach + 1))


def _covered(
    root: Path,
    source: str,
    query: str,
    *,
    path: str = "page.html",
    full_lines: int = 400,
    extra: tuple[str, ...] = (),
) -> set[int]:
    engine = ContextEngine(root, full_lines=full_lines)
    return engine._plan(query, query, {path: source}, [path], extra, 1).covered[path]


def test_text_names_finds_quoted_and_code_shaped_words() -> None:
    names = _text_names("Fix `#list` and save_week_plan in renderWeek with plain words")
    assert "#list" in names
    assert "save_week_plan" in names
    assert "renderWeek" in names
    assert "plain" not in names


def test_text_names_skips_short_quotes_and_repeats() -> None:
    names = _text_names("`ab` then `renderWeek` and renderWeek again")
    assert "ab" not in names
    assert names.count("renderWeek") == 1


def test_text_hits_matches_whole_words_only() -> None:
    assert _text_hits(["week"], ["weekly", "a week b", "week_x"]) == [("week", 2)]


def test_text_hits_leaves_punctuation_edge_open() -> None:
    assert _text_hits(["#list"], ["#listing", "x #list y"]) == [("#list", 2)]


def test_text_hits_puts_rarest_name_first() -> None:
    hits = _text_hits(["common", "rare"], ["common", "common rare", "common"])
    assert hits[0] == ("rare", 2)


@pytest.mark.parametrize(
    "definition",
    [
        "function openPanel() {",
        'case "openPanel":',
        "const openPanel = 1;",
        '<div id="openPanel">',
    ],
)
def test_text_hits_puts_defining_line_before_uses(definition: str) -> None:
    hits = _text_hits(["openPanel"], ["openPanel();", "x(openPanel);", definition])
    assert hits[0] == ("openPanel", 3)


def test_text_hits_caps_hits_per_name() -> None:
    assert len(_text_hits(["weekPanel"], ["weekPanel"] * 50)) == _MAX_TEXT_HITS


def test_request_terms_falls_back_to_last_part() -> None:
    source = 'render id="list" class="card" foo_bar'
    names = ["Widget.render", "#list", ".card", "`foo_bar()`", "absent"]
    assert request_terms(names, source) == ["render", "list", "card", "foo_bar"]


def test_request_terms_drops_duplicates_and_short_names() -> None:
    assert request_terms(["render", "Widget.render", "re"], "render") == ["render"]


def test_spread_hits_gives_every_name_a_first_turn() -> None:
    hits = [("a", 1), ("a", 2), ("b", 3)]
    assert spread_hits(hits) == [("a", 1), ("b", 3), ("a", 2)]


@pytest.mark.parametrize("path", ["page.html", "page.css", "page.md"])
def test_large_file_shows_lines_around_named_text(tmp_path: Path, path: str) -> None:
    source = _page(1000, {HIT: MARK})
    assert _covered(tmp_path, source, "Fix the `weekPanel` layout", path=path) == _window(HIT)


def test_large_file_finds_unquoted_camel_case_name(tmp_path: Path) -> None:
    source = _page(1000, {HIT: MARK})
    assert _covered(tmp_path, source, "Fix the weekPanel layout") == _window(HIT)


def test_small_file_is_still_shown_whole(tmp_path: Path) -> None:
    source = _page(50, {25: MARK})
    assert _covered(tmp_path, source, "Fix the `weekPanel` layout") == set(range(1, 51))


def test_windows_per_file_are_capped(tmp_path: Path) -> None:
    lines = range(100, 3000, 100)
    source = _page(3000, {n: f"<a>weekPanel {n}</a>" for n in lines})
    covered = _covered(tmp_path, source, "Fix the `weekPanel` layout")
    assert len(covered.intersection(lines)) == min(_MAX_TEXT_HITS, _MAX_TEXT_WINDOWS)


def test_tight_budget_falls_back_to_the_hit_line(tmp_path: Path) -> None:
    source = _page(1000, {HIT: MARK})
    covered = _covered(tmp_path, source, "Fix the `weekPanel` layout", full_lines=10)
    assert covered == set(range(HIT - _CONTEXT_PAD, HIT + _CONTEXT_PAD + 1))


@pytest.mark.parametrize("spec", ["weekPanel", "page.html::weekPanel"])
def test_requested_text_is_shown_despite_tight_budget(tmp_path: Path, spec: str) -> None:
    source = _page(1000, {HIT: MARK})
    covered = _covered(tmp_path, source, "Tidy the layout", full_lines=10, extra=(spec,))
    assert covered == _window(HIT)


def test_requested_text_for_another_file_is_ignored(tmp_path: Path) -> None:
    source = _page(1000, {HIT: MARK})
    extra = ("other.html::weekPanel",)
    assert _covered(tmp_path, source, "Tidy the layout", full_lines=10, extra=extra) == set()


def test_requested_selector_finds_its_attribute(tmp_path: Path) -> None:
    source = _page(1000, {HIT: '<div class="card">'})
    covered = _covered(tmp_path, source, "Tidy the layout", full_lines=10, extra=(".card",))
    assert covered == _window(HIT)


@pytest.mark.parametrize("extra", [(), ("weekPanel",)])
def test_python_files_get_no_text_windows(tmp_path: Path, extra: tuple[str, ...]) -> None:
    lines = [f"# note {n}" for n in range(1, 1001)]
    lines[HIT - 1] = 'PANEL = "weekPanel"'
    source = "\n".join(lines) + "\n"
    query = "Fix the `weekPanel` layout"
    assert HIT not in _covered(tmp_path, source, query, path="page.py", extra=extra)


def test_build_slice_includes_the_window_text(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text(_page(1000, {HIT: MARK}), encoding="utf-8")
    sliced = ContextEngine(tmp_path).build_slice(
        objective="Fix the `weekPanel` layout.",
        acceptance_criteria=["The layout is fixed"],
        allowed_files=["page.html"],
    )
    text = "\n".join(s.text for s in sliced.snippets if s.path == "page.html")
    assert MARK in text
    assert f"<p>filler {HIT + 1}</p>" in text
    assert "<p>filler 900</p>" not in text
