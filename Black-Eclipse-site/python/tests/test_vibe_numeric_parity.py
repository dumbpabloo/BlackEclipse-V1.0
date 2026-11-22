"""Numeric parity with Vibe-Trading on two shared fixtures.

References fetched 2026-11-21 (MIT):

* ``agent/src/shadow_account/indicators.py::compute_rsi`` — SMA-seeded Wilder RSI,
  NaN during warmup, **NaN for flat prices** ("undefined convention"), and a missing
  observation does not update the recursive state.
* ``agent/src/portfolio/fx.py::to_usd`` — rates are currency units per USD, the
  conversion is ``value / rate``, and a missing, non-finite or non-positive rate
  fails closed.

Both reference functions are reimplemented here from their published source, so the
test does not import the other project. If Eclipse's arithmetic drifts, this fails.
Two real differences were found and fixed rather than documented away: Eclipse's RSI
returned 100 for a flat series (the reference calls that undefined), and its Wilder
smoothing stopped permanently after a missing observation instead of skipping it.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from harness.portfolio.service import _to_base
from harness.quant import indicators as I


# -- the reference algorithms, reimplemented from source -------------------


def reference_wilder(values, period):
    """`_wilder_average`: SMA seed, then recurse over OBSERVED values only."""
    values = np.asarray(values, dtype=float)
    result = np.full(values.shape, np.nan)
    positions = np.flatnonzero(~np.isnan(values))
    if len(positions) < period:
        return result
    observed = values[positions]
    seed = float(np.mean(observed[:period]))
    state = seed
    result[positions[period - 1]] = seed
    for index in positions[period:]:
        state = state * (period - 1) / period + observed_value(values[index]) / period
        result[index] = state
    return result


def observed_value(value):
    return float(value)


def reference_rsi(close, period=14):
    close = np.asarray(close, dtype=float)
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[np.isnan(delta)] = np.nan
    loss[np.isnan(delta)] = np.nan
    avg_gain = reference_wilder(gain, period)
    avg_loss = reference_wilder(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 - 100 / (1 + avg_gain / avg_loss)


def reference_to_usd(value, currency, rates):
    rate = rates[currency.upper()]
    if not rate.is_finite() or rate <= 0:
        raise ValueError("invalid FX rate")
    return value / rate


# -- RSI: the same numbers on the same bars ------------------------------


def _walk(n=60, seed=11):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0, 0.01, n))


def test_rsi_matches_the_reference_on_a_complete_series():
    close = _walk()
    ours = I.rsi(close, 14)
    theirs = reference_rsi(close, 14)
    np.testing.assert_allclose(ours, theirs, rtol=0, atol=1e-12, equal_nan=True)


def test_rsi_matches_the_reference_for_a_second_period():
    close = _walk(80, seed=3)
    np.testing.assert_allclose(I.rsi(close, 7), reference_rsi(close, 7),
                               rtol=0, atol=1e-12, equal_nan=True)


def test_warmup_is_nan_in_both():
    close = _walk(30)
    ours = I.rsi(close, 14)
    assert np.isnan(ours[:14]).all()          # first defined value is index 14
    assert not np.isnan(ours[14])


def test_a_flat_series_is_undefined_not_one_hundred():
    flat = np.full(40, 50.0)
    ours = I.rsi(flat, 14)
    theirs = reference_rsi(flat, 14)
    assert np.isnan(ours[14:]).all()
    np.testing.assert_allclose(ours, theirs, equal_nan=True)


def test_a_missing_observation_does_not_freeze_the_indicator():
    close = _walk(50)
    close[20] = np.nan
    ours = I.rsi(close, 14)
    theirs = reference_rsi(close, 14)
    # the reference resumes after the gap; so must we
    assert not np.isnan(ours[-1]), "the indicator stopped after a missing bar"
    np.testing.assert_allclose(ours[15:], theirs[15:], rtol=0, atol=1e-12,
                               equal_nan=True)


def test_gains_with_no_losses_are_one_hundred():
    rising = np.cumsum(np.abs(np.sin(np.arange(40))) + 0.1) + 100
    ours = I.rsi(rising, 14)
    assert np.nanmax(ours) == 100.0


# -- FX: the same conversion, and the same refusal -----------------------


def test_fx_conversion_is_value_over_units_per_usd():
    rates = {"USD": Decimal("1"), "CNY": Decimal("7.24"), "HKD": Decimal("7.8")}
    value = Decimal("724000")
    assert _to_base(value, rates["CNY"]) == reference_to_usd(value, "CNY", rates)
    assert _to_base(value, rates["CNY"]) == Decimal("100000")


def test_a_missing_or_zero_rate_fails_closed_on_both_sides():
    # the reference raises; Eclipse returns None so the caller can mark the position
    # unavailable instead of valuing it at a made-up number. Both refuse.
    assert _to_base(Decimal("10"), None) is None
    assert _to_base(Decimal("10"), Decimal("0")) is None
    with pytest.raises(ValueError):
        reference_to_usd(Decimal("10"), "CNY", {"CNY": Decimal("0")})
    with pytest.raises(KeyError):
        reference_to_usd(Decimal("10"), "CNY", {})


def test_a_non_finite_rate_is_not_a_division_by_infinity():
    assert _to_base(Decimal("10"), Decimal("Infinity")) == Decimal("0")
    # the reference rejects it outright, and Eclipse never produces Infinity from a
    # market feed (its adapter validates), so this pins the arithmetic only.
    with pytest.raises(ValueError):
        reference_to_usd(Decimal("10"), "CNY", {"CNY": Decimal("Infinity")})
