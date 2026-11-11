"""Tests for ``foundry.replay``, with fake Claude, snapshots and workspace functions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from foundry.claude_client import ClaudeClient, ClaudeRateLimited, ClaudeRole
from foundry.config import FoundryConfig
from foundry.main import builder_claude_args
from foundry.orchestrator_queue import FoundryQueue
from foundry.piece_workspace import WorkspaceChanges, WorkspaceError
from foundry.replay import ReplayDeps, base_sha_for, diffstat, find_prompt, main
from foundry.schemas import ClaudeResult, SubtaskAllocation, TaskSizingDirective

TASK = "F-1"
TICKET = FoundryQueue.ticket_id_for(TASK, "F-1.1")
SAFE_TICKET = "F-1_F-1.1"
PINNED = "a" * 40
REPINNED = "b" * 40
ALLOWED = "src/app.py"
DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " keep\n"
    "-old\n"
    "+new\n"
    "diff --git a/other.py b/other.py\n"
    "--- a/other.py\n"
    "+++ b/other.py\n"
    "@@ -0,0 +1 @@\n"
    "+added\n"
)


class Fakes:
    """Stands in for every outside dependency and records how it was used."""

    def __init__(self, tmp_path: Path) -> None:
        self.snapshot = tmp_path / "snapshot"
        self.result = ClaudeResult(
            text="done", input_tokens=11, output_tokens=7, cache_read_tokens=3, duration_seconds=12.5,
            session_id="session-1", num_turns=4,
        )
        self.changes = WorkspaceChanges(diff=DIFF, changed=["other.py", ALLOWED], outside=["other.py"])
        self.run_error: Exception | None = None
        self.checkout_error: Exception | None = None
        self.collect_error: Exception | None = None
        self.calls: list[str] = []
        self.repos: list[Path] = []
        self.shas: list[str] = []
        self.dests: list[Path] = []
        self.removed: list[Path] = []
        self.prompts: list[str] = []
        self.run_options: list[dict[str, Any]] = []
        self.client_args: list[list[str]] = []
        self.allowed: list[list[str]] = []

    def snapshots(self, repo: Path) -> Fakes:
        self.calls.append("snapshots")
        self.repos.append(repo)
        return self

    async def checkout(self, sha: str) -> Path:
        self.calls.append("checkout")
        self.shas.append(sha)
        if self.checkout_error:
            raise self.checkout_error
        return self.snapshot

    async def make_workspace(self, snapshot: Path, dest: Path) -> Path:
        self.calls.append("make_workspace")
        assert snapshot == self.snapshot
        self.dests.append(dest)
        dest.mkdir(parents=True)
        return dest

    async def collect_changes(self, workspace: Path, allowed_files: Sequence[str]) -> WorkspaceChanges:
        self.calls.append("collect_changes")
        assert workspace.is_dir()
        self.allowed.append(list(allowed_files))
        if self.collect_error:
            raise self.collect_error
        return self.changes

    async def remove_workspace(self, workspace: Path) -> None:
        self.calls.append("remove_workspace")
        self.removed.append(workspace)

    def client(self, _config: FoundryConfig, args: list[str]) -> ClaudeClient:
        self.calls.append("client")
        self.client_args.append(args)
        return self

    async def run(
        self, prompt: str, *, agent_id: str, role: ClaudeRole, cwd: str | None = None, timeout: float = 600
    ) -> ClaudeResult:
        self.calls.append("run")
        self.prompts.append(prompt)
        self.run_options.append({"agent_id": agent_id, "role": role, "cwd": cwd, "timeout": timeout})
        if self.run_error:
            raise self.run_error
        return self.result


class World:
    """A seeded queue database, a prompt archive and the fakes."""

    def __init__(self, tmp_path: Path, config: FoundryConfig) -> None:
        self.db = tmp_path / "foundry.db"
        self.repo = tmp_path / "repo"
        self.prompts = tmp_path / "prompts"
        self.prompts.mkdir()
        self.config = config
        self.fakes = Fakes(tmp_path)
        self.deps = ReplayDeps(
            config=lambda: self.config,
            snapshots=self.fakes.snapshots,
            make_workspace=self.fakes.make_workspace,
            collect_changes=self.fakes.collect_changes,
            remove_workspace=self.fakes.remove_workspace,
            client=self.fakes.client,
        )
        with FoundryQueue(self.db) as queue:
            queue.add_task(TASK, "Task", "desc", "alpha", repo_root=self.repo)
            queue.apply_sizing(TaskSizingDirective(
                task_id=TASK, team_id="alpha", complexity="S", allocated_builders=1, rationale="test",
                subtasks=[SubtaskAllocation(subtask_id="F-1.1", title="Do it", acceptance_criteria=["works"],
                                            allowed_files=[ALLOWED])],
            ))

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        with FoundryQueue(self.db) as queue:
            queue.record_event(kind, TASK, payload)

    def pin(self) -> None:
        self.event("base_pinned", {"base": "main", "sha": PINNED})

    def save_prompt(self, attempt: int, text: str) -> None:
        (self.prompts / f"{SAFE_TICKET}_attempt{attempt}.txt").write_text(text, encoding="utf-8")

    def replay(self, *options: str) -> int:
        return main([TICKET, "--db", str(self.db), *options], deps=self.deps)

    def rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM replays ORDER BY id").fetchall()
        finally:
            conn.close()

    def has_replays_table(self) -> bool:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE name='replays'").fetchone() is not None
        finally:
            conn.close()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    config = FoundryConfig(builder_model="base-model")
    built = World(tmp_path, config)
    monkeypatch.setenv("FOUNDRY_PROMPT_DIR", str(built.prompts))
    return built


def test_replays_the_newest_prompt_on_the_pinned_base(world: World) -> None:
    world.pin()
    world.save_prompt(2, "second")
    world.save_prompt(10, "tenth")
    assert world.replay() == 0
    assert world.fakes.prompts == ["tenth"]
    assert world.fakes.shas == [PINNED]
    assert world.fakes.repos == [world.repo.resolve()]
    assert world.fakes.allowed == [[ALLOWED]]
    options = world.fakes.run_options[0]
    assert options["role"] == "builder"
    assert options["cwd"] == str(world.fakes.dests[0])
    assert world.rows()[0]["attempt"] == 10


def test_explicit_attempt_is_used(world: World) -> None:
    world.pin()
    world.save_prompt(1, "first")
    world.save_prompt(2, "second")
    assert world.replay("--attempt", "1") == 0
    assert world.fakes.prompts == ["first"]


def test_find_prompt_ignores_other_tickets(tmp_path: Path) -> None:
    (tmp_path / "T-1_attempt1.txt").write_text("x", encoding="utf-8")
    (tmp_path / "T-11_attempt9.txt").write_text("x", encoding="utf-8")
    assert find_prompt("T-1", None, tmp_path) == (tmp_path / "T-1_attempt1.txt", 1)


def test_latest_repin_wins_over_the_pin(world: World) -> None:
    world.pin()
    world.event("base_repinned", {"base": "main", "old_sha": PINNED, "new_sha": "c" * 40})
    world.event("base_repinned", {"base": "main", "old_sha": "c" * 40, "new_sha": REPINNED})
    with FoundryQueue(world.db) as queue:
        assert base_sha_for(queue, TASK) == REPINNED


def test_missing_base_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.save_prompt(1, "prompt")
    assert world.replay() == 2
    assert "no pinned base commit" in capsys.readouterr().err
    assert world.fakes.calls == []
    assert not world.has_replays_table()


def test_missing_prompt_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    assert world.replay() == 2
    assert "no saved prompt" in capsys.readouterr().err
    assert world.fakes.calls == []


def test_missing_attempt_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay("--attempt", "7") == 2
    assert "attempt 7" in capsys.readouterr().err
    assert world.fakes.calls == []


def test_unknown_ticket_is_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["nope", "--db", str(world.db)], deps=world.deps) == 2
    assert "no ticket nope" in capsys.readouterr().err


def test_missing_database_is_refused_and_not_created(world: World, tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "foundry.db"
    assert main([TICKET, "--db", str(missing)], deps=world.deps) == 2
    assert not missing.parent.exists()


def test_unbalanced_extra_args_are_refused(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay("--extra-args", "--x 'open") == 2
    assert "--extra-args" in capsys.readouterr().err
    assert world.fakes.calls == []


def test_prompt_file_replaces_the_saved_prompt(world: World, tmp_path: Path) -> None:
    world.pin()
    changed = tmp_path / "changed.txt"
    changed.write_text("changed prompt", encoding="utf-8")
    assert world.replay("--prompt-file", str(changed)) == 0
    assert world.fakes.prompts == ["changed prompt"]
    row = world.rows()[0]
    assert row["attempt"] is None
    assert "prompt=changed.txt" in row["variant"]


def test_default_variant_keeps_the_builder_args(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay() == 0
    assert world.fakes.client_args == [builder_claude_args(world.config)]
    assert world.rows()[0]["variant"] == f"prompt={SAFE_TICKET}_attempt1.txt model=base-model args=none"


def test_model_and_extra_args_are_applied(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay("--model", "other-model", "--extra-args", "--verbose --name 'a b'") == 0
    args = world.fakes.client_args[0]
    expected = builder_claude_args(world.config.model_copy(update={"builder_model": "other-model"}))
    assert args == [*expected, "--verbose", "--name", "a b"]
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == "other-model"
    assert world.rows()[0]["variant"].endswith("model=other-model args=--verbose --name a b")


def test_single_flag_extra_args_pass_argparse(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay("--extra-args", "--verbose") == 0
    assert world.fakes.client_args[0][-1] == "--verbose"


def test_results_are_stored_and_printed(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    world.save_prompt(3, "prompt")
    assert world.replay() == 0
    row = world.rows()[0]
    assert row["ticket_id"] == TICKET
    assert row["attempt"] == 3
    assert row["base_sha"] == PINNED
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (11, 7, 3)
    assert (row["num_turns"], row["session_id"], row["duration_seconds"]) == (4, "session-1", 12.5)
    assert json.loads(row["changed_files"]) == ["other.py", ALLOWED]
    assert json.loads(row["outside_files"]) == ["other.py"]
    assert row["diffstat"] == "2 file(s) changed, +2 -1"
    assert row["is_error"] == 0
    out = capsys.readouterr().out
    for expected in (TICKET, PINNED, "input 11, output 7, cache read 3", "turns: 4", "session-1", "12.5s",
                     "changed files: other.py, src/app.py", "outside allowed files: other.py",
                     "2 file(s) changed, +2 -1", "is_error: false"):
        assert expected in out


def test_each_replay_adds_a_row(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay() == 0
    assert world.replay("--model", "other-model") == 0
    assert len(world.rows()) == 2


def test_error_result_is_stored_with_its_changes(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    world.fakes.result = world.fakes.result.model_copy(update={"is_error": True, "error": "bad turn"})
    assert world.replay() == 1
    row = world.rows()[0]
    assert (row["is_error"], row["error"], row["input_tokens"]) == (1, "bad turn", 11)
    assert json.loads(row["changed_files"]) == ["other.py", ALLOWED]


def _assert_cleaned(world: World) -> None:
    assert world.fakes.removed == world.fakes.dests
    assert world.fakes.calls[-1] == "remove_workspace"
    assert not world.fakes.removed[0].parent.exists()


def test_workspace_removed_after_a_clean_run(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    assert world.replay() == 0
    _assert_cleaned(world)


def test_workspace_removed_when_claude_raises(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    world.fakes.run_error = ClaudeRateLimited(None, "limit reached")
    assert world.replay() == 1
    _assert_cleaned(world)
    row = world.rows()[0]
    assert row["is_error"] == 1
    assert "limit reached" in row["error"]
    assert "limit reached" in capsys.readouterr().out


def test_workspace_removed_when_collecting_fails_and_tokens_are_kept(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    world.fakes.collect_error = WorkspaceError("collect_failed", "git add failed")
    assert world.replay() == 1
    _assert_cleaned(world)
    row = world.rows()[0]
    assert (row["is_error"], row["input_tokens"], row["session_id"]) == (1, 11, "session-1")
    assert "git add failed" in row["error"]


def test_snapshot_failure_runs_no_claude(world: World) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    world.fakes.checkout_error = RuntimeError("no such commit")
    assert world.replay() == 1
    assert "run" not in world.fakes.calls
    assert world.fakes.calls[-1] == "remove_workspace"
    assert "no such commit" in world.rows()[0]["error"]


def test_dry_run_calls_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.pin()
    world.save_prompt(1, "the secret prompt")
    assert world.replay("--dry-run", "--model", "other-model") == 0
    assert world.fakes.calls == []
    assert not world.has_replays_table()
    out = capsys.readouterr().out
    assert PINNED in out and "model=other-model" in out and "--output-format" in out
    assert "the secret prompt" not in out


def test_dry_run_still_refuses_a_missing_base(world: World) -> None:
    world.save_prompt(1, "prompt")
    assert world.replay("--dry-run") == 2


def test_repo_option_overrides_the_task_repo(world: World, tmp_path: Path) -> None:
    world.pin()
    world.save_prompt(1, "prompt")
    other = tmp_path / "other-repo"
    assert world.replay("--repo", str(other)) == 0
    assert world.fakes.repos == [other.resolve()]


def test_diffstat_counts_removed_lines_that_look_like_headers() -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,1 @@\n--- not a header\n keep\n\\ No newline at end of file\n"
    assert diffstat(diff) == "1 file(s) changed, +0 -1"


def test_diffstat_of_nothing() -> None:
    assert diffstat("") == "0 file(s) changed, +0 -0"
