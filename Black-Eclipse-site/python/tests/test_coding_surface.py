"""Gap #6: a coding run gets a coding surface, not the whole DevOps catalogue.

The defect this gates: `CODE_AGENT_TOOLS` declared ~380 entries, so a coding run
advertised runtime/container/cloud/orchestration/package/browser/computer tools
it almost never needs. The preset is structural: the tool is REMOVED before the
model sees its schema. It stays discoverable through `tool_search` (marked
`outsideSurface`), and an operator can add it with
`BLACK_ECLIPSE_CODING_EXTRA_TOOLS` - the model cannot widen its own surface.
"""

from __future__ import annotations

import asyncio

import pytest

from harness.agents import CHAT_CODE_TOOLS, CODE_AGENT, ECLIPSE_AGENT
from harness.code import policy
from harness.surface import chat_code_allowlist, effective_allowlist, resolve_surface_detail
from harness.tool_surface import register_tool_surface_tools
from harness.tools import ToolContext, ToolDefinition, ToolRegistry

NON_CODING = (
    "container_run", "container_build", "runtime_start", "package_install",
    "browser_launch", "computer_click", "workspace_start", "cloud_deploy",
)


def _tool(name: str, *, permission: str = "read", effect=None, description: str = "") -> ToolDefinition:
    async def _handler(_args, _ctx):
        return name, {"status": "ok"}

    return ToolDefinition(
        name=name, description=description or f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, permission=permission, risk="low", effect=effect,
    )


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_tool(name))
    return reg


def test_the_coding_preset_is_about_twenty_tools():
    surface = policy.coding_surface("edit_verify")
    assert 20 <= len(surface) <= 48, len(surface)
    for name in ("code_edit", "write_file", "read_file", "search_text", "command_run",
                 "shell_spawn", "tool_search", "tool_invoke"):
        assert name in surface, name
    for name in NON_CODING:
        assert name not in surface, name


def test_the_code_agent_surface_drops_non_coding_effectful_tools(monkeypatch):
    monkeypatch.delenv(policy.CODING_SURFACE_ENV, raising=False)
    monkeypatch.delenv(policy.CODING_EXTRA_ENV, raising=False)
    registry = _registry("code_edit", "read_file", *NON_CODING)

    allowed = effective_allowlist(CODE_AGENT, registry, "edit_verify")

    assert allowed is not None
    assert "code_edit" in allowed and "read_file" in allowed
    for name in NON_CODING:
        assert name not in allowed, f"{name} survived the coding preset"
    assert len(allowed) <= 48


def test_an_operator_can_enable_a_removed_tool(monkeypatch):
    monkeypatch.setenv(policy.CODING_EXTRA_ENV, "container_run")
    monkeypatch.delenv(policy.CODING_SURFACE_ENV, raising=False)
    registry = _registry("code_edit", "container_run")

    allowed = effective_allowlist(CODE_AGENT, registry, "edit_verify")

    assert allowed is not None
    assert "container_run" in allowed
    assert "code_edit" in allowed


def test_the_preset_can_be_disabled_entirely(monkeypatch):
    monkeypatch.setenv(policy.CODING_SURFACE_ENV, "0")
    monkeypatch.delenv(policy.CODING_EXTRA_ENV, raising=False)
    registry = _registry("code_edit", "container_run")

    allowed = effective_allowlist(CODE_AGENT, registry, "edit_verify")

    assert allowed is not None and "container_run" in allowed


def test_an_extra_write_tool_is_still_removed_at_plan_only(monkeypatch):
    monkeypatch.setenv(policy.CODING_EXTRA_ENV, "code_edit")
    monkeypatch.delenv(policy.CODING_SURFACE_ENV, raising=False)
    assert "code_edit" not in policy.coding_surface("plan_only")


def test_the_chat_code_surface_is_narrowed_too(monkeypatch):
    monkeypatch.delenv(policy.CODING_SURFACE_ENV, raising=False)
    monkeypatch.delenv(policy.CODING_EXTRA_ENV, raising=False)
    registry = _registry("code_edit", *NON_CODING)
    assert "runtime_start" in CHAT_CODE_TOOLS

    allowed = chat_code_allowlist(ECLIPSE_AGENT, registry, "edit_verify")

    assert allowed is not None
    assert "code_edit" in allowed
    for name in NON_CODING:
        assert name not in allowed, name


def test_a_removed_tool_is_refused_by_tool_invoke():
    registry = _registry("read_file", "container_run")
    register_tool_surface_tools(registry)
    ctx = ToolContext(
        run_id="r-surface", log=lambda *_: None,
        allowed={"tool_search", "tool_invoke", "read_file"},
        autonomy="edit_verify",
    )
    ctx.tool_registry = registry

    result = asyncio.run(registry.execute(
        "tool_invoke", "call_1",
        {"name": "container_run", "arguments": {}}, ctx, allowed=ctx.allowed,
    ))
    assert result.data["status"] == "REFUSED"
    assert "outside the surface" in result.text
    assert result.data["reason"] == "outside the surface"


def test_tool_search_discovers_an_outside_tool_and_marks_it():
    registry = _registry("read_file")
    registry.register(_tool(
        "container_run", permission="write", effect="external_action",
        description="Run a container image with port mappings.",
    ))
    register_tool_surface_tools(registry)
    ctx = ToolContext(
        run_id="r-surface", log=lambda *_: None,
        allowed={"tool_search", "tool_invoke", "read_file"},
        autonomy="edit_verify",
    )
    ctx.tool_registry = registry

    result = asyncio.run(registry.execute(
        "tool_search", "call_2", {"query": "container"}, ctx, allowed=ctx.allowed,
    ))
    assert result.data["status"] == "OK"
    rows = {row["name"]: row for row in result.data["tools"]}
    assert "container_run" in rows
    row = rows["container_run"]
    assert row.get("outsideSurface") is True
    assert row.get("requiresEnable") is True
    assert policy.CODING_EXTRA_ENV in row.get("enableHint", "")
    assert "container_run" in result.text


def test_the_resolution_names_what_the_preset_removed(monkeypatch):
    monkeypatch.delenv(policy.CODING_SURFACE_ENV, raising=False)
    monkeypatch.delenv(policy.CODING_EXTRA_ENV, raising=False)
    registry = _registry("code_edit", "container_run")

    detail = resolve_surface_detail(CODE_AGENT, registry, {}, "edit_verify")

    assert detail.presetRemoved
    assert "container_run" in detail.presetRemoved
    assert detail.to_dict()["presetRemoved"]
    assert detail.removed == ()
