"""Tests for JavaScript symbol edits and their duplicate definition check."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from foundry.edits import Edit, _apply_one

SOURCE = (
    "function first() {\n"
    "  return 1;\n"
    "}\n"
    "\n"
    "function second() {\n"
    "  return 2;\n"
    "}\n"
)
CLASS_SOURCE = "class Box {\n  size() {\n    return 1;\n  }\n}\n"
SECOND_AGAIN = "function second() {\n  return 3;\n}\n"
THIRD = "function third() {\n  return 3;\n}\n"


def _apply(source: str, op: str, code: str | None = None, symbol: str | None = None) -> str:
    state: Any = SimpleNamespace(rel="app.js", text=source, newline="\n")
    _apply_one(state, Edit(op=op, file="app.js", symbol=symbol, code=code))
    return str(state.text)


def test_replace_function() -> None:
    code = "function first() {\n  return 9;\n}\n"
    result = _apply(SOURCE, "replace_symbol", code, "first")
    assert "return 9;" in result
    assert "return 1;" not in result
    assert "function second()" in result
    assert result.count("function first()") == 1


def test_replace_method() -> None:
    code = "size() {\n  return 2;\n}\n"
    result = _apply(CLASS_SOURCE, "replace_symbol", code, "Box.size")
    assert "return 2;" in result
    assert "return 1;" not in result
    assert result.count("size()") == 1


def test_replace_rejects_code_defining_another_name() -> None:
    with pytest.raises(ValueError):
        _apply(SOURCE, "replace_symbol", THIRD, "first")


def test_delete_function() -> None:
    result = _apply(SOURCE, "delete_symbol", symbol="second")
    assert "function second()" not in result
    assert "function first()" in result


def test_insert_after_places_new_function_after_anchor() -> None:
    result = _apply(SOURCE, "insert_after_symbol", THIRD, "first")
    assert result.index("function first") < result.index("function third")
    assert result.index("function third") < result.index("function second")


def test_insert_before_places_new_function_before_anchor() -> None:
    result = _apply(SOURCE, "insert_before_symbol", THIRD, "first")
    assert result.index("function third") < result.index("function first")


def test_unknown_symbol_is_rejected() -> None:
    with pytest.raises(ValueError):
        _apply(SOURCE, "insert_after_symbol", THIRD, "missing")


@pytest.mark.parametrize("op", ["insert_after_symbol", "insert_before_symbol"])
def test_inserting_existing_function_is_rejected(op: str) -> None:
    with pytest.raises(ValueError, match="'second' already defined"):
        _apply(SOURCE, op, SECOND_AGAIN, "first")


def test_inserting_existing_const_is_rejected() -> None:
    source = "const limit = 1;\n\nfunction first() {\n  return 1;\n}\n"
    with pytest.raises(ValueError, match="'limit' already defined"):
        _apply(source, "insert_after_symbol", "const limit = 2;\n", "first")


def test_inserting_existing_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="'Box.size' already defined"):
        _apply(CLASS_SOURCE, "insert_after_symbol", "size() {\n  return 2;\n}\n", "Box.size")


def test_getter_and_setter_do_not_clash() -> None:
    source = "class Box {\n  get value() {\n    return 1;\n  }\n}\n"
    code = "set value(next) {\n  this.next = next;\n}\n"
    result = _apply(source, "insert_after_symbol", code, "Box.value")
    assert "get value()" in result
    assert "set value(next)" in result


def test_duplicate_already_in_file_does_not_block_other_edits() -> None:
    source = SOURCE + "\n" + SECOND_AGAIN
    result = _apply(source, "insert_after_symbol", THIRD, "first")
    assert "function third()" in result
    assert result.count("function second()") == 2


def test_nested_function_is_not_a_top_level_clash() -> None:
    source = (
        "function first() {\n"
        "  function helper() {\n"
        "    return 1;\n"
        "  }\n"
        "  return helper();\n"
        "}\n"
    )
    code = "function helper() {\n  return 2;\n}\n"
    result = _apply(source, "insert_after_symbol", code, "first")
    assert result.count("function helper()") == 2


def test_name_in_comment_or_string_is_not_a_clash() -> None:
    source = (
        "// function third() {}\n"
        "const text = 'function third() {}';\n"
        "\n"
        "function first() {\n"
        "  return 1;\n"
        "}\n"
    )
    result = _apply(source, "insert_after_symbol", THIRD, "first")
    assert "return 3;" in result


def test_append_new_function() -> None:
    result = _apply(SOURCE, "append", THIRD)
    assert result.index("function second") < result.index("function third")


def test_append_existing_function_is_rejected() -> None:
    with pytest.raises(ValueError, match="'second' already defined"):
        _apply(SOURCE, "append", SECOND_AGAIN)


def test_append_to_unscannable_file_is_still_allowed() -> None:
    source = "const broken = 'never closed\n"
    result = _apply(source, "append", THIRD)
    assert "function third()" in result
