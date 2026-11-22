"""Asset evolution: the contract, admission and reversible apply/rollback."""

from __future__ import annotations

import pytest

from harness.agents import AGENT_PRESETS
from harness.evolution import (
    AdapterRegistry,
    AgentPresetAdapter,
    AssetEvolution,
    AssetError,
    AssetKind,
    CandidateStatus,
    PromptSectionAdapter,
    mutation_digest,
    new_candidate,
)
from harness.kernel.services.agents import AgentCatalog
from harness.kernel.services.prompt import PromptSection, PromptService
from harness.surface import resolve_surface_run
from harness.tools import ToolRegistry


@pytest.fixture()
def prompt_service():
    return PromptService()


@pytest.fixture()
def evolution(prompt_service):
    adapters = AdapterRegistry()
    adapters.register(PromptSectionAdapter(prompt_service, default_scope="eclipse-agent"))
    return AssetEvolution(adapters)


def _mutation(text: str = "Prefer the filing index.", target: str = "learned.filings", **kwargs):
    return {
        "op": "add",
        "kind": "prompt_section",
        "target": target,
        "content": {"text": text},
        "rationale": "the filing workflow recurred",
        **kwargs,
    }


def test_the_pipeline_is_propose_admit_apply_rollback(prompt_service, evolution):
    agent = AGENT_PRESETS["eclipse-agent"]
    baseline = resolve_surface_run(agent, ToolRegistry(), {}, None)[1]

    candidate = evolution.propose(
        kind="prompt_section", target="learned.filings", mutations=[_mutation()],
    )
    assert candidate.status is CandidateStatus.PROPOSED
    assert candidate.digest.startswith("emd_")
    assert prompt_service.resolve(agent, ToolRegistry(), {}, None)[1] == baseline

    evolution.admit(candidate.id)
    assert candidate.status is CandidateStatus.ADMITTED
    evolution.apply(candidate.id)
    assert candidate.status is CandidateStatus.APPLIED
    assert prompt_service.resolve(agent, ToolRegistry(), {}, None)[1].endswith(
        "\n\nPrefer the filing index."
    )

    evolution.rollback(candidate.id)
    assert candidate.status is CandidateStatus.ROLLED_BACK
    assert prompt_service.resolve(agent, ToolRegistry(), {}, None)[1] == baseline


def test_apply_requires_admission(evolution):
    candidate = evolution.propose(
        kind="prompt_section", target="learned.filings", mutations=[_mutation()],
    )
    with pytest.raises(AssetError) as info:
        evolution.apply(candidate.id)
    assert info.value.code == "EVOLUTION_NOT_ADMITTED"


def test_missing_content_is_rejected_before_any_apply(evolution):
    with pytest.raises(AssetError) as info:
        evolution.propose(
            kind="prompt_section",
            target="learned.empty",
            mutations=[{
                "op": "add", "kind": "prompt_section", "target": "learned.empty",
                "content": {"text": ""}, "rationale": "x",
            }],
        )
    assert info.value.code == "EVOLUTION_CONTENT_INVALID"


def test_kind_mismatch_and_target_mismatch_are_rejected(evolution):
    with pytest.raises(AssetError) as info:
        evolution.propose(
            kind="prompt_section",
            target="learned.filings",
            mutations=[{**_mutation(), "kind": "skill"}],
        )
    assert info.value.code == "EVOLUTION_KIND_MISMATCH"

    with pytest.raises(AssetError) as info:
        evolution.propose(
            kind="prompt_section",
            target="learned.filings",
            mutations=[{**_mutation(), "target": "other"}],
        )
    assert info.value.code == "EVOLUTION_TARGET_MISMATCH"


def test_missing_adapter_refuses_admission(evolution):
    candidate = evolution.propose(
        kind="model_route",
        target="main",
        mutations=[{
            "op": "tune", "kind": "model_route", "target": "main",
            "content": {"provider": "deepseek", "model": "deepseek-chat"},
            "rationale": "test",
        }],
    )
    with pytest.raises(AssetError) as info:
        evolution.admit(candidate.id)
    assert info.value.code == "EVOLUTION_ADAPTER_MISSING"


def test_digest_is_stable_for_the_same_mutations():
    first = new_candidate(
        kind="prompt_section", target="x", mutations=[_mutation("same", target="x")],
    )
    second = new_candidate(
        kind="prompt_section", target="x", mutations=[_mutation("same", target="x")],
    )
    assert first.digest == second.digest == mutation_digest(first.mutations)


def test_refuse_marks_the_candidate_and_apply_is_impossible(evolution):
    candidate = evolution.propose(
        kind="prompt_section", target="learned.filings", mutations=[_mutation()],
    )
    evolution.refuse(candidate.id, "not enough evidence")
    assert candidate.status is CandidateStatus.REFUSED
    with pytest.raises(AssetError):
        evolution.apply(candidate.id)


def test_patch_of_an_existing_section_restores_the_parent_on_rollback(prompt_service, evolution):
    original = PromptSection(id="learned.filings", text="old", order=5)
    prompt_service.register("eclipse-agent", original)
    candidate = evolution.propose(
        kind="prompt_section",
        target="learned.filings",
        mutations=[{
            "op": "patch", "kind": "prompt_section", "target": "learned.filings",
            "content": {"text": "new", "order": 5}, "rationale": "better wording",
        }],
    )
    evolution.admit(candidate.id)
    evolution.apply(candidate.id)
    assert prompt_service.render_sections("eclipse-agent") == "new"
    evolution.rollback(candidate.id)
    assert prompt_service.render_sections("eclipse-agent") == "old"


def test_add_of_an_existing_target_and_patch_of_a_missing_target_are_refused(prompt_service, evolution):
    prompt_service.register("eclipse-agent", PromptSection(id="learned.filings", text="old"))
    existing = evolution.propose(
        kind="prompt_section", target="learned.filings", mutations=[_mutation()],
    )
    with pytest.raises(AssetError) as info:
        evolution.admit(existing.id)
    assert info.value.code == "EVOLUTION_TARGET_EXISTS"

    missing = evolution.propose(
        kind="prompt_section",
        target="learned.missing",
        mutations=[{
            "op": "patch", "kind": "prompt_section", "target": "learned.missing",
            "content": {"text": "new"}, "rationale": "x",
        }],
    )
    with pytest.raises(AssetError) as info:
        evolution.admit(missing.id)
    assert info.value.code == "EVOLUTION_TARGET_MISSING"


def test_retire_removes_and_rollback_restores(prompt_service, evolution):
    prompt_service.register("eclipse-agent", PromptSection(id="learned.filings", text="old"))
    candidate = evolution.propose(
        kind="prompt_section",
        target="learned.filings",
        mutations=[{
            "op": "retire", "kind": "prompt_section", "target": "learned.filings",
            "content": {}, "rationale": "superseded",
        }],
    )
    evolution.admit(candidate.id)
    evolution.apply(candidate.id)
    assert prompt_service.render_sections("eclipse-agent") == ""
    evolution.rollback(candidate.id)
    assert prompt_service.render_sections("eclipse-agent") == "old"


def test_agent_preset_adapter_applies_and_reverts_exactly():
    catalog = AgentCatalog()
    adapters = AdapterRegistry()
    adapters.register(AgentPresetAdapter(catalog))
    evolution = AssetEvolution(adapters)

    candidate = evolution.propose(
        kind="agent_preset",
        target="research-desk",
        mutations=[{
            "op": "add", "kind": "agent_preset", "target": "research-desk",
            "content": {
                "systemPrompt": "You are the research desk.",
                "name": "Research Desk",
                "tools": ["web_search"],
            },
            "rationale": "a research preset is missing",
        }],
    )
    evolution.admit(candidate.id)
    evolution.apply(candidate.id)
    assert catalog.resolve("research-desk").systemPrompt == "You are the research desk."
    evolution.rollback(candidate.id)
    assert catalog.get_local("research-desk") is None


def test_status_counts_admitted_and_applied(evolution):
    first = evolution.propose(kind="prompt_section", target="a", mutations=[_mutation("A", target="a")])
    second = evolution.propose(kind="prompt_section", target="b", mutations=[_mutation("B", target="b")])
    evolution.admit(first.id)
    evolution.apply(first.id)
    assert evolution.status()["candidates"]["applied"] == 1
    assert evolution.status()["candidates"]["proposed"] == 1
    assert second.status is CandidateStatus.PROPOSED
