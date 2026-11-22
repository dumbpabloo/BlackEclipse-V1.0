"""Trade-plan Phase 1: sizing, account risk, and the plan's refusal modes.

The maths is asserted as arithmetic, not as prose: a 1% budget over a 1000-per-lot
risk MUST produce 0.1, and a budget smaller than the broker minimum MUST refuse
rather than round up. The plan service test uses a fake service so no broker is
needed and no number is invented.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.trade.plan import build_trade_plan
from harness.trade.risk import RiskLimits, assess_trade_risk
from harness.trade.sizing import size_position


# -- sizing -------------------------------------------------------------------


def test_tick_value_basis_is_exact_in_account_currency():
    sized = size_position(
        equity=10_000, risk_pct=1, entry=1.10, stop=1.09, side="buy",
        tick_size=0.00001, tick_value=1.0,
        volume_min=0.01, volume_max=100, volume_step=0.01,
    )
    assert sized["allowed"] is True
    assert sized["volume"] == pytest.approx(0.1)
    assert sized["riskAmount"] == pytest.approx(100.0)
    assert sized["riskPctActual"] == pytest.approx(1.0)
    assert sized["basis"] == "tick-value"


def test_contract_size_basis_sizes_shares():
    sized = size_position(
        equity=100_000, risk_pct=1, entry=50, stop=48, side="buy",
        contract_size=1, volume_min=1, volume_max=10_000, volume_step=1,
    )
    assert sized["allowed"] is True
    assert sized["volume"] == 500
    assert sized["riskAmount"] == pytest.approx(1_000)


def test_volume_is_rounded_down_never_up():
    sized = size_position(
        equity=1_000, risk_pct=1, entry=100, stop=90, side="buy",
        contract_size=1, volume_min=0.01, volume_max=100, volume_step=0.01,
    )
    # budget 10, risk per unit 10 -> raw 1.0; an awkward budget must not round up.
    assert sized["volume"] == pytest.approx(1.0)
    exact = size_position(
        equity=1_010, risk_pct=1, entry=100, stop=90, side="buy",
        contract_size=1, volume_min=0.01, volume_max=100, volume_step=0.03,
    )
    # budget 10.1, risk/unit 10 -> raw 1.01; floor to the 0.03 step -> 0.99
    assert exact["volume"] == pytest.approx(0.99)
    assert exact["riskAmount"] == pytest.approx(9.9)


def test_minimum_lot_above_budget_is_a_named_refusal():
    sized = size_position(
        equity=100, risk_pct=1, entry=1.10, stop=1.09, side="buy",
        tick_size=0.00001, tick_value=1.0,
        volume_min=0.01, volume_max=100, volume_step=0.01,
    )
    assert sized["allowed"] is False
    assert sized["error"]["code"] == "RISK_TOO_SMALL"
    assert sized["minVolume"] == pytest.approx(0.01)
    assert sized["minRiskAmount"] == pytest.approx(10.0)
    assert "budget" in sized["error"]["message"]


def test_stop_inside_the_broker_stops_level_is_refused():
    sized = size_position(
        equity=10_000, risk_pct=1, entry=1.10, stop=1.0999, side="buy",
        tick_size=0.00001, tick_value=1.0, point=0.00001,
        stops_level_points=30, volume_min=0.01, volume_max=100, volume_step=0.01,
    )
    assert sized["allowed"] is False
    assert sized["error"]["code"] == "STOP_TOO_TIGHT"


def test_a_missing_stop_and_a_bad_side_are_refused():
    assert size_position(
        equity=10_000, risk_pct=1, entry=1.10, stop=0, side="buy",
        tick_size=0.00001, tick_value=1.0,
    )["error"]["code"] == "PRICE_INVALID"
    assert size_position(
        equity=10_000, risk_pct=1, entry=1.10, stop=1.09, side="sideways",
        tick_size=0.00001, tick_value=1.0,
    )["error"]["code"] == "SIDE_INVALID"


def test_no_equity_is_refused_not_assumed():
    sized = size_position(
        equity=0, risk_pct=1, entry=1.10, stop=1.09, side="buy",
        tick_size=0.00001, tick_value=1.0,
    )
    assert sized["allowed"] is False
    assert sized["error"]["code"] == "EQUITY_UNAVAILABLE"


# -- account risk -------------------------------------------------------------


def _account(**overrides):
    base = {"equity": 10_000, "balance": 10_000, "margin": 0, "marginFree": 10_000}
    base.update(overrides)
    return base


def _pos(symbol="EURUSD", side="buy"):
    return {"symbol": symbol, "side": side, "volume": 0.1, "stopLoss": 1.09}


def test_concentration_at_the_symbol_limit_blocks():
    risk = assess_trade_risk(
        account=_account(), positions=[_pos()] * 3, symbol="EURUSD",
        planned_risk=100, risk_pct=1.0,
    )
    assert risk["verdict"] == "block"
    assert any("EURUSD" in reason for reason in risk["reasons"])


def test_open_position_count_limit_blocks():
    positions = [_pos("EURUSD"), _pos("GBPUSD"), _pos("USDJPY")]
    risk = assess_trade_risk(
        account=_account(), positions=positions, symbol="AUDUSD",
        planned_risk=100, risk_pct=1.0,
        limits=RiskLimits(max_open_positions=3),
    )
    assert risk["blocking"] is True


def test_margin_headroom_and_level_block():
    risk = assess_trade_risk(
        account=_account(margin=4_000, marginLevel=120),
        positions=[], symbol="EURUSD", planned_risk=100, risk_pct=1.0,
        estimated_margin=2_000,
    )
    assert risk["verdict"] == "block"
    assert any("Margin after" in reason for reason in risk["reasons"])
    assert any("Margin level" in reason for reason in risk["reasons"])


def test_spread_that_eats_the_risk_blocks():
    # 30 points of spread at 1.0 per 0.1-point tick -> 30 currency of a 100 risk.
    risk = assess_trade_risk(
        account=_account(), positions=[], symbol="EURUSD",
        planned_risk=100, risk_pct=1.0,
        spread_points=30, point=0.1, tick_size=0.1, tick_value=1.0,
    )
    assert risk["spreadCost"] == pytest.approx(30)
    assert risk["spreadPctOfRisk"] == pytest.approx(30)
    assert risk["blocking"] is True


def test_same_side_exposure_is_a_warning_not_a_block():
    risk = assess_trade_risk(
        account=_account(), positions=[_pos(), _pos()], symbol="EURUSD",
        planned_risk=100, risk_pct=1.0,
    )
    assert risk["verdict"] == "warn"
    assert not risk["blocking"]
    assert any("same direction" in warning for warning in risk["warnings"])


def test_opposite_side_is_called_out_as_a_hedge_or_reversal():
    risk = assess_trade_risk(
        account=_account(), positions=[_pos(side="sell")], symbol="EURUSD",
        planned_risk=100, risk_pct=1.0,
    )
    assert any("opposite" in warning for warning in risk["warnings"])


def test_missing_margin_data_is_unmeasured_not_passed():
    risk = assess_trade_risk(
        account={"equity": 10_000}, positions=[], symbol="EURUSD",
        planned_risk=100, risk_pct=1.0,
    )
    assert "margin cost" not in " ".join(risk["unmeasured"])
    assert risk["verdict"] in {"ok", "warn"}


# -- the plan service ---------------------------------------------------------


class _FakeService:
    """A service-shaped object: only the reads the plan uses."""

    def __init__(self, *, equity=10_000, positions=None, spread=2):
        self._equity = equity
        self._positions = positions or []
        self._spread = spread
        self.connection = SimpleNamespace(
            id="c1", mode=SimpleNamespace(value="demo"), config={},
        )

    def _connection(self, connection_id):
        return self.connection

    def symbol_info(self, connection_id, symbol):
        return {
            "available": True,
            "data": {
                "symbol": "EURUSD", "digits": 5, "point": 0.00001,
                "tickSize": 0.00001, "tickValue": 1.0, "contractSize": 100_000,
                "volumeMin": 0.01, "volumeMax": 100, "volumeStep": 0.01,
                "stopsLevelPoints": 0, "spreadPoints": self._spread,
                "tradeMode": "full", "sessionsAvailable": False,
                "description": "Euro vs US Dollar",
            },
        }

    def account(self, connection_id):
        return {
            "available": True,
            "data": {
                "equity": self._equity, "balance": self._equity,
                "margin": 0, "marginFree": self._equity,
                "marginLevel": 0, "currency": "USD",
            },
        }

    def quote(self, connection_id, symbol):
        return {
            "available": True,
            "data": {"symbol": "EURUSD", "bid": 1.09990, "ask": 1.10000,
                     "spreadPoints": self._spread, "retrievedAt": 1},
        }

    def positions(self, connection_id):
        return {"available": True, "data": self._positions}


def test_the_plan_sizes_from_equity_and_the_stop():
    plan = build_trade_plan(
        _FakeService(), "c1",
        {"symbol": "EURUSD", "side": "buy", "stop": 1.0950, "riskPct": 1},
    )
    assert plan["allowed"] is True
    assert plan["entry"] == pytest.approx(1.1)
    assert plan["entrySource"] == "broker quote"
    # distance 0.005 / 0.00001 * 1 = 500 per lot; budget 100 -> 0.2
    assert plan["volume"] == pytest.approx(0.2)
    assert plan["riskAmount"] == pytest.approx(100)
    assert plan["risk"]["verdict"] in {"ok", "warn"}
    assert plan["planId"].startswith("plan_")
    assert "max loss" in plan["summary"]


def test_the_plan_names_the_target_reward_ratio():
    plan = build_trade_plan(
        _FakeService(), "c1",
        {"symbol": "EURUSD", "side": "buy", "stop": 1.0950,
         "target": 1.1100, "riskPct": 1},
    )
    # reward 0.01 / risk 0.005 = 2R
    assert plan["riskReward"] == pytest.approx(2.0)


def test_the_plan_refuses_when_the_minimum_lot_costs_too_much():
    plan = build_trade_plan(
        _FakeService(equity=50), "c1",
        {"symbol": "EURUSD", "side": "buy", "stop": 1.0950, "riskPct": 1},
    )
    assert plan["allowed"] is False
    assert plan["status"] == "REFUSED"
    assert plan["error"]["code"] == "RISK_TOO_SMALL"
    assert plan["summary"].startswith("Not placed:")


def test_the_plan_blocks_on_concentration_and_says_why():
    plan = build_trade_plan(
        _FakeService(positions=[_pos()] * 3), "c1",
        {"symbol": "EURUSD", "side": "buy", "stop": 1.0950, "riskPct": 1},
    )
    assert plan["allowed"] is False
    assert plan["status"] == "REFUSED"
    assert any("EURUSD" in reason for reason in plan["reasons"])


def test_the_plan_is_unavailable_when_the_broker_cannot_report_equity():
    class Broken(_FakeService):
        def account(self, connection_id):
            return {"available": False, "error": {"code": "ADAPTER_ERROR"}}

    plan = build_trade_plan(
        Broken(), "c1", {"symbol": "EURUSD", "side": "buy", "stop": 1.0950},
    )
    assert plan["available"] is False
    assert plan["status"] == "UNAVAILABLE"


def test_the_tools_are_registered_as_read_only():
    from harness.trade.tools import BROKER_TOOLS, register_broker_tools
    from harness.tools import ToolRegistry

    registry = ToolRegistry()
    register_broker_tools(registry)
    assert "trade_plan" in BROKER_TOOLS and "broker_symbol_info" in BROKER_TOOLS
    assert registry.get("trade_plan").permission == "read"
    assert registry.get("broker_symbol_info").permission == "read"
