"""Check that the open Foundry pull requests still work together on the latest base.

Two PRs built from the same base can each pass alone and still break once both are
merged. :func:`run_combined` merges them on a throwaway detached worktree of the
latest base and compares the whole test suite with the base's. The user's working
tree is never touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundry.git_publisher import GitPublisher, default_runner, valid_ref
from foundry.suite_check import run_suite

log = logging.getLogger("foundry.combined_check")

__all__ = [
    "COMMENT_MARKER", "CombinedChecker", "CombinedResult", "combined_check_enabled", "comment_body", "fingerprint",
    "open_foundry_prs", "run_combined", "status_line",
]

_GIT_TIMEOUT_S = 300
_GH_TIMEOUT_S = 120
#: Hidden first line of every comment this check posts.
COMMENT_MARKER = "<!-- foundry-combined-check -->"
#: Queue kv entry: {"fingerprints": {base: fingerprint}, "status": {PR number: last status line posted},
#: "conflicts": {PR number: head commit already told it conflicts with its base},
#: "green": {PR number: fingerprint of the combined run it passed}, "merged": {PR number: head merged automatically}}.
_STATE_KEY = "combined_check:last"
#: Status line of a PR that passed the combined check.
_OK_LINE = "✅ Works together with the other open Foundry PRs"
#: The merge states auto-merge accepts: UNSTABLE means a check is failing.
_AUTO_MERGE_STATES = ("CLEAN", "HAS_HOOKS")
_AUTO_MERGE_COMMENT = "Merged automatically: tests/docs only, green and mergeable."
#: File name endings that are always a test or a doc.
_TEST_DOC_SUFFIXES = ("_test.py", ".test.mjs", ".test.js", ".test.ts", ".md")
#: Failing test ids named in a status line.
_MAX_IDS_SHOWN = 10
#: Applied to every git call: files as stored, readable paths, and none of the user's
#: hooks or signing prompts on merges nobody will ever keep.
_GIT_CONFIG = (
    "-c", "core.autocrlf=false",
    "-c", "core.quotepath=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "commit.gpgsign=false",
)
_MERGE_IDENTITY = ("-c", "user.name=Foundry", "-c", "user.email=foundry@localhost")

#: Failures of finished base runs, by (repo, base sha, FOUNDRY_SUITE_CMD).
_BASE_RESULTS: dict[tuple[str, str, str], frozenset[str]] = {}
_BASE_RESULTS_MAX = 16
_BASE_RESULTS_LOCK = threading.Lock()


@dataclass
class CombinedResult:
    """Outcome of merging the open PRs of one base together and running the suite."""

    ok: bool
    ran: bool
    base: str
    base_sha: str = ""
    #: The PRs that merged in, in merge order.
    prs: list[int] = field(default_factory=list)
    #: PR number -> the files that conflicted when it was merged on top of the earlier ones.
    conflicts: dict[int, list[str]] = field(default_factory=dict)
    new_failures: list[str] = field(default_factory=list)
    summary: str = ""
    checked_at: str = ""
    fingerprint: str = ""


class _GitFailed(Exception):
    """A git step the check cannot go on without failed."""


def fingerprint(base_sha: str, prs: list[dict[str, Any]]) -> str:
    """Identifies a base commit plus the exact commits of a set of PRs."""
    heads = sorted((int(pr["number"]), str(pr.get("headRefOid") or "")) for pr in prs)
    return hashlib.sha256(json.dumps([base_sha, heads]).encode("utf-8")).hexdigest()


def _git(git: str, *args: str) -> tuple[int, str, str]:
    """Run git; returns (returncode, stdout, stderr)."""
    # files, not pipes: on Windows a grandchild holding a pipe open makes subprocess.run
    # wait forever after the timeout killed git (see foundry.suite_check.run_suite)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.run([git, *_GIT_CONFIG, *args], env=GitPublisher._env(), stdin=subprocess.DEVNULL,
                              stdout=out, stderr=err, timeout=_GIT_TIMEOUT_S, check=False)
        out.seek(0)
        err.seek(0)
        return proc.returncode, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def _git_ok(git: str, *args: str, what: str) -> str:
    """Run git and return its stdout; raises :class:`_GitFailed` on a non-zero exit."""
    rc, out, err = _git(git, *args)
    if rc != 0:
        raise _GitFailed(f"{what} failed: {(err or out).strip()[-300:]}")
    return out


def _remove_worktrees(git: str, repo_root: Path, tmp_root: Path, trees: tuple[Path, ...]) -> None:
    """Remove the check's worktrees and folder. Never raises."""

    def best_effort(*args: str) -> None:
        try:
            _git(git, "-C", str(repo_root), *args)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("combined check: git %s failed: %s", args[1], exc)

    for tree in trees:
        if tree.exists():
            best_effort("worktree", "remove", "--force", str(tree))
    shutil.rmtree(tmp_root, ignore_errors=True)
    best_effort("worktree", "prune")


def _base_failures(git: str, repo_root: Path, base_sha: str, tree: Path, timeout_s: float) -> tuple[set[str], bool]:
    """(failing tests, completed) of the bare base; a finished run is reused for the same commit."""
    key = (str(repo_root), base_sha, os.environ.get("FOUNDRY_SUITE_CMD", ""))
    with _BASE_RESULTS_LOCK:
        cached = _BASE_RESULTS.get(key)
    if cached is not None:
        return set(cached), True
    _git_ok(git, "-C", str(repo_root), "worktree", "add", "--detach", str(tree), base_sha, what="adding the base worktree")
    failures, completed = run_suite(tree, timeout_s=timeout_s)
    if completed:
        with _BASE_RESULTS_LOCK:
            _BASE_RESULTS[key] = frozenset(failures)
            while len(_BASE_RESULTS) > _BASE_RESULTS_MAX:
                del _BASE_RESULTS[next(iter(_BASE_RESULTS))]
    return failures, completed


def _merge_pr(git: str, tree: Path, number: int) -> list[str] | None:
    """Merge PR ``number`` into ``tree``; returns None when merged, else the conflicting files."""
    where = ("-C", str(tree))
    _git_ok(git, *where, "fetch", "--quiet", "origin", f"pull/{number}/head", what=f"fetching PR #{number}")
    if _git(git, *_MERGE_IDENTITY, *where, "merge", "--no-edit", "--no-ff", "FETCH_HEAD")[0] == 0:
        return None
    files = _git(git, *where, "diff", "--name-only", "--diff-filter=U")[1].splitlines()
    if _git(git, *where, "merge", "--abort")[0] != 0:
        # nothing to abort, or a stuck merge: the next PR still needs a clean tree
        _git_ok(git, *where, "reset", "--hard", "--quiet", "HEAD", what=f"cleaning up after PR #{number}")
    return files


def run_combined(repo_root: Path, base: str, prs: list[dict[str, Any]], *, timeout_s: float, workdir: Path,
                 git: str = "git") -> CombinedResult:
    """Merge the PRs of ``base`` onto its latest commit and run the whole suite there.

    Blocking: call it in a thread. PRs merge in ascending number order; one that
    conflicts is recorded and left out. Only failures the base does not have count.
    A check that cannot finish gives ``ran=False`` and is never a failure by itself
    (conflicts already found still are).
    """
    repo_root = Path(repo_root).resolve()
    workdir = Path(workdir).resolve()
    selected = sorted((pr for pr in prs if pr.get("baseRefName") == base and isinstance(pr.get("number"), int)),
                      key=lambda pr: pr["number"])
    base_sha = ""
    merged: list[int] = []
    conflicts: dict[int, list[str]] = {}

    def result(ok: bool, ran: bool, summary: str, new_failures: list[str] | None = None) -> CombinedResult:
        return CombinedResult(
            ok=ok, ran=ran, base=base, base_sha=base_sha, prs=merged, conflicts=conflicts,
            new_failures=new_failures or [], summary=summary,
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            fingerprint=fingerprint(base_sha, selected),
        )

    def skipped(why: str) -> CombinedResult:
        return result(not conflicts, False, f"combined check could not finish: {why}")

    if not valid_ref(base):
        return skipped(f"{str(base)[:80]!r} is not a plain branch name")
    if not selected:
        return skipped(f"no open pull requests target {base}")

    tmp_root: Path | None = None
    trees: tuple[Path, ...] = ()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        tmp_root = Path(tempfile.mkdtemp(prefix="combined-", dir=workdir))
        merged_tree, base_tree = tmp_root / "merged", tmp_root / "base"
        trees = (merged_tree, base_tree)
        repo = ("-C", str(repo_root))
        _git_ok(git, *repo, "fetch", "--quiet", "origin", base, what=f"fetching {base}")
        _git_ok(git, *repo, "worktree", "add", "--detach", str(merged_tree), f"refs/remotes/origin/{base}",
                what="adding the worktree")
        base_sha = _git_ok(git, "-C", str(merged_tree), "rev-parse", "HEAD", what="reading the base commit").strip()
        for pr in selected:
            files = _merge_pr(git, merged_tree, pr["number"])
            if files is None:
                merged.append(pr["number"])
            else:
                conflicts[pr["number"]] = files
        head_failures, completed = run_suite(merged_tree, timeout_s=timeout_s)
        if not completed:
            return skipped("the suite did not complete with the PRs merged")
        base_failures: set[str] = set()
        if head_failures:  # without failures on the merged tree there is nothing to compare
            base_failures, completed = _base_failures(git, repo_root, base_sha, base_tree, timeout_s)
            if not completed:
                return skipped("the suite did not complete on the base")
    except (OSError, subprocess.SubprocessError, ValueError, _GitFailed) as exc:  # ValueError: a bad FOUNDRY_SUITE_CMD
        return skipped(f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}")
    finally:
        if tmp_root is not None:
            _remove_worktrees(git, repo_root, tmp_root, trees)

    new_failures = sorted(head_failures - base_failures)
    problems = [f"#{number} conflicts in {', '.join(files) or 'unknown files'}" for number, files in conflicts.items()]
    if new_failures:
        problems.append(f"{len(new_failures)} new test failure(s): {', '.join(new_failures[:10])}")
    if problems:
        return result(False, True, "; ".join(problems), new_failures)
    return result(True, True, f"{len(merged)} PR(s) work together on {base}")


def _env_seconds(name: str, default: float, minimum: float) -> float:
    """A number of seconds from the environment (``default`` when unset or not a finite number)."""
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return max(value, minimum) if math.isfinite(value) else default


def combined_check_enabled() -> bool:
    """False when FOUNDRY_COMBINED_CHECK switches the check off (it is on by default)."""
    return os.environ.get("FOUNDRY_COMBINED_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")


async def open_foundry_prs(gh: str, repo_root: Path, runner: Any = default_runner, *,
                           merge_state: bool = False) -> list[dict[str, Any]]:
    """The open pull requests on ``foundry/`` branches; ``[]`` (and a warning) when gh fails.

    ``merge_state`` also asks for ``mergeable`` and ``mergeStateStatus``.
    """
    fields = "number,title,headRefName,headRefOid,baseRefName"
    if merge_state:
        fields += ",mergeable,mergeStateStatus"
    argv = [gh, "pr", "list", "--state", "open", "--limit", "50", "--json", fields]
    try:
        rc, out, err = await runner(argv, cwd=str(repo_root), timeout=_GH_TIMEOUT_S, env=GitPublisher._env())
        if rc != 0:
            log.warning("combined check: gh pr list failed: %s", (err or out or f"exit {rc}").strip()[-300:])
            return []
        rows = json.loads(out or "[]")
    except Exception as exc:  # noqa: BLE001 - the check is optional: the next round tries again
        log.warning("combined check: could not list the open pull requests: %s", exc)
        return []
    if not isinstance(rows, list):
        log.warning("combined check: gh pr list did not return a list")
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("headRefName") or "").startswith("foundry/")]


def _file_list(files: list[str]) -> str:
    return ", ".join(files) or "unknown files"


def status_line(result: CombinedResult, pr_number: int) -> str:
    """What the check says about one PR; a new comment is posted only when this line changes."""
    own = result.conflicts.get(pr_number)
    if own is not None:
        earlier = [f"#{n}" for n in result.prs if n < pr_number]
        return f"❌ Conflicts with {', '.join(earlier) or result.base} in {_file_list(own)}"
    if pr_number in result.prs:
        later = [f"#{n} in {_file_list(files)}" for n, files in sorted(result.conflicts.items()) if n > pr_number]
        if later:
            return "❌ Conflicts with " + "; ".join(later)
    if not result.ran:
        reason = result.summary.removeprefix("combined check could not finish: ")
        return f"⚠️ Combined check could not run the tests: {reason}"
    if result.new_failures:
        ids = ", ".join(result.new_failures[:_MAX_IDS_SHOWN])
        hidden = len(result.new_failures) - _MAX_IDS_SHOWN
        more = f" and {hidden} more" if hidden > 0 else ""
        return f"❌ New test failures when merged with the other open Foundry PRs: {ids}{more}"
    return _OK_LINE


def _is_test_or_doc(path: str) -> bool:
    """Whether a repo-relative path is a test or a doc as far as auto-merge is concerned."""
    *folders, name = path.split("/")
    if name.lower() == "claude.md" or ".claude" in folders:
        return False
    return ("tests" in folders or "test" in folders or folders[:1] == ["docs"] or name.endswith(_TEST_DOC_SUFFIXES)
            or (name.startswith("test_") and name.endswith(".py")))


def _only_tests_or_docs(files: Any) -> bool:
    """Whether a ``files`` list from gh names at least one file and only tests or docs."""
    if not isinstance(files, list) or not files:
        return False
    paths = [row.get("path") if isinstance(row, dict) else None for row in files]
    return all(isinstance(path, str) and _is_test_or_doc(path) for path in paths)


def comment_body(result: CombinedResult, pr_number: int) -> str:
    """The Markdown comment for one PR: hidden marker, status line, then what was checked."""
    merged = ", ".join(f"#{n}" for n in result.prs) or "none"
    lines = [COMMENT_MARKER, status_line(result, pr_number), "", f"- Merged in: {merged}"]
    if result.conflicts:
        lines.append("- Left out (conflicts): " + ", ".join(f"#{n}" for n in sorted(result.conflicts)))
    lines.append(f"- Base: `{result.base}` at `{result.base_sha[:7] or 'unknown'}`")
    lines.append(f"- Checked: {result.checked_at}")
    return "\n".join(lines) + "\n"


class CombinedChecker:
    """Regularly checks that the open Foundry PRs of each base still work together.

    Args:
        service: the running :class:`~foundry.serve.Service`.
        interval_s: seconds between rounds (default FOUNDRY_COMBINED_INTERVAL_S).
        runner / gh / git: how commands are run (tests pass fakes).
    """

    def __init__(self, service: Any, *, interval_s: float | None = None, runner: Any = None, gh: str | None = None,
                 git: str = "git") -> None:
        self.service = service
        self.interval_s = _env_seconds("FOUNDRY_COMBINED_INTERVAL_S", 600.0, 60.0) if interval_s is None else interval_s
        self.timeout_s = _env_seconds("FOUNDRY_SUITE_TIMEOUT_S", 900.0, 1.0)
        self.runner = runner or default_runner
        self.gh = gh or shutil.which("gh") or "gh"
        self.git = git
        self.rounds = 0
        self.last_error: str | None = None
        self.last_result: CombinedResult | None = None
        self._task: asyncio.Task | None = None
        self._state: dict[str, dict[str, str]] | None = None
        #: base -> (fingerprint, result) of the last run of this process.
        self._latest: dict[str, tuple[str, CombinedResult]] = {}

    def start(self) -> None:
        """Start the background loop (no-op when it is already running)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background loop. A check already running in its thread cleans up after itself."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the check must never take the service down
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("combined check: round failed: %s", self.last_error)

    async def run_once(self) -> None:
        """One round: follow the bases, check each base with two or more open Foundry PRs that moved, then auto-merge."""
        if not self.service.running:
            return
        self.rounds += 1
        prs = await open_foundry_prs(self.gh, Path(self.service.repo), self.runner, merge_state=True)
        if not prs:  # none open, or gh failed: what was posted is still remembered
            return
        state = self._load_state()
        await self._follow_base(prs)
        groups: dict[str, list[dict[str, Any]]] = {}
        for pr in prs:
            base = pr.get("baseRefName")
            if isinstance(pr.get("number"), int) and isinstance(base, str) and valid_ref(base):
                groups.setdefault(base, []).append(pr)
        failed = False
        checked: dict[str, str] = {}  # base -> fingerprint of its current base and heads, when it could be read
        for base, group in sorted(groups.items()):
            if len(group) < 2:  # a single PR already passed the whole-suite gate before it opened
                continue
            if not self.service.running:
                return
            try:
                checked[base] = await self._check_base(base, group)
            except Exception as exc:  # noqa: BLE001 - one bad base must not starve the others
                failed = True
                self.last_error = f"{base}: {type(exc).__name__}: {exc}"
                log.warning("combined check: %s", self.last_error)
        if not failed:
            self.last_error = None
        numbers = {str(pr.get("number")) for pr in prs}
        state["status"] = {number: line for number, line in state["status"].items() if number in numbers}
        state["green"] = {number: mark for number, mark in state["green"].items() if number in numbers}
        self._save_state()
        if getattr(getattr(self.service, "config", None), "auto_merge", "off") == "tests_docs":
            await self._auto_merge(groups, checked)
            self._save_state()

    async def _auto_merge(self, groups: dict[str, list[dict[str, Any]]], checked: dict[str, str]) -> None:
        """Merge the green, mergeable PRs that only touch tests or docs: one attempt per PR and round."""
        merged = self._load_state()["merged"]
        for base, group in sorted(groups.items()):
            for pr in sorted(group, key=lambda row: row["number"]):
                number = pr["number"]
                if not self.service.running:
                    return
                if str(number) in merged or not self._merge_ready(pr, len(group) == 1, checked.get(base, "")):
                    continue
                try:
                    await self._merge_if_tests_docs(number, str(pr["headRefOid"]))
                except Exception as exc:  # noqa: BLE001 - one bad PR must not stop the round
                    log.warning("combined check: could not auto-merge #%d: %s: %s", number, type(exc).__name__, exc)

    def _merge_ready(self, pr: dict[str, Any], alone: bool, current: str) -> bool:
        """Mergeable, and alone on its base or green in a combined run of exactly ``current``."""
        if (pr.get("mergeable") != "MERGEABLE" or pr.get("mergeStateStatus") not in _AUTO_MERGE_STATES
                or not pr.get("headRefOid")):
            return False
        return alone or (bool(current) and self._load_state()["green"].get(str(pr["number"])) == current)

    async def _merge_if_tests_docs(self, number: int, head: str) -> None:
        """Squash-merge the PR when every file is a test or a doc; ``head`` pins the commit that was judged."""
        rc, out, err = await self._run([self.gh, "pr", "view", str(number), "--json", "files"], _GH_TIMEOUT_S)
        if rc != 0:
            log.warning("combined check: could not read the files of #%d: %s", number,
                        (err or out or f"exit {rc}").strip()[-300:])
            return
        if not _only_tests_or_docs(json.loads(out or "{}").get("files")):
            return
        rc, out, err = await self._run([self.gh, "pr", "merge", str(number), "--squash", "--match-head-commit", head],
                                       _GH_TIMEOUT_S)
        if rc != 0:
            log.warning("combined check: could not auto-merge #%d: %s", number,
                        (err or out or f"exit {rc}").strip()[-300:])
            return
        self._load_state()["merged"][str(number)] = head
        self._save_state()  # a restart must never merge or comment on it again
        await self._comment(number, f"{COMMENT_MARKER}\n{_AUTO_MERGE_COMMENT}\n")
        log.info("combined check: merged #%d automatically", number)

    async def _follow_base(self, prs: list[dict[str, Any]]) -> None:
        """Update the PRs that are behind their base and flag the ones that conflict with it.

        An updated PR gets a new head, so the next round's combined test picks it up.
        """
        state = self._load_state()
        numbers = {str(pr.get("number")) for pr in prs}
        state["conflicts"] = {number: head for number, head in state["conflicts"].items() if number in numbers}
        failures: list[str] = []
        for pr in prs:
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            if not self.service.running:
                break
            try:
                failure = await self._follow_pr(number, pr)
            except Exception as exc:  # noqa: BLE001 - one bad PR must not stop the round
                failure = f"{type(exc).__name__}: {exc}"
            if failure:
                failures.append(f"#{number}: {failure}")
        if failures:
            log.warning("combined check: could not follow the base on %d PR(s): %s", len(failures), "; ".join(failures))
        self._save_state()  # the combined test can take long: a restart must not repeat these comments

    async def _follow_pr(self, number: int, pr: dict[str, Any]) -> str | None:
        """Act on one PR's merge state; returns why updating it failed, None otherwise."""
        mergeable = pr.get("mergeable")
        if mergeable == "CONFLICTING":
            conflicts = self._load_state()["conflicts"]
            head = str(pr.get("headRefOid") or "")
            if conflicts.get(str(number)) != head:
                body = (f"{COMMENT_MARKER}\nConflicts with the latest {pr.get('baseRefName')}: "
                        "this piece needs a rebuild on the new base.\n")
                if await self._comment(number, body):
                    conflicts[str(number)] = head
            return None
        if mergeable != "MERGEABLE" or pr.get("mergeStateStatus") != "BEHIND":
            return None
        rc, out, err = await self._run([self.gh, "pr", "update-branch", str(number)], _GH_TIMEOUT_S)
        if rc != 0:
            return (err or out or f"exit {rc}").strip()[-300:]
        log.info("combined check: updated #%d with its base", number)
        return None

    async def _check_base(self, base: str, group: list[dict[str, Any]]) -> str:
        """Check one base unless nothing moved; returns the current fingerprint, empty when the base is unreadable."""
        sha = await self._base_sha(base)
        if not sha:
            return ""
        state = self._load_state()
        current = fingerprint(sha, group)
        if state["fingerprints"].get(base) == current:
            latest = self._latest.get(base)
            if latest is not None and latest[0] == current:
                await self._post(latest[1], group)  # only comments that could not be posted earlier
            return current
        result = await asyncio.to_thread(run_combined, Path(self.service.repo), base, group, timeout_s=self.timeout_s,
                                         workdir=self._workdir(), git=self.git)
        state["fingerprints"][base] = current
        self._latest[base] = (current, result)
        self.last_result = result
        self._remember_green(result, sha, current, group)
        posted = await self._post(result, group)
        self._save_state()
        log.info("combined check: %s: %s (%d PR(s), %d comment(s) posted)", base, result.summary, len(group), posted)
        return current

    def _remember_green(self, result: CombinedResult, sha: str, current: str, group: list[dict[str, Any]]) -> None:
        """Record which PRs passed this run, whether or not their comments can be posted."""
        green = self._load_state()["green"]
        for pr in group:
            number = pr["number"]
            passed = number in result.prs and status_line(result, number) == _OK_LINE
            if passed and result.ran and result.base_sha == sha:
                green[str(number)] = current
            else:
                green.pop(str(number), None)

    async def _post(self, result: CombinedResult, group: list[dict[str, Any]]) -> int:
        """Comment on each PR whose status line differs from the last one posted; returns how many."""
        status = self._load_state()["status"]
        posted = 0
        for number in sorted(pr["number"] for pr in group):
            line = status_line(result, number)
            if status.get(str(number)) != line and await self._comment(number, comment_body(result, number)):
                status[str(number)] = line
                posted += 1
        return posted

    async def _comment(self, number: int, body: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="combined-comment-") as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(body, encoding="utf-8")
            rc, out, err = await self._run([self.gh, "pr", "comment", str(number), "--body-file", str(path)],
                                           _GH_TIMEOUT_S)
        if rc != 0:
            log.warning("combined check: could not comment on #%d: %s", number, (err or out or f"exit {rc}").strip()[-300:])
        return rc == 0

    async def _base_sha(self, base: str) -> str:
        """The commit of ``origin/<base>`` after a fetch; empty (and a warning) when git fails."""
        rc, out, err = await self._run([self.git, "fetch", "--quiet", "origin", base], _GIT_TIMEOUT_S)
        if rc == 0:
            rc, out, err = await self._run([self.git, "rev-parse", "--verify", f"refs/remotes/origin/{base}"],
                                           _GIT_TIMEOUT_S)
        sha = out.strip() if rc == 0 else ""
        if not sha:
            log.warning("combined check: could not read origin/%s: %s", base, (err or f"exit {rc}").strip()[-300:])
        return sha

    async def _run(self, argv: list[str], timeout: float) -> tuple[int | None, str, str]:
        try:
            return await self.runner(argv, cwd=str(self.service.repo), timeout=timeout, env=GitPublisher._env())
        except (OSError, ValueError) as exc:
            return 127, "", str(exc)

    def _workdir(self) -> Path:
        """Next to the publisher's worktrees (git-ignored); the system's temp folder without a publisher."""
        manager = getattr(getattr(self.service, "factory", None), "manager", None)
        root = getattr(getattr(manager, "publisher", None), "worktree_root", None)
        if isinstance(root, (str, os.PathLike)):
            return Path(root).parent / "combined"
        return Path(tempfile.gettempdir()) / "foundry-combined"

    def _kv(self, name: str) -> Any:
        fn = getattr(getattr(getattr(self.service, "factory", None), "queue", None), name, None)
        return fn if callable(fn) else None

    def _load_state(self) -> dict[str, dict[str, str]]:
        """The remembered fingerprints, status lines, conflicts, green runs and merges (read from the queue once)."""
        if self._state is None:
            raw: Any = None
            get = self._kv("kv_get")
            if get is not None:
                try:
                    raw = get(_STATE_KEY)
                    if isinstance(raw, (str, bytes)):
                        raw = json.loads(raw)
                except Exception as exc:  # noqa: BLE001 - start with nothing remembered
                    log.debug("combined check: saved state not readable: %s", exc)
                    raw = None
            saved = raw if isinstance(raw, dict) else {}
            self._state = {key: dict(saved[key]) if isinstance(saved.get(key), dict) else {}
                           for key in ("fingerprints", "status", "conflicts", "green", "merged")}
        return self._state

    def _save_state(self) -> None:
        put = self._kv("kv_set")
        if put is None or self._state is None:
            return
        try:
            put(_STATE_KEY, json.dumps(self._state, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - the state is still kept in memory
            log.warning("combined check: state not saved: %s", exc)

    def summary(self) -> dict[str, Any]:
        """The latest results for the service's status (JSON-safe)."""
        return {
            "interval_s": self.interval_s,
            "rounds": self.rounds,
            "last_error": self.last_error,
            "latest": asdict(self.last_result) if self.last_result is not None else None,
            "results": {base: asdict(entry[1]) for base, entry in self._latest.items()},
        }
