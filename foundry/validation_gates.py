"""Deterministic validation gates for builder submissions.

Every builder submission passes through these gates *before* any reviewer
(Claude) sees it. The gates are cheap, local and deterministic:

* ``syntax`` - ``ast.parse`` for ``.py``, ``json.loads`` for ``.json`` and
  ``node --check`` for ``.js``/``.mjs``/``.cjs`` (skipped with a note when
  ``node`` is not installed).
* ``linter`` - ``ruff`` when it is on ``PATH``, otherwise a small built-in AST
  checker (syntax-level errors via ``compile``, unused imports, bare
  ``except:``).
* ``typecheck`` - ``mypy`` on the changed ``.py`` files (``--ignore-missing-imports
  --follow-imports=silent``, the repository's own mypy config is ignored), so
  only type errors in the files the builder touched fail the gate. Mode
  ``auto`` (default, ``FOUNDRY_TYPECHECK``) runs it when mypy is installed and
  skips it with a note otherwise; ``mypy`` requires it; ``off`` skips it. A
  mypy that crashes or times out is skipped with a note (not the builder's
  fault).
* ``tests`` - runs a given test command (e.g. ``python -m pytest tests -q``)
  in the workspace with a timeout. Skipped when no command is given.
* ``schema`` - checks that expected classes / functions / methods exist with
  the expected parameter names.

Gates short-circuit: the first failing gate stops the run, and
:class:`GateOutcome` exposes ``first_failure`` so the caller can send a fix
hint back to the builder and skip the reviewer entirely.

The module also provides :func:`apply_diff`, a pure-Python unified-diff
applier used to materialise a submission's diff into a workspace. It refuses
paths outside ``allowed_files`` and paths that escape the workspace.

``SolutionCache`` and ``RetryPolicy`` live in :mod:`foundry.solution_cache`
and are re-exported here for convenience.
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import importlib.util
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from foundry.schemas import BuilderSubmission, DeterministicGateResult, GateCheck
from foundry.solution_cache import RetryPolicy, SolutionCache

__all__ = [
    "ALL_CHECKS",
    "AppliedFile",
    "DiffApplyError",
    "GateOutcome",
    "RetryPolicy",
    "SolutionCache",
    "TypecheckMode",
    "ValidationGates",
    "STACK_COPY_IGNORE",
    "apply_diff",
    "builtin_lint",
    "prepare_stacked_workspace",
    "run_gates",
    "run_gates_outcome",
]

ALL_CHECKS: tuple[GateCheck, ...] = ("syntax", "linter", "typecheck", "tests", "schema")
"""Default gate order. Cheapest first so failures short-circuit early."""

LinterMode = Literal["auto", "ruff", "builtin"]
TypecheckMode = Literal["auto", "mypy", "off"]

_MAX_OUTPUT_CHARS = 20_000
_RUFF_RULES = "E4,E7,E9,F"
_JS_SUFFIXES = {".js", ".mjs", ".cjs"}


# --------------------------------------------------------------------------
# Diff application
# --------------------------------------------------------------------------


class DiffApplyError(ValueError):
    """Raised when a unified diff cannot be applied safely or cleanly."""


@dataclass(frozen=True)
class AppliedFile:
    """One file changed by :func:`apply_diff`."""

    path: str
    """Workspace-relative POSIX path."""
    action: Literal["added", "modified", "deleted"]


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    """``(tag, text)`` pairs; tag is ``" "``, ``"-"``, ``"+"``, or ``"\\"``."""


@dataclass
class _FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[_Hunk] = field(default_factory=list)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _header_path(raw: str) -> str | None:
    """Extract the path from a ``---``/``+++`` header value.

    Strips a trailing timestamp (tab separated), surrounding quotes and the
    conventional ``a/``/``b/`` prefixes. Returns ``None`` for ``/dev/null``.
    """
    value = raw.split("\t", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _parse_diff(diff: str) -> list[_FilePatch]:
    """Parse unified diff text into per-file patches."""
    lines = diff.splitlines()
    patches: list[_FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            raise DiffApplyError("binary patches are not supported")
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            patch = _FilePatch(_header_path(line[4:]), _header_path(lines[i + 1][4:]))
            if patch.old_path is None and patch.new_path is None:
                raise DiffApplyError("diff header has /dev/null on both sides")
            i += 2
            while i < len(lines):
                match = _HUNK_RE.match(lines[i])
                if not match:
                    break
                hunk = _Hunk(
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2)) if match.group(2) is not None else 1,
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4)) if match.group(4) is not None else 1,
                )
                i += 1
                old_seen = new_seen = 0
                while i < len(lines):
                    body = lines[i]
                    if old_seen >= hunk.old_count and new_seen >= hunk.new_count:
                        # Counts satisfied; only a trailing "\ No newline" may follow.
                        if body.startswith("\\"):
                            hunk.lines.append(("\\", body))
                            i += 1
                        break
                    if body.startswith("\\"):
                        hunk.lines.append(("\\", body))
                    elif body == "" or body[0] == " ":
                        # Some tools strip the space of empty context lines.
                        hunk.lines.append((" ", body[1:]))
                        old_seen += 1
                        new_seen += 1
                    elif body[0] == "-":
                        hunk.lines.append(("-", body[1:]))
                        old_seen += 1
                    elif body[0] == "+":
                        hunk.lines.append(("+", body[1:]))
                        new_seen += 1
                    else:
                        break  # lenient: hunk ended early (miscounted header)
                    i += 1
                patch.hunks.append(hunk)
            if not patch.hunks and patch.new_path is not None and patch.old_path is not None:
                raise DiffApplyError(f"no hunks for {patch.new_path}")
            patches.append(patch)
            continue
        i += 1
    if not patches:
        raise DiffApplyError("no file changes found in diff")
    return patches


def _normalise_rel(path: str) -> str:
    """Normalise a diff path to a clean workspace-relative POSIX path."""
    candidate = path.replace("\\", "/").strip()
    if not candidate or "\x00" in candidate:
        raise DiffApplyError(f"invalid path {path!r}")
    if candidate.startswith("/") or _DRIVE_RE.match(candidate):
        raise DiffApplyError(f"absolute path not allowed: {path!r}")
    parts = candidate.split("/")
    if ".." in parts:
        raise DiffApplyError(f"path escapes the workspace: {path!r}")
    normal = posixpath.normpath(candidate)
    if normal in (".", ""):
        raise DiffApplyError(f"invalid path {path!r}")
    return normal


def _is_allowed(rel: str, allowed_files: Iterable[str] | None) -> bool:
    """Return True when ``rel`` matches an entry of ``allowed_files``.

    Entries may be exact paths, glob patterns (``foundry/*.py``) or directory
    prefixes ending in ``/``. ``None`` means "no restriction".
    """
    if allowed_files is None:
        return True
    for entry in allowed_files:
        pattern = entry.replace("\\", "/").strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            if rel.startswith(posixpath.normpath(pattern) + "/"):
                return True
            continue
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatchcase(rel, pattern):
                return True
            continue
        if rel == posixpath.normpath(pattern):
            return True
    return False


def _resolve_inside(workspace: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``workspace``; refuse anything outside it (incl. symlinks)."""
    root = workspace.resolve()
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise DiffApplyError(f"path escapes the workspace: {rel!r}")
    if target == root:
        raise DiffApplyError(f"invalid path {rel!r}")
    return target


def _find_block(lines: list[str], block: list[str], expected: int, floor: int) -> int:
    """Find ``block`` in ``lines`` closest to ``expected`` at or after ``floor``."""
    n, m = len(lines), len(block)
    expected = max(floor, min(expected, n))
    max_offset = max(n, 1)
    for offset in range(0, max_offset + 1):
        for pos in (expected - offset, expected + offset) if offset else (expected,):
            if floor <= pos <= n - m and lines[pos : pos + m] == block:
                return pos
    return -1


def _apply_hunks(original: str | None, patch: _FilePatch, rel: str) -> tuple[str, bool]:
    """Apply a file's hunks to ``original``; return ``(new_text, has_content)``."""
    text = original or ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    ends_with_eol = text.endswith(("\n", "\r")) if text else True
    result_eol: bool | None = None

    floor = 0
    delta = 0
    for number, hunk in enumerate(patch.hunks, start=1):
        old_block = [t for tag, t in hunk.lines if tag in (" ", "-")]
        new_block = [t for tag, t in hunk.lines if tag in (" ", "+")]
        expected = (hunk.old_start - 1 if hunk.old_count else hunk.old_start) + delta
        if not old_block:
            pos = max(floor, min(expected, len(lines)))
        else:
            pos = _find_block(lines, old_block, expected, floor)
        if pos < 0:
            raise DiffApplyError(f"hunk {number} does not apply to {rel}")
        lines[pos : pos + len(old_block)] = new_block
        floor = pos + len(new_block)
        delta += len(new_block) - len(old_block)
        previous = ""
        for tag, _ in hunk.lines:
            if tag == "\\":
                if previous in (" ", "+"):
                    result_eol = False
                elif previous == "-" and result_eol is None:
                    result_eol = True
            previous = tag
    if result_eol is None:
        result_eol = ends_with_eol
    body = newline.join(lines)
    if lines and result_eol:
        body += newline
    return body, bool(lines)


def apply_diff(
    diff: str,
    workspace: str | Path,
    *,
    allowed_files: Iterable[str] | None = None,
) -> list[AppliedFile]:
    """Apply a unified diff to ``workspace``.

    Supports new files (``--- /dev/null``), modifications and deletions
    (``+++ /dev/null``), multiple files and hunks, ``a/``/``b/`` prefixes,
    CRLF files and ``\\ No newline at end of file`` markers. Hunks are located
    near their stated line numbers, tolerating small offsets.

    The diff is applied atomically: every file is validated and patched in
    memory first; nothing is written unless all files apply.

    Args:
        diff: Unified diff text.
        workspace: Directory the diff paths are relative to.
        allowed_files: Paths, glob patterns or ``dir/`` prefixes the diff may
            touch. ``None`` disables the allow-list (the workspace boundary is
            always enforced).

    Returns:
        The changed files, in diff order.

    Raises:
        DiffApplyError: on malformed diffs, renames, paths outside the allow
            list or the workspace, new files that already exist, missing files
            to modify, or hunks that do not match.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise DiffApplyError(f"workspace does not exist: {root}")
    allowed = list(allowed_files) if allowed_files is not None else None
    planned: list[tuple[Path, AppliedFile, str | None]] = []
    seen: set[str] = set()

    for patch in _parse_diff(diff):
        old_rel = _normalise_rel(patch.old_path) if patch.old_path else None
        new_rel = _normalise_rel(patch.new_path) if patch.new_path else None
        if old_rel and new_rel and old_rel != new_rel:
            raise DiffApplyError(f"renames are not supported: {old_rel} -> {new_rel}")
        rel = new_rel or old_rel
        assert rel is not None
        if not _is_allowed(rel, allowed):
            raise DiffApplyError(f"path not in allowed_files: {rel}")
        if rel in seen:
            raise DiffApplyError(f"file appears twice in diff: {rel}")
        seen.add(rel)
        target = _resolve_inside(root, rel)

        if old_rel is None:  # new file
            if target.exists():
                raise DiffApplyError(f"new file already exists: {rel}")
            content, _ = _apply_hunks(None, patch, rel)
            planned.append((target, AppliedFile(rel, "added"), content))
            continue
        if not target.is_file():
            raise DiffApplyError(f"file to change does not exist: {rel}")
        original = target.read_bytes().decode("utf-8")
        content, has_content = _apply_hunks(original, patch, rel)
        if new_rel is None:  # deletion
            if has_content:
                raise DiffApplyError(f"deletion of {rel} does not remove all of its content")
            planned.append((target, AppliedFile(rel, "deleted"), None))
        else:
            planned.append((target, AppliedFile(rel, "modified"), content))

    for target, _applied, content in planned:
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
    return [applied for _, applied, _ in planned]


#: Never copied into a stacked workspace (VCS data, environments, caches);
#: the same list as ``floor_manager.SCRATCH_IGNORE``.
STACK_COPY_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "*.db", "*.db-wal", "*.db-shm",
)


def prepare_stacked_workspace(
    base: str | Path,
    dst: str | Path,
    diffs: Iterable[str | tuple[str, str]],
) -> Path:
    """Copy ``base`` to ``dst`` and apply ``diffs`` there, in order.

    This is the workspace a stacked ticket is built in: the repository plus
    the approved diffs of the task's earlier tickets, each relative to the
    ones before it.  ``diffs`` items are diff texts or ``(label, diff)``
    pairs (e.g. ``(subtask_id, diff)``); empty diffs are skipped.  No
    allow-list applies (the diffs were already gated and approved); the
    workspace boundary is still enforced.  ``base`` is never modified.

    Raises:
        DiffApplyError: an earlier diff does not apply on top of the ones
            before it (the message names it).
        OSError / shutil.Error: the copy failed.
    """
    src, out = Path(base), Path(dst)
    if src.is_dir():
        # symlinks stay links: never follow them out of the repo (or loop on them)
        shutil.copytree(src, out, symlinks=True, ignore=shutil.ignore_patterns(*STACK_COPY_IGNORE))
    else:
        out.mkdir(parents=True)
    for n, item in enumerate(diffs, 1):
        label, diff = item if isinstance(item, tuple) else (f"#{n}", item)
        if not str(diff or "").strip():
            continue
        try:
            apply_diff(diff, out)
        except DiffApplyError as exc:
            raise DiffApplyError(f"earlier approved diff {label} does not apply: {exc}") from exc
    return out


# --------------------------------------------------------------------------
# Gate outcome
# --------------------------------------------------------------------------


@dataclass
class GateOutcome:
    """Aggregate result of a gate run.

    ``passed`` is False when any gate failed; ``first_failure`` is that gate's
    result. The caller must skip the reviewer when ``passed`` is False.
    """

    passed: bool
    results: list[DeterministicGateResult]
    first_failure: DeterministicGateResult | None = None

    @classmethod
    def from_results(cls, results: Sequence[DeterministicGateResult]) -> GateOutcome:
        """Build an outcome from gate results (in run order)."""
        failure = next((r for r in results if not r.passed), None)
        return cls(passed=failure is None, results=list(results), first_failure=failure)

    @property
    def failed_check(self) -> GateCheck | None:
        """Name of the first failing gate, if any."""
        return self.first_failure.check_type if self.first_failure else None

    def summary(self, max_chars: int = 160) -> str:
        """One-line deterministic summary, usable as a fallback fix hint."""
        if self.first_failure is None:
            return "all gates passed: " + ", ".join(r.check_type for r in self.results)
        failure = self.first_failure
        detail = ""
        for stream in (failure.stderr, failure.stdout):
            for line in stream.splitlines():
                if line.strip():
                    detail = line.strip()
                    break
            if detail:
                break
        text = f"{failure.check_type} gate failed" + (f": {detail}" if detail else "")
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _clip(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    """Keep the tail of long output (tracebacks end with the useful part)."""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def _result(
    submission: BuilderSubmission,
    check: GateCheck,
    passed: bool,
    started: float,
    stdout: str = "",
    stderr: str = "",
    note: str | None = None,
) -> DeterministicGateResult:
    return DeterministicGateResult(
        task_id=submission.task_id,
        subtask_id=submission.subtask_id,
        passed=passed,
        check_type=check,
        stdout=_clip(stdout),
        stderr=_clip(stderr),
        duration_seconds=max(0.0, time.monotonic() - started),
        note=note,
    )


def _submission_files(submission: BuilderSubmission, workspace: Path) -> list[tuple[str, Path]]:
    """Existing files named by the submission, as ``(rel, absolute)`` pairs.

    Deleted files are skipped; unsafe paths raise :class:`DiffApplyError`.
    """
    files: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name in submission.modified_files:
        rel = _normalise_rel(name)
        if rel in seen:
            continue
        seen.add(rel)
        target = _resolve_inside(workspace, rel)
        if target.is_file():
            files.append((rel, target))
    return files


# --------------------------------------------------------------------------
# Individual gates
# --------------------------------------------------------------------------


def _gate_syntax(files: list[tuple[str, Path]]) -> tuple[bool, str, str]:
    errors: list[str] = []
    notes: list[str] = []
    node = shutil.which("node")
    for rel, path in files:
        suffix = path.suffix.lower()
        if suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except SyntaxError as exc:
                errors.append(f"{rel}:{exc.lineno or 0}:{exc.offset or 0}: SyntaxError: {exc.msg}")
            except (UnicodeDecodeError, ValueError) as exc:
                errors.append(f"{rel}: cannot parse: {exc}")
            else:
                notes.append(f"{rel}: ok")
        elif suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"{rel}:{exc.lineno}:{exc.colno}: JSONDecodeError: {exc.msg}")
            except UnicodeDecodeError as exc:
                errors.append(f"{rel}: cannot decode: {exc}")
            else:
                notes.append(f"{rel}: ok")
        elif suffix in _JS_SUFFIXES:
            if node is None:
                notes.append(f"{rel}: skipped (node not installed)")
                continue
            try:
                proc = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{rel}: node --check timed out")
                continue
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()
                errors.append(f"{rel}: node --check failed\n{detail}")
            else:
                notes.append(f"{rel}: ok")
        else:
            notes.append(f"{rel}: skipped (no syntax checker for {suffix or 'no extension'})")
    if not files:
        notes.append("no files to check")
    return not errors, "\n".join(notes), "\n".join(errors)


class _ImportUsage(ast.NodeVisitor):
    """Collect imported names, used names and bare excepts."""

    def __init__(self) -> None:
        self.imports: list[tuple[str, int, str]] = []  # (bound name, line, display)
        self.used: set[str] = set()
        self.bare_excepts: list[int] = []
        self.exported: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.imports.append((bound, node.lineno, alias.name))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "__future__":
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            # "from x import y as y" is the explicit re-export idiom.
            if alias.asname is not None and alias.asname == alias.name:
                continue
            self.imports.append((bound, node.lineno, alias.name))

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            self.used.add(root.id)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.bare_excepts.append(node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            self.exported.add(elt.value)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Names used only in string annotations ("Path") still count as used.
        if isinstance(node.value, str) and node.value.isidentifier():
            self.used.add(node.value)


def builtin_lint(source: str, filename: str = "<string>") -> list[str]:
    """Light, dependency-free Python linter used when ruff is unavailable.

    Reports syntax-level errors (via ``compile``, which also catches things
    ``ast.parse`` accepts such as ``return`` outside a function), unused
    imports and bare ``except:`` clauses. Returns ``"file:line: CODE msg"``
    strings; an empty list means clean.
    """
    try:
        tree = ast.parse(source, filename=filename)
        compile(source, filename, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return [f"{filename}:{exc.lineno or 0}: E999 SyntaxError: {exc.msg}"]
    visitor = _ImportUsage()
    visitor.visit(tree)
    problems: list[str] = []
    is_init = Path(filename).name == "__init__.py"
    for bound, line, display in visitor.imports:
        if bound in visitor.used or bound in visitor.exported or bound == "_" or is_init:
            continue
        problems.append(f"{filename}:{line}: F401 '{display}' imported but unused")
    for line in visitor.bare_excepts:
        problems.append(f"{filename}:{line}: E722 do not use bare 'except'")
    return sorted(problems, key=lambda p: int(p.split(":")[1]))


def _gate_linter(
    files: list[tuple[str, Path]], workspace: Path, mode: LinterMode
) -> tuple[bool, str, str]:
    py_files = [(rel, path) for rel, path in files if path.suffix.lower() == ".py"]
    if not py_files:
        return True, "no Python files to lint", ""
    ruff = shutil.which("ruff") if mode in ("auto", "ruff") else None
    if mode == "ruff" and ruff is None:
        return False, "", "ruff requested but not installed"
    if ruff is not None:
        cmd = [
            ruff, "check", "--isolated", "--no-cache", "--quiet",
            "--select", _RUFF_RULES, "--output-format", "concise",
            *[rel for rel, _ in py_files],
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=workspace, capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            return False, "", "ruff timed out"
        output = proc.stdout.strip()
        if proc.returncode == 0:
            return True, "ruff: clean" + (f"\n{output}" if output else ""), proc.stderr
        return False, output, proc.stderr.strip() or "ruff reported problems"
    problems: list[str] = []
    for rel, path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{rel}:0: E902 cannot decode: {exc}")
            continue
        problems.extend(builtin_lint(source, rel))
    if problems:
        return False, "\n".join(problems), "builtin linter reported problems"
    return True, "builtin linter: clean", ""


def _mypy_command() -> list[str] | None:
    """argv prefix that runs mypy, or None when it is not installed.

    Prefers mypy importable by this interpreter (the Foundry venv), run with
    ``-I`` (isolated mode) so a ``mypy.py`` or ``mypy/`` package in the
    workspace (the cwd of the run) is never imported instead; then a ``mypy``
    executable on ``PATH``, as an absolute path.
    """
    try:
        if importlib.util.find_spec("mypy") is not None:
            return [sys.executable, "-I", "-m", "mypy"]
    except (ImportError, ValueError):
        pass
    exe = shutil.which("mypy")
    return [str(Path(exe).resolve())] if exe else None


def _inside(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


_MYPY_LINE = re.compile(r"^(?P<path>[^:\n]+?):(?P<line>\d+)(?::\d+)?: (?P<kind>error|note|warning): ")


def _gate_typecheck(
    files: list[tuple[str, Path]], workspace: Path, mode: TypecheckMode, timeout: float
) -> tuple[bool, str, str]:
    if mode == "off":
        return True, "typecheck skipped (FOUNDRY_TYPECHECK=off)", ""
    py_files = [rel for rel, path in files if path.suffix.lower() in (".py", ".pyi")]
    if not py_files:
        return True, "no Python files to typecheck", ""
    base = _mypy_command()
    if base is not None and (not Path(base[0]).is_absolute() or _inside(base[0], workspace)):
        # never run a mypy that lives in (or resolves into) the workspace
        base = None
    if base is None:
        if mode == "mypy":
            return False, "", "mypy requested but not installed (pip install mypy)"
        return True, "typecheck skipped (mypy not installed)", ""
    cmd = [
        *base,
        "--config-file=",
        "--ignore-missing-imports",
        "--follow-imports=silent",
        "--explicit-package-bases",
        "--no-error-summary",
        "--no-color-output",
        "--no-pretty",
        "--show-column-numbers",
        f"--cache-dir={os.devnull}",
        "--",
        *py_files,
    ]
    env = {k: v for k, v in os.environ.items() if k not in ("MYPYPATH", "MYPY_CACHE_DIR")}
    try:
        proc = subprocess.run(
            cmd, cwd=workspace, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return True, f"typecheck skipped (mypy timed out after {timeout:g}s)", ""
    except OSError as exc:
        return True, f"typecheck skipped (mypy could not start: {exc})", ""
    output = proc.stdout.strip()
    if proc.returncode not in (0, 1):
        reason = (proc.stderr.strip() or output).splitlines()
        first = reason[0] if reason else f"exit code {proc.returncode}"
        return True, f"typecheck skipped (mypy could not run: {first})\n{output}".rstrip(), proc.stderr
    changed = {_normalise_rel(rel) for rel in py_files}
    errors: list[str] = []
    for line in output.splitlines():
        match = _MYPY_LINE.match(line)
        if not match or match.group("kind") != "error":
            continue
        try:
            rel = _normalise_rel(match.group("path"))
        except DiffApplyError:
            continue
        if rel in changed:
            errors.append(line)
    if errors:
        return False, "\n".join(errors), f"mypy: {len(errors)} type error(s) in changed files"
    return True, "mypy: clean" + (f"\n{output}" if output else ""), ""


def _split_cmd(test_cmd: str | Sequence[str]) -> list[str]:
    if isinstance(test_cmd, str):
        parts = shlex.split(test_cmd, posix=os.name != "nt")
    else:
        parts = [str(p) for p in test_cmd]
    if parts and parts[0] in ("python", "python3", "py"):
        parts[0] = sys.executable
    return parts


def _gate_tests(
    test_cmd: str | Sequence[str] | None, workspace: Path, timeout: float
) -> tuple[bool, str, str]:
    if not test_cmd:
        return True, "skipped (no test command given)", ""
    cmd = _split_cmd(test_cmd)
    if not cmd:
        return True, "skipped (empty test command)", ""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return False, out, f"tests timed out after {timeout:g}s"
    except OSError as exc:
        return False, "", f"cannot run test command {cmd[0]!r}: {exc}"
    if proc.returncode == 0:
        return True, proc.stdout, proc.stderr
    stderr = proc.stderr
    if not stderr.strip():
        stderr = f"test command exited with code {proc.returncode}"
    return False, proc.stdout, stderr


def _param_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args)]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def _parse_spec(spec: str) -> tuple[list[str], list[str] | None]:
    """Parse ``"Cls.method(a, *, b)"`` into ``(["Cls", "method"], ["a", "*", "b"])``."""
    spec = spec.strip()
    params: list[str] | None = None
    if "(" in spec:
        name, _, rest = spec.partition("(")
        if not rest.endswith(")"):
            raise ValueError(f"bad schema target {spec!r}")
        fn = ast.parse(f"def _x({rest[:-1]}): pass").body[0]
        assert isinstance(fn, ast.FunctionDef)
        params = _param_names(fn.args)
    else:
        name = spec
    dotted = [part.strip() for part in name.split(".")]
    if not all(part.isidentifier() for part in dotted):
        raise ValueError(f"bad schema target {spec!r}")
    return dotted, params


def _find_def(body: list[ast.stmt], name: str) -> ast.AST | None:
    found: ast.AST | None = None
    for node in body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            found = node  # last definition wins, like at runtime
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                found = node
    return found


def _gate_schema(
    schema_targets: Mapping[str, Sequence[str]] | None, workspace: Path
) -> tuple[bool, str, str]:
    if not schema_targets:
        return True, "skipped (no schema targets given)", ""
    errors: list[str] = []
    notes: list[str] = []
    for raw_path, specs in schema_targets.items():
        try:
            rel = _normalise_rel(raw_path)
            path = _resolve_inside(workspace, rel)
        except DiffApplyError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"{rel}: file not found")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: cannot parse: {exc}")
            continue
        for spec in specs:
            try:
                dotted, params = _parse_spec(spec)
            except (ValueError, SyntaxError) as exc:
                errors.append(f"{rel}: {exc}")
                continue
            body: list[ast.stmt] = tree.body
            node: ast.AST | None = None
            for depth, part in enumerate(dotted):
                node = _find_def(body, part)
                if node is None:
                    break
                if depth < len(dotted) - 1:
                    if not isinstance(node, ast.ClassDef):
                        node = None
                        break
                    body = node.body
            label = ".".join(dotted)
            if node is None:
                errors.append(f"{rel}: missing {label}")
                continue
            if params is not None:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    errors.append(f"{rel}: {label} is not a function")
                    continue
                actual = _param_names(node.args)
                if actual != params:
                    errors.append(
                        f"{rel}: {label} signature mismatch: expected ({', '.join(params)}), "
                        f"found ({', '.join(actual)})"
                    )
                    continue
            notes.append(f"{rel}: {spec} ok")
    return not errors, "\n".join(notes), "\n".join(errors)


# --------------------------------------------------------------------------
# Public runner
# --------------------------------------------------------------------------


def run_gates(
    submission: BuilderSubmission,
    workspace: str | Path,
    *,
    checks: Sequence[GateCheck] = ALL_CHECKS,
    test_cmd: str | Sequence[str] | None = None,
    schema_targets: Mapping[str, Sequence[str]] | None = None,
    test_timeout: float = 300.0,
    linter: LinterMode = "auto",
    typecheck: TypecheckMode = "auto",
    typecheck_timeout: float = 180.0,
    short_circuit: bool = True,
) -> list[DeterministicGateResult]:
    """Run the deterministic gates on a submission already applied to ``workspace``.

    Args:
        submission: The builder's submission; ``modified_files`` selects the
            files checked by the syntax and linter gates.
        workspace: Directory containing the patched files.
        checks: Gates to run, in order.
        test_cmd: Command for the tests gate (string or argv list). A leading
            ``python`` is replaced by the current interpreter. ``None`` skips
            the gate (it passes with a note).
        schema_targets: ``{path: ["Class", "func(a, b)", "Class.method(self, x)"]}``.
        test_timeout: Seconds before the test command is killed (gate fails).
        linter: ``"auto"`` (ruff if installed, else built-in), ``"ruff"`` or
            ``"builtin"``.
        typecheck: ``"auto"`` (mypy if installed, else skipped), ``"mypy"``
            (fail when mypy is missing) or ``"off"``.
        typecheck_timeout: Seconds before mypy is killed (the gate is then
            skipped with a note).
        short_circuit: Stop after the first failing gate (default).

    Returns:
        One :class:`DeterministicGateResult` per gate that ran, in order.
    """
    root = Path(workspace)
    results: list[DeterministicGateResult] = []
    files: list[tuple[str, Path]] | None = None
    files_error = ""
    try:
        files = _submission_files(submission, root)
    except DiffApplyError as exc:
        files_error = str(exc)

    for check in checks:
        started = time.monotonic()
        if check in ("syntax", "linter", "typecheck") and files is None:
            passed, out, err = False, "", files_error
        elif check == "syntax":
            passed, out, err = _gate_syntax(files or [])
        elif check == "linter":
            passed, out, err = _gate_linter(files or [], root, linter)
        elif check == "typecheck":
            passed, out, err = _gate_typecheck(files or [], root, typecheck, typecheck_timeout)
        elif check == "tests":
            passed, out, err = _gate_tests(test_cmd, root, test_timeout)
        elif check == "schema":
            passed, out, err = _gate_schema(schema_targets, root)
        else:
            raise ValueError(f"unknown gate {check!r}")
        skipped = [line for line in out.splitlines() if "skipped" in line]
        note = "; ".join(skipped) if skipped else None
        results.append(_result(submission, check, passed, started, out, err, note))
        if not passed and short_circuit:
            break
    return results


def run_gates_outcome(
    submission: BuilderSubmission,
    workspace: str | Path,
    **kwargs: object,
) -> GateOutcome:
    """:func:`run_gates` wrapped in a :class:`GateOutcome`."""
    return GateOutcome.from_results(run_gates(submission, workspace, **kwargs))  # type: ignore[arg-type]


class ValidationGates:
    """Configured gate runner used by the builder worker.

    ``validate`` applies the submission's diff into the workspace (refusing
    files outside ``allowed_files``) and then runs the gates. A diff that
    cannot be applied counts as a failed ``syntax`` gate so the builder gets
    the reason back as a fix hint.
    """

    def __init__(
        self,
        *,
        checks: Sequence[GateCheck] = ALL_CHECKS,
        test_cmd: str | Sequence[str] | None = None,
        schema_targets: Mapping[str, Sequence[str]] | None = None,
        test_timeout: float = 300.0,
        linter: LinterMode = "auto",
        typecheck: TypecheckMode = "auto",
        typecheck_timeout: float = 180.0,
    ) -> None:
        self.checks = tuple(checks)
        self.typecheck: TypecheckMode = typecheck
        self.typecheck_timeout = typecheck_timeout
        self.test_cmd = test_cmd
        self.schema_targets = dict(schema_targets) if schema_targets else None
        self.test_timeout = test_timeout
        self.linter: LinterMode = linter

    def check(self, submission: BuilderSubmission, workspace: str | Path) -> GateOutcome:
        """Run the gates on an already-patched workspace."""
        return GateOutcome.from_results(
            run_gates(
                submission,
                workspace,
                checks=self.checks,
                test_cmd=self.test_cmd,
                schema_targets=self.schema_targets,
                test_timeout=self.test_timeout,
                linter=self.linter,
                typecheck=self.typecheck,
                typecheck_timeout=self.typecheck_timeout,
            )
        )

    def validate(
        self,
        submission: BuilderSubmission,
        workspace: str | Path,
        *,
        allowed_files: Iterable[str] | None = None,
    ) -> GateOutcome:
        """Apply ``submission.diff`` to ``workspace`` and run the gates."""
        started = time.monotonic()
        try:
            apply_diff(submission.diff, workspace, allowed_files=allowed_files)
        except (DiffApplyError, UnicodeDecodeError, OSError) as exc:
            failure = _result(
                submission, "syntax", False, started, "", f"diff could not be applied: {exc}"
            )
            return GateOutcome(passed=False, results=[failure], first_failure=failure)
        return self.check(submission, workspace)

    async def validate_async(
        self,
        submission: BuilderSubmission,
        workspace: str | Path,
        *,
        allowed_files: Iterable[str] | None = None,
    ) -> GateOutcome:
        """:meth:`validate` in a worker thread so the event loop keeps running."""
        return await asyncio.to_thread(
            self.validate, submission, workspace, allowed_files=allowed_files
        )
