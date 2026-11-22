"""Trust Mode autoland: one call to land, automatic undo on a failed check."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.code import autoland, promotion, worktree
from harness.code.promotion import PromotionError


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _git_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _write(repo / "src" / "app.py", "print('hello')\n")
    _write(repo / ".gitignore", ".black-eclipse/\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = _git_fixture(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    yield root
    shutil.rmtree(root / worktree.SCRATCH_RELPATH, ignore_errors=True)


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def list_workspaces(self, *, run_id=None, limit=100, scope_id=None):
        return [row for row in self._rows if row.get("runId") == run_id][:limit]


def _prepare(repo: Path, run_id: str) -> tuple[Path, _Store]:
    ws = worktree.allocate(base_revision="HEAD")
    _write(ws.root / "src" / "app.py", "print('world')\n")
    row = {
        "id": ws.id, "root": str(ws.root), "strategy": ws.strategy,
        "baseRevision": ws.base_revision, "releasedAt": None, "runId": run_id,
        "checkoutRoot": str(repo),
    }
    return repo / "src" / "app.py", _Store([row])


def test_autoland_applies_when_checks_pass_and_the_undo_restores(repo):
    target, store = _prepare(repo, "run-pass")
    result = autoland.autoland(
        "run-pass", code_store=store, checkout_root=repo,
        verify=lambda files, root: {"ok": True, "checks": []},
    )
    assert result["ok"] is True and result["applied"] is True
    assert result["rolledBack"] is False
    assert target.read_text(encoding="utf-8") == "print('world')\n"
    assert result["rollbackId"]

    promotion.rollback_promotion(result["rollbackId"], checkout_root=repo)
    assert target.read_text(encoding="utf-8") == "print('hello')\n"


def test_a_failed_check_undoes_the_change_automatically(repo):
    target, store = _prepare(repo, "run-fail")
    result = autoland.autoland(
        "run-fail", code_store=store, checkout_root=repo,
        verify=lambda files, root: {"ok": False, "failed": ["tests"]},
    )
    assert result["ok"] is False and result["applied"] is False
    assert result["rolledBack"] is True
    assert result["checks"]["failed"] == ["tests"]
    assert target.read_text(encoding="utf-8") == "print('hello')\n"


def test_a_crashing_check_is_a_failure_and_still_undoes(repo):
    target, store = _prepare(repo, "run-crash")

    def boom(files, root):
        raise RuntimeError("runner crashed")

    result = autoland.autoland("run-crash", code_store=store, checkout_root=repo, verify=boom)
    assert result["ok"] is False
    assert result["rolledBack"] is True
    assert "runner crashed" in result["checks"]["error"]
    assert target.read_text(encoding="utf-8") == "print('hello')\n"


def test_autoland_without_a_change_is_an_honest_error(repo):
    _target, store = _prepare(repo, "run-none")
    with pytest.raises(PromotionError) as info:
        autoland.autoland("run-other", code_store=store, checkout_root=repo)
    assert info.value.code == "PROMOTION_NO_CHANGE"


def test_autoland_route_is_operator_gated_and_returns_the_outcome(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from harness.deps import require_operator
    from harness.routers import code as code_router

    fake = {
        "ok": True, "applied": True, "rolledBack": False, "rollbackId": "rb-1",
        "preview": {"previewDigest": "digest"}, "checks": {"ok": True},
    }
    monkeypatch.setattr(code_router, "service", SimpleNamespace(
        get_run=lambda task_id: {"id": task_id},
        code_store=None,
        run_lineage=lambda: SimpleNamespace(save_checkpoint=lambda *a, **k: {"id": "cp"}),
    ))
    monkeypatch.setattr(autoland, "autoland", lambda *a, **k: fake)
    app = FastAPI()
    app.include_router(code_router.router)
    app.dependency_overrides[require_operator] = lambda: True
    with TestClient(app) as client:
        response = client.post("/code/tasks/run-ok/autoland")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True and body["rollbackId"] == "rb-1"
