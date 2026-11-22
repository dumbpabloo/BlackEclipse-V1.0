"""Round 5 / C3: discovery, safe read-only dispatch, and the lazy surface."""

from __future__ import annotations

import json

import pytest

from harness.tool_surface import (
    LAZY_CORE_TOOLS,
    lazy_tool_surface,
    register_tool_surface_tools,
)
from harness.tools import ToolContext, ToolDefinition, ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def read_handler(args, _ctx):
        return f"read:{args.get('symbol')}"

    async def write_handler(_args, _ctx):
        return "wrote"

    reg.register(ToolDefinition(
        name="market_get_quote", description="Read a quote for a symbol.",
        input_schema={"type": "object", "properties": {"symbol": {"type": "string"}},
                      "required": ["symbol"]},
        handler=read_handler, permission="read", risk="none",
    ))
    reg.register(ToolDefinition(
        name="portfolio_write", description="Write a portfolio position.",
        input_schema={"type": "object", "properties": {}},
        handler=write_handler, permission="write", risk="medium",
    ))
    register_tool_surface_tools(reg)
    return reg


def _ctx(reg: ToolRegistry) -> ToolContext:
    return ToolContext(run_id="run_1", log=lambda *_: None, tool_registry=reg)


@pytest.mark.asyncio
async def test_search_returns_the_schema_for_a_matching_tool():
    reg = _registry()
    result = await reg.execute("tool_search", "c1", {"query": "quote"}, _ctx(reg))
    assert result.ok
    payload = json.loads(result.text)
    assert payload["tools"][0]["name"] == "market_get_quote"
    assert "symbol" in payload["tools"][0]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_invoke_runs_a_discovered_read_tool():
    reg = _registry()
    result = await reg.execute(
        "tool_invoke", "c1",
        {"name": "market_get_quote", "arguments": {"symbol": "AAPL"}},
        _ctx(reg),
    )
    assert result.ok
    assert result.text == "read:AAPL"
    assert result.data["status"] == "OK"


@pytest.mark.asyncio
async def test_invoke_refuses_an_effectful_tool():
    reg = _registry()
    result = await reg.execute(
        "tool_invoke", "c1", {"name": "portfolio_write", "arguments": {}}, _ctx(reg),
    )
    assert result.ok
    assert result.data["status"] == "REFUSED"
    assert "effectful" in result.text


@pytest.mark.asyncio
async def test_invoke_refuses_recursion_and_unknowns():
    reg = _registry()
    recursive = await reg.execute(
        "tool_invoke", "c1", {"name": "tool_invoke", "arguments": {}}, _ctx(reg),
    )
    assert recursive.data["status"] == "REFUSED"

    missing = await reg.execute(
        "tool_invoke", "c2", {"name": "not_a_tool", "arguments": {}}, _ctx(reg),
    )
    assert missing.data["status"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_search_and_invoke_never_widen_beyond_the_run_surface():
    reg = _registry()
    ctx = _ctx(reg)
    ctx.allowed = {"market_get_quote", "tool_search", "tool_invoke"}

    searched = await reg.execute("tool_search", "c1", {"query": "portfolio"}, ctx)
    payload = json.loads(searched.text)
    rows = {row["name"]: row for row in payload["tools"]}
    # Discovery is honest: an outside tool may be NAMED, never bound or invoked.
    if "portfolio_write" in rows:
        assert rows["portfolio_write"]["outsideSurface"] is True
        assert rows["portfolio_write"]["requiresEnable"] is True

    refused = await reg.execute(
        "tool_invoke", "c2",
        {"name": "portfolio_write", "arguments": {}}, ctx,
    )
    assert refused.data["status"] == "REFUSED"
    assert refused.data["reason"] == "outside the surface"


def test_lazy_surface_is_off_by_default_and_keeps_effectful_tools(monkeypatch):
    reg = _registry()
    allowed = {
        "market_get_quote", "portfolio_write", "research_search",
        "tool_search", "tool_invoke", "read_tool_output",
    }
    monkeypatch.delenv("HARNESS_LAZY_TOOLS", raising=False)
    assert lazy_tool_surface(allowed, reg) == allowed

    monkeypatch.setenv("HARNESS_LAZY_TOOLS", "1")
    narrowed = lazy_tool_surface(allowed, reg)
    assert "portfolio_write" in narrowed  # effectful is never hidden
    assert "market_get_quote" not in narrowed  # read-only long tail is discoverable
    assert {"tool_search", "tool_invoke", "read_tool_output"} <= narrowed


def test_lazy_core_never_widens_an_agent_that_lacks_the_tools(monkeypatch):
    reg = _registry()
    monkeypatch.setenv("HARNESS_LAZY_TOOLS", "1")
    narrowed = lazy_tool_surface({"portfolio_write"}, reg)
    assert narrowed == {"portfolio_write"}
    assert not (narrowed & LAZY_CORE_TOOLS)
