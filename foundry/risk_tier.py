"""Risk tier: classify a change as tests_docs, low, normal or high with plain code.

``python -m foundry.risk_tier PATCH_FILE`` prints the tier and the reasons.
"""
from __future__ import annotations

import re
import sys
from typing import Any

from foundry.combined_check import _is_test_or_doc

__all__ = ["at_most", "classify", "main", "paths_from_diff", "tier_order"]

tier_order = ("tests_docs", "low", "normal", "high")

_HIGH_DIRS = ("schema/", "netlify/", ".github/")
_HIGH_NAMES = ("package.json", "package-lock.json", "pyproject.toml", "netlify.toml")
_HIGH_FILES = (
    "foundry/config.py",
    "foundry/validation_gates.py", "foundry/git_publisher.py", "foundry/self_update.py",
    "foundry/combined_check.py", "foundry/merge_train.py", "foundry/stack_keeper.py",
    "foundry/update_watcher.py",
)
_MAX_REMOVED = 150
_LOW_FILES = 3
_LOW_LINES = 80
_DEF_RE = re.compile(r"(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")


def at_most(tier: str, limit: str) -> bool:
    """Whether ``tier`` is no riskier than ``limit``."""
    return tier_order.index(tier) <= tier_order.index(limit)


def _strip(path: str) -> str:
    path = path.strip().split("\t")[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _high_path_reason(path: str) -> str | None:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    if name == "claude.md":
        return f"{path}: CLAUDE.md instructions"
    if low.startswith(".claude/"):
        return f"{path}: under .claude/"
    for folder in _HIGH_DIRS:
        if low.startswith(folder):
            return f"{path}: under {folder}"
    if name.startswith("requirements") and name.endswith(".txt"):
        return f"{path}: dependency file"
    if low in _HIGH_NAMES or name in _HIGH_NAMES:
        return f"{path}: dependency or settings file"
    if low in _HIGH_FILES:
        return f"{path}: Foundry safety or merge code" if low != "foundry/config.py" else f"{path}: settings file"
    return None


def _scan(diff: str) -> tuple[int, int, list[str], list[str]]:
    """(added, removed, deleted paths, removed public names as 'path: name')."""
    added = removed = 0
    deleted: list[str] = []
    gone: list[str] = []
    lines = diff.splitlines()
    in_hunk = False
    old_path = new_path = ""
    adds: set[str] = set()
    rems: list[str] = []

    def flush() -> None:
        if new_path.endswith(".py") or old_path.endswith(".py"):
            target = new_path if new_path and new_path != "/dev/null" else old_path
            if new_path != "/dev/null":
                gone.extend(f"{target}: {n}" for n in rems if n not in adds)

    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            flush()
            in_hunk = False
            old_path = new_path = ""
            adds, rems = set(), []
            continue
        if not in_hunk and line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if old_path or new_path:
                flush()
                adds, rems = set(), []
            old_path = _strip(line[4:])
            continue
        if not in_hunk and line.startswith("+++ "):
            new_path = _strip(line[4:])
            if new_path == "/dev/null" and old_path:
                deleted.append(old_path)
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("+"):
            added += 1
            m = _DEF_RE.match(line[1:])
            if m:
                adds.add(m.group(1))
        elif line.startswith("-"):
            removed += 1
            m = _DEF_RE.match(line[1:])
            if m:
                rems.append(m.group(1))
    flush()
    return added, removed, deleted, gone


def paths_from_diff(diff: str) -> list[str]:
    """Repo-relative changed paths named by the headers of a unified diff."""
    paths: list[str] = []
    lines = diff.splitlines()
    in_hunk = False
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ "):
            path = _strip(line[4:])
            if path == "/dev/null":
                prev = lines[i - 1] if i else ""
                path = _strip(prev[4:]) if prev.startswith("--- ") else ""
            if path and path not in paths:
                paths.append(path)
    return paths


def classify(paths: list[str], diff: str) -> dict[str, Any]:
    """Risk tier of a change from its changed paths and unified diff; first matching rule wins."""
    added, removed, deleted, gone = _scan(diff)
    files = len(paths)
    high: list[str] = []
    for path in paths:
        reason = _high_path_reason(path)
        if reason:
            high.append(reason)
    high.extend(f"{path}: file deleted" for path in deleted)
    if removed > _MAX_REMOVED:
        high.append(f"{removed} lines removed (more than {_MAX_REMOVED})")
    high.extend(f"{item} removed or renamed" for item in gone)
    if high:
        tier, reasons = "high", high
    elif paths and all(_is_test_or_doc(p) for p in paths):
        tier, reasons = "tests_docs", ["only test or doc files"]
    elif files <= _LOW_FILES and added + removed <= _LOW_LINES:
        tier, reasons = "low", [f"{files} file(s), {added + removed} changed line(s)"]
    else:
        tier, reasons = "normal", [f"{files} file(s), {added + removed} changed line(s)"]
    return {"tier": tier, "reasons": reasons, "added": added, "removed": removed, "files": files}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m foundry.risk_tier PATCH_FILE", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8", errors="replace") as handle:
        diff = handle.read()
    result = classify(paths_from_diff(diff), diff)
    print(result["tier"])
    for reason in result["reasons"]:
        print(f"- {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
