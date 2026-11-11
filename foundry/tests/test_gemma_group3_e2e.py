"""End-to-end checks for T-019 (Gemma group 3: automatic PRs, a line check that only logs).

* **Automatic pull request.** ``foundry.serve`` runs on a free loopback port
  for a temporary git repository whose ``origin`` is a local bare repository,
  with a fake ``gh`` first on PATH (records its argv and the ``--body-file``
  content, prints a PR URL). An order sent over HTTP completes; the branch
  ``foundry/<task>-<slug>`` is pushed to the bare remote with one commit that
  holds the patch; ``gh pr create`` got Gemma's title and body (with the
  ``Opened by Foundry`` footer); ``/api/status`` shows ``pr.state == opened``
  with the URL. The user's working tree and current branch are untouched and
  the temporary worktree is gone.
* **Failed PR, then Retry.** ``gh pr create`` fails once: the task row shows
  ``pr.state == failed`` / ``stage == pr_failed``; ``POST
  /api/tasks/<id>/pr/retry`` opens it.
* **Stuck task -> REASSIGN_TEAM is only logged.** Dispatch is paused so the
  task's tickets stay open on their team; the line check
  (``service.monitor_once``) logs a REASSIGN_TEAM escalation (the Foreman's
  detection itself is unit-tested in test_shop_foreman) without moving the
  tickets, the entry appears in ``GET /api/actions``, and ``POST
  /api/actions/<id>/undo`` is refused.

Safety: simulated Claude and Gemma only, dry board bridge, DEPT_HUB=off plus a
scratch DEPT_DATA, a fake ``gh`` and a local bare "origin" (no GitHub, no
network). The git/gh parts need ``sh``-style executables and are skipped on
Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ORDER = {
    "title": "Add clamp helper",
    "description": "Add clamp(x, lo, hi) to the math helpers.",
    "files": ["shop/mathx.py"],
}
PR_URL = "https://github.com/example/shop/pull/12"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX script")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _scratch_dept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPT_HUB", "off")
    monkeypatch.setenv("DEPT_DATA", str(tmp_path / "dept-data"))


# --------------------------------------------------------------------------- helpers


def _http(method: str, url: str, body: Any = None, timeout: float = 15.0, expect: int = 200) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            code, text = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read().decode("utf-8")
    assert code == expect, (code, text)
    return json.loads(text)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate git from the machine's global/system config (signing, hooks, templates)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


def _git_repo(tmp_path: Path) -> tuple[Path, Path]:
    """The demo repo as a git repo on ``main`` with a bare ``origin``; returns (repo, bare)."""
    from foundry.main import write_demo_repo

    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(bare))
    repo = write_demo_repo(tmp_path / "repo")
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "Pat Example")
    _git(repo, "config", "user.email", "pat@example.com")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "--quiet", "-u", "origin", "main")
    return repo, bare


FAKE_GH = r'''#!{python}
"""Fake GitHub CLI: records argv (+ the --body-file text); never touches the network."""
import json, sys
from pathlib import Path
log = Path({log!r})
fail = Path({fail!r})
args = sys.argv[1:]
entry = {{"argv": args}}
if "--body-file" in args:
    entry["body"] = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")
if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["pr", "list"]:
    print("[]")
    sys.exit(0)
if args[:2] == ["pr", "create"]:
    if fail.exists():
        fail.unlink()
        print("GraphQL: something went wrong (createPullRequest)", file=sys.stderr)
        sys.exit(1)
    print({url!r})
    sys.exit(0)
sys.exit(2)
'''


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail_once: bool = False) -> Path:
    """Put a fake ``gh`` first on PATH; returns its JSON-lines call log."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.jsonl"
    fail = tmp_path / "gh-fail-once"
    if fail_once:
        fail.write_text("1")
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable, log=str(log), fail=str(fail), url=PR_URL), encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert shutil.which("gh") == str(gh)
    return log


def _gh_calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


@contextmanager
def _service(tmp_path: Path, repo: Path, *, auto_pr: bool, claude: Any = None) -> Iterator[tuple[Any, str]]:
    from foundry import serve
    from foundry.board_bridge import BoardBridge
    from foundry.config import FoundryConfig
    from foundry.main import demo_claude, demo_gemma

    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=600, tpm=10_000_000,
        max_concurrent=3, board_bridge_mode="dry", pre_review="off", auto_pr=auto_pr,
    )
    service = serve.Service(
        config, repo=repo, claude=claude or demo_claude(repo, fail_first=None), gemma=demo_gemma(), monitor_s=3600,
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"},
    )
    lines: list[str] = []
    holder: dict[str, Any] = {}
    ready = threading.Event()

    def out(line: str = "") -> None:
        lines.append(str(line))
        if "running for" in str(line):
            ready.set()

    async def main() -> None:
        holder["loop"] = asyncio.get_running_loop()
        holder["stop"] = asyncio.Event()
        try:
            await serve.run_service(service, port=0, stop_event=holder["stop"], out=out)
        except BaseException as exc:
            holder["error"] = exc
            ready.set()
            raise

    thread = threading.Thread(target=lambda: asyncio.run(main()), name="foundry-g3-e2e", daemon=True)
    thread.start()
    try:
        assert ready.wait(20), f"service did not start: {lines}"
        assert "error" not in holder, holder.get("error")
        m = re.search(r"http://127\.0\.0\.1:(\d+)", " ".join(lines))
        assert m, lines
        yield service, f"http://127.0.0.1:{m.group(1)}"
    finally:
        loop = holder.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(30)
        assert not thread.is_alive(), "service did not stop"


def _on_loop(service: Any, coro: Any, timeout: float = 15.0) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, service.loop).result(timeout)


def _row(base: str, tid: str) -> dict[str, Any]:
    status = _http("GET", f"{base}/api/status")
    return next((t for t in status["tasks"] if t["id"] == tid), {})


def _wait(pred: Any, what: str, timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


def _wait_pr(base: str, tid: str, states: tuple[str, ...], timeout: float = 60.0) -> dict[str, Any]:
    def done() -> dict[str, Any] | None:
        row = _row(base, tid)
        pr = row.get("pr") or {}
        return row if row.get("state") == "complete" and pr.get("state") in states else None

    try:
        return _wait(done, f"{tid} pr in {states}", timeout)
    except AssertionError:
        raise AssertionError(f"{tid}: {_row(base, tid)}") from None


# --------------------------------------------------------------------------- 1 automatic PR


@posix_only
@needs_git
def test_approved_task_is_pushed_and_gets_a_pr(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry import git_publisher

    mp = clean_foundry_env
    _git_env(tmp_path, mp)
    worktrees = tmp_path / "worktrees"
    mp.setattr(git_publisher, "DEFAULT_WORKTREE_ROOT", worktrees)
    repo, bare = _git_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    log = _fake_gh(tmp_path, mp)

    with _service(tmp_path, repo, auto_pr=True) as (service, base):
        health = _wait(lambda: (h := _http("GET", f"{base}/api/health")).get("pr_ready") and h,
                       "pr_ready in /api/health")
        assert health["pr_ready"] is True and not health.get("pr_reason"), health

        tid = _http("POST", f"{base}/api/orders", ORDER)["task_id"]
        row = _wait_pr(base, tid, ("opened", "failed", "skipped"))
        pr = row["pr"]
        assert pr["state"] == "opened", pr
        assert pr["url"] == PR_URL, pr
        branch = pr["branch"]
        assert branch == f"foundry/{tid.lower()}-add-clamp-helper", pr

        req = service.factory.manager.pr_request(tid)
        bridge_ops = list(service.factory.manager.bridge.ops)

    # gh: auth checked, then exactly one `pr create` with Gemma's title and body (via a file).
    calls = _gh_calls(log)
    creates = [c for c in calls if c["argv"][:2] == ["pr", "create"]]
    assert len(creates) == 1, calls
    argv = creates[0]["argv"]
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == branch
    title = argv[argv.index("--title") + 1]
    assert title == req["title"] == "feat(shop): add clamp helper", (title, req)
    body = creates[0]["body"]
    assert body == req["body"]
    assert "Drafted by the Gemma Shop Foreman" in body and f"Opened by Foundry (task {tid})" in body
    assert "--body" not in argv  # body goes through --body-file, never argv
    assert "--draft" not in argv

    # The bare remote has the branch: one commit on top of main, holding the patch, authored as the user.
    log_lines = _git(bare, "log", "--format=%H %an <%ae>%n%s", f"main..{branch}").splitlines()
    assert len(log_lines) == 2, log_lines
    assert log_lines[0].endswith("Pat Example <pat@example.com>"), log_lines
    assert log_lines[1] == title
    changed = _git(bare, "diff", "--name-only", f"main..{branch}").split()
    assert changed == ["shop/mathx.py"], changed
    assert "def mathx_helper(value):" in _git(bare, "show", f"{branch}:shop/mathx.py")
    assert _git(bare, "rev-parse", "main").strip() == head_before  # base branch not pushed to

    # The user's checkout is untouched: same branch, same HEAD, clean tree; the worktree is gone.
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert "def mathx_helper" not in (repo / "shop" / "mathx.py").read_text(encoding="utf-8")
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not worktrees.exists() or not any(worktrees.iterdir())

    # The board task got the PR link as a progress note.
    notes = [json.dumps(op) for op in bridge_ops]
    assert any(PR_URL in n for n in notes), notes


@posix_only
@needs_git
def test_failed_pr_shows_failed_then_retry_over_http_opens_it(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry import git_publisher

    mp = clean_foundry_env
    _git_env(tmp_path, mp)
    mp.setattr(git_publisher, "DEFAULT_WORKTREE_ROOT", tmp_path / "worktrees")
    repo, bare = _git_repo(tmp_path)
    log = _fake_gh(tmp_path, mp, fail_once=True)

    with _service(tmp_path, repo, auto_pr=True) as (service, base):
        tid = _http("POST", f"{base}/api/orders", ORDER)["task_id"]
        row = _wait_pr(base, tid, ("opened", "failed", "skipped"))
        pr = row["pr"]
        assert pr["state"] == "failed" and pr["stage"] == "pr_failed", pr
        assert "createPullRequest" in (pr["error"] or ""), pr
        first_branch = f"foundry/{tid.lower()}-add-clamp-helper"
        assert _git(bare, "branch", "--list", first_branch).strip(), "branch is pushed before gh runs"

        res = _http("POST", f"{base}/api/tasks/{tid}/pr/retry", {})
        assert res["ok"] is True, res
        row = _wait_pr(base, tid, ("opened", "failed"))
        assert row["pr"]["state"] == "opened" and row["pr"]["url"] == PR_URL, row["pr"]

        # An opened PR is not opened twice.
        again = _http("POST", f"{base}/api/tasks/{tid}/pr/retry", {}, expect=409)
        assert again["ok"] is False, again

    creates = [c for c in _gh_calls(log) if c["argv"][:2] == ["pr", "create"]]
    assert len(creates) == 2, creates
    retried_head = creates[1]["argv"][creates[1]["argv"].index("--head") + 1]
    assert row["pr"]["branch"] == retried_head
    # Retry reopens the PR for the already pushed branch: no second "-2" branch on the remote.
    assert retried_head == first_branch, retried_head
    assert _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/foundry/").split() == [first_branch]


# --------------------------------------------------------------------------- 2 stuck task -> logged, undo refused


def test_stuck_task_is_logged_and_undo_over_http_is_refused(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry.main import write_demo_repo
    from foundry.schemas import ForemanEscalation

    repo = write_demo_repo(tmp_path / "repo")
    with _service(tmp_path, repo, auto_pr=False) as (service, base):
        f = service.factory
        # Hold dispatch (like a usage-limit pause) so the task's tickets stay open on their team.
        _on_loop(service, _call(lambda: f.limiter.pause_until(seconds=600)))
        tid = _http("POST", f"{base}/api/orders", ORDER)["task_id"]

        def open_tickets() -> list[Any]:
            return [t for t in f.queue.tickets(task_id=tid, status="open")]

        tickets = _wait(open_tickets, f"{tid} tickets")
        from_team = tickets[0].team_id
        to_team = next(t for t in ("alpha", "bravo", "charlie") if t != from_team)
        ids = sorted(t.ticket_id for t in tickets)

        alert = ForemanEscalation(
            alert_id="ALT-20261108-001",
            timestamp="2026-11-08T12:00:00Z",
            severity="MEDIUM",
            category="CAPACITY_REBALANCE",
            target_entity={"task_id": tid, "team_id": from_team},
            metrics_snapshot={"queue_depth": len(ids), "dwell_seconds": 840.0, "gate_failure_count": 0,
                              "current_tpm": 0.0},
            diagnosis=f"{tid} has waited 14 min behind busy builders on team {from_team}. Move it to {to_team}.",
            prescribed_action="REASSIGN_TEAM",
            action_parameters={"from_team": from_team, "to_team": to_team, "ticket_ids": ids, "waited_s": 840.0},
        )
        fired: list[str] = []

        async def evaluate() -> Any:
            if fired:
                return None
            fired.append(alert.alert_id)
            return alert

        f.foreman.evaluate_line_anomaly = evaluate  # the detector is unit-tested; drive the real log path
        result = _on_loop(service, service.monitor_once())
        assert result is not None and result["action"].get("applied") is False, result
        assert result["action"].get("action") == "REASSIGN_TEAM", result
        assert {t.team_id for t in f.queue.tickets(task_id=tid)} == {from_team}
        assert f.queue.get_task(tid)["team_id"] == from_team
        assert _row(base, tid).get("team") == from_team

        actions = _http("GET", f"{base}/api/actions")
        assert actions["ok"] is True
        entry = actions["actions"][0]  # newest first
        assert entry["action"] == "REASSIGN_TEAM" and entry["task_id"] == tid, actions
        assert entry["alert_id"] == alert.alert_id
        assert entry["applied"] is False and entry["undoable"] is False and not entry["undone_at"], entry

        # The same alert is not logged twice.
        assert _on_loop(service, service.monitor_once()) is None

        gone = "undo is gone: the Foreman only logs and never acts"
        undo = _http("POST", f"{base}/api/actions/{entry['action_id']}/undo", {}, expect=409)
        assert undo["ok"] is False and undo["reason"] == gone and undo["action_id"] == entry["action_id"], undo
        assert sorted(t.ticket_id for t in f.queue.tickets(task_id=tid, status="open")) == ids
        assert {t.team_id for t in f.queue.tickets(task_id=tid)} == {from_team}

        after = _http("GET", f"{base}/api/actions")["actions"][0]
        assert after["action_id"] == entry["action_id"] and not after["undone_at"], after

        missing = _http("POST", f"{base}/api/actions/A-9999/undo", {}, expect=409)
        assert missing["ok"] is False and missing["reason"] == gone, missing


async def _call(fn: Any) -> Any:
    return fn()
