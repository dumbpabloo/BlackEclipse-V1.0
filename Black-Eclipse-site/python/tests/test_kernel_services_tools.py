"""Phase 3: scoped tool views do not leak across agents or concurrent runs."""

from __future__ import annotations

import asyncio

import pytest

from harness.kernel.services.runtime import KernelRuntime
from harness.kernel.services.tools import ScopedToolRegistry, ToolService
from harness.tools import ToolDefinition, ToolRegistry


def _tool(name: str, description: str = "shared"):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=description, input_schema={}, handler=handler)


@pytest.fixture()
def registry():
    reg = ToolRegistry()
    reg.register(_tool("shared"))
    return reg


def test_view_with_no_local_tools_equals_the_global_registry(registry):
    service = ToolService(registry)
    view = service.view("run-1")
    assert view.names() == registry.names()
    assert view.get("shared") is registry.get("shared")
    assert view.schemas() == registry.schemas()
    assert [t.name for t in view.list()] == [t.name for t in registry.list()]


def test_local_tool_is_visible_only_in_its_view(registry):
    service = ToolService(registry)
    first = service.view("agent-a")
    second = service.view("agent-b")
    first.register_local(_tool("alpha"))

    assert "alpha" in first.names()
    assert "alpha" not in second.names()
    assert registry.get("alpha") is None
    assert "alpha" not in registry.names()


def test_local_tool_may_shadow_a_global_name_without_changing_global(registry):
    service = ToolService(registry)
    view = service.view("agent-a")
    view.register_local(_tool("shared", description="local"))
    assert view.get("shared").description == "local"
    assert registry.get("shared").description == "shared"
    assert service.view("agent-b").get("shared").description == "shared"


def test_child_run_view_inherits_agent_scope_tools(registry):
    service = ToolService(registry)
    agent_scope = service.agent_scope("agent-a")
    agent_scope.register_local(_tool("agent_only"))
    run_scope = service.run_scope("run-1", agent_id="agent-a")

    assert "agent_only" in run_scope.names()
    run_scope.register_local(_tool("run_only"))
    assert "run_only" in run_scope.names()
    assert "run_only" not in agent_scope.names()
    assert "run_only" not in service.agent_scope("agent-b").names()


def test_disposal_removes_exactly_one_registration(registry):
    service = ToolService(registry)
    view = service.view("agent-a")
    dispose = view.register_local(_tool("temporary"))
    assert "temporary" in view.names()
    dispose()
    assert "temporary" not in view.names()
    assert "shared" in view.names()


async def test_twenty_concurrent_agents_see_only_their_own_tool(registry):
    runtime = KernelRuntime(enabled=True).configure(registry)
    assert runtime.tools is not None

    async def agent(index: int) -> set[str]:
        view = runtime.tools.view(f"agent-{index}")
        view.register_local(_tool(f"private_{index}"))
        await asyncio.sleep(0)
        visible = set(view.names())
        others = {f"private_{other}" for other in range(20) if other != index}
        assert not (visible & others), index
        assert f"private_{index}" in visible
        assert "shared" in visible
        return visible

    results = await asyncio.gather(*(agent(index) for index in range(20)))
    assert len(results) == 20
    for index, visible in enumerate(results):
        assert visible == {"shared", f"private_{index}"}
    assert registry.names() == ["shared"]
    await runtime.kernel.shutdown()


async def test_run_view_executes_a_local_tool_through_the_shared_pipeline(registry):
    service = ToolService(registry)
    view = service.view("run-1")
    view.register_local(_tool("local"))
    from harness.tools import ToolContext

    ctx = ToolContext(run_id="run-1", log=lambda *_: None)
    result = await view.execute("local", "call-1", {}, ctx)
    assert result.ok is True
    assert "local" in result.text


async def test_foreign_view_delegates_base_tools_to_the_base_execute(registry):
    """A base registry's execute override (recording/instrumentation) must run."""
    from harness.tools import ToolContext

    calls: list[str] = []

    class Recording(ToolRegistry):
        async def execute(self, name, call_id, arguments, ctx, **kwargs):
            calls.append(name)
            return await super().execute(name, call_id, arguments, ctx, **kwargs)

    base = Recording()
    base.register(_tool("eval_like"))
    view = ScopedToolRegistry(base, key="run:foreign")
    ctx = ToolContext(run_id="run-foreign", log=lambda *_: None)
    result = await view.execute("eval_like", "call-1", {}, ctx)
    assert result.ok is True
    assert calls == ["eval_like"]
    # A nested child run builds a ToolContext from the registry's mapping; the
    # merged view must support subscript lookup like a dict.
    assert view._tools["eval_like"] is base.get("eval_like")
