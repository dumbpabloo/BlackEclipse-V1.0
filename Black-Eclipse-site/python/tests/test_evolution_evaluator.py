"""Phase 6: measured/UNMEASURED evaluation and the durable journal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.evolution import (
    AdapterRegistry,
    AssetEvaluator,
    AssetEvolution,
    CandidateStatus,
    PromptSectionAdapter,
)
from harness.evolution.journal import EvolutionJournal as Journal
from harness.kernel.services.prompt import PromptService


class _Suite:
    def __init__(self, aggregates, *, measured=1, run_id="eval-run"):
        self.aggregates = aggregates
        self.measured_trials = measured
        self.run_id = run_id


class _Runner:
    def __init__(self, suite):
        self.suite = suite

    async def run_suite(self, *args, **kwargs):
        return self.suite


def _mutation(text="Prefer the filing index.", target="learned.filings", op="add"):
    return {
        "op": op, "kind": "prompt_section", "target": target,
        "content": {"text": text}, "rationale": "r",
    }


def _stack(journal=None, **kwargs):
    prompts = PromptService()
    adapters = AdapterRegistry()
    adapters.register(PromptSectionAdapter(prompts, default_scope="eclipse-agent"))
    return prompts, AssetEvolution(adapters, journal, **kwargs)


def _candidate(evolution, target="learned.filings"):
    candidate = evolution.propose(
        kind="prompt_section", target=target, mutations=[_mutation(target=target)],
    )
    evolution.admit(candidate.id)
    return candidate


async def test_accepted_measurement_marks_measured_ok_and_stays_applied():
    prompts, evolution = _stack()
    candidate = _candidate(evolution)
    evaluator = AssetEvaluator(evolution)
    result = await evaluator.evaluate(
        candidate.id,
        runner=_Runner(_Suite({"task_success": 0.9}, run_id="run-ok")),
        tasks=[],
        baseline_metrics={"aggregates": {"task_success": 0.5}},
    )
    assert result["status"] == "measured_ok"
    assert candidate.status is CandidateStatus.MEASURED_OK
    assert prompts.render_sections("eclipse-agent") == "Prefer the filing index."
    assert "run-ok" in candidate.evidence_refs


async def test_rejected_measurement_marks_measured_bad_and_reverts():
    prompts, evolution = _stack()
    candidate = _candidate(evolution)
    evaluator = AssetEvaluator(evolution)
    result = await evaluator.evaluate(
        candidate.id,
        runner=_Runner(_Suite({"task_success": 0.2}, run_id="run-bad")),
        tasks=[],
        baseline_metrics={"aggregates": {"task_success": 0.5}},
    )
    assert result["status"] == "measured_bad"
    assert candidate.status is CandidateStatus.MEASURED_BAD
    assert prompts.render_sections("eclipse-agent") == ""


async def test_no_measured_trial_is_unmeasured_and_never_applied():
    prompts, evolution = _stack()
    candidate = _candidate(evolution)
    evaluator = AssetEvaluator(evolution)
    result = await evaluator.evaluate(
        candidate.id,
        runner=_Runner(_Suite({"task_success": 0.9}, measured=0)),
        tasks=[],
        baseline_metrics={"aggregates": {"task_success": 0.5}},
    )
    assert result["status"] == "unmeasured"
    assert candidate.status is CandidateStatus.UNMEASURED
    assert prompts.render_sections("eclipse-agent") == ""


async def test_missing_baseline_dimension_is_unmeasured_without_applying():
    prompts, evolution = _stack()
    candidate = _candidate(evolution)
    evaluator = AssetEvaluator(evolution)
    result = await evaluator.evaluate(
        candidate.id,
        runner=_Runner(_Suite({"task_success": 0.9})),
        tasks=[],
        baseline_metrics={"aggregates": {}},
    )
    assert result["status"] == "unmeasured"
    assert prompts.render_sections("eclipse-agent") == ""


async def test_post_apply_regression_rolls_back_and_is_journalled(tmp_path):
    journal = Journal(tmp_path / "evo.jsonl")
    prompts, evolution = _stack(journal)
    candidate = _candidate(evolution)
    evaluator = AssetEvaluator(evolution)
    await evaluator.evaluate(
        candidate.id,
        runner=_Runner(_Suite({"task_success": 0.9}, run_id="run-watch")),
        tasks=[],
        baseline_metrics={"aggregates": {"task_success": 0.5}},
        watch_keys=["filing_lookup_failed"],
    )
    assert candidate.status is CandidateStatus.MEASURED_OK
    assert prompts.render_sections("eclipse-agent") != ""

    rolled = evolution.observe_failure("filing_lookup_failed")
    assert rolled == [candidate.id]
    assert candidate.status is CandidateStatus.ROLLED_BACK
    assert prompts.render_sections("eclipse-agent") == ""
    assert any(record.kind == "regression" for record in journal.records())
    assert journal.verify()[0] is True


async def test_ttl_expiry_rolls_back(tmp_path):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    journal = Journal(tmp_path / "ttl.jsonl")
    prompts, evolution = _stack(journal, ttl_seconds=60, now=lambda: clock["now"])
    candidate = _candidate(evolution)
    evolution.apply(candidate.id)
    assert prompts.render_sections("eclipse-agent") != ""
    clock["now"] = clock["now"] + timedelta(seconds=61)
    assert evolution.sweep_expired() == [candidate.id]
    assert candidate.status is CandidateStatus.ROLLED_BACK
    assert prompts.render_sections("eclipse-agent") == ""


async def test_crash_recovery_does_not_apply_twice(tmp_path):
    path = tmp_path / "crash.jsonl"
    prompts, evolution = _stack(Journal(path))
    candidate = _candidate(evolution)
    evolution.apply(candidate.id)

    # A fresh service over the same journal recovers APPLIED; re-applying the
    # same candidate is a no-op (no adapter call).
    fresh_prompts, fresh = _stack(Journal(path))
    assert fresh.recovered_intents == []
    recovered = fresh.get(candidate.id)
    assert recovered.status is CandidateStatus.APPLIED
    fresh.apply(candidate.id)
    assert recovered.status is CandidateStatus.APPLIED
    assert fresh_prompts.render_sections("eclipse-agent") == ""


async def test_pending_apply_intent_is_surfaced_on_recovery(tmp_path):
    path = tmp_path / "pending.jsonl"
    journal = Journal(path)
    prompts, evolution = _stack(journal)
    candidate = _candidate(evolution)
    journal.begin(f"apply:{candidate.id}:{candidate.digest}")

    fresh = AssetEvolution(
        evolution.adapters,
        Journal(path),
    )
    assert fresh.recovered_intents == [f"apply:{candidate.id}:{candidate.digest}"]
    assert fresh.get(candidate.id).status is CandidateStatus.APPLIED
    assert fresh.get(candidate.id).recovered is True


async def test_active_cap_refuses_a_second_apply():
    prompts, evolution = _stack(max_active=1)
    first = _candidate(evolution, "a")
    second = _candidate(evolution, "b")
    evolution.apply(first.id)
    with pytest.raises(Exception) as info:
        evolution.apply(second.id)
    assert "cap" in str(info.value)


async def test_journal_detects_a_rewritten_record(tmp_path):
    journal = Journal(tmp_path / "tamper.jsonl")
    journal.append("intent", "k1", {"x": 1})
    journal.append("complete", "k1", {"ok": True})
    path = journal.path
    text = path.read_text(encoding="utf-8").replace('"x":1', '"x":2')
    path.write_text(text, encoding="utf-8")
    fresh = Journal(path)
    ok, reason = fresh.verify()
    assert ok is False
    assert "journal broken" in reason
