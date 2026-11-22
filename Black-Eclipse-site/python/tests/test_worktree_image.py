"""Bounded picture preview from a workspace (gap #15 residual).

The route may only return a real image from inside the workspace. These tests pin
the two confinement rules (path and magic bytes) and the size bound, because the
endpoint is reachable from the review UI.
"""

from __future__ import annotations

import base64
import shutil
import subprocess

import pytest

from harness.code.worktree import WorkspaceError, read_image, scratch_root

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
GIT = shutil.which("git")


def _git(repo, *args):
    subprocess.run(
        [GIT, "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _workspace(tmp_path, workspace_id: str = "ws"):
    folder = scratch_root(tmp_path) / workspace_id
    folder.mkdir(parents=True)
    return folder


def test_returns_a_real_png_as_base64(tmp_path):
    folder = _workspace(tmp_path)
    (folder / "logo.png").write_bytes(PNG)
    result = read_image("ws", "logo.png", root=tmp_path)
    assert result["mime"] == "image/png"
    assert result["size"] == len(PNG)
    assert base64.b64decode(result["bytes"]) == PNG


def test_refuses_to_leave_the_workspace(tmp_path):
    _workspace(tmp_path)
    (tmp_path / "secret.png").write_bytes(PNG)
    for path in ("../secret.png", "/etc/passwd", "C:/Windows/win.ini", "a/../../secret.png"):
        with pytest.raises(WorkspaceError) as excinfo:
            read_image("ws", path, root=tmp_path)
        assert excinfo.value.code == "PATH_OUTSIDE_WORKSPACE"


def test_refuses_a_file_that_is_not_an_image(tmp_path):
    folder = _workspace(tmp_path)
    (folder / "notes.txt").write_bytes(b"just text, renamed to look harmless")
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "notes.txt", root=tmp_path)
    assert excinfo.value.code == "NOT_AN_IMAGE"


def test_refuses_an_oversized_picture(tmp_path):
    folder = _workspace(tmp_path)
    (folder / "big.png").write_bytes(PNG)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "big.png", root=tmp_path, max_bytes=8)
    assert excinfo.value.code == "IMAGE_TOO_LARGE"


def test_a_removed_picture_is_reported_as_missing(tmp_path):
    _workspace(tmp_path)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "gone.png", root=tmp_path)
    assert excinfo.value.code == "IMAGE_NOT_FOUND"


def test_an_unknown_workspace_is_reported(tmp_path):
    scratch_root(tmp_path)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("nope", "logo.png", root=tmp_path)
    assert excinfo.value.code == "UNKNOWN_WORKSPACE"


def test_an_invalid_side_is_refused(tmp_path):
    _workspace(tmp_path)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "logo.png", side="middle", root=tmp_path)
    assert excinfo.value.code == "IMAGE_SIDE_INVALID"


def test_a_snapshot_cannot_show_a_previous_picture(tmp_path):
    _workspace(tmp_path)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "logo.png", side="before", strategy="snapshot", root=tmp_path)
    assert excinfo.value.code == "IMAGE_BEFORE_UNAVAILABLE"


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_before_reads_the_base_revision_and_after_reads_the_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "logo.png").write_bytes(PNG)
    _git(repo, "add", "logo.png")
    _git(repo, "commit", "-m", "one")

    newer = PNG + b"newer-pixels"
    (repo / "logo.png").write_bytes(newer)

    before = read_image("ws", "logo.png", side="before", strategy="direct", root=repo)
    after = read_image("ws", "logo.png", side="after", strategy="direct", root=repo)
    assert base64.b64decode(before["bytes"]) == PNG
    assert base64.b64decode(after["bytes"]) == newer
    assert before["side"] == "before"
    assert after["side"] == "after"


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_before_refuses_a_picture_that_was_not_in_the_base_revision(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "readme.txt").write_text("hello", encoding="utf-8")
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-m", "one")
    (repo / "added.png").write_bytes(PNG)
    with pytest.raises(WorkspaceError) as excinfo:
        read_image("ws", "added.png", side="before", strategy="direct", root=repo)
    assert excinfo.value.code == "IMAGE_BEFORE_UNAVAILABLE"
