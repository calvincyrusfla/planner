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
    default_runner,
    github_repo,
    parse_trailers,
    patch_digest,
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
# fake GitHub CLI: logs calls, copies the --body-file, remembers created PRs per head branch
log="$FAKE_GH_LOG"
echo "$*" >> "$log.calls"
if [ "$1" = "auth" ]; then
  [ "${FAKE_GH_AUTH:-ok}" = "ok" ] && exit 0
  echo "You are not logged into any GitHub hosts." >&2
  exit 1
fi
head=""; body=""; prev=""
for a in "$@"; do
  [ "$prev" = "--head" ] && head="$a"
  [ "$prev" = "--body-file" ] && body="$a"
  prev="$a"
done
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then
  url=$(grep "^$head " "$log.prs" 2>/dev/null | head -n 1 | cut -d' ' -f2)
  if [ -n "$url" ]; then echo "[{\\"url\\":\\"$url\\",\\"number\\":12}]"; else echo "[]"; fi
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  if [ "${FAKE_GH_PR:-ok}" = "exists" ]; then
    echo "a pull request for branch \\"$head\\" into branch \\"main\\" already exists:" >&2
    echo "https://github.com/acme/widget/pull/9" >&2
    exit 1
  fi
  if [ "${FAKE_GH_PR:-ok}" != "ok" ]; then echo "pull request create failed: boom" >&2; exit 1; fi
  printf '%s\\n' "$@" > "$log.argv"
  cat "$body" > "$log.body"
  echo "$head https://github.com/acme/widget/pull/12" >> "$log.prs"
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
    pushed: list = []
    res = asyncio.run(world["pub"].publish("F-012", "Add world", world["patch"], PR,
                                           on_pushed=lambda b, c: pushed.append((b, c))))
    assert res.ok, res
    assert res.branch == "foundry/f-012-add-world"
    assert pushed == [(res.branch, res.commit)]
    assert "--repo" not in Path(str(world["log"]) + ".argv").read_text(encoding="utf-8").split()
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


def _creates(world) -> int:
    calls = Path(str(world["log"]) + ".calls")
    return sum(1 for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("pr create")) \
        if calls.exists() else 0


@needs_sh
def test_publish_twice_is_idempotent(world) -> None:
    pub = world["pub"]
    first = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    # crash before the caller saved the result: the resume calls publish again
    second = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    assert first.ok and second.ok
    assert second.branch == first.branch and second.pr_url == first.pr_url and second.commit == first.commit
    assert _git(world["origin"], "branch", "--format=%(refname:short)").splitlines() == [first.branch, "main"]
    assert _creates(world) == 1


@needs_sh
def test_pushed_branch_without_pr_gets_its_pr(world) -> None:
    pushed: list[tuple[str, str | None]] = []
    pub = world["pub"]
    world_pr = dict(PR)
    os.environ["FAKE_GH_PR"] = "fail"
    try:
        r1 = asyncio.run(pub.publish("F-012", "Add world", world["patch"], world_pr,
                                     on_pushed=lambda b, c: pushed.append((b, c))))
    finally:
        os.environ.pop("FAKE_GH_PR", None)
    assert r1.stage == "pr_failed" and pushed == [(r1.branch, r1.commit)]

    async def record(b, c):
        pushed.append((b, c))

    r2 = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR, on_pushed=record))
    assert r2.ok and r2.branch == r1.branch and r2.commit == r1.commit and r2.pr_url.endswith("/pull/12")
    assert pushed[-1] == (r1.branch, r1.commit)
    assert _git(world["origin"], "branch", "--format=%(refname:short)").splitlines() == [r1.branch, "main"]


@needs_sh
def test_local_only_branch_is_never_deleted(world) -> None:
    repo = world["repo"]
    # a local-only branch of this task with a commit the user made by hand
    _git(repo, "branch", "foundry/f-012-add-world", "main")
    other = world["tmp"] / "other-wt"
    _git(repo, "worktree", "add", "-q", str(other), "foundry/f-012-add-world")
    (other / "mine.txt").write_text("by hand\n", encoding="utf-8")
    _git(other, "add", "mine.txt")
    _git(other, "commit", "-q", "-m", "user work")
    _git(repo, "worktree", "remove", str(other))
    user_sha = _git(repo, "rev-parse", "foundry/f-012-add-world")
    res = asyncio.run(world["pub"].publish("F-012", "Add world", world["patch"], PR))
    assert res.ok and res.branch == "foundry/f-012-add-world-2"
    assert _git(repo, "rev-parse", "foundry/f-012-add-world") == user_sha  # untouched
    assert _git(world["origin"], "rev-parse", res.branch) == res.commit
    again = asyncio.run(world["pub"].publish("F-012", "Add world", world["patch"], PR))
    assert again.branch == res.branch and _creates(world) == 1
    assert _git(repo, "rev-parse", "foundry/f-012-add-world") == user_sha


@needs_sh
def test_stale_worktrees_removed(world) -> None:
    pub = world["pub"]
    stale_dir = pub._wt_dir
    stale_dir.mkdir(parents=True)
    _git(world["repo"], "worktree", "add", "-q", "--detach", str(stale_dir / "f-1-dead"), "main")
    (stale_dir / "junk").mkdir()
    (stale_dir / "f-1-dead.msg").write_text("x", encoding="utf-8")
    res = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    assert res.ok
    assert list(world["wts"].iterdir()) == []
    assert len(_git(world["repo"], "worktree", "list").splitlines()) == 1


@needs_sh
def test_open_pr_reuses_existing_and_already_exists(world, monkeypatch) -> None:
    pub = world["pub"]
    first = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    again = asyncio.run(pub.open_pr(first.branch, PR))
    assert again.ok and again.pr_url == first.pr_url and _creates(world) == 1
    # gh says the PR already exists (pr list could not see it): success with that URL
    monkeypatch.setenv("FAKE_GH_PR", "exists")
    _git(world["repo"], "push", "-q", "origin", "main:foundry/f-077-other")
    res = asyncio.run(pub.open_pr("foundry/f-077-other", PR))
    assert res.ok and res.pr_url == "https://github.com/acme/widget/pull/9"


@needs_sh
def test_inherited_git_dir_is_ignored(world, monkeypatch, tmp_path) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    res = asyncio.run(world["pub"].publish("F-012", "Add world", world["patch"], PR))
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert res.ok, res
    assert _git(world["origin"], "rev-parse", res.branch) == res.commit


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
def test_per_call_base_overrides_the_publisher_base(world) -> None:
    repo = world["repo"]
    _git(repo, "push", "-q", "origin", "main:develop")
    res = asyncio.run(world["pub"].publish("F-2", "x", world["patch"], PR, base="develop"))
    assert res.ok and res.base == "develop"
    argv = Path(str(world["log"]) + ".argv").read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--base") + 1] == "develop"


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


def test_github_repo_parsing() -> None:
    assert github_repo("https://github.com/acme/widget.git") == "acme/widget"
    assert github_repo("https://user@github.com/acme/widget") == "acme/widget"
    assert github_repo("git@github.com:acme/widget.git") == "acme/widget"
    assert github_repo("ssh://git@github.com:22/acme/widget.git") == "acme/widget"
    assert github_repo("https://ghe.corp.example/team/app.git") == "ghe.corp.example/team/app"
    assert github_repo("/srv/git/origin.git") is None
    assert github_repo("C:\\repos\\origin.git") is None
    assert github_repo("") is None


def test_injected_runner_passes_repo_and_finds_existing_pr(tmp_path: Path) -> None:
    patch = tmp_path / "p.patch"
    patch.write_text("diff\n", encoding="utf-8")

    class R(FakeRunner):
        async def __call__(self, argv, **kw):
            if argv[1:3] == ["remote", "get-url"]:
                self.calls.append(list(argv))
                return 0, "git@github.com:acme/widget.git\n", ""
            if argv[1:3] == ["pr", "list"]:
                self.calls.append(list(argv))
                return 0, "[]", ""
            return await super().__call__(argv, **kw)

    runner = R()
    pub = GitPublisher(tmp_path, runner=runner, gh="g", git="git-fake", worktree_root=tmp_path / "wt")
    res = asyncio.run(pub.publish("F-3", "Do it", patch, {"title": "T", "body": "B"}, draft=True))
    assert res.ok
    pr = next(c for c in runner.calls if c[1:3] == ["pr", "create"])
    assert pr[pr.index("--repo") + 1] == "acme/widget" and "--draft" in pr


def test_default_runner_kills_child_on_cancel(monkeypatch) -> None:
    procs: list = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def go():
        t = asyncio.ensure_future(default_runner([sys.executable, "-c", "import time; time.sleep(60)"], timeout=120))
        while not procs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.wait_for(procs[0].wait(), 10)
        return procs[0].returncode

    assert asyncio.run(go()) is not None


def test_default_runner_timeout() -> None:
    rc, _, err = asyncio.run(default_runner([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5))
    assert rc is None and "timed out" in err


def test_patch_digest_and_trailers() -> None:
    assert patch_digest("a\r\nb\r\n") == patch_digest("a\nb\n") == patch_digest("a\nb")
    assert patch_digest("a\n") != patch_digest("b\n")
    msg = commit_message("T", "Body", trailers={"Foundry-Task": "F-1", "Foundry-Patch": "abc"})
    assert msg.endswith("\n\nFoundry-Task: F-1\nFoundry-Patch: abc\n")
    assert parse_trailers(msg) == {"Foundry-Task": "F-1", "Foundry-Patch": "abc"}
    assert parse_trailers("subject only") == {}


@needs_sh
def test_same_task_id_different_patch_gets_new_branch_and_pr(world) -> None:
    pub = world["pub"]
    first = asyncio.run(pub.publish("F-012", "Add world", world["patch"], PR))
    msg = _git(world["origin"], "log", "-1", "--format=%B", first.branch)
    assert "Foundry-Task: F-012" in msg and "Foundry-Patch: " in msg
    # a reset / second database: same task id, different work
    p2 = world["tmp"] / "p2.patch"
    p2.write_text("diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n"
                  "@@ -0,0 +1 @@\n+brand new\n", encoding="utf-8", newline="\n")
    second = asyncio.run(pub.publish("F-012", "Add world", p2, {"title": "Different", "body": "c"}))
    assert second.ok and second.branch == first.branch + "-2" and second.commit != first.commit
    assert _git(world["origin"], "show", f"{second.branch}:new.txt") == "brand new"
    assert _creates(world) == 2
    # the original work republished (CRLF copy of the same patch) still finds its own branch + PR
    crlf = world["tmp"] / "crlf.patch"
    crlf.write_bytes(world["patch"].read_bytes().replace(b"\n", b"\r\n"))
    third = asyncio.run(pub.publish("F-012", "Add world", crlf, PR))
    assert third.branch == first.branch and _creates(world) == 2
    # a branch without trailers (pushed by hand) is never adopted
    _git(world["repo"], "push", "-q", "origin", "main:refs/heads/foundry/f-099-add-world")
    fresh = asyncio.run(pub.publish("F-099", "Add world", world["patch"], PR))
    assert fresh.ok and fresh.branch == "foundry/f-099-add-world-2"
