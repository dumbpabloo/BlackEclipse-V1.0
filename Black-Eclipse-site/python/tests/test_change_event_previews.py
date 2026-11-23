"""Change events — the bounded, redacted diff preview the UI renders.

The chat and the Agent Workspace show WHAT the agent changed from these events.
These tests pin the contract on the REAL registry path: the preview shape, secret
redaction, dry-run silence, and the hard cap at the event seam.
"""

from __future__ import annotations

import asyncio

from harness.code.events import EVENT_DIFF_MAX_CHARS, diff_event_payload

from fs_helpers import make_ws, tree


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _ctx(root):
    from harness.code.tools import make_code_authorizer, register_code_tools
    from harness.tools import ToolContext, ToolRegistry

    registry = ToolRegistry()
    register_code_tools(registry)
    ctx = ToolContext(
        run_id="run_change", log=lambda *_: None, workspace=make_ws(root),
        autonomy="edit_verify", authorize_effect=make_code_authorizer(), direct=True,
    )
    emitter = _Emitter()
    ctx.emitter = emitter
    return registry, ctx, emitter


def _call(registry, name, args, ctx):
    return asyncio.run(registry.execute(name, f"call_{name}", args, ctx))


def _events(emitter, name):
    return [d for t, d in emitter.events if t == name]


def test_edit_file_event_carries_a_diff_preview(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    registry, ctx, emitter = _ctx(root)
    result = _call(registry, "edit_file", {
        "path": "src/app.py", "instruction": "rename beta to BETA",
        "oldText": "beta", "newText": "BETA",
    }, ctx)
    assert result.ok
    applied = _events(emitter, "fs.edit_file.applied")
    assert applied and applied[-1]["previews"]
    preview = applied[-1]["previews"][0]
    assert preview["path"] == "src/app.py"
    assert "+BETA" in preview["diff"] and "-beta" in preview["diff"]
    assert preview["additions"] == 1 and preview["deletions"] == 1
    assert preview["diffTruncated"] is False


def test_write_file_event_redacts_secret_shaped_values(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    registry, ctx, emitter = _ctx(root)
    result = _call(registry, "write_file", {
        "path": "src/config.py", "content": 'API_KEY = "abcdef123456"\n', "createIfMissing": True,
    }, ctx)
    assert result.ok
    events = _events(emitter, "fs.write_file")
    assert events and events[-1]["previews"]
    preview = events[-1]["previews"][0]
    # The diff is shown, but a secret-shaped value never rides the event stream.
    assert "abcdef123456" not in preview["diff"]
    assert "«redacted»" in preview["diff"]


def test_write_file_reports_the_absolute_path_it_wrote(tmp_path, monkeypatch):
    """A write must be answerable with a real path — "where is it?" has no
    future if the tool only says "src/config.py"."""
    root = tree(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    registry, ctx, _emitter = _ctx(root)
    result = _call(registry, "write_file", {
        "path": "src/config.py", "content": "VALUE = 1\n", "createIfMissing": True,
    }, ctx)
    assert result.ok
    absolute = result.data["absolutePath"]
    import os
    assert os.path.isabs(absolute)
    assert os.path.realpath(absolute) == os.path.realpath(root / "src" / "config.py")
    assert absolute in result.text


def test_dry_run_emits_no_preview(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    registry, ctx, emitter = _ctx(root)
    result = _call(registry, "edit_file", {
        "path": "src/app.py", "instruction": "x", "oldText": "beta", "newText": "BETA",
        "dryRun": True,
    }, ctx)
    assert result.data["dryRun"] is True
    # The event still fires (audit), but a dry run changed nothing: no preview.
    for data in _events(emitter, "fs.edit_file.applied"):
        assert "previews" not in data


def test_no_op_edit_emits_no_change_card(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(root))
    registry, ctx, emitter = _ctx(root)
    result = _call(registry, "edit_file", {
        "path": "src/app.py", "instruction": "keep beta",
        "oldText": "beta", "newText": "beta",
    }, ctx)
    assert result.ok
    assert result.data.get("changed") is False
    for data in _events(emitter, "fs.edit_file.applied"):
        assert "previews" not in data


def test_diff_event_payload_is_capped_and_flagged():
    huge = "\n".join(f"+line {i}" for i in range(EVENT_DIFF_MAX_CHARS))
    payload = diff_event_payload({"diff": huge, "additions": 1, "deletions": 0})
    assert payload is not None
    assert payload["diffTruncated"] is True
    assert len(payload["diff"]) <= EVENT_DIFF_MAX_CHARS + 100
    assert payload["additions"] == 1


def test_diff_event_payload_none_without_a_diff():
    assert diff_event_payload({"path": "src/x.py"}) is None
    assert diff_event_payload(None) is None
    assert diff_event_payload({"diff": "   "}) is None
