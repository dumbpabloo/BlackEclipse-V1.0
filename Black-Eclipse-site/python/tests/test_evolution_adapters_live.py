"""Live consumers: tool policy, model route and composition rows."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from harness.agents import ECLIPSE_AGENT
from harness.kernel import Kernel, Supervisor
from harness.kernel.services.policy import CompositionRowStore
from harness.kernel.services.runtime import KernelRuntime
from harness.schemas import ModelSettings
from harness.surface import resolve_surface_detail
from harness.tools import ToolDefinition, ToolRegistry


def _tool(name: str):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=name, input_schema={}, handler=handler)


def _registry():
    registry = ToolRegistry()
    for name in ("web_search", "portfolio_read", "order_submit"):
        registry.register(_tool(name))
    return registry


@pytest.fixture(autouse=True)
def _reset():
    from harness.kernel.services import runtime as runtime_module

    runtime_module.kernel_runtime.reset(enabled=False)
    yield
    runtime_module.kernel_runtime.reset(enabled=False)


def test_tool_policy_narrows_surface_and_is_explained():
    from harness.kernel.services import runtime as runtime_module

    runtime = KernelRuntime(enabled=True).configure(_registry())
    runtime_module.kernel_runtime.reset(enabled=False)
    runtime.policies.set("eclipse-agent", allow=[], deny=["portfolio_read", "order_submit"])
    # Bind the singleton the surface resolver consults.
    original = runtime_module.kernel_runtime
    try:
        runtime_module.kernel_runtime = runtime
        resolution = resolve_surface_detail(ECLIPSE_AGENT, _registry(), {}, None)
    finally:
        runtime_module.kernel_runtime = original

    allowed = set(resolution.allowed or ())
    assert "portfolio_read" not in allowed
    assert "order_submit" not in allowed
    assert "web_search" in allowed
    assert "order_submit" in set(resolution.policyRemoved)


def test_model_route_applies_only_for_the_same_provider():
    runtime = KernelRuntime(enabled=True).configure(_registry())
    runtime.model_routes.set("eclipse-agent", provider="deepseek", model="deepseek-flash")
    settings = ModelSettings(provider="deepseek", model="deepseek-chat")
    routed = runtime.apply_model_route("eclipse-agent", settings)
    assert routed.model == "deepseek-flash"
    assert routed.provider == "deepseek"

    other = ModelSettings(provider="openai", model="gpt-5")
    assert runtime.apply_model_route("eclipse-agent", other).model == "gpt-5"


async def test_composition_row_mounts_in_the_live_supervisor():
    module = types.ModuleType("kernel_fixtures")

    def good(ctx, config):
        ctx.provide("row_good", config or {})

    module.good = good
    sys.modules["kernel_fixtures"] = module
    kernel = Kernel()
    supervisor = Supervisor(kernel)
    store = CompositionRowStore(supervisor)
    try:
        undo = store.set("row", {"id": "row", "name": "kernel_fixtures:good", "config": {"v": 1}})
        await asyncio.sleep(0)
        await kernel.settle()
        assert kernel.root.require("row_good") == {"v": 1}
        undo()
        await asyncio.sleep(0)
        await kernel.settle()
        assert kernel.root.get("row_good") is None
    finally:
        sys.modules.pop("kernel_fixtures", None)
        await kernel.shutdown()
