"""Phase 3 runtime seam: disabled is a no-op, enabled wires the services."""

from __future__ import annotations

import pytest

from harness.agents import get_agent
from harness.kernel.services.runtime import KernelRuntime
from harness.schemas import AgentConfig, ModelSettings, RunInput, RunRequest
from harness.tools import ToolDefinition, ToolRegistry


def _tool(name: str):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=name, input_schema={}, handler=handler)


@pytest.fixture()
def registry():
    reg = ToolRegistry()
    reg.register(_tool("shared"))
    return reg


def _request(agent_id: str) -> RunRequest:
    return RunRequest(
        runId="run-1",
        input=RunInput(query="hello"),
        agentId=agent_id,
        modelSettings=ModelSettings(provider="deepseek", model="deepseek-chat"),
        callbackBaseUrl="http://127.0.0.1:3000",
    )


def test_disabled_runtime_returns_the_original_registry(registry):
    runtime = KernelRuntime(enabled=False)
    assert runtime.configure(registry) is runtime
    assert runtime.kernel is None
    assert runtime.view_for("run-1", registry) is registry
    assert runtime.status()["enabled"] is False


async def test_enabled_runtime_provides_three_services(registry):
    runtime = KernelRuntime(enabled=True).configure(registry)
    assert runtime.kernel is not None
    assert runtime.tools is not None and runtime.prompt is not None and runtime.agents is not None
    assert set(runtime.kernel.inspect()["services"]) >= {"tools", "prompt", "agents"}
    await runtime.kernel.shutdown()


async def test_enabled_view_is_a_scoped_view_over_the_same_tools(registry):
    runtime = KernelRuntime(enabled=True).configure(registry)
    view = runtime.view_for("run-1", registry, agent_id="eclipse-agent")
    assert view is not registry
    assert view.names() == registry.names()
    view.register_local(_tool("run_only"))
    assert "run_only" in view.names()
    assert "run_only" not in registry.names()
    await runtime.kernel.shutdown()


async def test_a_foreign_registry_keeps_its_own_tools(registry):
    runtime = KernelRuntime(enabled=True).configure(registry)
    other = ToolRegistry()
    other.register(_tool("private_eval_tool"))
    view = runtime.view_for("run-eval", other)
    assert view is not other
    assert view.names() == ["private_eval_tool"]
    assert "shared" not in view.names()
    await runtime.kernel.shutdown()


async def test_resolve_agent_uses_local_catalog_then_builtin(registry):
    runtime = KernelRuntime(enabled=True).configure(registry)
    local = AgentConfig(id="custom", name="Custom", systemPrompt="Local.")
    runtime.agents.register(local)
    assert runtime.resolve_agent("custom") is local
    assert runtime.resolve_agent("eclipse-agent").id == "eclipse-agent"
    await runtime.kernel.shutdown()


def test_graph_resolve_agent_consults_the_runtime(monkeypatch, registry):
    from harness import graph

    runtime = KernelRuntime(enabled=True).configure(registry)
    local = AgentConfig(id="custom", name="Custom", systemPrompt="Local.")
    runtime.agents.register(local)
    monkeypatch.setattr(graph, "kernel_runtime", runtime)

    assert graph.resolve_agent(_request("custom")) is local
    assert graph.resolve_agent(_request("code-agent")).id == get_agent("code-agent").id


async def test_resolve_surface_is_byte_identical_through_the_runtime(registry):
    from harness.agents import AGENT_PRESETS
    from harness.surface import resolve_surface_run

    runtime = KernelRuntime(enabled=True).configure(registry)
    agent = AGENT_PRESETS["eclipse-agent"]
    baseline_resolution, baseline_prompt = resolve_surface_run(
        agent, registry, {"memory_enabled": False}, None, provider="deepseek", model="deepseek-chat",
    )
    resolution, prompt = runtime.resolve_surface(
        agent, registry, {"memory_enabled": False}, None, provider="deepseek", model="deepseek-chat",
    )
    assert prompt == baseline_prompt
    assert resolution.to_dict() == baseline_resolution.to_dict()
    await runtime.kernel.shutdown()


async def test_resolve_surface_matches_the_graph_call_without_provider_args(registry):
    """The graph does not pass provider/model; the seam must not add them."""
    from harness.agents import AGENT_PRESETS
    from harness.surface import resolve_surface_run

    runtime = KernelRuntime(enabled=True).configure(registry)
    agent = AGENT_PRESETS["code-agent"]
    baseline_resolution, baseline_prompt = resolve_surface_run(
        agent, registry, {}, "edit_verify", apply_code_surface=True,
    )
    resolution, prompt = runtime.resolve_surface(
        agent, registry, {}, "edit_verify", apply_code_surface=True,
    )
    assert prompt == baseline_prompt
    assert resolution.to_dict() == baseline_resolution.to_dict()
    await runtime.kernel.shutdown()
