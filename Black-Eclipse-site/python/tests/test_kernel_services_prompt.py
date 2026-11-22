"""Phase 3: byte-identical prompts, and prompt sections isolated by agent."""

from __future__ import annotations

import pytest

from harness.agents import AGENT_PRESETS
from harness.kernel.services.prompt import PromptSection, PromptService
from harness.surface import resolve_surface_run
from harness.tools import ToolDefinition, ToolRegistry


def _tool(name: str):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=name, input_schema={}, handler=handler)


@pytest.fixture()
def registry():
    reg = ToolRegistry()
    for name in ("web_search", "memory", "portfolio_read", "code_edit", "research_run"):
        reg.register(_tool(name))
    return reg


SETTINGS = [
    {},
    {"memory_enabled": False},
    {"session_history_enabled": False},
    {"memory_enabled": False, "session_history_enabled": False},
]


@pytest.mark.parametrize("agent_id", sorted(AGENT_PRESETS))
@pytest.mark.parametrize("settings", SETTINGS)
def test_prompt_service_is_byte_identical_with_no_sections(agent_id, settings, registry):
    agent = AGENT_PRESETS[agent_id]
    baseline_resolution, baseline_prompt = resolve_surface_run(
        agent, registry, settings, None, provider="deepseek", model="deepseek-chat",
    )
    service = PromptService()
    resolution, prompt = service.resolve(
        agent, registry, settings, None, provider="deepseek", model="deepseek-chat",
    )
    assert prompt == baseline_prompt
    assert resolution.to_dict() == baseline_resolution.to_dict()


@pytest.mark.parametrize("autonomy", [None, "plan_only", "edit_verify"])
def test_prompt_service_matches_code_surface_reasoning(autonomy, registry):
    agent = AGENT_PRESETS["code-agent"]
    baseline_resolution, baseline_prompt = resolve_surface_run(
        agent, registry, {}, autonomy, provider="anthropic", model="claude-sonnet",
    )
    service = PromptService()
    resolution, prompt = service.resolve(
        agent, registry, {}, autonomy, provider="anthropic", model="claude-sonnet",
    )
    assert prompt == baseline_prompt
    assert resolution.to_dict() == baseline_resolution.to_dict()


def test_a_section_is_visible_only_in_its_scope(registry):
    service = PromptService()
    agent = AGENT_PRESETS["eclipse-agent"]
    dispose = service.register("eclipse-agent", PromptSection(id="extra", text="Extra guidance.", order=10))

    eclipse_resolution, eclipse_prompt = service.resolve(agent, registry, {}, None)
    code_agent = AGENT_PRESETS["code-agent"]
    _code_resolution, code_prompt = service.resolve(code_agent, registry, {}, None)

    assert eclipse_prompt.endswith("\n\nExtra guidance.")
    baseline = resolve_surface_run(agent, registry, {}, None)[1]
    assert eclipse_prompt == f"{baseline}\n\nExtra guidance."
    assert not code_prompt.endswith("Extra guidance.")

    dispose()
    assert service.resolve(agent, registry, {}, None)[1] == baseline


def test_sections_are_ordered_by_order_then_id(registry):
    service = PromptService()
    service.register("eclipse-agent", PromptSection(id="b", text="B", order=20))
    service.register("eclipse-agent", PromptSection(id="a", text="A", order=10))
    assert service.render_sections("eclipse-agent") == "A\n\nB"


def test_duplicate_section_in_one_scope_is_rejected():
    service = PromptService()
    service.register("eclipse-agent", PromptSection(id="x", text="one"))
    with pytest.raises(ValueError):
        service.register("eclipse-agent", PromptSection(id="x", text="two"))


def test_replace_restores_the_previous_section_on_dispose():
    service = PromptService()
    service.register("eclipse-agent", PromptSection(id="x", text="one"))
    dispose = service.register("eclipse-agent", PromptSection(id="x", text="two"), replace=True)
    assert service.render_sections("eclipse-agent") == "two"
    dispose()
    assert service.render_sections("eclipse-agent") == "one"


def test_plan_is_the_existing_builder(registry):
    from harness.prompt_segments import build_prompt_plan

    service = PromptService()
    kwargs = dict(
        system_prompt="p", agent_id="eclipse-agent", tool_names=["web_search"],
        profile_block="", profile_snapshot_id="", skill_manifest="", skill_manifest_version="",
        lesson_manifest="", runtime_context="", user_message="q", repo_map="", repo_map_version="",
        provider="deepseek",
    )
    assert service.plan(**kwargs).to_dict() == build_prompt_plan(**kwargs).to_dict()
