"""Reference plugin for Phase 4: executable agent, skill and prompt section.

Every contribution is declared in ``plugin.yaml`` and produced by a synchronous
handler so the kernel can mount it during ``register_all``. Disabling or
uninstalling the plugin runs the exact undo for each contribution.
"""

from __future__ import annotations

from harness.plugins.sdk import (
    Plugin,
    agent_contribution,
    data_provider,
    model_provider,
    prompt_section,
    research_provider,
    skill,
    ui_panel,
    workflow_contribution,
)


class _DemoSearchProvider:
    name = "demo-research"
    capabilities = ("demo",)

    def available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 8, **kwargs):
        from harness.research.web_contracts import SearchResponse

        return SearchResponse(provider=self.name, query=query, results=[])


class KernelDemo(Plugin):
    id = "kernel-demo"
    version = "1.0.0"

    def provided_services(self) -> list[str]:
        return ["demo.agent"]

    @agent_contribution("demo-agent", description="A small agent contributed by the kernel demo.")
    def demo_agent(self) -> dict:
        return {
            "id": "demo-agent",
            "name": "Kernel Demo Agent",
            "systemPrompt": "You are the kernel demo agent. Answer briefly and cite the demo.",
            "tools": [],
            "maxSteps": 4,
        }

    @skill("demo.procedure", description="A reusable procedure contributed by the kernel demo.")
    def demo_skill(self) -> str:
        return (
            "# Demo procedure\n\n"
            "## When to Use\n\nWhen the user asks for the kernel demonstration.\n\n"
            "## Procedure\n\n1. Read the request.\n2. Answer with one sentence.\n"
        )

    @prompt_section("demo.guidance", scope="demo-agent", order=50)
    def demo_prompt(self) -> str:
        return "Kernel demo guidance: keep the answer to one short paragraph."

    @workflow_contribution("demo-flow")
    def demo_workflow(self) -> dict:
        return {
            "id": "demo-flow",
            "name": "Demo flow",
            "steps": [{"id": "start", "kind": "agent", "instruction": "Say hello.", "terminal": True}],
        }

    @model_provider("demo-model", provider="demo")
    def demo_model(self) -> dict:
        return {"name": "demo-model", "provider": "demo", "kind": "custom", "keyless": True}

    @research_provider("demo-research", kinds=["demo"])
    def demo_research(self) -> dict:
        return _DemoSearchProvider()

    @data_provider("demo-data", kinds=["demo"])
    def demo_data(self) -> dict:
        return {"name": "demo-data", "kinds": ["demo"]}

    @ui_panel("panel", "demo-panel", title="Demo")
    def demo_panel(self, ctx) -> dict:  # noqa: ANN001
        return {"type": "keyvalue", "items": [{"label": "Plugin", "value": "kernel-demo"}]}


plugin = KernelDemo
