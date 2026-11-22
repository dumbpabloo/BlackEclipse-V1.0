"""Phase 4: plugin-contributed agents, skills and prompt sections mount through
the kernel, are permissioned, and unwind exactly on disable."""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness.kernel.inspect_tools import register_inspect_tool
from harness.kernel.services.runtime import kernel_runtime
from harness.skills.registry import SkillRegistry
from harness.tools import ToolContext, ToolRegistry

from plugins_helpers import env


@pytest.fixture(autouse=True)
def _reset_kernel():
    yield
    kernel_runtime.reset(enabled=False)


@pytest.fixture()
def kernel_env(env):
    kernel_runtime.reset(enabled=True, registry=env.registry)
    skills = SkillRegistry()
    kernel_runtime.attach_skills(skills)
    env.skills = skills
    from harness.model_runtime.registry import ProviderRegistry as ModelProviderRegistry
    from harness.research.web_providers import SearchProviderRegistry
    from harness.retrieval.registry import ProviderRegistry as RetrievalProviderRegistry
    from harness.workflows.store import WorkflowStore

    class _FakeUI:
        def __init__(self):
            self._panels: dict[str, Any] = {}

        def register_dynamic_panel(self, panel_id, handler):
            self._panels[panel_id] = handler

            def dispose():
                self._panels.pop(panel_id, None)

            return dispose

        def dynamic_panels(self):
            return [{"id": key} for key in self._panels]

    env.retrieval = RetrievalProviderRegistry()
    env.search = SearchProviderRegistry()
    env.workflows = WorkflowStore(env.manager._agent_store)
    env.models = ModelProviderRegistry()
    env.ui = _FakeUI()
    kernel_runtime.attach_retrieval(env.retrieval)
    kernel_runtime.attach_search(env.search)
    kernel_runtime.attach_workflows(env.workflows)
    kernel_runtime.attach_models(env.models)
    kernel_runtime.attach_ui(env.ui)
    return env


async def test_bundled_kernel_demo_mounts_all_three_contributions(kernel_env):
    result = await kernel_env.manager.install("kernel-demo", enable=True, operator=True)
    assert result["ok"] is True

    agent = kernel_runtime.agents.resolve("demo-agent")
    assert agent.name == "Kernel Demo Agent"
    assert "kernel demo agent" in agent.systemPrompt.lower()

    assert kernel_runtime.prompt.render_sections("demo-agent").endswith(
        "keep the answer to one short paragraph."
    )
    skill = kernel_env.skills.get("demo.procedure")
    assert skill is not None
    assert "Demo procedure" in skill.load_instructions()
    assert kernel_runtime.provided("kernel-demo") == ["demo.agent"]
    catalog = kernel_runtime.extras.catalog()
    assert [row["name"] for row in catalog["research_provider"]] == ["demo-research"]
    assert [row["name"] for row in catalog["data_provider"]] == ["demo-data"]
    assert [row["name"] for row in catalog["workflow"]] == ["demo-flow"]
    assert [row["name"] for row in catalog["model"]] == ["demo-model"]
    assert [row["name"] for row in catalog["ui"]] == ["demo-panel"]
    assert kernel_env.retrieval.get("demo-data") is not None
    assert kernel_env.search.get("demo-research") is not None
    assert kernel_env.workflows.get("demo-flow") is not None
    assert kernel_env.models.get_provider("demo-model") is not None
    assert [row["id"] for row in kernel_env.ui.dynamic_panels()] == ["demo-panel"]
    assert any(row["id"] == "demo-panel" for row in kernel_runtime.ui_slots.entries("plugins.panel"))


async def test_disable_unwinds_agent_prompt_and_skill(kernel_env):
    await kernel_env.manager.install("kernel-demo", enable=True, operator=True)
    await kernel_env.manager.disable("kernel-demo")

    assert kernel_runtime.agents.get_local("demo-agent") is None
    assert kernel_runtime.prompt.render_sections("demo-agent") == ""
    assert kernel_env.skills.get("demo.procedure") is None
    assert kernel_runtime.provided("kernel-demo") == []
    assert kernel_runtime.extras.catalog() == {
        "research_provider": [], "data_provider": [], "workflow": [], "model": [], "ui": [],
    }
    assert kernel_env.retrieval.get("demo-data") is None
    assert kernel_env.search.get("demo-research") is None
    assert kernel_env.workflows.get("demo-flow") is None
    assert kernel_env.models.get_provider("demo-model") is None
    assert kernel_env.ui.dynamic_panels() == []
    assert kernel_runtime.ui_slots.entries("plugins.panel") == []


async def test_declared_provides_must_match_the_runtime(kernel_env):
    package = {
        "manifest": {
            "id": "liar",
            "name": "liar",
            "version": "1.0.0",
            "runtime": {"type": "python", "entrypoint": "plugin:Impl"},
            "permissions": [],
            "provides": [{"key": "claimed.service"}],
            "contributes": {},
        },
        "files": {
            "plugin.py": (
                "from harness.plugins.sdk import Plugin\n\n\n"
                "class Impl(Plugin):\n"
                "    pass\n\n"
                "plugin = Impl\n"
            ),
        },
    }
    with pytest.raises(Exception) as info:
        await kernel_env.manager.install_package(package["manifest"], package["files"], enable=True, operator=True)
    assert "provided-service mismatch" in str(info.value) or "declared but not provided" in str(info.value)


async def test_contribution_requires_its_permission(kernel_env):
    package = {
        "manifest": {
            "id": "unpermissioned",
            "name": "unpermissioned",
            "version": "1.0.0",
            "runtime": {"type": "python", "entrypoint": "plugin:Impl"},
            "permissions": [],
            "contributes": {"agents": [{"id": "sneaky"}]},
        },
        "files": {
            "plugin.py": (
                "from harness.plugins.sdk import Plugin, agent_contribution\n\n\n"
                "class Impl(Plugin):\n"
                "    @agent_contribution('sneaky')\n"
                "    def agent(self):\n"
                "        return {'id': 'sneaky', 'name': 'Sneaky', 'systemPrompt': 'x'}\n\n"
                "plugin = Impl\n"
            ),
        },
    }
    with pytest.raises(Exception) as info:
        await kernel_env.manager.install_package(package["manifest"], package["files"], enable=True, operator=True)
    assert "agents.contribute" in str(info.value)


async def test_requires_a_missing_service_is_refused(kernel_env):
    package = {
        "manifest": {
            "id": "needs-service",
            "name": "needs-service",
            "version": "1.0.0",
            "runtime": {"type": "python", "entrypoint": "plugin:Impl"},
            "permissions": [],
            "requires": [{"key": "absent.service"}],
            "contributes": {},
        },
        "files": {
            "plugin.py": "from harness.plugins.sdk import Plugin\n\nclass Impl(Plugin):\n    pass\n\nplugin = Impl\n",
        },
    }
    with pytest.raises(Exception) as info:
        await kernel_env.manager.install_package(package["manifest"], package["files"], enable=True, operator=True)
    assert "PLUGIN_SERVICE_MISSING" in str(info.value) or "absent.service" in str(info.value)


async def test_harness_inspect_is_read_only_and_reports_the_tree(kernel_env):
    await kernel_env.manager.install("kernel-demo", enable=True, operator=True)
    registry = ToolRegistry()
    register_inspect_tool(registry)
    ctx = ToolContext(run_id="inspect-run", log=lambda *_: None)
    result = await registry.execute("harness_inspect", "call-1", {}, ctx)
    assert result.ok is True
    payload = json.loads(result.text)
    assert payload["enabled"] is True
    assert "demo-agent" in payload["agents"]
    assert payload["provided"]["kernel-demo"] == ["demo.agent"]
    assert "tools" in payload


async def test_harness_inspect_reports_disabled_when_kernel_is_off(env):
    registry = ToolRegistry()
    register_inspect_tool(registry)
    ctx = ToolContext(run_id="inspect-run", log=lambda *_: None)
    result = await registry.execute("harness_inspect", "call-1", {}, ctx)
    payload = json.loads(result.text)
    assert payload["enabled"] is False
    assert payload["services"] == []
