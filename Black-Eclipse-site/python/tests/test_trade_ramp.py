"""The gated live ramp: small money first, more only after live results.

The stage arithmetic is asserted: 19 live fills is still `probe`, 20 profitable
ones is `build`, and a losing expectancy steps DOWN rather than staying up.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.trade.ramp import (
    STAGES,
    caps_from,
    live_evidence,
    ramp_gap,
    ramp_limits,
    stage_for,
)


def _definition(cap=500, orders=20, *, config=None):
    return SimpleNamespace(
        id="bot_a", version=3,
        config=config or {},
        execution_authority=SimpleNamespace(
            tier="l2", max_margin_per_order=cap, max_orders=orders, require_stop=True,
        ),
    )


def _evidence(trades=0, expectancy=0.0, drawdown=0.0):
    return {
        "closedTrades": trades, "expectancy": expectancy, "maxDrawdownPct": drawdown,
    }


def test_no_live_history_is_probe():
    assert stage_for(_evidence()) is STAGES[0]
    assert stage_for(_evidence(trades=19, expectancy=1.0)).label == "probe"


def test_twenty_profitable_fills_earn_build():
    assert stage_for(_evidence(trades=20, expectancy=1.0)).label == "build"


def test_fifty_profitable_fills_earn_scale():
    assert stage_for(_evidence(trades=50, expectancy=1.0)).label == "scale"


def test_a_negative_expectancy_steps_down_one_stage():
    assert stage_for(_evidence(trades=50, expectancy=-0.01)).label == "build"
    assert stage_for(_evidence(trades=20, expectancy=-0.01)).label == "probe"


def test_a_drawdown_above_the_stage_cap_steps_down():
    # scale caps drawdown at 20%; 25% steps it back to build.
    assert stage_for(_evidence(trades=50, expectancy=1.0, drawdown=25)).label == "build"


def test_the_ramp_never_exceeds_the_bots_own_ceiling():
    definition = _definition(cap=500, orders=20)
    assert caps_from(definition) == {"maxMargin": 500.0, "maxOrders": 20.0}
    probe = ramp_limits(definition, _evidence())
    scale = ramp_limits(definition, _evidence(trades=50, expectancy=1.0))
    assert probe["maxMargin"] == pytest.approx(125.0)
    assert probe["maxOrders"] == 5
    assert scale["maxMargin"] == pytest.approx(500.0)
    assert scale["maxOrders"] == 20


def test_the_next_stage_names_what_it_needs():
    probe = ramp_limits(_definition(), _evidence())
    assert probe["nextStage"] == "build"
    assert probe["nextStageNeeds"]["minLiveTrades"] == 20
    scale = ramp_limits(_definition(), _evidence(trades=50, expectancy=1.0))
    assert scale["nextStage"] is None


# -- the refusal --------------------------------------------------------------


class _Connections:
    def __init__(self, fills):
        self._fills = fills

    def list_fills(self, connection_id, limit=1000):
        return list(self._fills.get(connection_id, []))


class _Service:
    def __init__(self, orders, fills, modes=None):
        self.orders = orders
        self.connections = _Connections(fills)
        self.modes = modes or {"c1": "demo", "c2": "live"}

    def ledger_orders(self, connection_id=None, limit=1000, automation_id=None):
        return [o for o in self.orders if o.get("automationId") == automation_id]

    def _connection(self, connection_id):
        return SimpleNamespace(
            id=connection_id, mode=SimpleNamespace(value=self.modes[connection_id]),
        )


def _fills(n, profit, *, ticket=222, start=1_700_000_000):
    return [
        {"profit": profit, "commission": 0, "swap": 0, "orderTicket": ticket,
         "dealtAt": start + i * 3600}
        for i in range(n)
    ]


def test_live_evidence_ignores_demo_connections():
    orders = [
        {"automationId": "bot_a", "connectionId": "c1", "ticket": 111},
        {"automationId": "bot_a", "connectionId": "c2", "ticket": 222},
    ]
    service = _Service(orders, {"c1": _fills(5, 2.0, ticket=111),
                                "c2": _fills(20, 1.0, ticket=222)})
    evidence = live_evidence(service, "bot_a")
    assert evidence["closedTrades"] == 20
    assert evidence["connections"] == ["c2"]


def test_a_mandate_above_the_probe_cap_is_refused():
    gap = ramp_gap(
        _Service([], {}), _definition(cap=500, orders=20),
        requested_margin=300, requested_orders=10,
    )
    assert gap is not None
    assert gap["reason"] == "RAMP_CAP_EXCEEDED"
    assert "probe stage cap 125" in gap["message"]
    assert gap["ramp"]["nextStage"] == "build"


def test_a_mandate_inside_the_probe_cap_is_allowed():
    assert ramp_gap(
        _Service([], {}), _definition(cap=500, orders=20),
        requested_margin=100, requested_orders=4,
    ) is None


def test_live_results_lift_the_cap():
    orders = [{"automationId": "bot_a", "connectionId": "c2", "ticket": 222}]
    service = _Service(orders, {"c2": _fills(20, 1.0)})
    assert ramp_gap(
        service, _definition(cap=500, orders=20),
        requested_margin=250, requested_orders=10,
    ) is None


def test_a_bot_with_no_money_ceiling_is_not_given_a_ramp_pass():
    # cap 0 means "no ceiling configured": the ramp cannot invent one, and the
    # mandate's own margin rule still applies. Requesting 0 is allowed.
    assert ramp_gap(
        _Service([], {}), _definition(cap=0, orders=0),
        requested_margin=0, requested_orders=0,
    ) is None


# -- the gate ----------------------------------------------------------------


def test_the_live_mandate_gate_refuses_above_the_ramp(monkeypatch):
    from harness.trade.authority import check_mandate_creation
    from harness.trade.model import TradeRefused

    monkeypatch.setattr("harness.trade.proof.live_proof_gap", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(cap=500),
    )
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(
            _Service([], {}), bot_id="bot_a", bot_version=3,
            max_margin=300, max_orders=10, mode="live",
        )
    assert exc.value.code == "RAMP_CAP_EXCEEDED"
    assert exc.value.detail["ramp"]["stage"] == "probe"
