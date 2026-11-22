"""Numeric parity, seventh tier: multiple-testing control.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/quantlib/multipletesting.py`). Two pieces were missing and are ported:

* **``probabilistic_sharpe_ratio``** as a standalone statistic. Eclipse computed the
  same quantity privately inside the deflated Sharpe, with no benchmark argument, no
  minimum-sample guard and no kurtosis sanity check — the reference refuses
  ``T < 30`` and a kurtosis below 1, because at small ``T`` the skew/kurtosis
  estimates are meaningless and an excess-kurtosis caller silently *overstates*
  confidence.
* **``benjamini_hochberg``** — false-discovery-rate control. Absent entirely.
  When the question is "which of these 462 factors are real" rather than "is the
  best one real", deflating one maximum is the wrong tool.

The reference functions are reimplemented here from source; nothing imports the
other project.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from harness.quant import multiple_testing as MT

PHI = NormalDist().cdf
EULER = 0.5772156649015329


# -- the reference formulas ------------------------------------------------


def reference_psr(observed_sharpe, n_observations, benchmark_sharpe=0.0,
                  skew=0.0, kurtosis=3.0):
    variance = (1.0 - skew * observed_sharpe
                + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2)
    z = ((observed_sharpe - benchmark_sharpe) * math.sqrt(n_observations - 1)
         / math.sqrt(variance))
    return PHI(z)


def reference_expected_max(n_trials, trial_sharpe_std):
    quantile_a = NormalDist().inv_cdf(1.0 - 1.0 / n_trials)
    quantile_b = NormalDist().inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return trial_sharpe_std * ((1.0 - EULER) * quantile_a + EULER * quantile_b)


def reference_bh(p_values, fdr=0.05):
    n = len(p_values)
    order = sorted(range(n), key=lambda i: p_values[i])
    sorted_p = [p_values[i] for i in order]
    cutoff = 0
    for rank in range(1, n + 1):
        if sorted_p[rank - 1] <= (rank / n) * fdr:
            cutoff = rank
    rejected = [False] * n
    for rank in range(1, cutoff + 1):
        rejected[order[rank - 1]] = True
    adjusted = [0.0] * n
    running = 1.0
    for rank in range(n, 0, -1):
        running = min(running, n / rank * sorted_p[rank - 1])
        adjusted[order[rank - 1]] = min(1.0, running)
    return rejected, adjusted, sorted_p[cutoff - 1] if cutoff else 0.0


# -- PSR: the closed form, exactly ----------------------------------------


@pytest.mark.parametrize("sharpe", [0.01, 0.05, 0.10, 0.20])
@pytest.mark.parametrize("observations", [30, 60, 252, 1000])
@pytest.mark.parametrize("benchmark", [0.0, 0.02])
@pytest.mark.parametrize("skew,kurt", [(0.0, 3.0), (-0.8, 6.0), (0.6, 2.5)])
def test_psr_matches_the_reference_closed_form(sharpe, observations, benchmark, skew, kurt):
    assert MT.probabilistic_sharpe_ratio(
        sharpe, observations, benchmark, skew, kurt
    ) == pytest.approx(reference_psr(sharpe, observations, benchmark, skew, kurt),
                       rel=0, abs=1e-12)


def test_psr_is_half_at_the_benchmark_whatever_the_moments():
    # at SR == SR* the statistic is exactly 0, so the answer is 0.5
    assert MT.probabilistic_sharpe_ratio(0.05, 100, 0.05, -0.5, 5.0) == pytest.approx(0.5)


def test_fat_tails_lower_confidence_for_the_same_sharpe():
    normal = MT.probabilistic_sharpe_ratio(0.08, 252, 0.0, 0.0, 3.0)
    fat = MT.probabilistic_sharpe_ratio(0.08, 252, 0.0, -1.0, 9.0)
    assert fat < normal


def test_psr_refuses_a_sample_too_short_for_its_moments():
    with pytest.raises(MT.MultipleTestingError) as exc:
        MT.probabilistic_sharpe_ratio(0.1, MT.MIN_OBSERVATIONS - 1)
    assert "observations" in str(exc.value)


def test_psr_refuses_an_excess_kurtosis_caller():
    """A Gaussian has kurtosis 3; 0 means the caller passed the excess form."""
    with pytest.raises(MT.MultipleTestingError) as exc:
        MT.probabilistic_sharpe_ratio(0.1, 100, 0.0, 0.0, 0.0)
    assert "non-excess" in str(exc.value)


def test_psr_refuses_inconsistent_moments_rather_than_returning_a_number():
    # positive skew with a Sharpe of 1 makes 1 - g3*SR + (g4-1)/4*SR^2 negative
    with pytest.raises(MT.MultipleTestingError) as exc:
        MT.probabilistic_sharpe_ratio(1.0, 100, 0.0, 5.0, 3.0)
    assert "variance term" in str(exc.value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_psr_refuses_non_finite_inputs(bad):
    with pytest.raises(MT.MultipleTestingError):
        MT.probabilistic_sharpe_ratio(bad, 100)


# -- DSR: the whole chain against the reference ----------------------------


def test_the_deflated_sharpe_chain_matches_the_reference():
    returns = [0.004, -0.002, 0.006, 0.001, -0.003, 0.005, 0.002, 0.0005,
               0.004, -0.001, 0.003, 0.002, 0.006, -0.002, 0.001, 0.003,
               0.002, -0.004, 0.005, 0.001, 0.002, 0.003, -0.002, 0.004,
               0.001, 0.002, 0.005, -0.001, 0.002, 0.003, 0.001, 0.004]
    trial_sharpes = [0.02, 0.05, 0.01, 0.03, 0.04, 0.0]
    out = MT.deflated_sharpe(returns, trial_sharpes=trial_sharpes)
    assert out["available"] is True

    sr = out["selectedSharpe"]
    bar = reference_expected_max(len(trial_sharpes), math.sqrt(out["trialSharpeVariance"]))
    expected = reference_psr(sr, len(returns), bar, out["skew"], out["kurtosis"])
    assert out["expectedMaxSharpe"] == pytest.approx(bar, rel=1e-6)
    assert out["deflatedSharpeRatio"] == pytest.approx(expected, rel=1e-6)


def test_the_bar_rises_with_the_number_of_trials():
    one = reference_expected_max(10, 0.03)
    many = reference_expected_max(500, 0.03)
    assert many > one > 0


# -- Benjamini-Hochberg ---------------------------------------------------


FDR_FIXTURES = [
    [0.01, 0.04, 0.03, 0.002, 0.5],
    [0.001, 0.002, 0.003],
    [0.5, 0.6, 0.7],
    [0.049, 0.05, 0.051],
    [0.0001, 0.2, 0.04, 0.9, 0.03, 0.01],
    [1.0, 0.0, 0.5],
]


@pytest.mark.parametrize("p_values", FDR_FIXTURES)
@pytest.mark.parametrize("fdr", [0.05, 0.10, 0.20])
def test_bh_matches_the_reference(p_values, fdr):
    got = MT.benjamini_hochberg(p_values, fdr=fdr)
    rejected, adjusted, threshold = reference_bh(p_values, fdr)
    assert got["rejected"] == rejected
    assert got["adjustedPValues"] == pytest.approx(adjusted, rel=0, abs=1e-12)
    assert got["threshold"] == pytest.approx(threshold)
    assert got["nRejected"] == sum(rejected)


def test_adjusted_p_values_are_monotone_in_the_raw_p_value():
    p_values = [0.04, 0.001, 0.03, 0.5, 0.002]
    out = MT.benjamini_hochberg(p_values)
    pairs = sorted(zip(p_values, out["adjustedPValues"]))
    adjusted = [a for _p, a in pairs]
    assert adjusted == sorted(adjusted)
    assert all(0.0 <= a <= 1.0 for a in adjusted)


def test_nothing_is_rejected_when_nothing_is_significant():
    out = MT.benjamini_hochberg([0.5, 0.6, 0.7])
    assert out["nRejected"] == 0
    assert out["threshold"] == 0.0


def test_the_threshold_is_the_largest_rejected_raw_p_value():
    out = MT.benjamini_hochberg([0.01, 0.04, 0.03, 0.002, 0.5])
    assert out["threshold"] == pytest.approx(0.04)
    assert out["nRejected"] == 4


def test_bh_refuses_inputs_it_cannot_control():
    with pytest.raises(MT.MultipleTestingError):
        MT.benjamini_hochberg([])
    with pytest.raises(MT.MultipleTestingError):
        MT.benjamini_hochberg([0.1, 1.2])
    with pytest.raises(MT.MultipleTestingError):
        MT.benjamini_hochberg([0.1, float("nan")])
    with pytest.raises(MT.MultipleTestingError):
        MT.benjamini_hochberg([0.1], fdr=0.0)
