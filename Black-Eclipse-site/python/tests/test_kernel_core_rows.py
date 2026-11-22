"""Core service rows can be replaced through live composition."""

from __future__ import annotations

import json
import sys
import types

import pytest

from harness.agents import AGENT_PRESETS
from harness.kernel.profile_boot import ProfileBoot
from harness.kernel.services.agents import AgentCatalog
from harness.kernel.services.runtime import KernelRuntime
from harness.schemas import AgentConfig
from harness.tools import ToolDefinition, ToolRegistry


class ReplacementPrompt:
    def resolve(self, agent, registry, settings, autonomy=None, **kwargs):
        from harness.surface import resolve_surface_run

        resolution, _ = resolve_surface_run(
            agent, registry, settings, autonomy,
            apply_code_surface=kwargs.get("apply_code_surface", True),
        )
        return resolution, "REPLACED PROMPT"


def make_prompt():
    return ReplacementPrompt()


def make_agents():
    catalog = AgentCatalog()
    catalog.register(AgentConfig(id="custom", name="Custom", systemPrompt="Custom prompt."))
    return catalog


def _tool(name):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=name, input_schema={}, handler=handler)


def make_tools():
    registry = ToolRegistry()
    registry.register(_tool("custom_tool"))
    return registry


def make_boom():
    raise RuntimeError("factory boom")


@pytest.fixture(autouse=True)
def _fixture_module():
    module = types.ModuleType("kernel_core_rows_fixtures")
    module.make_prompt = make_prompt
    module.make_agents = make_agents
    module.make_tools = make_tools
    module.make_boom = make_boom
    sys.modules["kernel_core_rows_fixtures"] = module
    yield
    sys.modules.pop("kernel_core_rows_fixtures", None)


def _profile(tmp_path, rows):
    root = tmp_path / "profiles"
    profile = root / "web"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(json.dumps({"name": "web", "bundles": [], "patch": "cordis.patch.yaml"}))
    (profile / "cordis.patch.yaml").write_text(rows, encoding="utf-8")
    return root


async def test_core_rows_replace_and_restore_tools_prompt_agents(tmp_path):
    root = _profile(tmp_path, """
- insert:
    - id: tools
      name: builtin:tools
      config: {factory: kernel_core_rows_fixtures:make_tools}
    - id: prompt
      name: builtin:prompt
      config: {factory: kernel_core_rows_fixtures:make_prompt}
    - id: agents
      name: builtin:agents
      config: {factory: kernel_core_rows_fixtures:make_agents}
""")
    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    baseline_tools = runtime.tools
    baseline_prompt = runtime.prompt
    baseline_agents = runtime.agents
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    status = await boot.apply()
    assert status["status"] == "applied", status

    assert runtime.tools is not baseline_tools
    assert "custom_tool" in runtime.view_for("run-1", runtime._registry).names()
    resolution, prompt = runtime.resolve_surface(
        AGENT_PRESETS["eclipse-agent"], runtime._registry, {}, None,
    )
    assert prompt == "REPLACED PROMPT"
    assert runtime.agents.resolve("custom").name == "Custom"

    await boot.stop()
    assert runtime.tools is baseline_tools
    assert runtime.prompt is baseline_prompt
    assert runtime.agents is baseline_agents
    assert runtime.agents.get_local("custom") is None
    await runtime.kernel.shutdown()


async def test_a_failing_core_row_leaves_the_previous_services_in_place(tmp_path):
    root = _profile(tmp_path, """
- insert:
    - id: prompt
      name: builtin:prompt
      config: {factory: kernel_core_rows_fixtures:make_boom}
""")
    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    original = runtime.prompt
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    status = await boot.apply()
    assert status["status"] == "failed"
    assert runtime.prompt is original
    await runtime.kernel.shutdown()
