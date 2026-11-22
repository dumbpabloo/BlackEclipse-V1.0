"""Round 3 / W3 (ADR-H282) - ONE edit tool, and it insists you read the file.

The defect this file is the gate for: the harness had THREE ways to change a
file's contents (`workspace_patch_apply`, the fs `patch_file`/`replace_text`/
`insert_text`/`delete_text`/`multi_file_patch` family, and the natural-language
`edit_file` intent parser), and NONE of them required proof that the model had
seen the file it was about to change. `expectedHash` was accepted everywhere
and required NOWHERE - so the model could write to a file it had never read,
and a file that had changed underneath it was overwritten silently.

The rules this file asserts:

1. A surface carries ONE text-edit tool: `code_edit`.
2. The mutators it subsumes are DEPRECATED, still callable, and name their
   replacement. The genuinely DIFFERENT operations (create, delete, move,
   insert, line-range delete, unified diff) are NOT deprecated - removing them
   would remove capability, not duplication.
3. A file may only be changed if THIS RUN read it. The refusal is
   `FILE_NOT_READ` and it names the read to make.
4. A file may only be changed if it has not changed since it was read. The
   ledger's hash is the engine's `expected_hash`, so the engine's own
   `STALE_FILE` refusal fires - the same code, the same recovery hint.
5. An ambiguous match is refused, not guessed.
6. A wrong path comes back with the nearest candidates, not a bare "not found".
7. `code_edit` is a WRITE tool: `plan_only` removes it.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fs_helpers  # noqa: E402

from harness.agents import CHAT_CODE_TOOLS, CODE_AGENT_TOOLS, CODE_NAVIGATOR_TOOLS  # noqa: E402
from harness.code import edit_tools, fs_tools, policy  # noqa: E402
from harness.tools import ToolContext, ToolRegistry, read_ledger  # noqa: E402

# The text-edit mutators `code_edit` subsumes.
SUPERSEDED = {
    "replace_text": "code_edit",
    "patch_file": "code_edit",
    "edit_file": "code_edit",
    "multi_file_patch": "apply_diff",
    "workspace_patch_apply": "code_edit",
}

# The operations that are genuinely DIFFERENT and must survive.
DISTINCT = (
    "write_file", "create_file", "delete_file", "move_file", "copy_file",
    "rename_file", "create_directory", "delete_directory",
    "insert_text", "delete_text", "apply_diff",
)


# ── the rule: ONE text-edit tool per surface ──────────────────────────────


@pytest.mark.parametrize("surface", [CODE_NAVIGATOR_TOOLS, CODE_AGENT_TOOLS, CHAT_CODE_TOOLS])
def test_a_surface_carries_one_text_edit_tool(surface) -> None:
    assert "code_edit" in surface
    for name in SUPERSEDED:
        assert name not in surface, f"{name} is still in the surface"


def test_the_distinct_operations_survive() -> None:
    """Removing a capability is not the same as removing a duplicate."""
    assert set(DISTINCT) <= set(CHAT_CODE_TOOLS)
    assert set(DISTINCT) <= set(CODE_AGENT_TOOLS)


def test_the_superseded_mutators_are_deprecated_and_still_callable() -> None:
    from harness.code.tools import register_code_tools

    registry = ToolRegistry()
    register_code_tools(registry)
    for name, replacement in SUPERSEDED.items():
        tool = registry.get(name)
        assert tool is not None, f"{name} is not registered"
        assert tool.deprecated is True, f"{name} must be deprecated"
        assert tool.replacement == replacement


def test_code_edit_is_a_write_tool() -> None:
    """`plan_only` must be able to restrain it."""
    assert "code_edit" in policy.WRITE_TOOLS
    allowed = policy.allowed_tools("plan_only", ("workspace_read", "code_edit", "command_run"))
    assert "code_edit" not in allowed
    assert "workspace_read" in allowed


# ── the rule: the run must have READ the file ─────────────────────────────


def _ctx(ws, autonomy="edit_verify"):
    return ToolContext(run_id="r1", log=lambda *_: None, workspace=ws, autonomy=autonomy)


def test_a_read_feeds_the_per_run_ledger(tmp_path) -> None:
    """One place, every read tool: the registry records the hash it returned."""
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    result = service.read_file("src/app.py")
    assert result["status"] == "ok"
    assert result["sha256"]

    ctx = _ctx(fs_helpers.make_ws(root))
    assert read_ledger(ctx) == {}
    from harness.tools import record_read

    record_read(ctx, result["path"], result["sha256"])
    assert read_ledger(ctx) == {result["path"]: result["sha256"]}


@pytest.mark.asyncio
async def test_read_file_suggests_candidates_for_a_wrong_path(tmp_path) -> None:
    ws = fs_helpers.make_ws(fs_helpers.tree(tmp_path))
    ctx = _ctx(ws)
    text, payload = await fs_tools._read_file({"path": "src/ap.py"}, ctx)
    assert payload["status"] == "refused"
    assert payload["code"] in ("PATH_NOT_FOUND", "NOT_FOUND")
    candidates = (payload.get("details") or {}).get("candidates") or []
    assert any(c.endswith("app.py") for c in candidates), candidates
    assert "Did you mean" in text


@pytest.mark.asyncio
async def test_editing_a_file_the_run_never_read_is_refused(tmp_path) -> None:
    ws = fs_helpers.make_ws(fs_helpers.tree(tmp_path))
    ctx = _ctx(ws)
    _text, payload = await edit_tools.code_edit(
        {"path": "src/app.py", "oldText": "alpha", "newText": "beta"}, ctx,
    )
    assert payload["status"] == "refused"
    assert payload["code"] == "FILE_NOT_READ"
    # The refusal names the read that would make the edit legal.
    assert payload["details"]["readTool"] == "read_file"


@pytest.mark.asyncio
async def test_the_refusal_offers_the_nearest_paths(tmp_path) -> None:
    """A wrong path comes back with candidates, not a bare 'not found'."""
    root = fs_helpers.tree(tmp_path)
    ws = fs_helpers.make_ws(root)
    ctx = _ctx(ws)
    _text, payload = await edit_tools.code_edit(
        {"path": "src/ap.py", "oldText": "alpha", "newText": "beta"}, ctx,
    )
    assert payload["code"] == "FILE_NOT_READ"
    candidates = payload["details"].get("candidates") or []
    assert candidates, "the refusal offered no candidates"
    assert any(c.endswith("app.py") for c in candidates), candidates


def test_nearest_paths_prefers_an_exact_basename(tmp_path) -> None:
    root = fs_helpers.tree(tmp_path)
    ws = fs_helpers.make_ws(root)
    candidates = edit_tools.nearest_paths(ws, "src/other.py")
    assert candidates
    assert candidates[0].endswith("other.py")


# ── the rule: the edit itself is the engine's, with the ledger's hash ─────


@pytest.mark.asyncio
async def test_the_edit_runs_with_the_ledgers_hash(tmp_path, monkeypatch) -> None:
    """The happy path, and the hash the guard uses."""
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    read = service.read_file("src/app.py")

    seen: dict = {}

    def _fake_write_service(ctx):
        def _replace_text(path, old_text, new_text, **kwargs):
            seen.update(kwargs)
            seen["path"] = path
            seen["old"] = old_text
            seen["new"] = new_text
            return {
                "status": "ok", "path": path, "occurrences": 1,
                "sha256Before": read["sha256"], "sha256After": "after",
            }
        return type("S", (), {"replace_text": staticmethod(_replace_text)})()

    monkeypatch.setattr(fs_tools, "_write_service", _fake_write_service)
    ws = fs_helpers.make_ws(root)
    ctx = _ctx(ws)
    from harness.tools import record_read

    record_read(ctx, read["path"], read["sha256"])
    text, payload = await edit_tools.code_edit(
        {"path": "src/app.py", "oldText": "alpha", "newText": "beta"}, ctx,
    )
    assert payload["status"] == "ok"
    assert payload["readVerified"] is True
    # The guard's hash IS the engine's `expected_hash`.
    assert seen["expected_hash"] == read["sha256"]
    assert seen["old"] == "alpha"
    assert seen["new"] == "beta"


def test_the_engine_refuses_a_file_that_changed_since_the_read(tmp_path) -> None:
    """The staleness rule is the engine's own - one code, one recovery hint."""
    root = fs_helpers.tree(tmp_path)
    service = fs_helpers.make_service(root)
    read = service.read_file("src/app.py")
    # The file changes under the run.
    (root / "src" / "app.py").write_text("alpha\nbeta\nCHANGED\n", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        service.replace_text(
            "src/app.py", "alpha", "x", count=1, expected_hash=read["sha256"],
        )
    assert getattr(excinfo.value, "code", "") == "STALE_FILE"


def test_the_engine_refuses_an_ambiguous_match(tmp_path) -> None:
    """An ambiguous match is refused, never guessed."""
    root = fs_helpers.tree(tmp_path)
    (root / "src" / "dup.py").write_text("x\nx\n", encoding="utf-8")
    service = fs_helpers.make_service(root)
    with pytest.raises(Exception) as excinfo:
        service.replace_text("src/dup.py", "x", "y", count=1)
    assert getattr(excinfo.value, "code", "") == "AMBIGUOUS_PATCH"
