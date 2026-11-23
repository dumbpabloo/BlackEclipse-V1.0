"""Broker graph nodes: honest read envelopes, a derived bar summary, a
symbol-wide close, and the risk-decision gate on an unattended order."""

from __future__ import annotations

import pytest

from harness.graph_runtime.model import ExecutionMode
from harness.graph_runtime.nodes import build_default_registry
from harness.graph_runtime.registry import NodeExecContext
from harness.trade import bootstrap

REGISTRY = build_default_registry()


class FakeMandates:
    def __init__(self):
        self.calls: list[dict] = []
        self.refusal: Exception | None = None

    def verify(self, mandate_id, **kwargs):
        self.calls.append({"mandateId": mandate_id, **kwargs})
        if self.refusal is not None:
            raise self.refusal
        return {"id": mandate_id}


class FakeService:
    """The subset of `BrokerService` these nodes call, with no broker."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.mandates = FakeMandates()
        self._account = {"available": True, "data": {"equity": 10_000.0, "mode": "demo"}}
        self._positions = {"available": True, "data": [
            {"ticket": 11, "symbol": "EURUSD", "volume": 0.1},
            {"ticket": 12, "symbol": "EURUSD", "volume": 0.2},
            {"ticket": 13, "symbol": "XAUUSD", "volume": 0.1},
        ]}
        self._orders = {"available": True, "data": [
            {"ticket": 21, "symbol": "EURUSD", "type": "buy limit"},
        ]}
        self._fills = [{"dealTicket": 31, "symbol": "EURUSD", "profit": 12.0}]
        self._ledger = [{"id": "ord_1", "status": "filled", "nodeId": "order_node"}]
        self._preview = {
            "available": True, "allowed": True, "mode": "demo", "connectionId": "brk_1",
            "intent": {"symbol": "EURUSD"},
            "preview": {"margin": 25.0, "reasons": []},
            "reasons": [],
        }
        self._bars = {"available": True, "data": {
            "symbol": "XAUUSD", "timeframe": "H1", "contractSize": 100, "tickSize": 0.01,
            "bars": [
                {"high": 10.0 + index, "low": 9.0 + index, "close": 9.5 + index}
                for index in range(30)
            ],
        }}
        for key, value in overrides.items():
            setattr(self, key, value)

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def account(self, connection_id):
        self._record("account", connection_id)
        return self._account

    def positions(self, connection_id):
        self._record("positions", connection_id)
        return self._positions

    def orders(self, connection_id):
        self._record("orders", connection_id)
        return self._orders

    def list_fills(self, connection_id, limit=100):
        self._record("list_fills", connection_id, limit=limit)
        return self._fills

    def ledger_orders(self, connection_id=None, limit=50):
        self._record("ledger_orders", connection_id, limit=limit)
        return self._ledger

    def preview_order(self, connection_id, payload, *, record_approval=True):
        self._record("preview_order", connection_id, payload, record_approval=record_approval)
        if record_approval:
            raise AssertionError("a graph preview must not create an approval")
        return self._preview

    def bars(self, connection_id, symbol, timeframe="H1", count=120):
        self._record("bars", connection_id, symbol, timeframe=timeframe, count=count)
        return self._bars

    def close_positions(self, connection_id, *, tickets=None, deviation=20):
        self._record("close_positions", connection_id, tickets=tickets, deviation=deviation)
        results = [{"ok": True, "ticket": ticket} for ticket in (tickets or [])]
        return {
            "available": True,
            "closed": len(results),
            "failed": 0,
            "requested": len(results),
            "results": results,
        }

    def close_position(self, connection_id, ticket, *, volume=None, deviation=20):
        self._record("close_position", connection_id, ticket, volume=volume, deviation=deviation)
        return {
            "available": True,
            "closed": True,
            "result": {"ok": True, "ticket": ticket, "verified": True},
        }

    def modify_position(self, connection_id, ticket, *, stop_loss=None, take_profit=None):
        self._record("modify_position", connection_id, ticket, stop_loss=stop_loss)
        return {
            "available": True,
            "modified": False,
            "result": {"ok": False, "retcodeName": "INVALID_STOPS", "reasons": ["invalid stops"]},
        }

    def modify_order(self, connection_id, ticket, *, price=None, stop_loss=None, take_profit=None):
        self._record("modify_order", connection_id, ticket, price=price,
                     stop_loss=stop_loss, take_profit=take_profit)
        return {
            "available": True,
            "modified": True,
            "result": {"ok": True, "price": price or 1.09, "verified": True},
        }

    def symbol_info(self, connection_id, symbol):
        self._record("symbol_info", connection_id, symbol)
        return {
            "available": True,
            "data": {"symbol": str(symbol).upper(), "tradeMode": "full",
                     "contractSize": 100_000, "volumeStep": 0.01},
        }

    def submit_order(self, connection_id, payload, **kwargs):
        self._record("submit_order", connection_id, payload, **kwargs)
        return {"status": "filled", "orderId": "ord_1", "sent": True, "filled": True}


@pytest.fixture
def service():
    fake = FakeService()
    bootstrap.set_service(fake)
    try:
        yield fake
    finally:
        bootstrap.set_service(None)


def _exec(node_type: str, config: dict, inputs: dict, node_id: str = "n1"):
    definition = REGISTRY.get(node_type)
    assert definition is not None and definition.executor is not None
    return definition.executor(NodeExecContext(
        node_id=node_id, run_id="run", mode=ExecutionMode.PAPER,
        inputs=inputs, config=config,
    ))


def test_a_read_is_ok_only_when_the_broker_answered(service):
    assert _exec("trading.broker_account", {"connectionId": "brk_1"}, {})["ok"] is True

    service._account = {
        "available": False,
        "status": "UNAVAILABLE",
        "error": {"code": "TERMINAL_NOT_RUNNING", "reason": "no terminal"},
    }
    result = _exec("trading.broker_account", {"connectionId": "brk_1"}, {})
    assert result["ok"] is False
    assert result["reason"] == "TERMINAL_NOT_RUNNING: no terminal"
    assert result["account"]["available"] is False


def test_broker_positions_returns_the_rows_and_a_count(service):
    result = _exec("trading.broker_positions", {"connectionId": "brk_1"}, {})
    assert result["ok"] is True
    assert result["count"] == 3
    assert result["positions"][0]["ticket"] == 11


def test_an_unavailable_position_read_is_empty_and_names_the_reason(service):
    service._positions = {"available": False, "error": {"code": "TERMINAL_NOT_RUNNING"}}
    result = _exec("trading.broker_positions", {"connectionId": "brk_1"}, {})
    assert result["ok"] is False
    assert result["positions"] == []
    assert result["count"] == 0
    assert result["reason"] == "TERMINAL_NOT_RUNNING"


def test_broker_orders_and_fills_are_lists(service):
    working = _exec("trading.broker_orders", {"connectionId": "brk_1"}, {})
    assert working["ok"] is True
    assert working["count"] == 1
    assert working["orders"][0]["ticket"] == 21

    fills = _exec("trading.broker_fills", {"connectionId": "brk_1", "limit": 25}, {})
    assert fills["ok"] is True
    assert fills["count"] == 1
    assert fills["fills"][0]["dealTicket"] == 31
    assert service.calls[-1][2]["limit"] == 25


def test_the_ledger_reads_every_connection_when_none_is_pinned(service):
    result = _exec("trading.order_ledger", {"connectionId": "", "limit": 10}, {})
    assert result["ok"] is True
    assert result["orders"][0]["id"] == "ord_1"
    assert service.calls[-1][1][0] is None


def test_order_intent_builds_the_shape_both_adapters_read():
    result = _exec(
        "trading.order_intent",
        {"symbol": "eurusd", "side": "buy", "orderType": "limit", "quantity": 0.5, "price": 1.09},
        {},
    )
    intent = result["intent"]
    assert intent["symbol"] == "EURUSD"
    assert intent["quantity"] == intent["volume"] == 0.5
    assert intent["type"] == intent["orderType"] == "limit"
    assert intent["price"] == 1.09
    assert intent["timeInForce"] == "gtc"


def test_order_intent_takes_wired_values_over_config():
    result = _exec(
        "trading.order_intent",
        {"symbol": "XAUUSD", "side": "sell", "quantity": 9.0, "orderType": "market"},
        {"side": "buy", "quantity": 0.1, "stopLoss": 4010.0, "takeProfit": 3990.0},
    )
    assert result["intent"]["side"] == "buy"
    assert result["intent"]["quantity"] == 0.1
    assert result["intent"]["stopLoss"] == 4010.0
    assert "price" not in result["intent"]  # a market order carries no price


def test_order_intent_refuses_a_pending_order_without_a_price():
    with pytest.raises(ValueError, match="needs a positive price"):
        _exec(
            "trading.order_intent",
            {"symbol": "EURUSD", "side": "buy", "orderType": "limit", "quantity": 0.1},
            {},
        )


def test_order_intent_needs_a_symbol_and_a_positive_quantity():
    with pytest.raises(ValueError, match="symbol"):
        _exec("trading.order_intent", {"side": "buy", "quantity": 1.0}, {})
    with pytest.raises(ValueError, match="quantity"):
        _exec("trading.order_intent", {"symbol": "EURUSD", "side": "buy", "quantity": 0}, {})


def test_an_allowed_preview_creates_no_approval_and_reports_it(service):
    result = _exec(
        "trading.order_preview", {"connectionId": "brk_1"},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1}},
    )
    assert result["ok"] is True
    assert result["allowed"] is True
    name, _args, kwargs = service.calls[-1]
    assert name == "preview_order"
    assert kwargs["record_approval"] is False


def test_a_refused_preview_is_an_outcome_with_reasons(service):
    service._preview = {
        "available": True, "allowed": False, "mode": "demo",
        "reasons": ["margin cap exceeded"],
    }
    result = _exec(
        "trading.order_preview", {"connectionId": "brk_1"},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1}},
    )
    assert result["ok"] is True
    assert result["allowed"] is False
    assert result["reason"] == "margin cap exceeded"


def test_a_mandate_that_does_not_cover_the_order_refuses_the_preview(service):
    from harness.trade.model import TradeRefused

    service.mandates.refusal = TradeRefused(
        "MANDATE_SYMBOL_NOT_ALLOWED", "the mandate does not allow EURUSD",
    )
    result = _exec(
        "trading.order_preview", {"connectionId": "brk_1", "mandateId": "mnd_1"},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1}},
    )
    assert result["allowed"] is False
    assert "does not allow EURUSD" in result["reason"]
    # verify only — a preview never consumes a mandate use.
    assert service.mandates.calls[0]["mandateId"] == "mnd_1"


def test_a_dry_run_broker_order_previews_and_sends_nothing(service):
    result = _exec(
        "trading.broker_order",
        {"connectionId": "brk_1", "mandateId": "mnd_1", "dryRun": True},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1}},
    )
    assert result["status"] == "PREVIEW_ALLOWED"
    assert result["ok"] is True
    assert result["orderId"] == ""
    assert [call[0] for call in service.calls] == ["preview_order"]


def test_a_preview_of_an_unusable_intent_is_an_outcome_not_an_exception(service):
    result = _exec(
        "trading.order_preview", {"connectionId": "brk_1"},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0}},
    )
    assert result["ok"] is False
    assert result["allowed"] is False
    assert "quantity" in result["reason"]
    assert service.calls == []


def test_broker_bars_adds_the_same_atr_the_chat_tool_computes(service):
    result = _exec(
        "trading.broker_bars", {"connectionId": "brk_1", "symbol": "XAUUSD"}, {},
    )
    assert result["ok"] is True
    assert result["atr"] == pytest.approx(1.5)
    assert result["contractSize"] == 100
    assert result["summary"]["lookback"] == 20
    assert result["summary"]["atrPeriod"] == 14


def test_broker_bars_is_not_ok_without_a_series(service):
    service._bars = {"available": False, "error": {"code": "BARS_UNAVAILABLE"}}
    result = _exec("trading.broker_bars", {"connectionId": "brk_1", "symbol": "XAUUSD"}, {})
    assert result["ok"] is False
    assert result["reason"] == "BARS_UNAVAILABLE"
    assert result["atr"] is None


def test_close_by_symbol_closes_only_that_symbol(service):
    result = _exec(
        "trading.broker_close", {"connectionId": "brk_1"}, {"symbol": "eurusd"},
    )
    assert result["ok"] is True
    name, args, kwargs = service.calls[-1]
    assert name == "close_positions"
    assert kwargs["tickets"] == [11, 12]


def test_a_rest_ticket_survives_without_int_coercion(service):
    # A REST venue's position id is opaque; the node must pass it through, not
    # parse it as a number.
    result = _exec(
        "trading.broker_close", {"connectionId": "brk_1"}, {"ticket": "BTC-USD"},
    )
    assert result["ok"] is True
    name, args, kwargs = service.calls[-1]
    assert name == "close_position"
    assert args[1] == "BTC-USD"


def test_close_by_symbol_is_a_named_outcome_when_nothing_is_held(service):
    result = _exec("trading.broker_close", {"connectionId": "brk_1"}, {"symbol": "GBPUSD"})
    assert result["ok"] is False
    assert result["status"] == "no_position"
    assert "GBPUSD" in result["reason"]


def test_a_working_order_can_be_re_priced(service):
    result = _exec(
        "trading.broker_modify_order", {"connectionId": "brk_1"},
        {"ticket": 21, "price": 1.12, "stopLoss": 1.11},
    )
    assert result["ok"] is True
    assert result["status"] == "modified"
    name, _args, kwargs = service.calls[-1]
    assert name == "modify_order"
    assert kwargs["price"] == pytest.approx(1.12)
    assert kwargs["stop_loss"] == pytest.approx(1.11)


def test_modify_order_needs_a_field_to_change(service):
    with pytest.raises(ValueError, match="pass a price"):
        _exec(
            "trading.broker_modify_order", {"connectionId": "brk_1"}, {"ticket": 21},
        )


def test_broker_symbol_reports_the_trade_mode(service):
    result = _exec(
        "trading.broker_symbol", {"connectionId": "brk_1", "symbol": "xauusd"}, {},
    )
    assert result["ok"] is True
    assert result["tradeMode"] == "full"
    assert result["symbol"]["data"]["contractSize"] == 100_000


def test_a_refused_modify_surfaces_the_broker_reason(service):
    result = _exec(
        "trading.broker_modify", {"connectionId": "brk_1"},
        {"ticket": 11, "stopLoss": 1.2},
    )
    assert result["ok"] is False
    assert result["reason"] == "invalid stops"
    assert result["status"] == "rejected"


def test_a_rejected_risk_decision_stops_the_order_before_the_broker(service):
    result = _exec(
        "trading.broker_order",
        {"connectionId": "brk_1", "mandateId": "mnd_1"},
        {
            "intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1},
            "decision": {"allowed": False, "reasons": ["daily loss brake"]},
        },
    )
    assert result["status"] == "REJECTED"
    assert result["ok"] is False
    assert result["reason"] == "daily loss brake"
    assert service.calls == []


def test_an_approved_decision_reaches_the_broker_with_its_traceability(service):
    result = _exec(
        "trading.broker_order",
        {"connectionId": "brk_1", "mandateId": "mnd_1"},
        {
            "intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1},
            "decision": {"allowed": True},
        },
        node_id="order_node",
    )
    assert result["ok"] is True
    assert result["orderId"] == "ord_1"
    name, args, kwargs = service.calls[-1]
    assert name == "submit_order"
    assert kwargs["node_id"] == "order_node"
    assert kwargs["mandate_id"] == "mnd_1"


def test_a_refused_order_is_not_ok(service):
    def refused(connection_id, payload, **kwargs):
        return {"status": "REFUSED", "sent": False, "reasons": ["margin cap exceeded"]}

    service.submit_order = refused
    result = _exec(
        "trading.broker_order",
        {"connectionId": "brk_1", "mandateId": "mnd_1"},
        {"intent": {"symbol": "EURUSD", "side": "buy", "quantity": 0.1}},
    )
    assert result["ok"] is False
    assert result["reason"] == "margin cap exceeded"
