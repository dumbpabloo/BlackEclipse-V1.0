"""End-to-end evidence: a bot crosses the demo proof gate and enters live at `probe`.

This drives the REAL `BrokerService` stores (ledger + fills + connections) through the
REAL `check_mandate_creation` chain - tier -> version -> margin ceiling -> proof (OOS +
demo fills) -> ramp stage. It touches no broker and sends no order: the live cap is
decided before any money moves.

The first live grant must be `probe` (25% of the ceiling). Twenty profitable LIVE fills
later, `build` (50%) is allowed. That is the ramp, evidenced end to end.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from harness.persistence import AgentMemoryStore
from harness.trade.authority import check_mandate_creation
from harness.trade.model import TradeRefused
from harness.trade.service import BrokerService

BOT = "auto_e2e"
BOT_VERSION = 3
CAP = 400.0
OOS = {"trades": 120, "expectancy": 0.18, "costsIncluded": True,
       "maxDrawdownPct": 6.0}


def _definition():
    return SimpleNamespace(
        id=BOT,
        version=BOT_VERSION,
        config={"proof": {"outOfSample": OOS}},
        execution_authority=SimpleNamespace(
            tier="l2", max_margin_per_order=CAP, max_orders=8, require_stop=True,
        ),
    )


def _service(tmp_path) -> BrokerService:
    store = AgentMemoryStore(os.path.join(str(tmp_path), "e2e.db"))
    return BrokerService(store)


def _seed(service, *, connection_id, orders=1, fills=(), ticket_base=1000):
    for index in range(orders):
        service.connections.create_order(
            connection_id=connection_id,
            intent={"symbol": "EURUSD", "side": "buy", "volume": 0.1},
            digest=f"digest-{connection_id}-{index}",
            automation_id=BOT,
            automation_version=BOT_VERSION,
            status="filled",
        )
    service.connections.update_order(
        service.ledger_orders(limit=1)[0]["id"], ticket=ticket_base,
    )
    service.connections.save_fills(connection_id, list(fills))


def _fills(n, profit, *, ticket, start=1_700_000_000, days_apart=0):
    return [
        {"dealTicket": ticket * 10 + i, "orderTicket": ticket, "positionTicket": ticket,
         "symbol": "EURUSD", "side": "sell", "entry": "out", "volume": 0.1,
         "price": 1.1, "commission": 0.0, "swap": 0.0, "profit": profit,
         "dealtAt": start + i * (86_400 if days_apart else 3_600), "magic": 7}
        for i in range(n)
    ]


def test_the_chain_admits_a_proven_bot_at_probe_and_refuses_above_it(
    tmp_path, monkeypatch,
):
    service = _service(tmp_path)
    demo = service.create_connection(kind="mt5", label="demo", mode="demo")["id"]
    live = service.create_connection(kind="mt5", label="live", mode="live")["id"]
    # 35 demo fills over 34 days, positive after costs: the proof gate's demo half.
    _seed(service, connection_id=demo, ticket_base=111,
          fills=_fills(35, 0.2, ticket=111, days_apart=True))

    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )

    # 1) Above the probe cap (25% of 400 = 100) is refused, and the reason names the ramp.
    with pytest.raises(TradeRefused) as over:
        check_mandate_creation(
            service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=300, mode="live",
        )
    assert over.value.code == "RAMP_CAP_EXCEEDED"
    assert over.value.detail["ramp"]["stage"] == "probe"

    # 2) At the probe cap it is admitted: proof passed, ramp stage 0.
    check_mandate_creation(
        service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=100, mode="live",
    )

    # 3) Twenty profitable LIVE fills raise the stage to build (50% = 200).
    _seed(service, connection_id=live, ticket_base=2000,
          fills=_fills(20, 0.5, ticket=2000))
    check_mandate_creation(
        service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=200, mode="live",
    )
    with pytest.raises(TradeRefused) as still:
        check_mandate_creation(
            service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=300, mode="live",
        )
    assert still.value.code == "RAMP_CAP_EXCEEDED"
    assert still.value.detail["ramp"]["stage"] == "build"


def test_no_demo_evidence_means_no_live_mandate(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.create_connection(kind="mt5", label="live", mode="live")
    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(
            service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=50, mode="live",
        )
    assert exc.value.code == "PROOF_INSUFFICIENT"
    assert "demo trades 0" in exc.value.message


def test_a_stale_version_is_refused_before_the_evidence_gate(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.create_connection(kind="mt5", label="live", mode="live")
    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )
    with pytest.raises(TradeRefused) as exc:
        check_mandate_creation(
            service, bot_id=BOT, bot_version=BOT_VERSION - 1, max_margin=50, mode="live",
        )
    assert exc.value.code == "BOT_VERSION_STALE"


def test_a_demo_mandate_needs_no_proof_and_no_ramp(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.create_connection(kind="mt5", label="demo", mode="demo")
    monkeypatch.setattr(
        "harness.trade.authority.bot_definition", lambda _id: _definition(),
    )
    # No OOS, no fills - a demo mandate still passes: that is where evidence comes from.
    check_mandate_creation(
        service, bot_id=BOT, bot_version=BOT_VERSION, max_margin=400, mode="demo",
    )
