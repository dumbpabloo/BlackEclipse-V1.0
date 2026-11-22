"""P1 demo execution: preview → single-use approval → submit.

Every test is synthetic. The fake adapter proves the authorization seam; it
cannot prove MetaTrader's wire behaviour, which is why the MT5 request builder is
also unit-tested separately.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.finance_guardrails.service import FinanceGuardrailService
from harness.graph_runtime.trading_nodes import exec_broker_order
from harness.mcp.approvals import ApprovalStore
from harness.persistence import AgentMemoryStore
from harness.trade.adapter import TradingAdapter
from harness.trade.bootstrap import set_service
from harness.trade.model import BrokerKind, OrderIntent, TradeRefused
from harness.trade.registry import register_kind
from harness.trade.service import BrokerService


class FakeTradingAdapter(TradingAdapter):
    kind = "fake-trade"
    ACCOUNT_MODE = "demo"
    SPREAD_POINTS = 2.0
    MARGIN = 11.0
    OPEN_POSITIONS = 0
    POSITIONS: list[dict] = []
    CLOSED: list[int] = []
    MODIFIED: list[tuple] = []
    DEALS: list[dict] = []
    SUBMIT_CALLS = 0

    @classmethod
    def availability(cls):
        return True, None

    def probe(self):
        return {"available": True, "connected": True, "code": None, "reason": None,
                "terminalPath": "/fake",
                "terminal": {"connected": True, "tradeAllowed": True},
                "account": self._account(), "warnings": []}

    def _account(self):
        return {
            "login": 7, "mode": self.ACCOUNT_MODE,
            "server": "Fake", "company": "Fake", "currency": "USD",
            "balance": 10_000.0, "equity": 10_000.0, "margin": 0.0,
            "marginFree": 10_000.0, "marginLevel": 0.0, "profit": 0.0,
            "leverage": 100, "marginMode": "hedging",
            "tradeAllowed": True, "expertAllowed": True,
        }

    def account(self):
        return self._account()

    def positions(self):
        return list(type(self).POSITIONS)

    def close_position(self, ticket, deviation=20, volume=None):
        type(self).CLOSED.append(int(ticket))
        held = 0.02 if volume is not None else 0.01
        return {"ok": True, "ticket": int(ticket), "symbol": "EURUSD", "side": "sell",
                "volume": volume if volume is not None else 0.01,
                "partial": volume is not None and volume < held,
                "remainingVolume": max(held - (volume or held), 0.0),
                "price": 1.1, "deal": 900 + int(ticket),
                "retcode": 10009, "retcodeName": "DONE", "reasons": [],
                "verified": True, "account": self._account()}

    def modify_position(self, ticket, stop_loss=None, take_profit=None):
        type(self).MODIFIED.append((int(ticket), stop_loss, take_profit))
        return {"ok": True, "ticket": int(ticket), "symbol": "EURUSD",
                "stopLoss": stop_loss or 0, "takeProfit": take_profit or 0,
                "retcode": 10009, "retcodeName": "DONE", "reasons": [],
                "verified": True, "account": self._account()}

    def orders(self):
        return []

    def quote(self, symbol):
        return {"symbol": symbol.upper(), "bid": 1.1, "ask": 1.1002,
                "spreadPoints": self.SPREAD_POINTS, "provider": "fake"}

    def order_preview(self, intent):
        return {
            "ok": True,
            "symbol": intent.symbol.upper(),
            "requestedSymbol": intent.symbol,
            "side": intent.side,
            "type": intent.order_type,
            "volume": intent.volume,
            "price": 1.1002,
            "notional": 1_100.2,
            "margin": self.MARGIN,
            "marginFree": 10_000.0,
            "spreadPoints": self.SPREAD_POINTS,
            "openPositions": self.OPEN_POSITIONS,
            "account": self._account(),
            "terminalTradeAllowed": True,
            "quote": self.quote(intent.symbol),
            "request": {"symbol": intent.symbol.upper(), "volume": intent.volume},
            "retcode": 0,
            "retcodeName": "DONE",
            "reasons": [],
        }

    def submit_order(self, intent):
        type(self).SUBMIT_CALLS += 1
        return {
            "ok": True, "status": "FILLED", "symbol": intent.symbol.upper(),
            "side": intent.side, "type": intent.order_type, "volume": intent.volume,
            "price": 1.1002, "ticket": 4242, "deal": 99,
            "retcode": 10009, "retcodeName": "DONE", "comment": "done",
            "requestId": 1, "reasons": [], "account": self._account(),
        }

    def cancel_order(self, ticket):
        return {"ok": True, "ticket": ticket, "retcode": 10009,
                "retcodeName": "DONE", "reasons": []}

    def cancel_all(self, magic):
        type(self).LAST_CANCEL_MAGIC = magic
        return {"ok": True, "magic": magic, "matched": 1, "cancelled": 1,
                "failed": 0, "results": []}

    def deals_since(self, since_ms):
        return list(type(self).DEALS)


def _service(tmp_path, adapter_cls=FakeTradingAdapter) -> BrokerService:
    store = AgentMemoryStore(os.path.join(str(tmp_path), "orders.db"))
    approvals = ApprovalStore(store)
    guardrails = FinanceGuardrailService(store, approvals=approvals)
    register_kind(
        BrokerKind(id="fake-trade", label="Fake trade", auth="attach",
                   execution=True, execution_modes=("demo", "live")),
        adapter_cls,
        replace=True,
    )
    service = BrokerService(store, approvals=approvals, guardrails=guardrails)
    service.create_connection(kind="fake-trade", label="Fake demo", mode="demo", config={})
    return service


def test_a_connection_gets_an_ownership_magic_for_the_kill_switch(tmp_path):
    """A missing magic is repaired at the service boundary, never left at 0."""
    service = _service(tmp_path)
    connection = service.list_connections()[0]
    assert int(connection["config"].get("magic") or 0) > 0

    # An existing 0 is backfilled on the next read (old connections).
    service.update_connection(
        connection["id"], {"config": {"maxMargin": 500, "magic": 0}},
    )
    assert service.get_connection(connection["id"])["config"]["magic"] > 0

    explicit = BrokerService(
        AgentMemoryStore(os.path.join(str(tmp_path), "explicit.db")),
    )
    register_kind(
        BrokerKind(id="fake-trade", label="Fake trade", auth="attach",
                   execution=True, execution_modes=("demo", "live")),
        FakeTradingAdapter,
        replace=True,
    )
    made = explicit.create_connection(
        kind="fake-trade", label="Other", mode="demo", config={"magic": 4242},
    )
    assert made["config"]["magic"] == 4242


class RealLiveAdapter(FakeTradingAdapter):
    ACCOUNT_MODE = "real"


def _live_service(tmp_path, adapter_cls=RealLiveAdapter, config=None) -> BrokerService:
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    store = AgentMemoryStore(os.path.join(str(root), "orders-live.db"))
    approvals = ApprovalStore(store)
    guardrails = FinanceGuardrailService(store, approvals=approvals)
    register_kind(
        BrokerKind(id="fake-trade", label="Fake trade", auth="attach",
                   execution=True, execution_modes=("demo", "live")),
        adapter_cls,
        replace=True,
    )
    service = BrokerService(store, approvals=approvals, guardrails=guardrails)
    service.create_connection(
        kind="fake-trade",
        label="Fake live",
        mode="live",
        config={"maxMargin": 500, "magic": 99, **(config or {})},
    )
    return service


def _intent(**overrides):
    payload = {"symbol": "EURUSD", "side": "buy", "type": "market", "volume": 0.1}
    payload.update(overrides)
    return payload


def _connection_id(service: BrokerService) -> str:
    return service.list_connections()[0]["id"]


def test_preview_creates_a_pending_approval_and_sends_nothing(tmp_path):
    service = _service(tmp_path)
    result = service.preview_order(_connection_id(service), _intent())
    assert result["allowed"] is True
    assert result["approval"]["status"] == "pending"
    assert result["guardrail"]["decision"] in ("allow", "confirm")
    assert service.ledger_orders(_connection_id(service)) == []
    assert service.list_approvals("pending")


def test_pending_approval_carries_what_the_chat_card_needs(tmp_path):
    """The card submits from this view; it must not have to re-type the order."""
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    service.preview_order(connection_id, _intent(volume=0.25, stopLoss=1.09))

    row = service.list_approvals("pending")[0]
    assert row["connectionId"] == connection_id
    assert row["intent"]["symbol"] == "EURUSD"
    assert row["intent"]["side"] == "buy"
    assert row["intent"]["volume"] == 0.25
    assert row["intent"]["stopLoss"] == 1.09
    assert row["actionSummary"]


def test_submit_consumes_the_approval_exactly_once(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    preview = service.preview_order(connection_id, _intent())
    approval_id = preview["approval"]["id"]
    service.decide_approval(approval_id, approve=True)

    result = service.submit_order(connection_id, _intent(), approval_id)
    assert result["sent"] is True
    assert result["filled"] is True
    assert result["status"] == "filled"
    assert result["ledger"]["ticket"] == 4242
    assert service.ledger_orders(connection_id)[0]["status"] == "filled"

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), approval_id)
    assert exc.value.code in ("APPROVAL_NOT_APPROVED", "APPROVAL_USED")


def test_approval_is_bound_to_the_exact_order(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    preview = service.preview_order(connection_id, _intent())
    approval_id = preview["approval"]["id"]
    service.decide_approval(approval_id, approve=True)

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(volume=0.2), approval_id)
    assert exc.value.code == "APPROVAL_ORDER_MISMATCH"


def test_unapproved_order_is_refused(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    preview = service.preview_order(connection_id, _intent())
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), preview["approval"]["id"])
    assert exc.value.code == "APPROVAL_NOT_APPROVED"


def test_live_connection_is_refused_before_any_broker_call(tmp_path):
    service = _live_service(tmp_path)
    result = service.preview_order(_connection_id(service), _intent())
    assert result["allowed"] is False
    assert result["status"] == "REFUSED"
    assert result["error"]["code"] == "LIVE_NOT_ARMED"


def test_a_real_account_is_refused_when_the_record_says_demo(tmp_path):
    class RealAccountAdapter(FakeTradingAdapter):
        ACCOUNT_MODE = "real"

    service = _service(tmp_path, RealAccountAdapter)
    result = service.preview_order(_connection_id(service), _intent())
    assert result["allowed"] is False
    assert result["error"]["code"] == "DEMO_ACCOUNT_REQUIRED"


def test_spread_cap_refuses_without_creating_an_approval(tmp_path):
    class WideSpreadAdapter(FakeTradingAdapter):
        SPREAD_POINTS = 5.0

    service = _service(tmp_path, WideSpreadAdapter)
    connection_id = _connection_id(service)
    service.update_connection(connection_id, {"config": {"maxSpreadPoints": 1}})
    result = service.preview_order(connection_id, _intent())
    assert result["allowed"] is False
    assert "policy.max-spread" in result["policy"]["rules"]
    assert result["approval"] is None
    assert service.list_approvals("pending") == []


def test_money_guardrail_denies_the_order(tmp_path):
    service = _service(tmp_path)
    service.guardrails.update_config({"perTxnCap": 1})
    result = service.preview_order(_connection_id(service), _intent())
    assert result["allowed"] is False
    assert any("limit.per-transaction" in rule for rule in result["guardrail"]["rules"])
    assert result["approval"] is None


def test_guardrail_cooling_off_delays_the_send(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    service.guardrails.update_config({"highValue": 1})
    preview = service.preview_order(connection_id, _intent())
    assert preview["allowed"] is True
    assert preview["requiresTypedConfirmation"] is True
    phrase = preview["confirmPhrase"]
    assert phrase
    approval_id = preview["approval"]["id"]
    service.decide_approval(approval_id, approve=True)

    early = service.submit_order(connection_id, _intent(), approval_id, confirmation=phrase)
    assert early["status"] == "COOLING_OFF"
    assert early["sent"] is False
    assert early["remainingSeconds"] > 0

    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    service.approvals._db.execute(
        "UPDATE McpApproval SET createdAt=? WHERE id=?", (past, approval_id)
    )
    service.approvals._db.commit()
    later = service.submit_order(connection_id, _intent(), approval_id, confirmation=phrase)
    assert later["status"] == "filled"


def test_high_value_requires_the_typed_phrase(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    service.guardrails.update_config({"highValue": 1})
    preview = service.preview_order(connection_id, _intent())
    approval_id = preview["approval"]["id"]
    service.decide_approval(approval_id, approve=True)

    refused = service.submit_order(connection_id, _intent(), approval_id)
    assert refused["status"] == "CONFIRMATION_REQUIRED"
    assert refused["requiredPhrase"] == preview["confirmPhrase"]
    assert refused["sent"] is False
    # The approval is not consumed by a missing phrase.
    assert service.approvals.get(approval_id)["status"] == "approved"


def test_cancel_is_allowed_without_a_trade_approval(tmp_path):
    service = _service(tmp_path)
    result = service.cancel_order(_connection_id(service), 4242)
    assert result["cancelled"] is True
    assert result["result"]["retcode"] == 10009


def test_close_position_is_allowed_without_a_mandate_or_approval(tmp_path):
    FakeTradingAdapter.POSITIONS = [{"ticket": 111, "symbol": "EURUSD", "volume": 0.01}]
    FakeTradingAdapter.CLOSED = []
    service = _service(tmp_path)
    result = service.close_position(_connection_id(service), 111)
    assert result["closed"] is True
    assert result["result"]["ticket"] == 111
    assert FakeTradingAdapter.CLOSED == [111]


def test_close_positions_closes_every_open_position(tmp_path):
    FakeTradingAdapter.POSITIONS = [
        {"ticket": 111, "symbol": "EURUSD", "volume": 0.01},
        {"ticket": 222, "symbol": "XAUUSD", "volume": 0.01},
    ]
    FakeTradingAdapter.CLOSED = []
    service = _service(tmp_path)
    result = service.close_positions(_connection_id(service))
    assert result["closed"] == 2
    assert result["failed"] == 0
    assert result["requested"] == 2
    assert FakeTradingAdapter.CLOSED == [111, 222]


def test_close_positions_can_target_named_tickets(tmp_path):
    FakeTradingAdapter.POSITIONS = [
        {"ticket": 111, "symbol": "EURUSD", "volume": 0.01},
        {"ticket": 222, "symbol": "XAUUSD", "volume": 0.01},
    ]
    FakeTradingAdapter.CLOSED = []
    service = _service(tmp_path)
    result = service.close_positions(_connection_id(service), tickets=[222])
    assert result["closed"] == 1
    assert FakeTradingAdapter.CLOSED == [222]


def test_close_positions_reports_a_missing_ticket_as_failed(tmp_path):
    FakeTradingAdapter.POSITIONS = [{"ticket": 111, "symbol": "EURUSD", "volume": 0.01}]
    FakeTradingAdapter.CLOSED = []
    service = _service(tmp_path)
    result = service.close_positions(_connection_id(service), tickets=[111, 999])
    assert result["closed"] == 1
    assert result["failed"] == 1
    assert any(row["retcodeName"] == "POSITION_NOT_FOUND" for row in result["results"])


def test_close_position_can_close_part_of_a_position(tmp_path):
    FakeTradingAdapter.CLOSED = []
    service = _service(tmp_path)
    result = service.close_position(_connection_id(service), 111, volume=0.01)
    assert result["closed"] is True
    assert result["result"]["partial"] is True
    assert result["result"]["remainingVolume"] == 0.01


def test_modify_position_moves_the_bracket_without_a_mandate(tmp_path):
    FakeTradingAdapter.MODIFIED = []
    service = _service(tmp_path)
    result = service.modify_position(
        _connection_id(service), 111, stop_loss=1.105, take_profit=1.12,
    )
    assert result["modified"] is True
    assert FakeTradingAdapter.MODIFIED == [(111, 1.105, 1.12)]


def test_modify_position_needs_at_least_one_level(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(TradeRefused) as caught:
        service.modify_position(_connection_id(service), 111)
    assert caught.value.code == "INVALID_STOPS"


def test_modify_live_position_is_allowed_without_an_arm(tmp_path):
    """Risk-reducing verbs must still work after a live arm expires."""
    RealLiveAdapter.MODIFIED = []
    service = _live_service(tmp_path)
    result = service.modify_position(
        _connection_id(service), 111, stop_loss=1.1, take_profit=1.12,
    )
    assert result["modified"] is True
    assert RealLiveAdapter.MODIFIED == [(111, 1.1, 1.12)]


def test_close_live_position_is_allowed_without_an_arm(tmp_path):
    RealLiveAdapter.CLOSED = []
    service = _live_service(tmp_path)
    result = service.close_position(_connection_id(service), 111)
    assert result["closed"] is True
    assert RealLiveAdapter.CLOSED == [111]


def test_live_close_all_is_scoped_to_eclipse_magic(tmp_path):
    RealLiveAdapter.CLOSED = []
    RealLiveAdapter.POSITIONS = [
        {"ticket": 501, "magic": 99, "symbol": "EURUSD"},
        {"ticket": 502, "magic": 7, "symbol": "EURUSD"},
    ]
    try:
        service = _live_service(tmp_path)
        result = service.close_positions(_connection_id(service))
    finally:
        RealLiveAdapter.POSITIONS = []
    assert result["closed"] == 1
    assert RealLiveAdapter.CLOSED == [501]


def test_live_connection_refuses_a_demo_account_for_closes(tmp_path):
    class DemoAdapter(FakeTradingAdapter):
        ACCOUNT_MODE = "demo"

    service = _live_service(tmp_path, DemoAdapter)
    with pytest.raises(TradeRefused) as caught:
        service.close_position(_connection_id(service), 111)
    assert caught.value.code == "LIVE_ACCOUNT_REQUIRED"


def test_order_intent_digest_is_stable_and_covers_every_field():
    one = OrderIntent.from_payload(_intent(), connection_id="brk_1")
    two = OrderIntent.from_payload(_intent(), connection_id="brk_1")
    other = OrderIntent.from_payload(_intent(volume=0.2), connection_id="brk_1")
    assert one.digest == two.digest
    assert one.digest != other.digest
    assert one.request_key == "trade:brk_1"


def test_order_intent_rejects_bad_orders():
    with pytest.raises(TradeRefused):
        OrderIntent.from_payload(_intent(side="hold"), connection_id="brk_1")
    with pytest.raises(TradeRefused):
        OrderIntent.from_payload(_intent(type="limit"), connection_id="brk_1")  # no price
    with pytest.raises(TradeRefused):
        OrderIntent.from_payload(_intent(volume=0), connection_id="brk_1")


# ── P2: armed live ───────────────────────────────────────────────────────────
def test_arming_requires_a_typed_phrase_a_cap_and_a_magic(tmp_path):
    service = _live_service(tmp_path)
    connection_id = _connection_id(service)
    with pytest.raises(TradeRefused) as exc:
        service.arm_connection(connection_id, confirmation="ARM LIVE wrong")
    assert exc.value.code == "LIVE_CONFIRMATION_MISMATCH"

    no_cap = _live_service(tmp_path / "nocap", config={"maxMargin": 0})
    with pytest.raises(TradeRefused) as exc:
        no_cap.arm_connection(_connection_id(no_cap), confirmation="ARM LIVE 7")
    assert exc.value.code == "LIVE_CAP_REQUIRED"

    no_magic = _live_service(tmp_path / "nomagic")
    # Force a 0 despite the backfill to exercise the last-line guard.
    import harness.trade.service as service_module

    no_magic.update_connection(
        _connection_id(no_magic), {"config": {"maxMargin": 500, "magic": 0}},
    )
    original = service_module.with_ownership_magic
    service_module.with_ownership_magic = lambda config, label: (dict(config or {}), 0)
    try:
        with pytest.raises(TradeRefused) as exc:
            no_magic.arm_connection(_connection_id(no_magic), confirmation="ARM LIVE 7")
        assert exc.value.code == "LIVE_MAGIC_REQUIRED"
    finally:
        service_module.with_ownership_magic = original


def test_armed_live_submit_requires_the_live_phrase(tmp_path):
    service = _live_service(tmp_path)
    connection_id = _connection_id(service)
    armed = service.arm_connection(
        connection_id, confirmation="ARM LIVE 7", ttl_seconds=600
    )
    assert armed["armed"] is True

    preview = service.preview_order(connection_id, _intent())
    assert preview["mode"] == "live"
    assert preview["allowed"] is True
    assert preview["requiresTypedConfirmation"] is True
    assert preview["confirmPhrase"] == "LIVE BUY 0.1 EURUSD"
    approval_id = preview["approval"]["id"]
    service.decide_approval(approval_id, approve=True)

    missing = service.submit_order(connection_id, _intent(), approval_id)
    assert missing["status"] == "CONFIRMATION_REQUIRED"
    assert missing["requiredPhrase"] == "LIVE BUY 0.1 EURUSD"

    sent = service.submit_order(
        connection_id, _intent(), approval_id,
        confirmation="LIVE BUY 0.1 EURUSD",
    )
    assert sent["status"] == "filled"
    assert sent["ledger"]["ticket"] == 4242


def test_disarm_and_kill_switch(tmp_path):
    service = _live_service(tmp_path)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    service.disarm_connection(connection_id)
    refused = service.preview_order(connection_id, _intent())
    assert refused["allowed"] is False
    assert refused["error"]["code"] == "LIVE_NOT_ARMED"

    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    killed = service.kill_switch(connection_id)
    assert killed["kill"]["magic"] == 99
    assert killed["armed"] is False
    assert RealLiveAdapter.LAST_CANCEL_MAGIC == 99
    assert service.get_connection(connection_id)["armedUntil"] is None


def test_arm_expiry_is_enforced(tmp_path):
    service = _live_service(tmp_path)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=60)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    service.connections.set_arm(connection_id, until=past)
    refused = service.preview_order(connection_id, _intent())
    assert refused["allowed"] is False
    assert refused["error"]["code"] == "LIVE_ARM_EXPIRED"
    assert service.get_connection(connection_id)["armedUntil"] is None


def test_live_connection_refuses_a_demo_account(tmp_path):
    class DemoAdapter(FakeTradingAdapter):
        ACCOUNT_MODE = "demo"

    service = _live_service(tmp_path, DemoAdapter)
    with pytest.raises(TradeRefused) as exc:
        service.arm_connection(_connection_id(service), confirmation="ARM LIVE 7")
    assert exc.value.code == "LIVE_ACCOUNT_REFUSED"


# ── P3: bounded demo mandates / unattended graph order ──────────────────────
def _mandate(service: BrokerService, connection_id: str, **overrides):
    payload = {
        "symbols": ["EURUSD"],
        "maxOrders": 1,
        "maxMargin": 100,
        "ttlSeconds": 600,
        "confirmation": f"MANDATE DEMO {service.get_connection(connection_id)['label']}",
    }
    payload.update(overrides)
    return service.create_mandate(connection_id, payload)


def test_mandate_requires_the_typed_phrase_and_a_pinned_live_workflow(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    with pytest.raises(TradeRefused) as exc:
        _mandate(service, connection_id, confirmation="MANDATE DEMO wrong")
    assert exc.value.code == "MANDATE_CONFIRMATION_MISMATCH"

    live = _live_service(tmp_path / "live")
    live_id = _connection_id(live)
    with pytest.raises(TradeRefused) as exc:
        live.create_mandate(live_id, {
            "confirmation": "MANDATE LIVE Fake live",
            "symbols": ["EURUSD"], "maxOrders": 1, "maxMargin": 100,
            "ttlSeconds": 600,
        })
    assert exc.value.code == "LIVE_NOT_ARMED"

    live.arm_connection(live_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    with pytest.raises(TradeRefused) as exc:
        live.create_mandate(live_id, {
            "confirmation": "MANDATE LIVE Fake live",
            "symbols": ["EURUSD"], "maxOrders": 1, "maxMargin": 100,
            "ttlSeconds": 600,
        })
    assert exc.value.code == "MANDATE_WORKFLOW_REQUIRED"

    mandate = live.create_mandate(live_id, {
        "confirmation": "MANDATE LIVE Fake live",
        "symbols": ["EURUSD"], "maxOrders": 1, "maxMargin": 100,
        "ttlSeconds": 600, "workflowId": "wf_live", "workflowVersion": 1,
    })
    assert mandate["mode"] == "live"
    assert mandate["workflowId"] == "wf_live"
    assert mandate["workflowVersion"] == 1


def test_live_mandate_submits_without_a_typed_order_phrase(tmp_path):
    service = _live_service(tmp_path)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    mandate = service.create_mandate(connection_id, {
        "confirmation": "MANDATE LIVE Fake live",
        "symbols": ["EURUSD"], "maxOrders": 1, "maxMargin": 100,
        "ttlSeconds": 600, "workflowId": "wf_live", "workflowVersion": 1,
    })
    result = service.submit_order(
        connection_id, _intent(), mandate_id=mandate["id"],
        workflow_id="wf_live",
    )
    assert result["status"] == "filled"

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(
            connection_id, _intent(), mandate_id=mandate["id"],
            workflow_id="wf_other",
        )
    assert exc.value.code == "MANDATE_WORKFLOW_MISMATCH"


def test_mandate_submits_without_an_approval_and_is_exhausted(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    mandate = _mandate(service, connection_id)
    result = service.submit_order(
        connection_id, _intent(), mandate_id=mandate["id"], run_id="run_graph_1"
    )
    assert result["status"] == "filled"
    assert result["ledger"]["mandateId"] == mandate["id"]
    assert result["ledger"]["approvalId"] is None

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), mandate_id=mandate["id"])
    assert exc.value.code in ("MANDATE_INACTIVE", "MANDATE_EXHAUSTED")


def test_mandate_limits_symbol_and_margin(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    wrong_symbol = _mandate(service, connection_id, symbols=["GBPUSD"])
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), mandate_id=wrong_symbol["id"])
    assert exc.value.code == "MANDATE_SYMBOL_NOT_ALLOWED"

    small_cap = _mandate(service, connection_id, maxMargin=1)
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), mandate_id=small_cap["id"])
    assert exc.value.code == "MANDATE_MARGIN_EXCEEDED"


def test_mandate_revocation_is_immediate(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    mandate = _mandate(service, connection_id)
    service.revoke_mandate(mandate["id"])
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent(), mandate_id=mandate["id"])
    assert exc.value.code == "MANDATE_INACTIVE"


def test_authorization_must_be_exactly_one_kind(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    preview = service.preview_order(connection_id, _intent())
    mandate = _mandate(service, connection_id)
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(
            connection_id, _intent(),
            approval_id=preview["approval"]["id"], mandate_id=mandate["id"],
        )
    assert exc.value.code == "AMBIGUOUS_AUTHORIZATION"
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(connection_id, _intent())
    assert exc.value.code == "AUTHORIZATION_REQUIRED"


class _NodeContext:
    def __init__(self, intent, config):
        self.inputs = {"intent": intent}
        self.config = config
        self.run_id = "run_graph_1"
        self.node_id = "broker-order-1"
        self.messages: list[str] = []

    def emit(self, message):
        self.messages.append(message)


def test_graph_broker_order_node_submits_under_a_mandate(tmp_path):
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    mandate = _mandate(service, connection_id)
    set_service(service)
    try:
        context = _NodeContext(
            {"symbol": "EURUSD", "side": "buy", "type": "market", "quantity": 0.1},
            {"connectionId": connection_id, "mandateId": mandate["id"]},
        )
        result = exec_broker_order(context)
    finally:
        set_service(None)
    assert result["status"] == "filled"
    assert result["orderId"]
    assert any("broker order filled" in message for message in context.messages)


def test_graph_broker_order_node_requires_a_mandate(tmp_path):
    service = _service(tmp_path)
    set_service(service)
    try:
        context = _NodeContext(
            {"symbol": "EURUSD", "side": "buy", "type": "market", "quantity": 0.1},
            {"connectionId": _connection_id(service), "mandateId": ""},
        )
        with pytest.raises(ValueError):
            exec_broker_order(context)
    finally:
        set_service(None)


# ── reveal the terminal after a fill ────────────────────────────────────────
def test_a_filled_order_reveals_the_attached_terminal(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "harness.trade.service.reveal_terminal",
        lambda connection: calls.append(connection.id) or {"opened": True},
    )
    service = _service(tmp_path)
    connection_id = _connection_id(service)
    preview = service.preview_order(connection_id, _intent())
    service.decide_approval(preview["approval"]["id"], approve=True)
    result = service.submit_order(connection_id, _intent(), preview["approval"]["id"])
    assert result["status"] == "filled"
    assert calls == [connection_id]


def test_a_refused_order_does_not_reveal_the_terminal(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "harness.trade.service.reveal_terminal",
        lambda connection: calls.append(connection.id) or {"opened": True},
    )
    service = _live_service(tmp_path)
    with pytest.raises(TradeRefused):
        service.submit_order(_connection_id(service), _intent(), mandate_id="mnd_missing")
    assert calls == []


def test_terminal_reveal_is_a_noop_off_windows(monkeypatch):
    from harness.trade import terminal

    monkeypatch.setattr(terminal.sys, "platform", "linux")
    result = terminal.reveal_terminal(object())
    assert result["opened"] is False
    assert result["reason"] == "PLATFORM_UNSUPPORTED"


def test_terminal_reveal_can_be_disabled_per_connection(monkeypatch):
    from harness.trade import terminal

    monkeypatch.setattr(terminal.sys, "platform", "win32")
    connection = type("C", (), {"config": {"openTerminalOnTrade": False}})()
    result = terminal.reveal_terminal(connection)
    assert result["opened"] is False
    assert result["reason"] == "DISABLED"

# -- P5 bot fence: BrokerService.submit_order is the one live gate ----------
class CountingLiveAdapter(RealLiveAdapter):
    """Counts previews AND submits so a refusal can prove the adapter was untouched."""
    PREVIEW_CALLS = 0
    SUBMIT_CALLS = 0

    def order_preview(self, intent):
        type(self).PREVIEW_CALLS += 1
        return super().order_preview(intent)


@pytest.fixture(autouse=True)
def _open_the_evidence_gates(monkeypatch):
    """This module tests the FENCE, not the evidence.

    The proof gate and the live ramp are covered end to end in
    `test_trade_proof.py`, `test_trade_ramp.py` and `test_live_ramp_e2e.py`.
    Here they are stubbed so a tier/version/mandate test is not re-testing them.
    """
    monkeypatch.setattr("harness.trade.proof.live_proof_gap", lambda *_a, **_k: None)
    monkeypatch.setattr("harness.trade.ramp.ramp_gap", lambda *_a, **_k: None)


def _bot_definition(tier="l2", version=3):
    return SimpleNamespace(
        id="aut_bot",
        version=version,
        execution_authority=SimpleNamespace(
            tier=tier,
            appetite="conservative",
            sizing_mode="risk_based",
            require_stop=True,
            max_open_positions=2,
            cooldown_s=0,
            max_margin_per_order=500,
            max_daily_loss_pct=3,
        ),
    )


def _bot_mandate(service, connection_id):
    return service.create_mandate(connection_id, {
        "confirmation": "MANDATE LIVE Fake live",
        "symbols": ["EURUSD"],
        "maxOrders": 1,
        "maxMargin": 100,
        "ttlSeconds": 600,
        "botId": "aut_bot",
        "botVersion": 3,
    })


def test_an_l1_bot_is_refused_before_the_adapter_is_touched(tmp_path, monkeypatch):
    import harness.trade.authority as authority_module

    service = _live_service(tmp_path, CountingLiveAdapter)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)

    # The grant is created while the bot is L2, then the tier is lowered.
    monkeypatch.setattr(authority_module, "bot_definition", lambda _id: _bot_definition("l2", 3))
    mandate = _bot_mandate(service, connection_id)

    monkeypatch.setattr(authority_module, "bot_definition", lambda _id: _bot_definition("l1", 3))
    CountingLiveAdapter.PREVIEW_CALLS = 0
    CountingLiveAdapter.SUBMIT_CALLS = 0

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(
            connection_id, _intent(stopLoss=1.09), mandate_id=mandate["id"],
            automation_id="aut_bot", bot_id="aut_bot", bot_version=3,
        )

    assert exc.value.code == "BOT_TIER_INSUFFICIENT"
    assert CountingLiveAdapter.PREVIEW_CALLS == 0
    assert CountingLiveAdapter.SUBMIT_CALLS == 0
    assert service.ledger_orders(connection_id) == []


def test_an_l2_bot_sends_exactly_once_under_its_pinned_mandate(tmp_path, monkeypatch):
    import harness.trade.authority as authority_module

    service = _live_service(tmp_path, CountingLiveAdapter)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    monkeypatch.setattr(authority_module, "bot_definition", lambda _id: _bot_definition("l2", 3))
    mandate = _bot_mandate(service, connection_id)

    CountingLiveAdapter.SUBMIT_CALLS = 0
    result = service.submit_order(
        connection_id, _intent(stopLoss=1.09), mandate_id=mandate["id"],
        automation_id="aut_bot", bot_id="aut_bot", bot_version=3,
    )

    assert result["status"] == "filled"
    assert CountingLiveAdapter.SUBMIT_CALLS == 1
    ledger = service.ledger_orders(connection_id, automation_id="aut_bot")
    assert len(ledger) == 1
    assert ledger[0]["automationId"] == "aut_bot"
    assert ledger[0]["mandateId"] == mandate["id"]

    # A second submit without a fresh grant must not reach the adapter again.
    with pytest.raises(TradeRefused) as exc:
        service.submit_order(
            connection_id, _intent(stopLoss=1.09), mandate_id=mandate["id"],
            automation_id="aut_bot", bot_id="aut_bot", bot_version=3,
        )
    assert exc.value.code in ("MANDATE_INACTIVE", "MANDATE_EXHAUSTED")
    assert CountingLiveAdapter.SUBMIT_CALLS == 1


def test_a_stale_bot_version_is_rejected_before_any_send(tmp_path, monkeypatch):
    import harness.trade.authority as authority_module

    service = _live_service(tmp_path, CountingLiveAdapter)
    connection_id = _connection_id(service)
    service.arm_connection(connection_id, confirmation="ARM LIVE 7", ttl_seconds=600)
    monkeypatch.setattr(authority_module, "bot_definition", lambda _id: _bot_definition("l2", 3))
    mandate = _bot_mandate(service, connection_id)

    # The bot's objective changed (version 4) since the mandate pinned version 3.
    monkeypatch.setattr(authority_module, "bot_definition", lambda _id: _bot_definition("l2", 4))
    CountingLiveAdapter.SUBMIT_CALLS = 0

    with pytest.raises(TradeRefused) as exc:
        service.submit_order(
            connection_id, _intent(stopLoss=1.09), mandate_id=mandate["id"],
            automation_id="aut_bot", bot_id="aut_bot", bot_version=4,
        )

    assert exc.value.code == "BOT_VERSION_STALE"
    assert CountingLiveAdapter.SUBMIT_CALLS == 0
