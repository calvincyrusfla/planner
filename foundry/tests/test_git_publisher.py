"""GitPublisher: branch + push + PR in a temporary worktree.

Uses a real temp git repo with a local bare repo as "origin" and a fake ``gh``
shell script (POSIX only; those tests skip on Windows). The injected-runner
tests run everywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from foundry.git_publisher import (
    STAGES,
    GitPublisher,
    PublishResult,
    branch_name,
    commit_message,
    slugify,
)

HAS_GIT = shutil.which("git") is not None
POSIX = sys.platform != "win32"
needs_sh = pytest.mark.skipif(not (HAS_GIT and POSIX), reason="needs git and a POSIX shell for the fake gh")

FAKE_GH = """#!/bin/sh
# fake GitHub CLI: logs every call as JSON lines, copies the --body-file
log="$FAKE_GH_LOG"
if [ "$1" = "auth" ]; then
  echo "auth $*" >> "$log.auth"
  [ "${FAKE_GH_AUTH:-ok}" = "ok" ] && exit 0
  echo "You are not logged into any GitHub hosts." >&2
  exit 1
fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  if [ "${FAKE_GH_PR:-ok}" != "ok" ]; then echo "pull request create failed: boom" >&2; exit 1; fi
  body=""
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--body-file" ]; then body="$a"; fi
    prev="$a"
  done
  {
    printf '%s\\n' "$@" > "$log.argv"
    cat "$body" > "$log.body"
  }
  echo "Creating pull request..." >&2
  echo "https://github.com/acme/widget/pull/12"
  exit 0
fi
exit 2
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Ada Lovelace")
    _git(repo, "config", "user.email", "ada@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    # the user is on their own branch with uncommitted work: must stay untouched
    _git(repo, "checkout", "-q", "-b", "my-work")
    (repo / "scratch.txt").write_text("mine\n", encoding="utf-8")

    patch = tmp_path / "F-012.patch"
    patch.write_text(
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1,2 @@\n hello\n+world\n",
        encoding="utf-8", newline="\n",
    )
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.delenv("FAKE_GH_AUTH", raising=False)
    monkeypatch.delenv("FAKE_GH_PR", raising=False)
    wts = tmp_path / "worktrees"
    pub = GitPublisher(repo, gh=str(gh), worktree_root=wts)
    return {"repo": repo, "origin": origin, "patch": patch, "gh": gh, "log": log, "wts": wts, "pub": pub,
            "tmp": tmp_path}


PR = {"title": "Add world to hello", "body": "Adds a second line.\n\nOpened by Foundry (task F-012)"}


def test_helpers() -> None:
    assert slugify("Add login page!") == "add-login-page"
    assert slugify("") == "change"
    assert len(slugify("x" * 200)) <= 40
    assert branch_name("F-012", "Fix: the Thing / now") == "foundry/f-012-fix-the-thing-now"
    msg = commit_message("Title  here", "Body text")
    assert msg.startswith("Title here\n\nBody text")
    assert "pr_failed" in STAGES and "gh_not_logged_in" in STAGES
    r = PublishResult(ok=True, branch="b")
    assert r.to_dict()["branch"] == "b" and r.to_dict()["pr_url"] is None


@needs_sh
def test_publish_happy_path(world) -> None:
    repo, origin = world["repo"], world["origin"]
    res = asyncio.run(world["pub"].publish("F-012", "Add world", world["patch"], PR))
    assert res.ok, res
    assert res.branch == "foundry/f-012-add-world"
    assert res.pr_url == "https://github.com/acme/widget/pull/12"
    assert res.base == "main" and res.stage is None
    # pushed to the bare remote with the patch commit, authored from repo config
    remote_sha = _git(origin, "rev-parse", f"refs/heads/{res.branch}")
    assert remote_sha == res.commit
    assert _git(origin, "show", f"{res.branch}:hello.txt") == "hello\nworld"
    assert _git(origin, "log", "-1", "--format=%an <%ae>|%cn|%s", res.branch) == \
        "Ada Lovelace <ada@example.com>|Ada Lovelace|Add world to hello"
    assert "Opened by Foundry" in _git(origin, "log", "-1", "--format=%b", res.branch)
    assert _git(origin, "rev-parse", f"{res.branch}~1") == _git(origin, "rev-parse", "main")
    # gh got the title in argv and the body through a file
    argv = Path(str(world["log"]) + ".argv").read_text(encoding="utf-8").splitlines()
    assert argv[:2] == ["pr", "create"]
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == res.branch
    assert argv[argv.index("--title") + 1] == PR["title"]
    assert "--body-file" in argv and "--draft" not in argv
    assert Path(str(world["log"]) + ".body").read_text(encoding="utf-8") == PR["body"]
    assert not Path(argv[argv.index("--body-file") + 1]).exists()  # temp body file cleaned up
    # the user's working tree and branch are untouched; the worktree is gone
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "my-work"
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / "scratch.txt").exists()
    assert _git(repo, "status", "--porcelain") == "?? scratch.txt"
    assert list(world["wts"].iterdir()) == []
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


@needs_sh
def test_branch_suffix_and_never_force(world) -> None:
    pub = world["pub"]
    first = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    second = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    assert first.ok and second.ok
    assert second.branch == first.branch + "-2"
    # a branch only on the remote also counts as taken
    _git(world["repo"], "branch", "-D", first.branch, second.branch)
    third = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    assert third.branch == first.branch + "-3"
    assert _git(world["origin"], "rev-parse", first.branch) == first.commit


@needs_sh
def test_draft_and_explicit_base(world) -> None:
    repo = world["repo"]
    _git(repo, "push", "-q", "origin", "main:release")
    pub = GitPublisher(repo, gh=str(world["gh"]), worktree_root=world["wts"], base="release", draft=True)
    res = asyncio.run(pub.publish("F-1", "x", world["patch"], PR))
    assert res.ok and res.base == "release"
    argv = Path(str(world["log"]) + ".argv").read_text(encoding="utf-8").splitlines()
    assert "--draft" in argv and argv[argv.index("--base") + 1] == "release"


@needs_sh
def test_apply_failed_cleans_up(world) -> None:
    bad = world["tmp"] / "bad.patch"
    bad.write_text("diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-a\n+b\n",
                   encoding="utf-8", newline="\n")
    res = asyncio.run(world["pub"].publish("F-9", "Broken", bad, PR))
    assert not res.ok and res.stage == "apply_failed" and res.error
    assert res.branch is None
    repo = world["repo"]
    assert "foundry/" not in _git(repo, "branch", "--list")  # local branch dropped
    assert list(world["wts"].iterdir()) == []
    assert _git(world["origin"], "branch", "--format=%(refname:short)") == "main"


@needs_sh
def test_no_patch(world) -> None:
    res = asyncio.run(world["pub"].publish("F-9", "x", world["tmp"] / "missing.patch", PR))
    assert res.stage == "no_patch"


@needs_sh
def test_gh_not_logged_in_pushes_nothing(world, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_AUTH", "no")
    res = asyncio.run(world["pub"].publish("F-12", "x", world["patch"], PR))
    assert not res.ok and res.stage == "gh_not_logged_in"
    assert _git(world["origin"], "branch", "--format=%(refname:short)") == "main"


@needs_sh
def test_gh_missing(world) -> None:
    pub = GitPublisher(world["repo"], gh=str(world["tmp"] / "no-such-gh"), worktree_root=world["wts"])
    res = asyncio.run(pub.publish("F-12", "x", world["patch"], PR))
    assert res.stage == "gh_missing"


@needs_sh
def test_pr_failed_then_open_pr(world, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_PR", "fail")
    pub = world["pub"]
    res = asyncio.run(pub.publish("F-12", "x", world["patch"], PR))
    assert not res.ok and res.stage == "pr_failed" and "boom" in res.error
    assert res.branch and res.commit  # pushed already
    assert _git(world["origin"], "rev-parse", res.branch) == res.commit
    monkeypatch.setenv("FAKE_GH_PR", "ok")
    again = asyncio.run(pub.open_pr(res.branch, PR))
    assert again.ok and again.pr_url.endswith("/pull/12") and again.branch == res.branch


@needs_sh
def test_push_failed(world) -> None:
    # point origin at something that is not a repo after the fetch: use a pre-receive hook that rejects
    hook = world["origin"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected by policy >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    res = asyncio.run(world["pub"].publish("F-12", "x", world["patch"], PR))
    assert not res.ok and res.stage == "push_failed" and "rejected" in res.error
    assert res.branch is None
    assert "foundry/" not in _git(world["repo"], "branch", "--list")
    assert list(world["wts"].iterdir()) == []


@needs_sh
def test_no_remote_and_not_a_repo(world, tmp_path) -> None:
    _git(world["repo"], "remote", "remove", "origin")
    res = asyncio.run(world["pub"].publish("F-1", "x", world["patch"], PR))
    assert res.stage == "no_remote"
    plain = tmp_path / "plain"
    plain.mkdir()
    res = asyncio.run(GitPublisher(plain, gh=str(world["gh"]), worktree_root=world["wts"]).publish(
        "F-1", "x", world["patch"], PR))
    assert res.stage == "not_a_repo"


@needs_sh
def test_base_falls_back_to_current_branch(world, tmp_path) -> None:
    # remote without a resolvable HEAD: an empty bare repo has no default branch
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "-q", str(empty)], check=True)
    repo = world["repo"]
    _git(repo, "remote", "set-url", "origin", str(empty))
    _git(repo, "push", "-q", "origin", "my-work")
    res = asyncio.run(world["pub"].publish("F-1", "x", world["patch"], PR))
    assert res.ok, res
    assert res.base == "my-work"


@needs_sh
def test_preflight(world, monkeypatch) -> None:
    ok = asyncio.run(world["pub"].preflight())
    assert ok == {"git": True, "repo": True, "remote": True, "gh": True, "gh_auth": True, "ok": True,
                  "reason": None}
    monkeypatch.setenv("FAKE_GH_AUTH", "no")
    bad = asyncio.run(world["pub"].preflight())
    assert bad["gh"] and not bad["gh_auth"] and not bad["ok"] and "gh auth login" in bad["reason"]
    missing = asyncio.run(GitPublisher(world["repo"], gh=str(world["tmp"] / "nope")).preflight())
    assert not missing["gh"] and "not installed" in missing["reason"]


# -- injected runner (runs on every OS) --------------------------------------


class FakeRunner:
    def __init__(self, fail: dict[str, tuple[int | None, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or {}

    async def __call__(self, argv, *, cwd=None, timeout=None, env=None):
        self.calls.append(list(argv))
        assert isinstance(argv, list) and timeout and env["GIT_TERMINAL_PROMPT"] == "0"
        key = " ".join(argv[1:3])
        if key in self.fail:
            return self.fail[key]
        if argv[1:3] == ["symbolic-ref", "--quiet"] and argv[-1].endswith("/HEAD") and "remotes" in argv[-1]:
            return 0, "origin/trunk\n", ""
        if argv[1:2] == ["ls-remote"]:
            return 0, "", ""
        if argv[1:3] == ["rev-parse", "--verify"]:
            return (0, "abc\n", "") if "remotes" in argv[-1] else (1, "", "")
        if argv[1:3] == ["worktree", "add"]:
            Path(argv[-2]).mkdir(parents=True, exist_ok=True)
        if argv[1:3] == ["worktree", "remove"]:
            shutil.rmtree(argv[-1], ignore_errors=True)
        if argv[1:2] == ["config"]:
            return 0, "someone\n", ""
        if argv[1:3] == ["rev-parse", "HEAD"]:
            return 0, "deadbeef\n", ""
        if argv[1:3] == ["pr", "create"]:
            return 0, "https://example.test/pull/7\n", ""
        return 0, "", ""


def test_injected_runner_flow(tmp_path: Path) -> None:
    patch = tmp_path / "p.patch"
    patch.write_text("diff\n", encoding="utf-8")
    runner = FakeRunner()
    pub = GitPublisher(tmp_path, runner=runner, gh="fake-gh-xyz", git="fake-git-xyz",
                       worktree_root=tmp_path / "wt")
    res = asyncio.run(pub.publish("F-3", "Do it", patch, {"title": "T", "body": "B"}))
    assert res.ok and res.pr_url == "https://example.test/pull/7" and res.base == "trunk"
    assert res.commit == "deadbeef" and res.branch == "foundry/f-3-do-it"
    joined = [" ".join(c[1:]) for c in runner.calls]
    assert any(j.startswith("push --quiet -u origin refs/heads/foundry/f-3-do-it:") for j in joined)
    assert not any("--force" in c or "-f" in c for c in runner.calls if c[1] == "push")
    assert not any("--no-verify" in c for c in runner.calls)
    pr = next(c for c in runner.calls if c[1:3] == ["pr", "create"])
    assert pr[pr.index("--base") + 1] == "trunk" and "B" not in pr
    assert any(c[1:3] == ["worktree", "remove"] for c in runner.calls)


def test_injected_runner_timeout_is_push_failed(tmp_path: Path) -> None:
    patch = tmp_path / "p.patch"
    patch.write_text("diff\n", encoding="utf-8")
    runner = FakeRunner(fail={"push --quiet": (None, "", "timed out after 300s")})
    pub = GitPublisher(tmp_path, runner=runner, gh="g", git="git-fake", worktree_root=tmp_path / "wt")
    res = asyncio.run(pub.publish("F-3", "Do it", patch, {"title": "T", "body": "B"}))
    assert res.stage == "push_failed" and "timed out" in res.error
    assert any(c[1:3] == ["branch", "-D"] for c in runner.calls)
    assert any(c[1:3] == ["worktree", "remove"] for c in runner.calls)


def test_runner_exception_never_raises(tmp_path: Path) -> None:
    async def boom(argv, **kw):
        raise RuntimeError("kaput")

    patch = tmp_path / "p.patch"
    patch.write_text("diff\n", encoding="utf-8")
    pub = GitPublisher(tmp_path, runner=boom, gh="g", git="git-fake", worktree_root=tmp_path / "wt")
    res = asyncio.run(pub.publish("F-3", "x", patch, {}))
    assert not res.ok and res.stage == "error" and "kaput" in res.error
    json.dumps(res.to_dict())
    assert os.path.isdir(tmp_path)
