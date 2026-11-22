"""Remaining evolution adapters: admission, apply, undo, evaluation."""

from __future__ import annotations

import pytest

from harness.evolution import (
    AdapterRegistry,
    AssetEvolution,
    AssetEvaluator,
    CandidateStatus,
    CompositionRowAdapter,
    HookAdapter,
    MemoryPolicyAdapter,
    ModelRouteAdapter,
    PluginBundleAdapter,
    SkillAdapter,
    ToolPolicyAdapter,
)
from harness.kernel.services.policy import (
    CompositionRowStore,
    ModelRouteService,
    ToolPolicyService,
)
from harness.kernel.services.skills import SkillService
from harness.plugins.hooks import HookRegistry


class _FakePolicy:
    def __init__(self):
        self._values = {"profile_enabled": True}

    def get_all(self):
        return dict(self._values)

    def set(self, key, value, *, updated_by=None):
        self._values[key] = value
        return {key: value}

    def reset(self, key=None):
        if key is None:
            self._values = {}
        else:
            self._values.pop(key, None)


def _candidate(evolution, kind, target, content, op="add"):
    candidate = evolution.propose(
        kind=kind, target=target, mutations=[{
            "op": op, "kind": kind, "target": target, "content": content, "rationale": "test",
        }],
    )
    evolution.admit(candidate.id)
    return candidate


def _with(adapter):
    adapters = AdapterRegistry()
    adapters.register(adapter)
    return AssetEvolution(adapters)


def test_skill_adapter_applies_and_rolls_back():
    skills = SkillService()
    evolution = _with(SkillAdapter(skills))
    candidate = _candidate(evolution, "skill", "learned.demo", {
        "name": "Demo", "description": "demo", "instructions": "# Procedure\nDo the thing.",
    })
    evolution.apply(candidate.id)
    assert skills.get("learned.demo") is not None
    evolution.rollback(candidate.id)
    assert skills.get("learned.demo") is None


def test_hook_adapter_registers_and_unregisters_the_owner():
    hooks = HookRegistry()
    calls: list[str] = []

    def handler(payload, ctx):
        calls.append("ran")

    evolution = _with(HookAdapter(hooks, handlers={"before_tool_call": handler}))
    candidate = _candidate(evolution, "hook", "before_tool_call", {"name": "before_tool_call"})
    evolution.apply(candidate.id)
    assert hooks.has("before_tool_call")
    evolution.rollback(candidate.id)
    assert hooks.has("before_tool_call") is False


def test_memory_policy_adapter_restores_the_previous_value():
    policy = _FakePolicy()
    evolution = _with(MemoryPolicyAdapter(policy))
    candidate = _candidate(evolution, "memory_policy", "profile_enabled", {
        "key": "profile_enabled", "value": False,
    }, op="patch")
    evolution.apply(candidate.id)
    assert policy.get_all()["profile_enabled"] is False
    evolution.rollback(candidate.id)
    assert policy.get_all()["profile_enabled"] is True


def test_tool_policy_model_route_and_composition_adapters():
    policies = ToolPolicyService()
    routes = ModelRouteService()
    rows = CompositionRowStore()

    tool_evolution = _with(ToolPolicyAdapter(policies))
    tool_candidate = _candidate(tool_evolution, "tool_policy", "eclipse-agent", {
        "allow": ["web_search"], "deny": ["order_submit"],
    })
    tool_evolution.apply(tool_candidate.id)
    assert policies.get("eclipse-agent").deny == ["order_submit"]

    route_evolution = _with(ModelRouteAdapter(routes))
    route_candidate = _candidate(route_evolution, "model_route", "eclipse-agent", {
        "provider": "deepseek", "model": "deepseek-chat",
    })
    route_evolution.apply(route_candidate.id)
    assert routes.get("eclipse-agent").model == "deepseek-chat"

    row_evolution = _with(CompositionRowAdapter(rows))
    row_candidate = _candidate(row_evolution, "composition_row", "extra-tool", {
        "id": "extra-tool", "name": "pkg.extra:apply",
    })
    row_evolution.apply(row_candidate.id)
    assert rows.get("extra-tool") is not None

    route_evolution.rollback(route_candidate.id)
    row_evolution.rollback(row_candidate.id)
    assert routes.get("eclipse-agent") is None
    assert rows.get("extra-tool") is None
    # tool_candidate is applied by its own evolution instance
    assert tool_candidate.status is CandidateStatus.APPLIED


def test_plugin_bundle_is_operator_only():
    evolution = _with(PluginBundleAdapter())
    candidate = _candidate(evolution, "plugin_bundle", "demo-bundle", {"name": "demo-bundle"})
    with pytest.raises(Exception) as info:
        evolution.apply(candidate.id)
    assert "operator" in str(info.value)


class _Suite:
    def __init__(self, value):
        self.aggregates = {"task_success": value}
        self.measured_trials = 1
        self.run_id = "adapter-eval"


class _Runner:
    def __init__(self, value):
        self.value = value

    async def run_suite(self, *args, **kwargs):
        return _Suite(self.value)


async def test_skill_adapter_can_be_evaluated_and_reverted():
    skills = SkillService()
    evolution = _with(SkillAdapter(skills))
    candidate = _candidate(evolution, "skill", "learned.demo", {
        "name": "Demo", "instructions": "# Procedure",
    })
    evaluator = AssetEvaluator(evolution)
    result = await evaluator.evaluate(
        candidate.id,
        runner=_Runner(0.9),
        tasks=[],
        baseline_metrics={"aggregates": {"task_success": 0.5}},
    )
    assert result["status"] == "measured_ok"
    assert skills.get("learned.demo") is not None
    evolution.rollback(candidate.id)
    assert skills.get("learned.demo") is None
