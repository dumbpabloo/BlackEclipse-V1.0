"""W7 shadow-git file checkpoints + restore."""

from __future__ import annotations

import os
import subprocess

import pytest

from harness.code import checkpoints


def _git_repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "a.txt").write_text("one", encoding="utf-8")
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("HARNESS_FILE_CHECKPOINTS", "warn")
    monkeypatch.setenv("HARNESS_FILE_CHECKPOINT_MAX_MB", "50")
    monkeypatch.setenv("HARNESS_FILE_CHECKPOINT_RETENTION", "10")


def test_snapshot_list_and_dedupe(tmp_path):
    root = _git_repo(tmp_path)
    first = checkpoints.snapshot(root, reason="before-write", run_id="r1", turn_key="r1")
    second = checkpoints.snapshot(root, reason="before-write", run_id="r1", turn_key="r1")
    assert first is not None
    assert second["id"] == first["id"]  # one snapshot per turn
    listed = checkpoints.list_checkpoints(root)
    assert listed[0]["id"] == first["id"]
    assert checkpoints.checkpoints_enabled() is True


def test_protected_paths_are_never_restored(tmp_path):
    root = _git_repo(tmp_path)
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    snap = checkpoints.snapshot(root, run_id="r1", turn_key="r1")
    (root / "a.txt").write_text("two", encoding="utf-8")
    (root / ".env").write_text("SECRET=2", encoding="utf-8")
    result = checkpoints.restore(root, snap["id"], all=True, run_id="r2", turn_key="r2")
    assert "a.txt" in result["restored"]
    assert ".env" not in result["restored"]
    assert (root / ".env").read_text(encoding="utf-8") == "SECRET=2"


def test_restore_one_file_keeps_other_changes(tmp_path):
    root = _git_repo(tmp_path)
    snap = checkpoints.snapshot(root, run_id="r1", turn_key="r1")
    (root / "a.txt").write_text("two", encoding="utf-8")
    (root / "keep.txt").write_text("changed", encoding="utf-8")
    result = checkpoints.restore(root, snap["id"], paths=["a.txt"], run_id="r2", turn_key="r2")
    assert result["restored"] == ["a.txt"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "one"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "changed"


def test_restore_is_itself_undoable(tmp_path):
    root = _git_repo(tmp_path)
    snap = checkpoints.snapshot(root, run_id="r1", turn_key="r1")
    (root / "a.txt").write_text("two", encoding="utf-8")
    result = checkpoints.restore(root, snap["id"], paths=["a.txt"], run_id="r2", turn_key="r2")
    assert result["preRestoreId"]
    # The pre-restore snapshot captured the "two" state.
    pre = checkpoints.find(root, result["preRestoreId"])
    assert pre and pre[0]["reason"] == "pre-restore"


def test_restore_all_deletes_a_file_added_after_snapshot(tmp_path):
    root = _git_repo(tmp_path)
    snap = checkpoints.snapshot(root, run_id="r1", turn_key="r1")
    (root / "new.txt").write_text("new", encoding="utf-8")
    result = checkpoints.restore(root, snap["id"], all=True, run_id="r2", turn_key="r2")
    assert "new.txt" in result["restored"]
    assert not (root / "new.txt").exists()


def test_no_store_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FILE_CHECKPOINTS", "off")
    root = _git_repo(tmp_path)
    assert checkpoints.snapshot(root, run_id="r1", turn_key="r1") is None
    assert checkpoints.list_checkpoints(root) == []
    assert not (root / ".black-eclipse" / "checkpoints" / "store").exists()


def test_enforce_mode_alias():
    os.environ["HARNESS_FILE_CHECKPOINTS"] = "on"
    try:
        assert checkpoints.checkpoint_mode() == "enforce"
    finally:
        os.environ["HARNESS_FILE_CHECKPOINTS"] = "warn"


def test_the_file_service_takes_one_checkpoint_per_turn(tmp_path):
    """The real edit path: turn 1 and turn 2 each get their own snapshot."""
    from harness.code.fs.service import FileSystemService
    from harness.skills.permissions import Actor

    import fs_helpers

    root = _git_repo(tmp_path)
    ws = fs_helpers.make_ws(root)
    actor = Actor(id="coder", workspace_id=ws.id, autonomy="edit_verify")
    service = FileSystemService(ws, ws.binding, actor, run_id="run-turns", turn_key="step1")

    service.write_file("a.txt", "turn-one", overwrite=True)
    first = [cp for cp in checkpoints.list_checkpoints(root) if cp.get("runId") == "run-turns"]
    assert len(first) == 1 and first[0]["turnKey"] == "step1"

    service._turn_key = "step2"  # the graph updates this at each step boundary
    service.write_file("a.txt", "turn-two", overwrite=True)
    both = [cp for cp in checkpoints.list_checkpoints(root) if cp.get("runId") == "run-turns"]
    assert len(both) == 2
    assert {cp["turnKey"] for cp in both} == {"step1", "step2"}


def test_undo_turn_restores_the_pre_turn_bytes(tmp_path):
    """Gap #10: a per-turn checkpoint + the agent-facing undo this turn."""
    import asyncio

    from harness.code.checkpoint_tools import checkpoint_undo_turn

    root = _git_repo(tmp_path)
    # Turn 1: snapshot before its first write, then write.
    checkpoints.snapshot(root, run_id="run-u", turn_key="step1")
    (root / "a.txt").write_text("turn-one", encoding="utf-8")
    # Turn 2: another snapshot, then a second write.
    checkpoints.snapshot(root, run_id="run-u", turn_key="step2")
    (root / "a.txt").write_text("turn-two", encoding="utf-8")
    assert (root / "a.txt").read_text(encoding="utf-8") == "turn-two"

    class _Ctx:
        workspace = None
        run_id = "run-u"
        checkpoint_turn = "step3"
        log = staticmethod(lambda *_: None)

    ctx = _Ctx()
    ctx.workspace = type("W", (), {"root": root})()
    text, data = asyncio.run(checkpoint_undo_turn({}, ctx))
    assert data["status"] == "restored"
    assert data["undoTurn"] is True
    assert (root / "a.txt").read_text(encoding="utf-8") == "turn-one"
    assert data["preRestoreId"]  # the undo is itself undoable


def test_default_checkpoint_mode_is_on(monkeypatch):
    monkeypatch.delenv("HARNESS_FILE_CHECKPOINTS", raising=False)
    assert checkpoints.checkpoint_mode() == "warn"
    assert checkpoints.checkpoints_enabled() is True


def test_undo_turn_refuses_without_a_snapshot(tmp_path):
    import asyncio

    from harness.code.checkpoint_tools import checkpoint_undo_turn

    root = _git_repo(tmp_path)

    class _Ctx:
        workspace = None
        run_id = "never-ran"
        checkpoint_turn = "step1"

    ctx = _Ctx()
    ctx.workspace = type("W", (), {"root": root})()
    text, data = asyncio.run(checkpoint_undo_turn({}, ctx))
    assert data["status"] == "refused"
    assert data["code"] == "CHECKPOINT_NOT_FOUND"


def _ctx(root):
    from types import SimpleNamespace

    return SimpleNamespace(workspace=SimpleNamespace(root=root), run_id="run_ctx")


def test_checkpoint_tools_list_diff_restore(tmp_path):
    import asyncio
    from harness.code.checkpoint_tools import (
        checkpoint_diff, checkpoint_list, checkpoint_restore,
    )

    root = _git_repo(tmp_path)
    snap = checkpoints.snapshot(root, run_id="r1", turn_key="r1")
    (root / "a.txt").write_text("two", encoding="utf-8")
    listed, data = asyncio.run(checkpoint_list({}, _ctx(root)))
    assert "Recent snapshots" in listed and data["status"] == "ok"
    diff_text, diff_data = asyncio.run(checkpoint_diff({"checkpointId": snap["id"]}, _ctx(root)))
    assert "a.txt" in diff_text and diff_data["status"] == "ok"
    restored, restore_data = asyncio.run(checkpoint_restore(
        {"checkpointId": snap["id"], "paths": ["a.txt"]}, _ctx(root),
    ))
    assert restore_data["status"] == "restored"
    assert (root / "a.txt").read_text(encoding="utf-8") == "one"


def test_command_may_write_is_conservative():
    assert checkpoints.command_may_write("rm -rf build") is True
    assert checkpoints.command_may_write("echo hi > out.txt") is True
    assert checkpoints.command_may_write("git status") is False
    assert checkpoints.command_may_write("ls -la") is False
