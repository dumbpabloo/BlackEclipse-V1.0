"""The fan-out benchmark is deterministic, honest, and measures overlap.

The model is a simulator with a fixed per-call latency: the report must SAY it is
simulated, both variants must cover every frozen fact, and the verdict is decided
by measured wall time (quality can veto, never be traded away).
"""

from __future__ import annotations

from harness.evaluation.delegate_benchmark import (
    FROZEN_TASKS,
    VariantResult,
    adoption_verdict,
    run_delegate_benchmark,
)


def test_the_benchmark_runs_both_variants_and_reports_a_verdict():
    task = FROZEN_TASKS[0]
    report = run_delegate_benchmark(task, delay_ms=60)
    assert report.simulated is True
    assert "Simulated" in report.note
    # Both variants are scripted to answer every fact — quality is held equal.
    assert report.single.coverage == 1.0, report.single.error
    assert report.fanout.coverage == 1.0, report.fanout.error
    assert report.single.model_calls == len(task.goals) + 1
    # parent call + one per part + the final combination.
    assert report.fanout.model_calls == len(task.goals) + 2
    # The VERDICT SHAPE is asserted here; the adoption DECISION itself is pure and
    # is covered by `adoption_verdict` tests below. Asserting `adopted is True`
    # from a real wall-clock run made this test fail under machine load (the
    # fan-out edge is milliseconds), which is a property of the scheduler, not
    # of the benchmark.
    assert {"adopted", "reason", "latencyGain", "qualityDelta"} <= set(report.verdict)
    assert report.verdict["qualityDelta"] == 0.0


def test_a_quality_regression_vetoes_adoption_even_when_faster():
    single = VariantResult("single", 1000, 4, 3, coverage=1.0)
    faster_but_wrong = VariantResult("fanout", 100, 5, 4, coverage=0.5)
    verdict = adoption_verdict(single, faster_but_wrong)
    assert verdict["adopted"] is False
    assert "missed a fact" in verdict["reason"]


def test_no_measured_gain_is_not_adopted():
    single = VariantResult("single", 1000, 4, 3, coverage=1.0)
    same = VariantResult("fanout", 1000, 5, 4, coverage=1.0)
    assert adoption_verdict(single, same)["adopted"] is False


def test_a_real_latency_gain_is_adopted():
    single = VariantResult("single", 1000, 4, 3, coverage=1.0)
    faster = VariantResult("fanout", 700, 5, 4, coverage=1.0)
    verdict = adoption_verdict(single, faster)
    assert verdict["adopted"] is True
    assert verdict["latencyGain"] == 0.3


def test_the_frozen_suite_is_stable_and_offline():
    # Every task's facts are distinct and fixed; no network or provider is used.
    ids = [task.id for task in FROZEN_TASKS]
    assert len(ids) == len(set(ids))
    for task in FROZEN_TASKS:
        assert task.goals and len(task.goals) == len(task.facts)
