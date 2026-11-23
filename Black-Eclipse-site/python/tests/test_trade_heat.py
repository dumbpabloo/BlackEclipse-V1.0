"""Portfolio heat, clusters, drawdown governor, volatility and event gates.

The arithmetic is asserted directly: 3 x 2% open risk is 6% heat, a 5% drawdown
at a 10% governor halves new size, and a stable-quoted crypto pair belongs to the
same cluster as another one. Unmeasurable positions are reported, never zeroed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from harness.trade.heat import (
    HeatLimits,
    assess_heat,
    cluster_for,
    drawdown_scale,
    event_blackout,
    position_risk,
    volatility_scale,
)


# -- clusters -----------------------------------------------------------------


def test_fx_pairs_sharing_a_quote_are_one_cluster():
    assert cluster_for("EURUSD") == "FX:USD"
    assert cluster_for("GBPUSD.a") == "FX:USD"
    assert cluster_for("USDJPY") == "FX:JPY"


def test_crypto_stable_pairs_group_and_metals_are_their_own():
    assert cluster_for("BTCUSDT") == "CRYPTO:USDT"
    assert cluster_for("ETHUSDT") == "CRYPTO:USDT"
    assert cluster_for("XAUUSD") == "METALS:USD"
    assert cluster_for("AAPL") == "AAPL"


# -- position risk ------------------------------------------------------------


def test_position_risk_uses_an_explicit_amount_first():
    assert position_risk({"riskAmount": 125.5}) == pytest.approx(125.5)


def test_position_risk_from_tick_value_and_contract_size():
    tick = {"openPrice": 1.10, "stopLoss": 1.09, "volume": 0.2,
            "tickSize": 0.00001, "tickValue": 1.0}
    # 0.01 / 0.00001 * 1 = 1000 per lot; 0.2 lots -> 200
    assert position_risk(tick) == pytest.approx(200.0)
    shares = {"openPrice": 50, "stopLoss": 48, "volume": 500,
              "contractSize": 1}
    assert position_risk(shares) == pytest.approx(1000.0)


def test_position_risk_without_a_stop_is_unknown():
    assert position_risk({"openPrice": 1.1, "volume": 1, "contractSize": 1}) is None


# -- heat ---------------------------------------------------------------------


def _pos(symbol, risk_amount, ticket=1):
    return {"ticket": ticket, "symbol": symbol, "riskAmount": risk_amount}


def test_heat_blocks_when_the_book_plus_this_order_exceeds_the_cap():
    report = assess_heat(
        positions=[_pos("AAPL", 200, 1), _pos("MSFT", 200, 2), _pos("NVDA", 200, 3)],
        equity=10_000, planned_risk=100, planned_symbol="TSLA",
        limits=HeatLimits(max_heat_pct=6.0),
    )
    assert report["blocking"] is True
    assert report["heatPct"] == pytest.approx(6.0)
    assert report["afterHeatPct"] == pytest.approx(7.0)
    assert any("heat limit" in reason for reason in report["reasons"])


def test_cluster_cap_groups_eur_and_gbp_usd_positions():
    report = assess_heat(
        positions=[_pos("EURUSD", 200, 1), _pos("GBPUSD", 200, 2)],
        equity=10_000, planned_risk=100, planned_symbol="AUDUSD",
        limits=HeatLimits(max_heat_pct=20.0, max_cluster_risk_pct=3.0),
    )
    assert report["plannedCluster"] == "FX:USD"
    assert report["clusterAfterPct"] == pytest.approx(5.0)
    assert report["blocking"] is True
    assert any("FX:USD" in reason for reason in report["reasons"])


def test_unmeasured_positions_make_the_heat_a_lower_bound_not_zero():
    report = assess_heat(
        positions=[{"ticket": 9, "symbol": "EURUSD", "volume": 1}],
        equity=10_000, planned_risk=100, planned_symbol="EURUSD",
    )
    assert report["measuredRisk"] == 0.0
    assert report["unmeasured"]
    assert any("lower bound" in warning for warning in report["warnings"])


# -- governor / volatility ----------------------------------------------------


def test_drawdown_scale_is_one_at_zero_and_the_floor_at_the_limit():
    limits = HeatLimits(max_drawdown_pct=10.0, min_risk_scale=0.25)
    assert drawdown_scale(0, limits) == pytest.approx(1.0)
    assert drawdown_scale(5, limits) == pytest.approx(0.5)
    assert drawdown_scale(10, limits) == pytest.approx(0.25)
    assert drawdown_scale(50, limits) == pytest.approx(0.25)
    assert drawdown_scale(-3, limits) == pytest.approx(1.0)


def test_volatility_scale_brings_a_wide_stop_back_to_the_target():
    limits = HeatLimits(target_stop_vol_pct=1.0)
    assert volatility_scale(2.0, limits) == pytest.approx(0.5)
    assert volatility_scale(0.5, limits) == pytest.approx(2.0)
    assert volatility_scale(1.0, limits) == pytest.approx(1.0)


# -- events -------------------------------------------------------------------


def test_high_impact_event_inside_the_window_blocks_entries():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.timezone.utc)
    events = [{"time": "2026-11-23T12:10:00Z", "impact": "high"}]
    result = event_blackout(events, now, window_minutes=30)
    assert result["blocking"] is True
    assert result["minutesToEvent"] == pytest.approx(10.0)


def test_event_outside_the_window_or_low_impact_does_not_block():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.timezone.utc)
    assert event_blackout(
        [{"time": "2026-11-23T14:00:00Z", "impact": "high"}], now,
    )["blocking"] is False
    assert event_blackout(
        [{"time": "2026-11-23T12:05:00Z", "impact": "low"}], now,
    )["blocking"] is False


# -- plan integration ---------------------------------------------------------


class _Connection:
    id = "c1"
    mode = type("M", (), {"value": "demo"})()
    config = {"risk": {"maxHeatPct": 4.0, "maxClusterRiskPct": 4.0,
                       "maxClusterPositions": 0, "maxDrawdownPct": 10.0}}


class _Connections:
    def __init__(self, day_open):
        self.day_open = day_open

    def get_risk_snapshot(self, scope, period):
        return {"equity": self.day_open} if self.day_open else None


class _PlanService:
    def __init__(self, equity=10_000, day_open=None, positions=None):
        self.connection = _Connection()
        self.connections = _Connections(day_open)
        self._equity = equity
        self._positions = positions or []

    def _connection(self, connection_id):
        return self.connection

    def symbol_info(self, connection_id, symbol):
        return {"available": True, "data": {
            "symbol": symbol, "tickSize": 0.00001, "tickValue": 1.0,
            "contractSize": 100_000, "volumeMin": 0.01, "volumeMax": 100,
            "volumeStep": 0.01, "stopsLevelPoints": 0, "point": 0.00001,
            "spreadPoints": 1,
        }}

    def account(self, connection_id):
        return {"available": True, "data": {
            "equity": self._equity, "balance": self._equity,
            "margin": 0, "marginFree": self._equity, "currency": "USD",
        }}

    def quote(self, connection_id, symbol):
        return {"available": True, "data": {
            "bid": 1.0999, "ask": 1.1, "spreadPoints": 1,
        }}

    def positions(self, connection_id):
        return {"available": True, "data": self._positions}


def test_plan_applies_the_drawdown_governor_before_sizing():
    from harness.trade.plan import build_trade_plan

    service = _PlanService(equity=9_500, day_open=10_000)
    plan = build_trade_plan(service, "c1", {
        "symbol": "EURUSD", "side": "buy", "stop": 1.095, "riskPct": 1,
    })
    assert plan["drawdownPct"] == pytest.approx(5.0)
    assert plan["riskScale"] == pytest.approx(0.5)
    assert plan["riskPctEffective"] == pytest.approx(0.5)
    # budget 47.5, risk/lot 500 -> 0.095 floored to the 0.01 step -> 0.09 * 500
    assert plan["riskAmount"] == pytest.approx(45.0)


def test_plan_blocks_on_heat_and_names_it():
    from harness.trade.plan import build_trade_plan

    positions = [
        {"ticket": 1, "symbol": "AAPL", "riskAmount": 200},
        {"ticket": 2, "symbol": "MSFT", "riskAmount": 200},
    ]
    service = _PlanService(positions=positions)
    plan = build_trade_plan(service, "c1", {
        "symbol": "EURUSD", "side": "buy", "stop": 1.095, "riskPct": 1,
    })
    assert plan["allowed"] is False
    assert plan["heat"]["blocking"] is True
    assert any("heat limit" in reason for reason in plan["reasons"])
