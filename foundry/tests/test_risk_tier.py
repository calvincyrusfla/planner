from __future__ import annotations

import pytest

from foundry.risk_tier import at_most, classify, paths_from_diff, tier_order


def _diff(path: str, added: list[str] | None = None, removed: list[str] | None = None) -> str:
    body = "".join(f"-{r}\n" for r in removed or []) + "".join(f"+{a}\n" for a in added or [])
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n{body}"


@pytest.mark.parametrize("path", [
    "CLAUDE.md", "claude.md", "docs/Claude.MD", ".claude/agents/x.md", "schema/a.sql", "netlify/f.js",
    ".github/workflows/ci.yml", "requirements.txt", "requirements-dev.txt", "package.json",
    "package-lock.json", "pyproject.toml", "netlify.toml", "foundry/config.py",
    "foundry/validation_gates.py", "foundry/git_publisher.py", "foundry/self_update.py",
    "foundry/combined_check.py", "foundry/merge_train.py", "foundry/stack_keeper.py",
    "foundry/update_watcher.py",
])
def test_high_paths(path: str) -> None:
    result = classify([path], _diff(path, ["x"]))
    assert result["tier"] == "high"
    assert result["reasons"] and path in result["reasons"][0]


def test_deleted_file_is_high() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    result = classify(["a.py"], diff)
    assert result["tier"] == "high"
    assert any("deleted" in r for r in result["reasons"])
    assert paths_from_diff(diff) == ["a.py"]


def test_many_removed_lines_is_high() -> None:
    assert classify(["a.py"], _diff("a.py", removed=["x"] * 151))["tier"] == "high"
    assert classify(["a.py"], _diff("a.py", removed=["x"] * 150))["tier"] == "normal"


def test_renamed_function_counts_as_removed() -> None:
    result = classify(["a.py"], _diff("a.py", ["def new(): pass"], ["def old(): pass"]))
    assert result["tier"] == "high"
    assert any("old" in r for r in result["reasons"])
    assert classify(["a.py"], _diff("a.py", ["class B: pass"], ["class A: pass"]))["tier"] == "high"


def test_moved_function_not_counted() -> None:
    result = classify(["a.py"], _diff("a.py", ["def f(): pass"], ["def f(): pass"]))
    assert result["tier"] == "low"


def test_indented_def_ignored() -> None:
    assert classify(["a.py"], _diff("a.py", removed=["    def m(self): pass"]))["tier"] == "low"


def test_tests_docs() -> None:
    result = classify(["foundry/tests/test_x.py", "README.md"], _diff("README.md", ["x"] * 500))
    assert result["tier"] == "tests_docs"
    assert result["reasons"]


def test_low_limits() -> None:
    paths = ["a.py", "b.py", "c.py"]
    assert classify(paths, _diff("a.py", ["x"] * 40, ["y"] * 40))["tier"] == "low"
    assert classify(paths, _diff("a.py", ["x"] * 41, ["y"] * 40))["tier"] == "normal"
    assert classify(paths + ["d.py"], _diff("a.py", ["x"]))["tier"] == "normal"


def test_counts() -> None:
    result = classify(["a.py"], _diff("a.py", ["x", "y"], ["z"]))
    assert (result["added"], result["removed"], result["files"]) == (2, 1, 1)


def test_at_most() -> None:
    assert tier_order == ("tests_docs", "low", "normal", "high")
    assert at_most("low", "normal")
    assert at_most("low", "low")
    assert not at_most("high", "normal")
