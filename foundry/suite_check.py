"""Whole-suite check: does a change break tests anywhere in the repo?

This part decides which commands run a repository's whole test suite and reads
the failing test ids out of their output, then compares a patched copy of the
repo with its base so only failures the patch introduces count. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SuiteResult", "check_patch", "failing_tests", "run_suite", "suite_commands"]

#: Directory names never searched for tests.
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv"})
#: Directory levels below the root searched for tests.
_MAX_DEPTH = 3
_PYTEST_LINE = re.compile(r"^(?:FAILED|ERROR) (\S.*)$")
_NODE_LINE = re.compile(r"^\s*not ok \d+ - (\S.*)$")


@dataclass
class SuiteResult:
    """Outcome of comparing a repo's whole test suite before and after a patch."""

    ok: bool
    ran: bool
    new_failures: list[str] = field(default_factory=list)
    base_failures: list[str] = field(default_factory=list)
    head_failures: list[str] = field(default_factory=list)
    summary: str = ""
    duration_s: float = 0.0


def suite_commands(root: Path) -> list[list[str]]:
    """The argv of each command that runs the whole suite of ``root`` (FOUNDRY_SUITE_CMD wins)."""
    configured = os.environ.get("FOUNDRY_SUITE_CMD", "")
    if configured.strip():
        split = (shlex.split(part, posix=os.name != "nt") for part in configured.split(";;"))
        return [argv for argv in split if argv]
    root = Path(root)
    commands: list[list[str]] = []
    if _has_python_tests(root):
        commands.append([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"])
    scripts = _test_scripts(root)
    npm = _find_npm() if scripts else None
    if npm:
        commands.extend([npm, "run", "-s", name] for name in scripts)
    return commands


def failing_tests(output: str) -> set[str]:
    """The ids of the failed tests in pytest and node:test output."""
    failed: set[str] = set()
    for line in output.splitlines():
        pytest_match = _PYTEST_LINE.match(line)
        if pytest_match:
            failed.add(pytest_match.group(1).split(" - ", 1)[0].strip())
            continue
        node_match = _NODE_LINE.match(line)
        if node_match:
            failed.add(f"node: {node_match.group(1).strip()}")
    return failed


def _has_python_tests(root: Path) -> bool:
    """True when a ``test_*.py`` file sits under a ``tests`` directory within the search depth."""
    for current, dirs, files in os.walk(root):
        parts = Path(current).relative_to(root).parts
        if "tests" in parts and any(f.startswith("test_") and f.endswith(".py") for f in files):
            return True
        if len(parts) >= _MAX_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and (*parts, d) != ("foundry", "data")]
    return False


def _test_scripts(root: Path) -> list[str]:
    """The package.json scripts named ``test`` or ``test:*`` (empty when there is no usable file)."""
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [name for name in scripts if name == "test" or name.startswith("test:")]


def _find_npm() -> str | None:
    """The npm executable (``npm.cmd`` is also tried on Windows), or None."""
    found = shutil.which("npm")
    if found is None and os.name == "nt":
        found = shutil.which("npm.cmd")
    return found


#: Directory names never copied into the scratch repo.
_COPY_SKIP = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_GIT_TIMEOUT_S = 120.0

_BaseKey = tuple[str, tuple[tuple[str, ...], ...]]
#: Failures of every base suite that ran to the end, so PRs on one snapshot run it once.
_BASE_RESULTS: dict[_BaseKey, frozenset[str]] = {}
_BASE_LOCKS: dict[_BaseKey, threading.Lock] = {}
_BASE_LOCKS_GUARD = threading.Lock()


def _suite_env() -> dict[str, str]:
    """The environment for suite and git commands (inherited git locations would point elsewhere)."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _command_name(argv: list[str]) -> str:
    """A stable label for a command: the same for every copy, different for each npm script."""
    return " ".join([Path(argv[0]).name, *argv[1:]])


def run_suite(root: Path, *, timeout_s: float) -> tuple[set[str], bool]:
    """Run every suite command in ``root``; returns (failing test ids, completed).

    ``completed`` is False when a command timed out or could not start. A command
    that exits non-zero without a parsable failure counts as one failure of its own.
    """
    failed: set[str] = set()
    completed = True
    env = _suite_env()
    for argv in suite_commands(root):
        # a file, not a pipe: on Windows a grandchild (npm's node) holding a pipe open
        # makes subprocess.run wait forever after the timeout killed the command
        with tempfile.TemporaryFile() as captured:
            try:
                proc = subprocess.run(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=captured,
                                      stderr=subprocess.STDOUT, timeout=timeout_s, check=False)
            except (OSError, subprocess.SubprocessError):
                completed = False
                continue
            captured.seek(0)
            output = captured.read().decode("utf-8", "replace")
        found = failing_tests(output)
        if proc.returncode != 0 and not found:
            found = {f"{_command_name(argv)}: exit {proc.returncode}"}
        failed |= found
    return failed, completed


def _base_failures(base_root: Path, copy: Path, timeout_s: float) -> tuple[set[str], bool]:
    """The base suite's failures, run in ``copy`` unless this base already has a finished run."""
    key: _BaseKey = (str(base_root), tuple(sorted(tuple(argv) for argv in suite_commands(base_root))))
    with _BASE_LOCKS_GUARD:
        lock = _BASE_LOCKS.setdefault(key, threading.Lock())
    with lock:  # PRs published at the same time wait for one base run
        cached = _BASE_RESULTS.get(key)
        if cached is not None:
            return set(cached), True
        failures, completed = run_suite(copy, timeout_s=timeout_s)
        if completed:
            _BASE_RESULTS[key] = frozenset(failures)
        return failures, completed


def _copy_ignore(root: Path, workdir: Path) -> Callable[[str, list[str]], set[str]]:
    """A ``copytree`` ignore for ``root``; ``workdir`` is skipped so the copy never contains itself."""
    excluded = (root / "foundry" / "data", workdir)

    def ignore(current: str, names: list[str]) -> set[str]:
        skip = {name for name in names if name in _COPY_SKIP}
        here = Path(current)
        skip.update(path.name for path in excluded if path.parent == here and path.name in names)
        return skip

    return ignore


def _apply_patch(copy: Path, patch_path: Path) -> bool:
    """Apply the patch in the scratch copy; False when it does not apply."""
    env = _suite_env()

    def git(*args: str) -> bool:
        proc = subprocess.run(["git", *args], cwd=str(copy), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=_GIT_TIMEOUT_S, check=False)
        return proc.returncode == 0

    # without its own repository, git apply would work relative to an enclosing one
    if not (copy / ".git").exists() and not git("init", "-q"):
        return False
    return git("apply", "--whitespace=nowarn", str(Path(patch_path).resolve()))


def check_patch(base_root: Path, patch_path: Path, *, timeout_s: float, workdir: Path) -> SuiteResult:
    """Run the whole suite on a copy of ``base_root``, before and after the patch.

    Only failures the patch adds make the result not ok. When the check itself
    cannot finish the result is ok with ``ran=False``: it never blocks a PR.
    """
    started = time.monotonic()
    base_root = Path(base_root).resolve()
    workdir = Path(workdir).resolve()

    def skipped(why: str) -> SuiteResult:
        return SuiteResult(ok=True, ran=False, summary=f"whole-suite check skipped: {why}",
                           duration_s=time.monotonic() - started)

    tmp: Path | None = None
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="suite-", dir=workdir))
        copy = tmp / "repo"
        shutil.copytree(base_root, copy, ignore=_copy_ignore(base_root, workdir), ignore_dangling_symlinks=True)
        base, completed = _base_failures(base_root, copy, timeout_s)
        if not completed:
            return skipped("the suite did not complete on the base")
        if not _apply_patch(copy, patch_path):
            return skipped("the patch did not apply to the copy")
        head, completed = run_suite(copy, timeout_s=timeout_s)
        if not completed:
            return skipped("the suite did not complete with the patch")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:  # ValueError: a badly quoted FOUNDRY_SUITE_CMD
        return skipped(f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    new = sorted(head - base)
    if new:
        summary = f"{len(new)} new test failure(s): {', '.join(new[:10])}"
    else:
        summary = f"no new test failures ({len(base)} already failing on the base)"
    return SuiteResult(ok=not new, ran=True, new_failures=new, base_failures=sorted(base),
                       head_failures=sorted(head), summary=summary, duration_s=time.monotonic() - started)
