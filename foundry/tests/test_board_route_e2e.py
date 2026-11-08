"""End-to-end checks for T-020 (the board route to Foundry).

A real Agent Department server (``department/server.mjs``, ``DEPT_HUB=off``,
scratch ``DEPT_DATA``, free loopback port) is the board. A real Foundry service
(simulated Claude and Gemma) points its board bridge and its batch watcher at
that server.

* **CLI + Python dispatch, run to done.** A 2-subtask batch is dispatched with
  ``node department/dept.mjs foundry dispatch`` and another with
  ``foundry.dispatch.dispatch_to_foundry``; a third batch names another
  repository. Foundry claims the two batches for its repository, sends every
  subtask in as an F-task (respecting ``dependsOn``), the subtasks complete with
  an opened PR (fake ``gh``, local bare ``origin``), and both batches end
  ``done``. ``foundry poll`` (CLI) and ``poll_foundry_sprint`` (Python) show the
  F-ids, states and PR links. The other repository's batch is never claimed.
* **Restart mid-batch.** Subtask 2 depends on subtask 1; dispatch is held so
  subtask 1 stays open; the service stops after subtask 1 got its F-id. A new
  service on the same database picks the batch up again, does not resubmit
  subtask 1, sends in subtask 2 once 1 is complete, and the batch ends ``done``
  with exactly two Foundry tasks.

Safety: simulated Claude and Gemma, ``DEPT_HUB=off`` with a scratch
``DEPT_DATA`` (the server and every CLI call), a fake ``gh`` and a local bare
"origin" (no GitHub, no shared hub, no network). The git/gh part needs
``sh``-style executables and is skipped on Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
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

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "department" / "server.mjs"
CLI = ROOT / "department" / "dept.mjs"
REPO_SLUG = "example/shop"
PR_URL = "https://github.com/example/shop/pull/12"
TERMINAL = ("complete", "quarantined", "failed")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX script")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

pytestmark = needs_node


@pytest.fixture(autouse=True)
def _scratch_dept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never the real board or the shared hub, whatever the machine has configured."""
    monkeypatch.setenv("DEPT_HUB", "off")
    monkeypatch.setenv("DEPT_DATA", str(tmp_path / "dept-data" / "state.json"))
    for name in ("DEPT_HUB_URL", "DEPT_HUB_KEY", "DEPT_URL"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- board (department/server.mjs)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _node_env(tmp_path: Path, port: int) -> dict[str, str]:
    """Env for the board server and the CLI: local scratch board only, no proxy, no hub."""
    env = {k: v for k, v in os.environ.items()
           if not re.match(r"^(HTTPS?_PROXY|https?_proxy|NO_PROXY|no_proxy|NODE_USE_ENV_PROXY|DEPT_|CLAUDE_|ANTHROPIC_)", k)}
    env.update({
        "DEPT_DATA": str(tmp_path / "dept-data" / "state.json"),
        "DEPT_HUB": "off",
        "DEPT_USAGE_DIRECT": "off",
        "PORT": str(port),
        "DEPT_URL": f"http://127.0.0.1:{port}",
        "HOME": str(tmp_path / "node-home"),
        "USERPROFILE": str(tmp_path / "node-home"),
    })
    (tmp_path / "dept-data").mkdir(exist_ok=True)
    (tmp_path / "node-home").mkdir(exist_ok=True)
    return env


@contextmanager
def _board(tmp_path: Path) -> Iterator[tuple[str, dict[str, str]]]:
    port = _free_port()
    env = _node_env(tmp_path, port)
    proc = subprocess.Popen(
        ["node", str(SERVER), "--port", str(port), "--hub-off"], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                _http("GET", f"{base}/api/state", timeout=2)
                break
            except (OSError, AssertionError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    out = proc.stdout.read() if proc.stdout else ""
                    raise AssertionError(f"board server did not start: {out}") from None
                time.sleep(0.1)
        yield base, env
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _http(method: str, url: str, body: Any = None, timeout: float = 15.0, expect: int = 200) -> Any:
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


def _dept(env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(["node", str(CLI), *args], cwd=str(ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _state(base: str) -> dict[str, Any]:
    body = _http("GET", f"{base}/api/state")
    return body.get("result", body) if isinstance(body.get("result"), dict) else body


def _batches(base: str) -> dict[str, dict[str, Any]]:
    return {b["id"]: b for b in _state(base).get("foundryBatches") or []}


def _wait(pred: Any, what: str, timeout: float = 60.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


def _wait_batch(base: str, sid: str, statuses: tuple[str, ...], timeout: float = 90.0) -> dict[str, Any]:
    try:
        return _wait(lambda: (b := _batches(base).get(sid)) and b.get("status") in statuses and b,
                     f"{sid} in {statuses}", timeout)
    except AssertionError:
        raise AssertionError(f"{sid}: {json.dumps(_batches(base).get(sid), indent=1)}") from None


# --------------------------------------------------------------------------- git + fake gh (as in group 3)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


def _git_repo(tmp_path: Path) -> tuple[Path, Path]:
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
import json, sys
from pathlib import Path
args = sys.argv[1:]
with Path({log!r}).open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": args}}) + "\n")
if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["pr", "list"]:
    print("[]")
    sys.exit(0)
if args[:2] == ["pr", "create"]:
    print({url!r})
    sys.exit(0)
sys.exit(2)
'''


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.jsonl"
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable, log=str(log), url=PR_URL), encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


# --------------------------------------------------------------------------- Foundry service


def _watch_env(mp: pytest.MonkeyPatch, board_url: str, tmp_path: Path) -> None:
    mp.setenv("DEPT_HUB", "off")
    mp.setenv("DEPT_DATA", str(tmp_path / "dept-data" / "state.json"))
    mp.setenv("DEPT_URL", board_url)
    mp.setenv("FOUNDRY_DASHBOARD_URL", board_url)
    mp.setenv("FOUNDRY_BATCHES", "on")
    mp.setenv("FOUNDRY_BATCH_REPO", REPO_SLUG)
    mp.setenv("FOUNDRY_BATCH_POLL_S", "0.3")
    for name in ("DEPT_HUB_URL", "DEPT_HUB_KEY"):
        mp.delenv(name, raising=False)


@contextmanager
def _service(tmp_path: Path, repo: Path, board_url: str, *, auto_pr: bool) -> Iterator[tuple[Any, str]]:
    from foundry import serve
    from foundry.board_bridge import BoardBridge
    from foundry.config import FoundryConfig
    from foundry.main import demo_claude, demo_gemma

    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=600, tpm=10_000_000,
        max_concurrent=3, board_bridge_mode="auto", pre_review="off", auto_pr=auto_pr,
    )
    service = serve.Service(
        config, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(), monitor_s=3600,
        factory_kwargs={"bridge": BoardBridge("auto", dashboard_url=board_url), "patches_dir": tmp_path / "patches"},
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

    thread = threading.Thread(target=lambda: asyncio.run(main()), name="foundry-route-e2e", daemon=True)
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


def _foundry_tasks(service: Any) -> list[dict[str, Any]]:
    return [dict(t) for t in service.factory.queue.all_tasks()]


def _on_loop(service: Any, fn: Any, timeout: float = 15.0) -> Any:
    async def call() -> Any:
        return fn()

    return asyncio.run_coroutine_threadsafe(call(), service.loop).result(timeout)


# --------------------------------------------------------------------------- 1 CLI + Python -> done


@posix_only
@needs_git
def test_batches_from_cli_and_python_run_to_done_and_other_repo_is_left_alone(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry import git_publisher
    from foundry.dispatch import DispatchFoundryBatch, SubtaskPayload, dispatch_to_foundry, poll_foundry_sprint

    mp = clean_foundry_env
    _git_env(tmp_path, mp)
    mp.setattr(git_publisher, "DEFAULT_WORKTREE_ROOT", tmp_path / "worktrees")
    repo, _bare = _git_repo(tmp_path)
    _fake_gh(tmp_path, mp)

    with _board(tmp_path) as (board, env):
        _watch_env(mp, board, tmp_path)

        # Another repository's batch first: it must stay queued and unclaimed throughout.
        other = _dept(env, "foundry", "dispatch", "--title", "Elsewhere", "--repo", "someone/else",
                      "--subtask", "Other thing::Not for this Foundry.::shop/slug.py::S")
        # The CLI batch: two independent subtasks.
        cli_out = _dept(env, "foundry", "dispatch", "--title", "Math helpers", "--repo", REPO_SLUG.upper(),
                        "--base", "main",
                        "--subtask", "Add mathx helper::Add a helper to mathx.::shop/mathx.py::S",
                        "--subtask", "Add strings helper::Add a helper to strings.::shop/strings.py::S")
        ids = sorted(_batches(board))
        assert len(ids) == 2, (other, cli_out, ids)
        other_id, cli_id = ids
        assert other_id in other and cli_id in cli_out, (other, cli_out)

        # The Python batch, through the local dashboard: subtask 2 depends on 1.
        mp.setenv("FOUNDRY_DASHBOARD_URL", board)
        sprint = dispatch_to_foundry(DispatchFoundryBatch(
            title="Slug helper", repo=REPO_SLUG, base="main",
            subtasks=[
                SubtaskPayload(title="Add slug helper", description="Add a helper to slug.", files=["shop/slug.py"],
                               size="S", acceptance_criteria=["the helper exists"]),
                SubtaskPayload(title="Add mathx helper again", files=["shop/mathx.py"], depends_on=["1"]),
            ],
        ))
        py_id = sprint.sprint_id
        assert py_id not in (other_id, cli_id) and sprint.status == "queued", sprint

        with _service(tmp_path, repo, board, auto_pr=True) as (service, _base):
            done_cli = _wait_batch(board, cli_id, ("done", "failed", "cancelled"))
            done_py = _wait_batch(board, py_id, ("done", "failed", "cancelled"))
            tasks = _foundry_tasks(service)

        for batch in (done_cli, done_py):
            assert batch["status"] == "done", batch
            assert batch.get("claimedBy", {}).get("host"), batch
            assert set(batch["results"]) == {s["key"] for s in batch["subtasks"]} == {"1", "2"}, batch
            for key, res in batch["results"].items():
                assert res["state"] == "complete", (key, res)
                assert re.fullmatch(r"F-\d+", res["foundryTask"]), res
                assert (res.get("pr") or {}).get("state") == "opened", res
                assert res["pr"]["url"] == PR_URL, res

        # Four subtasks, four distinct F-tasks, each sent in once.
        fids = [r["foundryTask"] for b in (done_cli, done_py) for r in b["results"].values()]
        assert len(set(fids)) == 4, fids
        assert len(tasks) == 4, [t.get("task_id") or t.get("id") for t in tasks]

        # dependsOn: the dependent subtask's F-task was created after its dependency's.
        dep_first, dep_second = (int(done_py["results"][k]["foundryTask"][2:]) for k in ("1", "2"))
        assert dep_first < dep_second, done_py["results"]

        # The other repository's batch was never touched.
        untouched = _batches(board)[other_id]
        assert untouched["status"] == "queued" and not untouched.get("claimedBy"), untouched
        assert not untouched.get("results"), untouched

        # Polling: CLI and Python agree with the board.
        polled = _dept(env, "foundry", "poll", cli_id)
        for res in done_cli["results"].values():
            assert res["foundryTask"] in polled and PR_URL in polled, polled
        assert "done" in polled.lower(), polled
        polled_json = json.loads(_dept(env, "foundry", "poll", cli_id, "--json"))
        polled_batch = polled_json.get("batch", polled_json)
        assert polled_batch["id"] == cli_id and polled_batch["status"] == "done", polled_json

        status = poll_foundry_sprint(py_id)
        assert status.sprint_id == py_id and status.status == "done", status
        dumped = json.dumps(status.model_dump(mode="json"))
        for res in done_py["results"].values():
            assert res["foundryTask"] in dumped, dumped
        assert PR_URL in dumped, dumped

        # Each F-task is also an ordinary task on the board (registered by the board bridge).
        titles = [t.get("title", "") for t in _state(board).get("tasks", [])]
        assert any("Add slug helper" in t for t in titles), titles


# --------------------------------------------------------------------------- 2 restart mid-batch


def test_restart_mid_batch_resumes_without_resubmitting(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry.main import write_demo_repo

    mp = clean_foundry_env
    repo = write_demo_repo(tmp_path / "repo")

    with _board(tmp_path) as (board, env):
        _watch_env(mp, board, tmp_path)
        spec = {
            "title": "Two steps", "repo": REPO_SLUG,
            "subtasks": [
                {"key": "1", "title": "Step one", "description": "Add a helper to mathx.", "files": ["shop/mathx.py"]},
                {"key": "2", "title": "Step two", "description": "Add a helper to strings.",
                 "files": ["shop/strings.py"], "dependsOn": ["1"]},
            ],
        }
        spec_file = tmp_path / "batch.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        _dept(env, "foundry", "dispatch", "--json", str(spec_file))
        (sid,) = list(_batches(board))

        with _service(tmp_path, repo, board, auto_pr=False) as (service, _base):
            # Hold dispatch (like a usage-limit pause) so step one stays open.
            _on_loop(service, lambda: service.factory.limiter.pause_until(seconds=600))
            first = _wait(lambda: (r := (_batches(board)[sid].get("results") or {}).get("1"))
                          and r.get("foundryTask") and r, "step one sent in")
            fid_one = first["foundryTask"]
            time.sleep(1.0)  # a few watcher ticks: step two must still wait for step one
            mid = _batches(board)[sid]
            assert mid["status"] in ("claimed", "running"), mid
            assert not (mid.get("results") or {}).get("2", {}).get("foundryTask"), mid
            assert len(_foundry_tasks(service)) == 1

        # A fresh service on the same database: no resubmission of step one.
        with _service(tmp_path, repo, board, auto_pr=False) as (service, _base):
            final = _wait_batch(board, sid, ("done", "failed", "cancelled"))
            tasks = _foundry_tasks(service)

        assert final["status"] == "done", final
        assert final["results"]["1"]["foundryTask"] == fid_one, final
        assert final["results"]["2"]["foundryTask"] != fid_one, final
        assert all(r["state"] == "complete" for r in final["results"].values()), final
        assert len(tasks) == 2, tasks
