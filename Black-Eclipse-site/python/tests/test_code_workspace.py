"""Phase 1 — workspace resolution and confinement (read-only).

Proves the three escape vectors produce DISTINCT structured codes, that the
confinement order makes `jdds.db` a `PROTECTED_PATH` (never `BINARY_FILE`), and
that the walker is bounded and ignore-aware. `confine` is the only place a path
is resolved — it delegates to the Phase 0 `policy.resolve_within`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.code import policy
from harness.code.workspace import (
    ResolvedWorkspace,
    WorkspaceDenial,
    confine,
    default_root,
    git_probe,
    resolve_workspace,
    walk,
)

FIXTURES = Path(__file__).parent / "fixtures" / "code"


def _make_escape_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    return False


def _fixture_ws(tmp_path: Path) -> ResolvedWorkspace:
    root = tmp_path / "project"
    shutil.copytree(FIXTURES, root)
    real = Path(os.path.realpath(root))
    binding = policy.WorkspaceBinding(id="ws_fixture", root=real, base_revision="unknown", strategy="checkout")
    return ResolvedWorkspace(
        id="ws_fixture", root=real, base_revision="unknown", strategy="checkout", binding=binding,
    )


# ── default_root / resolve_workspace ────────────────────────────────────────


def test_default_root_finds_the_project(monkeypatch):
    monkeypatch.delenv("BLACK_ECLIPSE_CODE_ROOT", raising=False)
    root = default_root()
    assert root is not None
    # Mirrors `eclipse_memory.config.repo_root()`: the site root, inside the
    # git checkout (the `.git` dir is an ancestor, not a child).
    assert (root / "python" / "harness").is_dir()
    assert any((parent / ".git").exists() for parent in [root, *root.parents])


def test_default_root_honours_the_env_override(tmp_path, monkeypatch):
    override = tmp_path / "code-root"
    override.mkdir()
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(override))
    root = default_root()
    assert root is not None
    assert os.path.samefile(root, override)


def test_default_root_is_none_when_the_override_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(tmp_path / "does-not-exist"))
    assert default_root() is None


def test_resolve_workspace_confirms_the_server_root(monkeypatch):
    monkeypatch.delenv("BLACK_ECLIPSE_CODE_ROOT", raising=False)
    ws = resolve_workspace(None)
    assert ws is not None
    assert ws.strategy == "checkout"
    # A matching request root is accepted.
    class Req:
        root = str(ws.root)

    assert resolve_workspace(Req()) is not None


def test_resolve_workspace_refuses_a_mismatched_request_root(monkeypatch, tmp_path):
    monkeypatch.delenv("BLACK_ECLIPSE_CODE_ROOT", raising=False)

    class Req:
        root = str(tmp_path)

    assert resolve_workspace(Req()) is None


def test_resolve_bot_workspace_is_server_owned_and_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(tmp_path))
    ws = resolve_workspace(None, root_id="bot:auto_1")
    assert ws is not None
    expected = tmp_path / ".black-eclipse" / "bots" / "auto_1"
    assert expected.is_dir()
    assert ws.root == expected.resolve()
    # A hostile id can never escape the bot root.
    ws = resolve_workspace(None, root_id="bot:../../outside")
    assert ws is not None
    assert ws.root.parent == (tmp_path / ".black-eclipse" / "bots").resolve()


def test_resolve_workspace_allows_a_real_subdirectory(monkeypatch):
    """A request may NARROW the server root, never widen it."""
    monkeypatch.delenv("BLACK_ECLIPSE_CODE_ROOT", raising=False)
    base = resolve_workspace(None)
    assert base is not None
    child = base.root / ".black-eclipse" / "bots" / "auto_test"
    child.mkdir(parents=True, exist_ok=True)

    class Req:
        root = str(child)

    narrowed = resolve_workspace(Req())
    assert narrowed is not None
    assert narrowed.root == child.resolve()


# ── confine: the three escape vectors, distinct codes ───────────────────────


def test_confine_traversal_is_refused(tmp_path):
    ws = _fixture_ws(tmp_path)
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, "../outside.txt")
    assert exc.value.code == "PATH_TRAVERSAL"


def test_confine_absolute_outside_is_refused(tmp_path):
    ws = _fixture_ws(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, str(outside))
    assert exc.value.code == "PATH_OUTSIDE_WORKSPACE"


def test_confine_symlink_escape_is_refused(tmp_path):
    ws = _fixture_ws(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    link = ws.root / "escape"
    if not _make_escape_link(link, outside_dir):
        pytest.skip("this host allows neither symlinks nor junctions")
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, "escape/secret.txt")
    assert exc.value.code == "SYMLINK_ESCAPE"


def test_confine_returns_the_real_path_for_a_legal_file(tmp_path):
    ws = _fixture_ws(tmp_path)
    resolved = confine(ws, "src/app.py")
    assert resolved.is_file()
    assert str(ws.root) in str(resolved)


def test_confine_refuses_a_protected_file_before_binary(tmp_path):
    ws = _fixture_ws(tmp_path)
    # jdds.db is BOTH protected and binary-shaped; PROTECTED_PATH must win.
    (ws.root / "jdds.db").write_bytes(b"\x00binary\x00data")
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, "jdds.db")
    assert exc.value.code == "PROTECTED_PATH"
    assert exc.value.code != "BINARY_FILE"


def test_confine_refuses_an_ignored_path(tmp_path):
    ws = _fixture_ws(tmp_path)
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, "node_modules/pkg/index.js")
    assert exc.value.code == "IGNORED_PATH"


def test_confine_refuses_a_missing_file(tmp_path):
    ws = _fixture_ws(tmp_path)
    with pytest.raises(WorkspaceDenial) as exc:
        confine(ws, "src/does-not-exist.py")
    assert exc.value.code == "NOT_FOUND"


def test_confinement_order_lists_the_fixed_codes():
    from harness.code.workspace import CONFINEMENT_ORDER

    assert CONFINEMENT_ORDER[:3] == ("PATH_TRAVERSAL", "PATH_OUTSIDE_WORKSPACE", "SYMLINK_ESCAPE")
    assert "PROTECTED_PATH" in CONFINEMENT_ORDER
    assert "BINARY_FILE" in CONFINEMENT_ORDER


# ── git_probe / walk ────────────────────────────────────────────────────────


def test_git_probe_reads_the_repo_head(monkeypatch):
    monkeypatch.delenv("BLACK_ECLIPSE_CODE_ROOT", raising=False)
    ws = resolve_workspace(None)
    assert ws is not None
    head = git_probe(ws, "head")
    assert head.get("available") is True
    assert head.get("head")


def test_git_probe_reports_unavailable_outside_a_checkout(tmp_path):
    ws = _fixture_ws(tmp_path)
    status = git_probe(ws, "status")
    assert status.get("available") is False
    assert status.get("reason")


def test_walk_is_bounded_and_ignore_aware(tmp_path):
    ws = _fixture_ws(tmp_path)
    entries, truncated = walk(ws, ".", depth=3, max_entries=1000)
    paths = {e["path"] for e in entries}
    assert "src/app.py" in paths
    assert not any(p.startswith("node_modules") for p in paths)
    assert truncated is False


def test_walk_truncates_at_the_bound(tmp_path):
    ws = _fixture_ws(tmp_path)
    entries, truncated = walk(ws, ".", depth=3, max_entries=2)
    assert len(entries) == 2
    assert truncated is True


# ── the policy authority is the only confinement implementation ─────────────


def test_confine_delegates_to_policy_resolve_within(tmp_path, monkeypatch):
    ws = _fixture_ws(tmp_path)
    called = {"n": 0}
    original = policy.resolve_within

    def _spy(root, candidate, **kwargs):
        # `confine` forwards `allow_ignored` to the policy authority; the spy must
        # pass it through so this still asserts delegation, not the call shape.
        called["n"] += 1
        return original(root, candidate, **kwargs)

    monkeypatch.setattr(policy, "resolve_within", _spy)
    confine(ws, "src/app.py")
    assert called["n"] == 1
