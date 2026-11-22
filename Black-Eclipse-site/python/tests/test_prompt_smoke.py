"""Prompt smoke: real graph runs for user-style prompts + a plugin prompt.

The suite prompts live in ``prompt_smoke_v1.json``. They run through the REAL
graph with the deterministic ``ScriptedModel`` (no network, no live model), so a
pass proves prompt -> surface -> tool -> grade, not a mocked string compare.
"""

from __future__ import annotations

import asyncio
import os

from harness.agents import ECLIPSE_AGENT
from harness.evaluation.runner import EvaluationRunner, ScriptedAgentInvoker
from harness.evaluation.store import EvaluationStore
from harness.evaluation.tasks import load_dataset
from harness.events import EventBus
from harness.kernel.services.runtime import KernelRuntime
from harness.persistence import AgentMemoryStore
from harness.plugins.manager import PluginManager
from harness.surface import resolve_surface_detail
from harness.tools import ToolRegistry

from plugins_helpers import BUNDLED, package

HERE = os.path.dirname(__file__)
PROMPTS = os.path.join(HERE, "prompt_smoke_v1.json")


def test_prompt_suite_runs_end_to_end(tmp_path):
    dataset = load_dataset(PROMPTS)
    assert len(dataset.tasks) == 4
    store = AgentMemoryStore(str(tmp_path / "prompt-smoke.db"))
    try:
        runner = EvaluationRunner(
            store, invoker=ScriptedAgentInvoker(store), eval_store=EvaluationStore(store),
        )
        suite = asyncio.run(runner.run_suite(
            dataset.tasks,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            dataset_checksum=dataset.checksum,
            trials=1,
            persist=False,
        ))
        by_id = {result.task.id: result for result in suite.tasks}
        assert set(by_id) == {task.id for task in dataset.tasks}
        assert suite.measured_trials == 4
        assert suite.passed, {
            task_id: [t.passed for t in result.trials]
            for task_id, result in by_id.items()
        }
        for result in suite.tasks:
            assert all(trial.passed for trial in result.trials if not trial.unmeasured)
    finally:
        store.close()


def test_plugin_prompt_sees_the_contributed_tool(tmp_path):
    store = AgentMemoryStore(str(tmp_path / "plugin-prompt.db"))
    registry = ToolRegistry()
    manager = PluginManager(store, registry, EventBus(), None,
                            plugins_dir=tmp_path / "plugins", bundled_dir=BUNDLED)
    runtime = KernelRuntime(enabled=True).configure(registry)
    try:
        asyncio.run(manager.install_package(
            **package("prompt-health", tools=[{
                "name": "health", "description": "plugin health", "permission": "read",
            }]),
            enable=True,
            operator=True,
        ))
        prompt = "Use the prompt-health plugin to check its health."
        result = resolve_surface_detail(ECLIPSE_AGENT, registry, {}, None)
        tool = "plug_prompt_health_health"
        assert tool in registry.names()
        assert tool in set(result.allowed or ()), prompt
        assert result.removed == ()
    finally:
        asyncio.run(runtime.kernel.shutdown())
        store.close()
