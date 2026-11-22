"""Gap #5: a successful write formats the file and reports diagnostics.

The failure this gates: an edit is applied, the project's formatter never runs,
and a type/syntax error introduced by the edit is invisible until the model
happens to run a whole-project check. The contract:

1. The PROJECT's formatter runs on the saved file, bounded and non-fatal; a
   formatter failure leaves the written text intact.
2. Diagnostics for the file ride on the edit/write result and in the model-facing
   text, not only on a separate check tool.
3. Diagnostics are bounded: a slow/unavailable language server degrades to the
   structural syntax check with an honest `lsp` status, never a hung edit.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from harness.code import edit_tools, formatting, fs_tools, post_edit
from harness.code.fs import errors as err
from harness.code.intel import runtime as intel_runtime
from harness.tools import ToolContext, record_read

import fs_helpers


class _FakeManager:
    def __init__(self, *, delay_s: float = 0.0, items=None) -> None:
        self.delay_s = delay_s
        self.items = items or [{
            "severity": "error", "file": "src/app.py", "line": 2, "column": 3,
            "message": "boom: undefined name", "source": "lsp", "code": "E1",
        }]
        self.synced: list[str] = []

    def sync_document(self, file: str) -> bool:
        self.synced.append(file)
        return True

    def get_diagnostics(self, *, file=None, severity=None):  # noqa: ARG002
        if self.delay_s:
            time.sleep(self.delay_s)
        return {"engine": "lsp", "file": file, "diagnostics": self.items,
                "count": len(self.items), "summary": {}}


class _FakeRuntime:
    def __init__(self, manager: _FakeManager) -> None:
        self.manager = manager

    def is_ready_for(self, _ws) -> bool:
        return True

    def manager_for(self, _ws) -> _FakeManager:
        return self.manager


@pytest.fixture()
def live_runtime():
    old = intel_runtime.peek_code_intel_runtime()
    yield
    intel_runtime.set_code_intel_runtime(old)


def _install_runtime(manager: _FakeManager) -> None:
    intel_runtime.set_code_intel_runtime(_FakeRuntime(manager))


def _fake_formatter(*, text_append: str = "\n# formatted\n", exit_code: int = 0):
    def _run(formatter):
        path = formatting.Path(formatter.argv[-1])
        if exit_code == 0:
            path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + text_append, encoding="utf-8")
        return exit_code, "formatter said no" if exit_code else ""

    return _run


def _ctx(ws) -> ToolContext:
    return ToolContext(run_id="r-post", log=lambda *_: None, workspace=ws, autonomy="edit_verify")


def test_the_project_formatter_runs_on_the_saved_file(tmp_path, monkeypatch):
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    monkeypatch.setattr(formatting, "detect", lambda r, rel, target=None: formatting.Formatter(
        "fakefmt", ("fakefmt", str(target or (r / rel))), formatting.Path(r),
    ))
    monkeypatch.setattr(formatting, "_run", _fake_formatter())

    result = service.replace_text("src/app.py", "beta", "BETA")

    assert result["format"]["status"] == "ok"
    assert result["format"]["formatter"] == "fakefmt"
    assert result["format"]["changed"] is True
    saved = (root / "src" / "app.py").read_text(encoding="utf-8")
    assert saved.endswith("# formatted\n")
    assert "BETA" in saved
    # The diff/hash describe the formatted bytes, not the pre-format text.
    assert "formatted" in result["diff"]


def test_a_formatter_failure_is_non_fatal_and_leaves_the_write_intact(tmp_path, monkeypatch):
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    monkeypatch.setattr(formatting, "detect", lambda r, rel, target=None: formatting.Formatter(
        "fakefmt", ("fakefmt", str(target or (r / rel))), formatting.Path(r),
    ))
    monkeypatch.setattr(formatting, "_run", _fake_formatter(exit_code=1))

    result = service.replace_text("src/app.py", "beta", "BETA")

    assert result["status"] == "ok"
    assert result["format"]["status"] == "failed"
    assert "formatter said no" in result["format"]["reason"]
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_diagnostics_reach_the_code_edit_result_and_text(tmp_path, monkeypatch, live_runtime):
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    ws = fs_helpers.make_ws(root)
    read = service.read_file("src/app.py")
    ctx = _ctx(ws)
    record_read(ctx, read["path"], read["sha256"])
    _install_runtime(_FakeManager())
    monkeypatch.setattr(fs_tools, "_write_service", lambda _ctx: service)

    text, payload = asyncio.run(edit_tools.code_edit(
        {"path": "src/app.py", "oldText": "beta", "newText": "BETA"}, ctx,
    ))

    assert payload["status"] == "ok"
    diagnostics = payload["diagnostics"]
    assert diagnostics["count"] == 1
    assert diagnostics["diagnostics"][0]["message"].startswith("boom")
    assert "Diagnostics (1):" in text
    assert "ERROR src/app.py:2:3 boom" in text


def test_diagnostics_reach_the_write_file_summary(tmp_path, monkeypatch, live_runtime):
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    ctx = _ctx(fs_helpers.make_ws(root))
    _install_runtime(_FakeManager())
    monkeypatch.setattr(fs_tools, "_write_service", lambda _ctx: service)

    text, payload = asyncio.run(fs_tools._write_file(
        {"path": "src/new.py", "content": "def f(  ):\n    return 1\n",
         "createIfMissing": True, "overwrite": True},
        ctx,
    ))

    assert payload["status"] == "ok"
    assert payload["diagnostics"]["count"] == 1
    assert "Diagnostics (1):" in text
    assert "boom" in text


def test_a_slow_language_server_degrades_to_structural_diagnostics(tmp_path, live_runtime):
    root = fs_helpers.tree(tmp_path)
    ws = fs_helpers.make_ws(root)
    _install_runtime(_FakeManager(delay_s=0.5))

    started = time.monotonic()
    result = post_edit.diagnostics_for(ws, "src/app.py", "alpha\nbeta\n", timeout_s=0.05)
    elapsed = time.monotonic() - started

    assert result["status"] == "ok"
    assert result["lsp"] == "timeout"
    assert result["engine"] == "structural"
    assert elapsed < 0.4


def test_no_runtime_means_structural_only_not_a_created_index(tmp_path, live_runtime):
    root = fs_helpers.tree(tmp_path)
    ws = fs_helpers.make_ws(root)
    intel_runtime.set_code_intel_runtime(None)

    result = post_edit.diagnostics_for(ws, "src/app.py", "alpha\nbeta\n")

    assert result["status"] == "ok"
    assert result["lsp"] == "unavailable"
    assert result["engine"] == "structural"
    assert intel_runtime.peek_code_intel_runtime() is None


def test_the_lsp_is_synced_with_the_saved_text_before_diagnostics(tmp_path, live_runtime):
    root = fs_helpers.tree(tmp_path)
    ws = fs_helpers.make_ws(root)
    manager = _FakeManager()
    intel_runtime.set_code_intel_runtime(_FakeRuntime(manager))

    result = post_edit.diagnostics_for(ws, "src/app.py", "alpha\nBETA\ngamma\n")

    assert result["lsp"] == "ok"
    assert manager.synced == ["src/app.py"], "the server was never given the edited text"


def test_detect_recognises_a_configured_prettier_project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text('{"devDependencies": {"prettier": "3.0.0"}}', encoding="utf-8")
    (root / ".prettierrc").write_text("{}", encoding="utf-8")
    (root / "src" / "a.ts").write_text("const x=1\n", encoding="utf-8")
    monkeypatch.setattr(formatting, "_prettier_binary", lambda _root: ("node", "prettier.cjs"))

    formatter = formatting.detect(root, "src/a.ts", root / "src" / "a.ts")
    assert formatter is not None
    assert formatter.name == "prettier"
    assert "--write" in formatter.argv
    assert formatting.detect(root, "src/README.unknown", root / "src" / "README.unknown") is None
