"""Behavioural parity with Vibe-Trading's decay state machine.

The reference is `agent/src/strategy_store/decay.py` (MIT, fetched 2026-11-21). Every
number in this suite is copied from that file's ``DecayThresholds`` defaults, and the
transition counts from its ``should_transition`` docstring. If this module and that
file ever disagree, one of them is wrong and this suite is where it shows.

The register listed `strategy_store` as ``equivalent`` on the strength of Eclipse's
`quant/store.py`. That was too generous: the store persists artifacts, but there was
**no decay state machine at all** — Eclipse only had an alpha-decay-by-horizon helper.
This suite exists because a path test cannot tell those two apart.
"""

from __future__ import annotations

import pytest

from harness.quant import decay


# -- the thresholds are the reference's, exactly ---------------------------


def test_the_thresholds_are_the_reference_defaults():
    t = decay.DecayThresholds()
    assert (t.ic_ratio_healthy, t.ic_ratio_warning, t.ic_ratio_decayed) == (0.7, 0.5, 0.3)
    assert (t.ir_healthy, t.ir_warning, t.ir_decayed) == (1.0, 0.5, 0.1)
    assert (t.ic_pos_ratio_healthy, t.ic_pos_ratio_warning,
            t.ic_pos_ratio_decayed) == (0.55, 0.45, 0.35)
    assert (t.sharpe_healthy, t.sharpe_warning, t.sharpe_decayed) == (1.0, 0.5, 0.0)
    assert (t.warnings_for_monitoring, t.warnings_for_decayed,
            t.critical_for_disabled) == (3, 2, 3)


# -- classification boundaries (>= semantics) ------------------------------


@pytest.mark.parametrize("value,expected", [
    (0.70, "HEALTHY"),
    (0.71, "HEALTHY"),
    (0.699, "WARNING"),
    (0.50, "WARNING"),
    (0.499, "DECAYED"),
    (0.30, "DECAYED"),
    (0.299, "CRITICAL"),
])
def test_ic_ratio_is_classified_at_the_boundary(value, expected):
    assert decay.classify(value, 0.7, 0.5, 0.3) == expected


def test_a_sharpe_of_exactly_zero_is_decayed_not_critical():
    # sharpe_decayed == 0.0, and the comparison is >=, so zero is not a failure.
    assert decay.classify(0.0, 1.0, 0.5, 0.0) == "DECAYED"
    assert decay.classify(-0.01, 1.0, 0.5, 0.0) == "CRITICAL"


# -- aggregation is worst-of, never an average -----------------------------


def test_the_weakest_metric_wins():
    signal = decay.evaluate_decay(ic_ratio=0.9, ir=0.9, ic_positive_ratio=0.6,
                                  sharpe=0.2)
    assert signal == "DECAYED"      # sharpe 0.2 sits between 0.0 and 0.5


def test_no_metrics_at_all_is_healthy_not_an_error():
    assert decay.evaluate_decay() == "HEALTHY"


def test_worst_orders_critical_below_decayed_below_warning():
    assert decay.worst("CRITICAL", "HEALTHY") == "CRITICAL"
    assert decay.worst("WARNING", "DECAYED") == "DECAYED"
    assert decay.worst("HEALTHY", "WARNING") == "WARNING"


def test_an_unknown_signal_is_refused():
    with pytest.raises(decay.DecayError):
        decay.worst("BROKEN", "HEALTHY")


# -- transitions need consecutive readings ---------------------------------


def test_active_moves_to_monitoring_after_three_non_healthy_readings():
    assert decay.should_transition("ACTIVE", ["WARNING", "WARNING"]) is None
    assert decay.should_transition(
        "ACTIVE", ["WARNING", "DECAYED", "CRITICAL"]) == "MONITORING"


def test_active_stays_active_when_the_window_contains_a_healthy_reading():
    assert decay.should_transition(
        "ACTIVE", ["DECAYED", "HEALTHY", "WARNING"]) is None


def test_monitoring_recovers_on_a_single_healthy_reading():
    assert decay.should_transition("MONITORING", ["DECAYED", "CRITICAL", "HEALTHY"]) == "ACTIVE"


def test_monitoring_decays_after_two_decayed_or_worse_readings():
    assert decay.should_transition("MONITORING", ["WARNING", "DECAYED"]) is None
    assert decay.should_transition("MONITORING", ["WARNING", "DECAYED", "CRITICAL"]) == "DECAYED"


def test_decayed_disables_only_after_three_consecutive_criticals():
    assert decay.should_transition("DECAYED", ["CRITICAL", "CRITICAL"]) is None
    assert decay.should_transition("DECAYED", ["CRITICAL", "CRITICAL", "CRITICAL"]) == "DISABLED"
    # a single DECAYED reading breaks the run
    assert decay.should_transition(
        "DECAYED", ["CRITICAL", "DECAYED", "CRITICAL", "CRITICAL"]) is None


def test_a_disabled_artifact_never_transitions_again():
    assert decay.should_transition("DISABLED", ["CRITICAL"] * 10) is None


def test_no_history_means_no_transition():
    assert decay.should_transition("ACTIVE", []) is None


def test_an_unknown_status_is_refused():
    with pytest.raises(decay.DecayError):
        decay.should_transition("PROBATION", ["HEALTHY"])


def test_a_short_history_cannot_satisfy_a_longer_requirement():
    # monitoring needs 2 consecutive decayed readings; one is not enough
    assert decay.should_transition("MONITORING", ["DECAYED"]) is None


def test_the_whole_lifecycle_runs_in_order():
    status = "ACTIVE"
    history: list[str] = []
    for _ in range(3):
        history.append(decay.evaluate_decay(ic_ratio=0.55))
        status = decay.should_transition(status, history) or status
    assert status == "MONITORING"
    for _ in range(2):
        history.append(decay.evaluate_decay(ic_ratio=0.35))
        status = decay.should_transition(status, history) or status
    assert status == "DECAYED"
    for _ in range(3):
        history.append(decay.evaluate_decay(ic_ratio=0.1))
        status = decay.should_transition(status, history) or status
    assert status == "DISABLED"
