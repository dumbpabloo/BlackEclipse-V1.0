"""Diff source modes (working / committed / revision).

The review view can compare the workspace against HEAD, against the workspace's
base revision, or against a caller-named branch/revision. These tests pin the
command each mode builds, because a wrong spec silently shows the wrong change.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from harness.code.worktree import WorkspaceError, diff, scratch_root

GIT = shutil.which("git")


def _git(repo, *args) -> str:
    proc = subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)
    return proc.stdout.decode("utf-8", "replace").strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.ts").write_text("export const x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.ts")
    _git(repo, "commit", "-m", "one")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_working_shows_uncommitted_changes(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "app.ts").write_text("export const x = 2\n", encoding="utf-8")
    result = diff("ws", strategy="direct", root=repo)
    assert result["mode"] == "working"
    assert "-export const x = 1" in result["unified"]
    assert "+export const x = 2" in result["unified"]
    assert result["modified"] == ["app.ts"]
    assert result["filesChanged"] == 1


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_working_includes_a_new_untracked_file(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "new.py").write_text("NEW = True\n", encoding="utf-8")
    result = diff("ws", strategy="direct", root=repo)
    assert "new.py" in result["added"]
    assert result["filesChanged"] == 1


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_working_is_empty_after_the_change_is_committed(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "app.ts").write_text("export const x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    result = diff("ws", strategy="direct", root=repo)
    assert result["unified"] == ""
    assert result["filesChanged"] == 0


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_committed_compares_the_base_revision_to_head(tmp_path):
    repo, first = _repo(tmp_path)
    (repo / "app.ts").write_text("export const x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    result = diff("ws", base=first, mode="committed", strategy="direct", root=repo)
    assert result["mode"] == "committed"
    assert "-export const x = 1" in result["unified"]
    assert "+export const x = 2" in result["unified"]
    assert result["modified"] == ["app.ts"]
    assert result["base"] == f"{first}..HEAD"


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_revision_is_working_against_a_named_revision(tmp_path):
    repo, first = _repo(tmp_path)
    (repo / "app.ts").write_text("export const x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    result = diff("ws", base=first, strategy="direct", root=repo)
    assert result["mode"] == "working"
    assert "+export const x = 2" in result["unified"]
    assert result["modified"] == ["app.ts"]


def test_an_unknown_mode_is_refused(tmp_path):
    scratch_root(tmp_path)
    with pytest.raises(WorkspaceError) as excinfo:
        diff("ws", mode="sideways", root=tmp_path)
    assert excinfo.value.code == "DIFF_MODE_INVALID"


def test_a_snapshot_cannot_show_committed_history(tmp_path):
    scratch_root(tmp_path)
    (scratch_root(tmp_path) / "ws").mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError) as excinfo:
        diff("ws", mode="committed", strategy="snapshot", root=tmp_path)
    assert excinfo.value.code == "DIFF_MODE_UNAVAILABLE"
