"""Stop management: break-even, ATR trail, giveback, time stop, partials.

Every assertion is arithmetic from a position's own entry/stop and the broker's
bars. The two invariants under test throughout: a stop can only tighten, and a
missing number is a refusal, never a guess.
"""

from __future__ import annotations

import pytest

from harness.trade.management import (
    ManagementPolicy,
    PartialTake,
    PositionManager,
    evaluate_management,
)


def _position(**overrides):
    base = {
        "ticket": 101,
        "symbol": "EURUSD",
        "side": "buy",
        "openPrice": 100.0,
        "stopLoss": 90.0,
        "currentPrice": 100.0,
        "volume": 1.0,
    }
    base.update(overrides)
    return base


def _bars(high=130.0, low=120.0, close=125.0, count=20):
    return [
        {"high": high, "low": low, "close": close, "open": close}
        for _ in range(count)
    ]


# -- break-even ---------------------------------------------------------------


def test_break_even_moves_the_stop_to_entry_plus_buffer_at_one_r():
    policy = ManagementPolicy(mode="break_even", break_even_at_r=1.0,
                              break_even_buffer_r=0.1)
    result = evaluate_management(_position(currentPrice=110.0), policy)
    assert result["ok"] is True
    stops = [a for a in result["actions"] if a["kind"] == "modify_stop"]
    assert len(stops) == 1
    # entry 100 + 0.1R (risk 10) = 101
    assert stops[0]["price"] == pytest.approx(101.0)
    assert stops[0]["code"] == "BREAK_EVEN_TRIGGERED"


def test_break_even_does_not_fire_before_the_trigger():
    policy = ManagementPolicy(mode="break_even", break_even_at_r=1.0)
    result = evaluate_management(_position(currentPrice=105.0), policy)
    assert result["actions"] == []


def test_break_even_never_widens_an_already_better_stop():
    policy = ManagementPolicy(mode="break_even", break_even_at_r=1.0)
    result = evaluate_management(
        _position(currentPrice=112.0, stopLoss=103.0), policy,
    )
    assert result["actions"] == []


def test_a_position_without_a_stop_is_refused_rather_than_managed():
    policy = ManagementPolicy(mode="break_even")
    result = evaluate_management(_position(stopLoss=None), policy)
    assert result["ok"] is False
    assert result["code"] == "NO_STOP"
    assert result["actions"] == []


# -- ATR trail ----------------------------------------------------------------


def test_atr_trail_tightens_the_stop_behind_the_highs():
    policy = ManagementPolicy(mode="atr_trail", atr_period=14, atr_multiple=2.5,
                              break_even_at_r=0)
    result = evaluate_management(
        _position(currentPrice=120.0), policy, bars=_bars(),
    )
    assert result["ok"] is True
    # ATR = 10; reference high 130 - 2.5*10 = 105
    stops = [a for a in result["actions"] if a["kind"] == "modify_stop"]
    assert stops and stops[0]["price"] == pytest.approx(105.0)
    assert stops[0]["code"] == "TRAIL_TIGHTENED"


def test_atr_trail_never_loosens_an_existing_stop():
    policy = ManagementPolicy(mode="atr_trail", atr_multiple=2.5, break_even_at_r=0)
    result = evaluate_management(
        _position(currentPrice=110.0, stopLoss=108.0), policy, bars=_bars(),
    )
    # candidate 105 is worse than the stored 108 -> no action
    assert result["actions"] == []


def test_atr_trail_without_bars_is_a_named_refusal():
    policy = ManagementPolicy(mode="atr_trail")
    result = evaluate_management(_position(currentPrice=120.0), policy)
    assert result["ok"] is False
    assert result["code"] == "ATR_UNAVAILABLE"


def test_a_stop_inside_the_broker_minimum_distance_is_not_applied():
    policy = ManagementPolicy(mode="atr_trail", atr_multiple=2.5,
                              break_even_at_r=0, min_stop_distance=50.0)
    result = evaluate_management(
        _position(currentPrice=120.0), policy, bars=_bars(),
    )
    stops = [a for a in result["actions"] if a["kind"] == "modify_stop"]
    assert stops and stops[0]["applied"] is False
    assert stops[0]["code"] == "STOP_TOO_CLOSE"


def test_short_side_mirrors_the_rules():
    short = _position(side="sell", openPrice=100.0, stopLoss=110.0,
                      currentPrice=80.0)
    policy = ManagementPolicy(mode="atr_trail", atr_multiple=2.5, break_even_at_r=0)
    bars = _bars(high=85.0, low=70.0, close=75.0)
    result = evaluate_management(short, policy, bars=bars)
    # ATR = 15; reference low 70 + 2.5*15 = 107.5, tighter than 110
    stops = [a for a in result["actions"] if a["kind"] == "modify_stop"]
    assert stops and stops[0]["price"] == pytest.approx(107.5)


# -- giveback / time stop / partials ------------------------------------------


def test_giveback_floor_locks_a_share_of_open_profit():
    policy = ManagementPolicy(mode="break_even", break_even_at_r=0,
                              giveback_cap_r=0.5)
    result = evaluate_management(_position(currentPrice=120.0), policy)
    stops = [a for a in result["actions"] if a["kind"] == "modify_stop"]
    # 2R favourable, keep 0.5R -> 1.5R = 115
    assert stops and stops[0]["price"] == pytest.approx(115.0)
    assert stops[0]["code"] == "GIVEBACK_FLOOR"


def test_time_stop_closes_a_trade_that_has_gone_nowhere():
    policy = ManagementPolicy(mode="break_even", time_stop_minutes=60)
    result = evaluate_management(
        _position(currentPrice=100.0), policy,
        now=10_000.0, opened_at=10_000.0 - 3_600.0,
    )
    closes = [a for a in result["actions"] if a["kind"] == "close"]
    assert len(closes) == 1
    assert closes[0]["code"] == "TIME_STOP"


def test_partial_take_fires_once():
    policy = ManagementPolicy(
        mode="break_even", break_even_at_r=0,
        partials=(PartialTake(at_r=2.0, fraction=0.5),),
    )
    first = evaluate_management(_position(currentPrice=121.0), policy)
    assert any(a["kind"] == "partial_close" for a in first["actions"])

    second = evaluate_management(
        _position(currentPrice=121.0), policy, partials_fired={"2"},
    )
    assert not any(a["kind"] == "partial_close" for a in second["actions"])


# -- configuration ------------------------------------------------------------


def test_policy_from_config_clamps_bad_values_and_parses_partials():
    policy = ManagementPolicy.from_config({
        "mode": "atr_trail",
        "breakEvenAtR": "bad",
        "atrMultiple": 0,
        "givebackCapR": -1,
        "timeStopMinutes": "60",
        "partials": [{"atR": 2, "fraction": 0.5}, {"atR": -1, "fraction": 2}],
        "unknown": 9,
    })
    assert policy.mode == "atr_trail"
    assert policy.break_even_at_r == 1.0
    assert policy.atr_multiple == 2.5
    assert policy.giveback_cap_r is None
    assert policy.time_stop_minutes == 60.0
    assert policy.partials == (PartialTake(at_r=2.0, fraction=0.5),)


# -- PositionManager ----------------------------------------------------------


class _FakeService:
    def __init__(self, positions):
        self._positions = positions
        self.modified = []
        self.closed = []

    def positions(self, connection_id):
        return {"available": True, "data": self._positions}

    def modify_position(self, connection_id, ticket, stop_loss=None, take_profit=None):
        self.modified.append((ticket, stop_loss))
        return {"modified": True, "result": {"verified": True}}

    def close_position(self, connection_id, ticket, volume=None, deviation=None):
        self.closed.append((ticket, volume))
        return {"closed": True, "result": {"verified": True}}


def test_manager_applies_a_tightening_stop_and_keeps_off_respected():
    service = _FakeService([_position(currentPrice=110.0)])
    manager = PositionManager(
        service, ManagementPolicy(mode="break_even", break_even_at_r=1.0),
    )
    report = manager.manage("brk_1")
    assert report["managed"] == 1
    assert service.modified == [(101, pytest.approx(100.0))]
    assert report["reports"][0]["actions"][0]["verified"] is True


def test_manager_leaves_positions_whose_policy_is_off_alone():
    service = _FakeService([_position(currentPrice=130.0)])
    manager = PositionManager(service, ManagementPolicy(mode="off"))
    report = manager.manage("brk_1")
    assert report["managed"] == 0
    assert service.modified == []


# -- config persistence + background sweep ------------------------------------


def test_connection_config_persists_and_sanitises_risk_and_management():
    from harness.trade.model import validate_config

    cleaned = validate_config({
        "risk": {"maxRiskPct": 1.5, "maxOpenPositions": "10", "bogus": 1,
                 "maxMarginPct": -5},
        "management": {"mode": "atr_trail", "atrMultiple": 3,
                       "partials": [{"atR": 2, "fraction": 0.5}], "bogus": 1},
        "supervisor": {"dailyLossPct": 1},
    })
    assert cleaned["risk"] == {"maxRiskPct": 1.5, "maxOpenPositions": 10}
    assert cleaned["management"]["mode"] == "atr_trail"
    assert cleaned["management"]["atrMultiple"] == 3.0
    assert cleaned["management"]["partials"] == [{"atR": 2.0, "fraction": 0.5}]
    assert "bogus" not in cleaned["management"]
    assert cleaned["supervisor"] == {"dailyLossPct": 1.0}

    from harness.trade.risk import limits_from_connection
    assert limits_from_connection(cleaned).max_risk_pct == 1.5


class _Connection:
    def __init__(self, connection_id, config):
        self.id = connection_id
        self.config = config


class _Connections:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return list(self._rows)


class _SweepService:
    def __init__(self, connections, positions):
        self.connections = _Connections(connections)
        self._positions = positions
        self.modified = []

    def positions(self, connection_id):
        return {"available": True, "data": [
            row for row in self._positions if row.get("connectionId") == connection_id
        ]}

    def quote(self, connection_id, symbol):
        return {"available": True, "data": {"bid": 119.9, "ask": 120.0}}

    def bars(self, connection_id, symbol, timeframe="H1", count=120):
        return {"available": True, "data": {"bars": _bars()}}

    def modify_position(self, connection_id, ticket, stop_loss=None, take_profit=None):
        self.modified.append((connection_id, ticket, stop_loss))
        return {"modified": True, "result": {"verified": True}}

    def close_position(self, connection_id, ticket, volume=None, deviation=None):
        return {"closed": True, "result": {"verified": True}}


def test_sweeper_only_manages_connections_with_a_policy():
    service = _SweepService(
        connections=[
            _Connection("brk_on", {"management": {"mode": "atr_trail"}}),
            _Connection("brk_off", {}),
        ],
        positions=[
            {**_position(currentPrice=120.0), "connectionId": "brk_on"},
            {**_position(ticket=202, currentPrice=120.0), "connectionId": "brk_off"},
        ],
    )
    from harness.trade.management_api import ManagementSweeper

    report = ManagementSweeper(service).sweep()
    assert report["connections"] == 1
    assert report["managed"] == 1
    assert service.modified == [("brk_on", 101, pytest.approx(105.0))]
