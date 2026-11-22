"""The strategy proof gate: no live mandate without OOS + demo evidence.

The arithmetic is asserted, not described: 29 demo trades is a gap, 30 with a
positive expectancy clears it; an absent out-of-sample record is always a gap.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.trade.model import TradeRefused
from harness.trade.proof import (
    ProofRequirements,
    demo_evidence,
    evaluate_proof,
    live_proof_gap,
    requirements_from,
)


OOS = {
    "trades": 120, "expectancy": 0.18, "costsIncluded": True,
    "maxDrawdownPct": 6.0,
}
DEMO = {
    "closedTrades": 35, "days": 21, "expectancy": 0.05,
    "maxDrawdownPct": 4.0,
}


def test_both_halves_clear_the_gate():
    report = evaluate_proof({"outOfSample": OOS, "demo": DEMO})
    assert report["passed"] is True
    assert report["verdict"] == "pass"
    assert report["gaps"] == []


def test_absent_out_of_sample_is_never_a_pass():
    report = evaluate_proof({"demo": DEMO})
    assert report["passed"] is False
    assert any("out-of-sample" in gap for gap in report["gaps"])


def test_costs_must_be_included():
    report = evaluate_proof(
        {"outOfSample": {**OOS, "costsIncluded": False}, "demo": DEMO}
    )
    assert report["passed"] is False
    assert any("costs" in gap for gap in report["gaps"])


def test_a_negative_expectancy_on_either_half_fails():
    assert not evaluate_proof(
        {"outOfSample": OOS, "demo": {**DEMO, "expectancy": -0.01}}
    )["passed"]
    assert not evaluate_proof(
        {"outOfSample": {**OOS, "expectancy": 0}, "demo": DEMO}
    )["passed"]


def test_too_few_demo_trades_or_days_fails():
    short = evaluate_proof(
        {"outOfSample": OOS, "demo": {**DEMO, "closedTrades": 29}}
    )
    assert not short["passed"]
    assert any("demo trades 29" in gap for gap in short["gaps"])
    brief = evaluate_proof(
        {"outOfSample": OOS, "demo": {**DEMO, "days": 13.5}}
    )
    assert any("demo history" in gap for gap in brief["gaps"])


def test_drawdown_above_the_cap_fails():
    report = evaluate_proof(
        {"outOfSample": OOS, "demo": {**DEMO, "maxDrawdownPct": 30}}
    )
    assert not report["passed"]
    assert any("drawdown" in gap for gap in report["gaps"])


def test_requirements_come_from_the_bot_with_safe_defaults():
    definition = SimpleNamespace(
        config={"proof": {"requirements": {
            "minDemoTrades": 10, "minDemoDays": 3, "minOosTrades": 20,
        }}},
    )
    req = requirements_from(definition)
    assert req.min_demo_trades == 10
    assert req.min_demo_days == 3.0
    assert req.min_oos_trades == 20
    assert req.max_drawdown_pct == 20.0


def test_requirements_resist_garbage():
    definition = SimpleNamespace(config={"proof": {"requirements": {
        "minDemoTrades": "nope", "minDemoDays": -5,
    }}})
    req = requirements_from(definition)
    assert req.min_demo_trades == 30
    assert req.min_demo_days == 14.0


# -- evidence from the broker's own history ----------------------------------


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


def _fills(profits, *, ticket=111, start=1_700_000_000):
    return [
        {"profit": p, "commission": 0, "swap": 0, "orderTicket": ticket,
         "dealtAt": start + i * 86_400}
        for i, p in enumerate(profits)
    ]


def test_demo_evidence_ignores_a_live_connection():
    orders = [
        {"automationId": "bot_a", "connectionId": "c1", "ticket": 111},
        {"automationId": "bot_a", "connectionId": "c2", "ticket": 222},
    ]
    service = _Service(
        orders,
        {"c1": _fills([1.0, 2.0]), "c2": _fills([-50.0], ticket=222)},
    )
    evidence = demo_evidence(service, "bot_a")
    assert evidence["closedTrades"] == 2
    assert evidence["realizedPnl"] == pytest.approx(3.0)
    assert evidence["connections"] == ["c1"]
    assert evidence["days"] == pytest.approx(1.0)


def test_demo_evidence_counts_commission_and_swap():
    service = _Service(
        [{"automationId": "bot_a", "connectionId": "c1", "ticket": 111}],
        {"c1": [
            {"profit": 2.0, "commission": 0.5, "swap": 0.5, "orderTicket": 111,
             "dealtAt": 1},
            {"profit": 2.0, "commission": 0.5, "swap": 0.5, "orderTicket": 111,
             "dealtAt": 2},
        ]},
    )
    evidence = demo_evidence(service, "bot_a")
    assert evidence["realizedPnl"] == pytest.approx(2.0)
    assert evidence["expectancy"] == pytest.approx(1.0)


def test_no_demo_trade_is_an_empty_evidence_not_an_error():
    service = _Service(
        [{"automationId": "bot_a", "connectionId": "c2", "ticket": 9}],
        {"c2": []},
    )
    evidence = demo_evidence(service, "bot_a")
    assert evidence["closedTrades"] == 0
    assert evidence["available"] is True


# -- the gate ----------------------------------------------------------------


def _definition(**extra):
    return SimpleNamespace(
        id="bot_a", version=3,
        config={"proof": {"outOfSample": OOS}},
        execution_authority=SimpleNamespace(
            tier="l2", max_margin_per_order=500, require_stop=True,
        ),
        **extra,
    )


def test_a_bot_with_oos_but_no_demo_history_is_refused():
    service = _Service([], {})
    gap = live_proof_gap(service, _definition())
    assert gap is not None
    assert gap["reason"] == "PROOF_INSUFFICIENT"
    assert "demo trades 0" in gap["message"]


def test_a_bot_with_both_halves_has_no_gap():
    orders = [{"automationId": "bot_a", "connectionId": "c1", "ticket": 111}]
    service = _Service(
        orders, {"c1": _fills([0.2] * 35, start=1_700_000_000)},
    )
    # 35 fills one day apart span 34 days, expectancy 0.2 -> passes.
    assert live_proof_gap(service, _definition()) is None


def test_a_live_mandate_is_refused_without_proof(monkeypatch):
    from harness.trade.authority import check_mandate_creation

    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )
    service = _Service([], {})
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(
            service, bot_id="bot_a", bot_version=3, max_margin=100, mode="live",
        )
    assert exc.value.code == "PROOF_INSUFFICIENT"
    assert exc.value.status_code == 409
    assert exc.value.detail["proof"]["verdict"] == "insufficient"


def test_a_demo_mandate_is_not_proof_gated(monkeypatch):
    from harness.trade.authority import check_mandate_creation

    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )
    # No out-of-sample, no demo fills - and a demo mandate still works.
    check_mandate_creation(
        _Service([], {}), bot_id="bot_a", bot_version=3, max_margin=100,
        mode="demo",
    )
