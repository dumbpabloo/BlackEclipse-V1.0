"""Behavioural parity with Vibe-Trading's walk-forward fold arithmetic.

Reference: `agent/src/research_lab/studies.py::walk_forward` (MIT, fetched
2026-11-21). Its window selection is four lines and they are not obvious, so they are
restated here as the thing under test:

    limit = len(calendar) - holdout_bars
    boundaries = range(train_bars, limit - test_bars + 1, test_bars)[:max_folds]

Each boundary is a fold's *test start*; the test window is ``[boundary, boundary +
test_bars)`` and the training window ends where the test begins. The reserved
holdout is the tail and no fold may touch it.

Eclipse's `windows()` looped to the end of the timeline and had no holdout at all,
which meant a "walk-forward" verdict could be read from bars that parameter
selection had already seen. The comparison that mattered was numeric, not structural
— that is why this file exists.
"""

from __future__ import annotations

import pytest

from harness.quant import data as D
from harness.quant import fixtures as F
from harness.quant import strategy as S
from harness.quant.walkforward import WalkForwardConfig, WalkForwardEngine


def _dataset(n):
    rows = F.bull_market("TESTUSDT", n=n, seed=7)
    return D.build_dataset(
        {"TESTUSDT": D.series_from_rows("TESTUSDT", "1d", rows, source="synthetic")}
    )


def _spec():
    return S.parse_strategy({
        "strategy": {"id": "ema_cross", "name": "EMA", "version": "1.0.0"},
        "universe": {"symbols": ["TESTUSDT"]},
        "indicators": [
            {"id": "ema_fast", "type": "ema", "params": {"period": 5}},
            {"id": "ema_slow", "type": "ema", "params": {"period": 20}},
            {"id": "atr", "type": "atr", "params": {"period": 14}},
        ],
        "rules": {
            "entry": {"long": {"conditions": [
                {"left": {"ref": "ema_fast"}, "op": "cross_above",
                 "right": {"ref": "ema_slow"}}]}},
            "exit": {"stop_loss": {"type": "atr", "value": 2},
                     "take_profit": {"type": "risk_reward", "value": 2}},
        },
        "position_sizing": {"type": "percent_equity", "value": 90},
        "execution": {"commission": {"type": "percentage", "value": 0.05},
                      "slippage": {"type": "percentage", "value": 0.02}},
    })


def _windows(n, train, test, *, holdout=0, max_windows=None, anchored=False,
             step=None):
    """The reference's boundary formula, computed independently here."""
    limit = n - holdout
    starts = list(range(train, limit - test + 1, step or test))
    if max_windows is not None:
        starts = starts[:max_windows]
    engine = WalkForwardEngine(
        _spec(), _dataset(n),
        WalkForwardConfig(train_bars=train, test_bars=test, holdout_bars=holdout,
                          max_windows=max_windows, anchored=anchored,
                          param_space={"indicators.0.params.period": [5, 8]}),
    )
    return starts, engine.windows()


# -- the shared fixture ----------------------------------------------------


@pytest.mark.parametrize("n,train,test,holdout", [
    (100, 40, 10, 0),
    (100, 40, 10, 10),
    (100, 40, 10, 25),
    (120, 50, 20, 20),
    (90, 30, 15, 0),
])
def test_the_test_starts_match_the_reference_boundaries(n, train, test, holdout):
    starts, windows = _windows(n, train, test, holdout=holdout)
    assert [w[2] for w in windows] == starts


def test_a_reserved_holdout_is_never_inside_a_test_window():
    n, train, test, holdout = 100, 40, 10, 10
    _, windows = _windows(n, train, test, holdout=holdout)
    assert windows, "the fixture must produce at least one fold"
    assert max(w[3] for w in windows) <= n - holdout
    # and the last fold ends exactly at the holdout boundary here
    assert windows[-1][3] == n - holdout


def test_the_holdout_changes_the_fold_count_the_reference_way():
    without, windows_without = _windows(100, 40, 10, holdout=0)
    with_holdout, windows_held = _windows(100, 40, 10, holdout=10)
    assert len(windows_without) == len(without) == 6
    assert len(windows_held) == len(with_holdout) == 5


def test_max_windows_caps_the_folds_from_the_oldest_end():
    starts, windows = _windows(100, 40, 10, max_windows=3)
    assert len(windows) == 3
    assert [w[2] for w in windows] == starts[:3] == [40, 50, 60]


def test_each_training_window_ends_where_its_test_begins():
    _, windows = _windows(100, 40, 10, holdout=10)
    for train_start, train_end, test_start, test_end in windows:
        assert train_end == test_start
        assert test_end - test_start == 10
        assert train_end - train_start == 40


def test_an_anchored_run_keeps_the_training_start_at_zero():
    _, windows = _windows(100, 40, 10, anchored=True)
    assert {w[0] for w in windows} == {0}
    assert [w[2] for w in windows] == [40, 50, 60, 70, 80, 90]


def test_a_negative_holdout_is_refused_rather_than_widening_the_window():
    engine = WalkForwardEngine(
        _spec(), _dataset(100),
        WalkForwardConfig(train_bars=40, test_bars=10, holdout_bars=-1,
                          param_space={"indicators.0.params.period": [5, 8]}),
    )
    with pytest.raises(Exception) as exc:
        engine.windows()
    assert "holdout_bars" in str(exc.value)


def test_a_holdout_that_swallows_the_timeline_yields_no_folds():
    _, windows = _windows(100, 40, 10, holdout=95)
    assert windows == []


def test_the_defaults_keep_the_historical_behaviour():
    """holdout_bars=0 must reproduce the pre-parity fold set exactly."""
    cfg = WalkForwardConfig()
    assert cfg.holdout_bars == 0 and cfg.max_windows is None
    starts, windows = _windows(100, 40, 10)
    # the historical loop: start at 0, step = test_bars while test_end <= n
    historical = [40, 50, 60, 70, 80, 90]
    assert [w[2] for w in windows] == historical == starts
