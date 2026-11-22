"""Numeric parity, nineteenth tier: strategy-discovery evidence arithmetic.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/strategy_discovery/models.py`).

Eclipse's discovery surface had no arithmetic for "is this evidence still any good".
Ported the reference's pure helpers with its constants, and three details that are
load-bearing rather than incidental:

* a window end is the **last day of its end month**, so `2024-06` covers through
  30 June and age is measured from month-end;
* an exact threshold belongs to the **stricter** side (age 180 ⇒ stale, 90 ⇒ aging),
  and an **undatable window is stale fail-closed**;
* the gating age is the **evidence window's**, never `last_verified` — refreshing
  unchanged artifacts must not keep a dead row fresh.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from harness.quant.discovery import evidence as E


TODAY = date(2026, 6, 30)


# -- constants are the reference's ---------------------------------------

def test_the_thresholds_are_the_references():
    assert E.MIN_TRADES == 10
    assert E.MIN_COVERAGE_DAYS == 730
    assert E.COST_SENSITIVE_BREAKEVEN_BPS == 5.0
    assert E.BORDERLINE_TRADE_BUFFER == 5
    assert E.BORDERLINE_BREAKEVEN_BPS == 10.0
    assert E.BORDERLINE_COVERAGE_BUFFER_DAYS == 365
    assert E.DECAY_AGING_EVIDENCE_AGE_DAYS == 90
    assert E.DECAY_STALE_EVIDENCE_AGE_DAYS == 180
    assert E.QUALITY_ORDER == {"adequate": 2, "marginal": 1, "insufficient": 0}


# -- coverage, anchored to month end -------------------------------------

def test_coverage_runs_from_the_first_day_to_the_last_day_of_the_end_month():
    # 2020-01-01 .. 2022-01-31 = 761 days
    assert E.coverage_days_from_ranges(["2020-01 to 2022-01"]) == 761


def test_coverage_spans_the_widest_range_not_the_first_one():
    span = E.coverage_days_from_ranges(["2021-01 to 2021-12", "2020-01 to 2022-01"])
    assert span == 761


def test_malformed_ranges_contribute_nothing_and_an_undatable_span_is_zero():
    assert E.coverage_days_from_ranges(["garbage", "2020-13 to 2021-01"]) == 0
    assert E.coverage_days_from_ranges([]) == 0


def test_the_quality_ladder_is_trades_then_coverage():
    assert E.classify_quality(9, 5_000) == "insufficient"
    assert E.classify_quality(10, 729) == "marginal"
    assert E.classify_quality(10, 730) == "adequate"


# -- decay, with the stricter boundary and fail-closed dating ------------

def test_a_window_ending_this_month_is_fresh():
    assert E.classify_decay(["2025-01 to 2026-06"], TODAY) == "fresh"


def test_exactly_ninety_days_is_aging_and_exactly_one_eighty_is_stale():
    # window end 2026-03-31 is 91 days before 2026-06-30 -> aging
    assert E.classify_decay(["2025-01 to 2026-03"], TODAY) == "aging"
    # window end 2025-12-31 is 181 days before -> stale
    assert E.classify_decay(["2025-01 to 2025-12"], TODAY) == "stale"


def test_the_exact_threshold_belongs_to_the_stricter_side():
    end = date(2026, 1, 31)
    assert E.evidence_age_days(["2025-01 to 2026-01"], TODAY) == (TODAY - end).days
    # pass thresholds that coincide with the computed age, both directions
    age = (TODAY - end).days
    assert E.classify_decay(["2025-01 to 2026-01"], TODAY,
                            aging_days=age, stale_days=age + 1) == "aging"
    assert E.classify_decay(["2025-01 to 2026-01"], TODAY,
                            aging_days=age - 1, stale_days=age) == "stale"


def test_an_undatable_window_is_stale_fail_closed():
    assert E.classify_decay([], TODAY) == "stale"
    assert E.classify_decay(["nonsense"], TODAY) == "stale"
    assert E.evidence_age_days([], TODAY) is None


def test_the_age_is_clamped_at_zero_for_a_future_window():
    assert E.evidence_age_days(["2025-01 to 2027-01"], TODAY) == 0


def test_last_verified_is_reported_but_never_decides_the_verdict():
    """A recently refreshed row with an old window is still stale."""
    assert E.staleness_days("2026-06-29", TODAY) == 1
    assert E.classify_decay(["2019-01 to 2019-12"], TODAY) == "stale"
    assert E.staleness_days("not-a-date", TODAY) is None
    assert E.staleness_days("", TODAY) is None


def test_the_warnings_carry_stable_prefix_tokens():
    stale = E.decay_warning("stale", 200)
    aging = E.decay_warning("aging", 100)
    undated = E.decay_warning("stale", None)
    assert stale.startswith("stale-evidence:") and "200 days" in stale
    assert aging.startswith("aged-evidence:")
    assert undated.startswith("stale-evidence:") and "unparseable" in undated
    assert E.decay_warning("fresh", 5) is None


# -- breakeven fee: the logarithmic identity ------------------------------

def test_breakeven_is_the_log_identity():
    expected = math.log(1.2) / (2 * 100 * 1.0) * 10_000
    assert E.breakeven_fee_bps(0.2, 100) == pytest.approx(expected)


def test_sizing_scales_the_breakeven_down():
    full = E.breakeven_fee_bps(0.2, 100, 1.0)
    half = E.breakeven_fee_bps(0.2, 100, 0.5)
    assert half == pytest.approx(full * 2)


def test_unusable_inputs_return_none_rather_than_infinity():
    assert E.breakeven_fee_bps(0.2, 0) is None          # no trades
    assert E.breakeven_fee_bps(0.2, -5) is None         # negative trades
    assert E.breakeven_fee_bps(-1.0, 10) is None        # total wipeout
    assert E.breakeven_fee_bps(-2.0, 10) is None        # worse than total loss
    assert E.breakeven_fee_bps(float("nan"), 10) is None
    assert E.breakeven_fee_bps(0.2, 10, 0.0) is None    # zero sizing
    assert E.breakeven_fee_bps(0.2, 10, -0.5) is None   # negative sizing


# -- borderline bands and warnings ---------------------------------------

def test_a_row_just_inside_a_threshold_is_borderline():
    # passes (10 trades, 730 days) but sits inside both buffered bands
    assert E.is_borderline(10, 8.0, 730) is True
    # comfortably clear of every band
    assert E.is_borderline(20, 25.0, 1_200) is False


def test_an_unknown_breakeven_does_not_by_itself_make_a_row_borderline():
    assert E.is_borderline(20, None, 1_200) is False


def test_warnings_are_emitted_only_for_conditions_that_hold():
    assert E.build_warnings(20, 1_200, 25.0, "adequate", False) == ()
    short = E.build_warnings(4, 100, 3.0, "insufficient", True)
    assert short[0].startswith("insufficient-trades:")
    assert short[1].startswith("short-coverage:")
    assert short[2].startswith("cost-sensitive:")
    assert "3.00 bps" in short[2]
    unknown = E.build_warnings(20, 1_200, None, "adequate", True)
    assert unknown == (
        "cost-sensitive: breakeven unknown below 5.0 bps threshold",
    )
