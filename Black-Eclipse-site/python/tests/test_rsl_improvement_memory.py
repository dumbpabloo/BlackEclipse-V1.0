"""SIM — the harness optimizer's problem-keyed improvement ledger.

These tests pin the properties that make the record trustworthy: no fabricated
measurement, append-only history, merge-preserving identity, a bounded render
that admits omission, and a dark-by-default wiring.
"""

from __future__ import annotations

import pytest

from harness.self_improvement.config import RSLConfig
from harness.self_improvement.improvement_memory import (
    ImprovementMemory,
    MemoryConfig,
    Provenance,
    compute_outcome,
    score_prediction,
)
from harness.self_improvement.loop import RSLLoop
from harness.self_improvement.proposer_model import _prompt
from harness.persistence import AgentMemoryStore


def _provenance(**overrides) -> Provenance:
    base = {"domain": "coding", "model": "gemini-flash", "round": 1}
    base.update(overrides)
    return Provenance(**base)


def test_missing_measurement_is_unmeasured_never_flat():
    assert compute_outcome(passed=0, total=0, score_before=None, score_after=None) == "unmeasured"
    # No measured trials is unmeasured even when two scores are supplied.
    assert compute_outcome(passed=0, total=0, score_before=0.7, score_after=0.7) == "unmeasured"
    assert compute_outcome(passed=9, total=10, score_before=None, score_after=None) == "flat"
    assert compute_outcome(passed=10, total=10, score_before=None, score_after=None) == "improved"
    assert compute_outcome(passed=0, total=10, score_before=None, score_after=None) == "regressed"
    assert compute_outcome(passed=5, total=10, score_before=0.5, score_after=0.6) == "improved"
    assert compute_outcome(passed=5, total=10, score_before=0.6, score_after=0.5) == "regressed"


def test_prediction_is_only_resolved_by_evidence():
    fixed, missed, unresolved = score_prediction(
        ["git-webserver", "fibsqrt", "routing"],
        ["git-webserver"],
        [],
    )
    assert fixed == ("git-webserver",)
    assert missed == ()
    assert unresolved == ("fibsqrt", "routing")


def test_identical_root_cause_text_merges_and_keeps_history():
    memory = ImprovementMemory()
    first = memory.record_result(
        "Premature completion without execution evidence",
        edit="added an execution interlock", provenance=_provenance(round=2),
        passed=5, total=5, score_before=0.2, score_after=0.5, accepted=True,
    )
    assert first.id == "P1.A1"
    merged = memory.record_result(
        # case/whitespace only differ: same problem, not a new entry
        "premature completion   without EXECUTION evidence",
        edit="tightened the confirmation prompt", provenance=_provenance(round=4),
        passed=0, total=0,
    )
    assert merged.id == "P1.A2"
    assert len(memory.problems) == 1
    assert memory.problems[0].attempt_count == 2
    # The unmeasured attempt is retained even though nothing was measured.
    assert memory.problems[0].attempts[-1].outcome == "unmeasured"


def test_alias_merge_preserves_attempts_and_resolves_the_old_id():
    memory = ImprovementMemory()
    memory.record_result("root cause one", edit="a", provenance=_provenance())
    memory.record_result("root cause two", edit="b", provenance=_provenance())
    assert [p.id for p in memory.problems] == ["P1", "P2"]
    target = memory.merge("P2", "P1")
    assert target.id == "P1"
    assert target.attempt_count == 2
    assert memory.aliases == {"P2": "P1"}
    assert memory.find("P2").id == "P1"


def test_rejected_and_regressed_attempts_are_recorded_not_pruned():
    memory = ImprovementMemory()
    memory.record_result(
        "completion gate", edit="tightened the gate",
        provenance=_provenance(round=1),
        passed=4, total=10, score_before=0.5, score_after=0.3, accepted=False,
    )
    attempt = memory.problems[0].attempts[0]
    assert attempt.accepted is False
    assert attempt.outcome == "regressed"
    assert memory.stats()["regressed"] == 1


def test_predictions_hit_rate_counts_only_scored_predictions():
    memory = ImprovementMemory()
    memory.record_result(
        "gate", edit="add predicate", provenance=_provenance(),
        predicted=["a", "b", "c"], fixed=["a"], missed=["b"],
    )
    stats = memory.stats()
    assert stats["predictions"] == 1
    assert stats["predictionsResolved"] == 1
    assert stats["predictionHitRate"] == 0.5
    # `c` stays explicitly unresolved.
    assert memory.problems[0].attempts[0].unresolved == ("c",)

    empty = ImprovementMemory()
    empty.record_result("gate", edit="x", provenance=_provenance())
    assert empty.stats()["predictionHitRate"] is None


def test_round_trip_is_exact_and_save_is_atomic(tmp_path):
    path = tmp_path / "memory.json"
    memory = ImprovementMemory()
    memory.record_result(
        "gate", edit="add predicate", rationale="cheap and falsifiable",
        provenance=_provenance(round=3, candidateId="c-9", stateDigest="deadbeef"),
        predicted=["a"], fixed=["a"], passed=9, total=10,
        score_before=0.4, score_after=0.6, accepted=True,
    )
    memory.save(str(path))
    assert path.exists()
    assert not list(tmp_path.glob(".memory-*.tmp"))

    loaded = ImprovementMemory.load(str(path))
    assert loaded.to_dict() == memory.to_dict()
    assert loaded.problems[0].attempts[0].provenance.candidateId == "c-9"
    assert loaded.problems[0].attempts[0].outcome == "improved"


def test_missing_file_is_empty_but_corrupt_file_is_loud(tmp_path):
    assert ImprovementMemory.load(str(tmp_path / "nope.json")).problems == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="SIM_MEMORY_CORRUPT"):
        ImprovementMemory.load(str(bad))


def test_carry_from_keeps_source_provenance_and_marks_the_carry():
    source = ImprovementMemory()
    source.record_result(
        "gate", edit="execution interlock", provenance=_provenance(domain="tb2.1"),
        passed=5, total=5, score_before=0.2, score_after=0.5, accepted=True,
    )
    carried = ImprovementMemory().carry_from(source, domain="tb4", model="gemini-3.8")
    assert carried.carried_from == {"domain": "tb4", "model": "gemini-3.8", "round": 0, "problems": 1}
    assert carried.problems[0].attempts[0].provenance.domain == "tb2.1"
    assert "Carried from: tb4 / gemini-3.8" in carried.render()


def test_render_is_bounded_and_says_what_it_omitted():
    memory = ImprovementMemory()
    for index in range(40):
        memory.record_result(
            f"problem number {index} with a deliberately long name",
            edit="a change that also has a fairly long description " * 3,
            rationale="because the failure repeated and the gate did not check it",
            provenance=_provenance(round=index),
            passed=1, total=10,
        )
    block = memory.render(max_chars=1200)
    assert len(block) <= 1200 + 400  # header + omission footer
    assert "Truncated:" in block
    assert "older problem(s)" in block
    # The newest problem (highest round) is the one that survives.
    assert "problem number 39" in block
    assert "problem number 0 " not in block
    assert memory.render(max_chars=1200) == block  # deterministic


def test_empty_memory_renders_nothing_and_prompt_gains_no_section():
    empty = ImprovementMemory()
    assert empty.render() == ""
    prompt = _prompt({"problemStatement": "gate"}, k=1, memory=empty)
    assert "Prior improvement experience" not in prompt


def test_prompt_carries_memory_and_the_reuse_rule():
    memory = ImprovementMemory()
    memory.record_result(
        "Premature completion", edit="added an execution interlock",
        rationale="completion needs evidence", provenance=_provenance(round=6),
        predicted=["configure-git-webserver"], fixed=["configure-git-webserver"],
        passed=5, total=5, score_before=0.2, score_after=1.0, accepted=True,
    )
    prompt = _prompt({"problemStatement": "gate"}, k=2, memory=memory)
    assert "Prior improvement experience" in prompt
    assert "P1 Premature completion" in prompt
    assert "Cite an existing problem id" in prompt
    assert "configure-git-webserver" in prompt


def test_prompt_drops_memory_that_would_leak_a_grader_verdict():
    memory = ImprovementMemory()
    memory.record_result(
        "gate", edit="adjusted the check",
        rationale="expected_output must be 42", provenance=_provenance(),
    )
    assert "expected_output" in memory.render()
    prompt = _prompt({"problemStatement": "gate"}, k=1, memory=memory)
    assert "Prior improvement experience" not in prompt
    assert "expected_output" not in prompt


def test_memory_config_is_off_by_default_and_clamped():
    assert MemoryConfig.from_env(environ={}).enabled is False
    config = MemoryConfig.from_env(environ={
        "HARNESS_RSL_MEMORY": "on",
        "HARNESS_RSL_MEMORY_PATH": "/tmp/sim.json",
        "HARNESS_RSL_MEMORY_CHARS": "100",
    })
    assert config.enabled is True
    assert config.path.endswith("sim.json")
    assert config.max_chars == 800  # clamped floor, never zero


def test_rsl_config_exposes_the_dark_memory_knob():
    default = RSLConfig.from_env(environ={})
    assert default.memory is False
    assert default.memory_chars == 6000
    enabled = RSLConfig.from_env(environ={
        "HARNESS_RSL_MEMORY": "on",
        "HARNESS_RSL_MEMORY_PATH": "C:/tmp/sim.json",
        "HARNESS_RSL_MEMORY_CHARS": "2500",
    })
    assert enabled.memory is True
    assert enabled.memory_path == "C:/tmp/sim.json"
    assert enabled.memory_chars == 2500
    assert enabled.to_dict()["memory"] is True
    assert RSLConfig.from_dict({"memory": True}).memory is True


@pytest.fixture()
def store(tmp_path):
    memory = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    yield memory
    memory.close()


def _hillclimb_loop(store, tmp_path, *, memory_on: bool) -> RSLLoop:
    return RSLLoop(
        service=None,
        config=RSLConfig(
            mode="verify", strategy="hillclimb", memory=memory_on,
            memory_path=str(tmp_path / "self-improvement-memory.json"),
        ),
        run_store=store,
    )


@pytest.mark.asyncio
async def test_hillclimb_appends_accepted_and_rejected_attempts(store, tmp_path):
    loop = _hillclimb_loop(store, tmp_path, memory_on=True)
    loop.state_store.save_state({
        "scope": "global", "snapshotDigest": "base", "utility": 0.7,
        "metrics": {"aggregates": {"task_success": 0.7, "successes": 7, "sampleSize": 10}},
        "status": "current",
    })
    accepted = {
        "id": "c1", "snapshotDigest": "cand-1",
        "problemStatement": "Completion accepted without execution evidence",
        "rationale": "the gate checks intent, not evidence",
        "predictedFixes": ["configure-git-webserver", "fibsqrt"],
        "fixed": ["configure-git-webserver"], "missed": ["fibsqrt"],
        "validation": {"aggregates": {"task_success": 0.9, "successes": 9, "sampleSize": 10}, "measuredTrials": 10},
        "replay": {"taskRates": {"a": 1.0}},
    }
    rejected = {
        "id": "c2", "snapshotDigest": "cand-2",
        "problemStatement": "Terminal output truncated on timeout",
        "validation": {"aggregates": {"task_success": 0.6, "successes": 6, "sampleSize": 10}, "measuredTrials": 10},
        "replay": {"taskRates": {"a": 1.0}},
    }

    async def propose(state, k, generation, rejected_digests):
        return [accepted, rejected] if generation == 0 else []

    async def evaluate(item, split):
        return item["validation"] if split == "validation" else item["replay"]

    result = await loop.run_hillclimb(
        propose=propose, evaluate=evaluate, replay_baseline={"a": 1.0},
    )

    assert result["refused"] is False
    assert result["memory"]["recorded"] == 2
    path = tmp_path / "self-improvement-memory.json"
    assert path.exists()

    loaded = ImprovementMemory.load(str(path))
    assert len(loaded.problems) == 2
    gate = loaded.find("Completion accepted without execution evidence")
    assert gate is not None
    attempt = gate.attempts[-1]
    assert attempt.accepted is True
    assert attempt.outcome == "improved"
    assert attempt.fixed == ("configure-git-webserver",)
    assert attempt.missed == ("fibsqrt",)
    # The rejected neighbor is evidence too: it is not pruned.
    truncated = loaded.find("Terminal output truncated on timeout")
    assert truncated is not None
    assert truncated.attempts[-1].accepted is False
    assert truncated.attempts[-1].outcome == "regressed"

    status = loop.status()
    assert status["memory"]["enabled"] is True
    assert status["memory"]["stats"]["attempts"] == 2


@pytest.mark.asyncio
async def test_memory_off_leaves_hillclimb_untouched(store, tmp_path):
    loop = _hillclimb_loop(store, tmp_path, memory_on=False)
    loop.state_store.save_state({
        "scope": "global", "snapshotDigest": "base", "utility": 0.7,
        "metrics": {"aggregates": {"task_success": 0.7}}, "status": "current",
    })
    candidate = {
        "id": "c1", "snapshotDigest": "cand-1", "utility": 0.8,
        "validation": {"aggregates": {"task_success": 0.8}},
        "replay": {"taskRates": {"a": 1.0}},
    }

    async def propose(state, k, generation, rejected_digests):
        return [candidate] if generation == 0 else []

    async def evaluate(item, split):
        return item["validation"] if split == "validation" else item["replay"]

    result = await loop.run_hillclimb(
        propose=propose, evaluate=evaluate, replay_baseline={"a": 1.0},
    )
    assert "memory" not in result
    assert not (tmp_path / "self-improvement-memory.json").exists()
    assert loop.status()["memory"] == {"enabled": False}
    assert loop.memory_block() == ""
