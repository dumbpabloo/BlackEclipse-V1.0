"""Numeric parity, sixth tier: backtest metrics.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/backtest/metrics.py`).
Four formulas are compared, and the two that disagreed are recorded rather than
silently reconciled:

* **drawdown** — the reference clips the high-water mark at ``initial_cash``
  (``peak = equity.cummax().clip(lower=initial_cash)``). Eclipse started the peak at
  the first observed equity, so a **first-bar loss showed zero drawdown**. Fixed and
  pinned here.
* **Sharpe** — the reference divides by ``vol + 1e-10`` and annualises with
  ``sqrt(bars_per_year)``; Eclipse uses the same ``ddof=1`` standard deviation and the
  same annualisation, so the two agree numerically. Pinned on a shared fixture.
* **Sortino** — a **deliberate divergence**: Eclipse uses the sample downside
  deviation (``n-1``, pinned by `test_quant_audit_fixes.py`), the reference uses the
  population RMS (``n``) with a ``1e-10`` fallback. The two differ by exactly
  ``sqrt(n/(n-1))``; the test asserts that ratio instead of pretending they match.
* **trade statistics** — ``profit_factor`` and ``profit_loss_ratio`` are ``None``
  (not 0.0) when there is no loss, so a run that never lost is not ranked last.
"""

from __future__ import annotations

import math

import pytest

from harness.quant.metrics import MetricInputs, MetricsEngine, _drawdown_series


# -- the reference formulas, restated from source --------------------------


def reference_drawdown(equity, initial_cash):
    peak = initial_cash
    out = []
    for value in equity:
        peak = max(peak, value)
        out.append(value / peak - 1.0 if peak else 0.0)
    return out


def reference_sharpe(returns, bars_per_year):
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return mean / (math.sqrt(var) + 1e-10) * math.sqrt(bars_per_year)


def reference_sortino_population(returns, bars_per_year):
    """Downside RMS over ALL periods (the reference's convention), not n-1."""
    n = len(returns)
    mean = sum(returns) / n
    downside = [min(0.0, r) for r in returns]
    dd = math.sqrt(sum(d * d for d in downside) / n)
    return mean / (dd + 1e-10) * math.sqrt(bars_per_year)


def _returns_from(equity):
    return [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity))]


def _engine(equity, *, trades=(), interval="1d", capital=100.0):
    return MetricsEngine(MetricInputs(
        equity=list(equity), timestamps=[None] * len(equity), trades=list(trades),
        exposure=[], interval=interval, initial_capital=capital,
    ))


# -- drawdown -------------------------------------------------------------

EQUITY = [95.0, 97.0, 90.0, 101.0, 99.0]
CAPITAL = 100.0


def test_a_first_bar_loss_is_a_drawdown_not_a_zero():
    got = _drawdown_series(EQUITY, CAPITAL)
    assert got[0] == pytest.approx(-0.05)          # 95 against 100, not 95 against 95
    assert got == pytest.approx(reference_drawdown(EQUITY, CAPITAL))


@pytest.mark.parametrize("equity", [
    [100.0, 100.0, 100.0],
    [100.0, 110.0, 90.0],
    [95.0, 120.0, 119.0],
    [80.0, 60.0, 90.0],
])
def test_the_drawdown_path_matches_the_reference(equity):
    assert _drawdown_series(equity, CAPITAL) == pytest.approx(
        reference_drawdown(equity, CAPITAL)
    )


def test_without_a_capital_argument_the_old_behaviour_is_unchanged():
    """The helper stays usable alone (peak = first observation)."""
    assert _drawdown_series([95.0, 90.0])[0] == 0.0


def test_the_engine_seeds_the_peak_with_the_capital():
    # 95 from 100 is -5%, 90 from 100 is -10%, 99 from 101 is -1.98% -> min is -10%
    assert _engine(EQUITY).max_drawdown() == pytest.approx(-10.0, abs=1e-9)


# -- Sharpe: agreement, not a divergence ---------------------------------


def test_sharpe_matches_the_reference_formula():
    equity = [100.0, 101.5, 100.2, 103.0, 99.5, 104.0]
    returns = [equity[0] / 100.0 - 1.0] + _returns_from(equity)
    engine = _engine(equity)
    # the reference's 1e-10 denominator epsilon is the only gap
    assert engine.sharpe() == pytest.approx(
        reference_sharpe(returns, engine.periods), rel=1e-7
    )


def test_sharpe_is_none_when_there_is_nothing_to_divide():
    # a flat curve has zero volatility; the reference would divide by 1e-10 and
    # report a number, Eclipse reports "undefined" rather than a huge ratio.
    assert _engine([100.0, 100.0, 100.0]).sharpe() is None


# -- Sortino: a stated difference, quantified ----------------------------


def test_sortino_differs_from_the_reference_only_by_the_degrees_of_freedom():
    # Both now use the same return series (seeded with capital). Eclipse divides the
    # downside sum by (n-1), the reference by n, so ours = theirs * sqrt((n-1)/n).
    equity = [100.0, 98.0, 101.0, 97.0, 100.0]
    engine = _engine(equity)
    returns = [equity[0] / 100.0 - 1.0] + _returns_from(equity)
    ours = engine.sortino()
    theirs = reference_sortino_population(returns, engine.periods)
    assert ours != pytest.approx(theirs, rel=1e-6)
    assert ours == pytest.approx(
        theirs * math.sqrt((len(returns) - 1) / len(returns)), rel=1e-6
    )


def test_sortino_is_none_without_downside():
    assert _engine([100.0, 101.0, 102.0]).sortino() is None


# -- trade statistics -----------------------------------------------------


class _Trade:
    def __init__(self, pnl, holding_bars=1, direction="long"):
        self.net_pnl = pnl
        self.bars_held = holding_bars
        self.direction = direction


def test_profit_factor_is_none_not_zero_when_nothing_lost():
    """The reference returns None here; 0.0 would rank a flawless run last."""
    engine = _engine([100.0, 110.0], trades=[_Trade(5.0), _Trade(3.0)])
    values = engine.values()
    assert values["profit_factor"] is None
    assert values["loss_rate"] == 0.0


def test_profit_factor_and_loss_rate_match_the_reference_definition():
    trades = [_Trade(10.0), _Trade(-5.0), _Trade(20.0), _Trade(-5.0)]
    values = _engine([100.0, 120.0], trades=trades).values()
    # gross win 30 / gross loss 10
    assert values["profit_factor"] == pytest.approx(3.0)
    # Eclipse reports these as percentages, the reference as fractions
    assert values["win_rate"] == pytest.approx(50.0)
    assert values["loss_rate"] == pytest.approx(50.0)


def test_max_consecutive_losses_matches_the_reference_walk():
    trades = [_Trade(-1), _Trade(-1), _Trade(2), _Trade(-1), _Trade(-1), _Trade(-1)]
    values = _engine([100.0, 90.0], trades=trades).values()
    # the reference's longest run is three, and it resets on a non-loss
    assert values["max_losing_streak"] == 3


# -- bars per year --------------------------------------------------------


@pytest.mark.parametrize("interval,asset_class,expected", [
    ("1d", None, 252.0),          # US equity sessions (reference: yfinance 252)
    ("1w", None, 52.0),           # calendar weeks (reference: 52)
    ("1mo", None, 12.0),          # calendar months (reference: 12)
    ("1m", None, 252.0 * 390),    # 6.5h session -> 390 one-minute bars
    ("1h", None, 252.0 * 6.5),
])
def test_annualisation_matches_the_reference_table(interval, asset_class, expected):
    engine = MetricsEngine(
        MetricInputs(equity=[100.0, 101.0], timestamps=[None, None], trades=[],
                     exposure=[], interval=interval, initial_capital=100.0),
        asset_class=asset_class,
    )
    assert engine.periods == pytest.approx(expected)
