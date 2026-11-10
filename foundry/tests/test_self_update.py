"""apply_update pulls the current branch fast-forward only and writes settings."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.self_update import UpdateResult, apply_update

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"edit {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    other = tmp_path / "other"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "a.txt", "one\n")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    return {"origin": origin, "clone": clone, "other": other, "tmp": tmp_path}


def _push_new(other: Path, name: str = "b.txt", text: str = "two\n") -> str:
    sha = _commit(other, name, text)
    _git(other, "push", "-q", "origin", "main")
    return sha


def _run(*args: Any, **kwargs: Any) -> UpdateResult:
    return asyncio.run(apply_update(*args, **kwargs))


def test_new_commit_is_pulled(world: dict[str, Path]) -> None:
    clone = world["clone"]
    before = _git(clone, "rev-parse", "HEAD")
    new = _push_new(world["other"])
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "done", res.message
    assert res.commit_before == before
    assert res.commit_after == new
    assert res.commit_before != res.commit_after
    assert res.message == f"pulled main {before[:7]}..{new[:7]}"
    assert _git(clone, "rev-parse", "HEAD") == new
    assert not (world["tmp"] / "settings.json").exists()


def test_already_up_to_date(world: dict[str, Path]) -> None:
    clone = world["clone"]
    head = _git(clone, "rev-parse", "HEAD")
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "done", res.message
    assert res.commit_before == res.commit_after == head
    assert res.message == "already up to date"


def test_modified_tracked_file_is_refused(world: dict[str, Path]) -> None:
    clone = world["clone"]
    head = _git(clone, "rev-parse", "HEAD")
    _push_new(world["other"])
    (clone / "a.txt").write_text("changed\n", encoding="utf-8")
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "refused"
    assert "a.txt" in res.message
    assert _git(clone, "rev-parse", "HEAD") == head


def test_untracked_files_are_allowed(world: dict[str, Path]) -> None:
    clone = world["clone"]
    new = _push_new(world["other"])
    (clone / "scratch.txt").write_text("notes\n", encoding="utf-8")
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "done", res.message
    assert res.commit_after == new


def test_detached_head_is_refused(world: dict[str, Path]) -> None:
    clone = world["clone"]
    _git(clone, "checkout", "-q", "--detach")
    head = _git(clone, "rev-parse", "HEAD")
    _push_new(world["other"])
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "refused"
    assert "detached HEAD" in res.message
    assert _git(clone, "rev-parse", "HEAD") == head


def test_diverged_history_is_refused(world: dict[str, Path]) -> None:
    clone = world["clone"]
    local = _commit(clone, "local.txt", "mine\n")
    _push_new(world["other"])
    res = _run(clone, settings_file=world["tmp"] / "settings.json")
    assert res.status == "refused"
    assert res.message.startswith("not a fast-forward")
    assert _git(clone, "rev-parse", "HEAD") == local


def test_settings_merged_and_written_sorted(world: dict[str, Path]) -> None:
    clone = world["clone"]
    settings_file = world["tmp"] / "data" / "settings.json"
    settings_file.parent.mkdir()
    settings_file.write_text(json.dumps({"FOUNDRY_RPM": "7"}), encoding="utf-8")
    new = _push_new(world["other"])
    res = _run(
        clone,
        {"FOUNDRY_TPM": "50000", "FOUNDRY_BUILDER_TOOLS": "read"},
        settings_file=settings_file,
    )
    assert res.status == "done", res.message
    assert res.commit_after == new
    assert "settings: FOUNDRY_BUILDER_TOOLS=read, FOUNDRY_TPM=50000" in res.message
    text = settings_file.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data == {"FOUNDRY_BUILDER_TOOLS": "read", "FOUNDRY_RPM": "7", "FOUNDRY_TPM": "50000"}
    assert list(data) == sorted(data)
    assert text.rstrip("\n") == json.dumps(data, sort_keys=True, indent=2)


def test_settings_file_created_when_missing(world: dict[str, Path]) -> None:
    settings_file = world["tmp"] / "newdir" / "settings.json"
    res = _run(world["clone"], {"FOUNDRY_RPM": "5"}, settings_file=settings_file)
    assert res.status == "done", res.message
    assert res.message == "already up to date; settings: FOUNDRY_RPM=5"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"FOUNDRY_RPM": "5"}


@pytest.mark.parametrize(
    "settings",
    [
        {"RPM": "7"},
        {"FOUNDRY_BUILDER_TOOLS": "bogus"},
        {"FOUNDRY_RPM": 7},
    ],
)
def test_bad_settings_change_nothing(world: dict[str, Path], settings: dict[str, Any]) -> None:
    clone = world["clone"]
    head = _git(clone, "rev-parse", "HEAD")
    _push_new(world["other"])
    settings_file = world["tmp"] / "settings.json"
    original = json.dumps({"FOUNDRY_RPM": "7"})
    settings_file.write_text(original, encoding="utf-8")
    res = _run(clone, settings, settings_file=settings_file)
    assert res.status == "refused"
    assert res.commit_before is None and res.commit_after is None
    assert res.message
    assert _git(clone, "rev-parse", "HEAD") == head
    assert settings_file.read_text(encoding="utf-8") == original


def test_not_a_repo_fails_without_raising(tmp_path: Path) -> None:
    res = _run(tmp_path / "missing", settings_file=tmp_path / "settings.json")
    assert res.status == "failed"
