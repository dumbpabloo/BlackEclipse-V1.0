"""Deribit execution: JSON-RPC order params, ownership label, close-by-position."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import TradingAdapter
from harness.trade.adapters.deribit import DeribitAdapter
from harness.trade.model import OrderIntent

POSITION = {
    "instrument_name": "BTC-PERPETUAL", "kind": "future", "direction": "sell",
    "size": -10.0, "average_price": 60000, "mark_price": 61000,
    "floating_profit_loss": 100.0, "timestamp": 1700000000000,
}
OPEN_ORDERS = [
    {"order_id": "D-1", "instrument_name": "BTC-PERPETUAL", "order_type": "limit",
     "amount": 10.0, "filled_amount": 0.0, "price": 60000, "order_state": "open",
     "direction": "buy", "creation_timestamp": 1700000000000,
     "label": "eclipse-77-abc123456789"},
    {"order_id": "D-2", "instrument_name": "BTC-PERPETUAL", "order_type": "limit",
     "amount": 1.0, "filled_amount": 0.0, "price": 50000, "order_state": "open",
     "direction": "buy", "creation_timestamp": 1700000000000,
     "label": "manual-1"},
]


def _order(order_id="D-1", state="open", **extra):
    row = {
        "order_id": order_id, "instrument_name": "BTC-PERPETUAL", "order_type": "limit",
        "order_state": state, "filled_amount": 0.0, "average_price": 0.0,
        "price": 60000, "label": "eclipse-77-abc123456789",
    }
    row.update(extra)
    return row


class Handler(BaseHTTPRequestHandler):
    submitted: list = []
    cancelled: list = []
    edited: list = []
    state = "open"
    cancel_state = "cancelled"

    def log_message(self, *args):
        return

    def _send(self, payload, status=200):
        body = json.dumps({"jsonrpc": "2.0", "result": payload}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if self.path.startswith("/api/v2/private/") and not auth.startswith(
                "deri-hmac-sha256 id="):
            self.send_response(401)
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if path == "/api/v2/private/get_account_summary":
            self._send({"currency": "BTC", "balance": 2.0, "equity": 2.5,
                        "available_funds": 2.0, "session_upl": 0.5})
        elif path == "/api/v2/private/get_positions":
            self._send([POSITION])
        elif path == "/api/v2/private/get_open_orders_by_currency":
            self._send(OPEN_ORDERS)
        elif path == "/api/v2/public/get_instruments":
            self._send([{"instrument_name": "BTC-PERPETUAL", "kind": "future",
                         "tick_size": 0.5, "min_trade_amount": 10, "contract_size": 10,
                         "settlement_currency": "BTC", "base_currency": "BTC",
                         "is_active": True}])
        elif path == "/api/v2/public/ticker":
            self._send({"last_price": 60000.5, "best_bid_price": 59999,
                        "best_ask_price": 60001, "timestamp": 1700000000000})
        elif path in ("/api/v2/private/buy", "/api/v2/private/sell"):
            Handler.submitted.append((path, params))
            self._send({"order": _order(order_id="D-3")})
        elif path == "/api/v2/private/cancel":
            Handler.cancelled.append(params)
            self._send({"order": _order(order_id=params["order_id"][0],
                                        state=Handler.cancel_state)})
        elif path == "/api/v2/private/edit":
            Handler.edited.append(params)
            self._send({"order": _order(order_id=params["order_id"][0], state="open")})
        elif path == "/api/v2/private/get_order_state":
            self._send(_order(order_id=params["order_id"][0], state=Handler.state))
        else:
            self._send(None, 404)

    def do_POST(self):  # noqa: N802
        self._send(None, 405)


@pytest.fixture()
def api(monkeypatch):
    Handler.submitted, Handler.cancelled, Handler.edited = [], [], []
    Handler.state = "open"
    Handler.cancel_state = "cancelled"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_DERIBIT_EXEC", "client-1:secret-2")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> DeribitAdapter:
    return DeribitAdapter({"baseUrl": url, "credentialRef": "TEST_DERIBIT_EXEC"})


def _intent(**overrides) -> OrderIntent:
    values = dict(connection_id="conn", symbol="BTC-PERPETUAL", side="buy",
                  order_type="limit", volume=10, price=60000, magic=77)
    values.update(overrides)
    return OrderIntent(**values)


def test_the_adapter_is_a_trading_adapter_with_a_demo_mode(monkeypatch):
    adapter = DeribitAdapter({
        "baseUrl": "https://testnet.deribit.com",
        "credentialRef": "TEST_DERIBIT_EXEC",
    })
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "currency": "BTC", "equity": 1.0, "available_funds": 1.0})
    account = adapter.account()
    assert isinstance(adapter, TradingAdapter)
    assert account["mode"] == "demo"
    assert account["tradeAllowed"] is True


def test_preview_normalises_the_contract_and_bracket(api):
    preview = adapter_for(api).order_preview(
        _intent(stop_loss=59000, take_profit=62000))
    assert preview["ok"] is True
    assert preview["volume"] == 10
    assert preview["price"] == 60000
    assert preview["notional"] == 600000
    assert preview["margin"] == 600000
    assert preview["openPositions"] == 1
    assert preview["volumeMin"] == 10


def test_submit_sends_signed_buy_params_with_stops_and_label(api):
    result = adapter_for(api).submit_order(
        _intent(stop_loss=59000, take_profit=62000))
    assert result["ok"] is True
    assert result["status"] == "placed"
    assert result["ticket"] == "D-3"
    path, params = Handler.submitted[-1]
    assert path == "/api/v2/private/buy"
    assert params["instrument_name"] == ["BTC-PERPETUAL"]
    assert params["amount"] == ["10"]
    assert params["type"] == ["limit"]
    assert params["price"] == ["60000"]
    assert params["stop_loss"] == ["59000"]
    assert params["take_profit"] == ["62000"]
    assert params["label"][0].startswith("eclipse-77-")


def test_a_sell_uses_the_sell_endpoint(api):
    result = adapter_for(api).submit_order(_intent(side="sell", price=61000))
    assert result["ok"] is True
    assert Handler.submitted[-1][0] == "/api/v2/private/sell"


def test_a_wrong_side_stop_is_refused_without_sending(api):
    result = adapter_for(api).submit_order(_intent(stop_loss=61000))
    assert result["ok"] is False
    assert result["retcodeName"] == "PREVIEW_REJECTED"
    assert Handler.submitted == []


def test_cancel_names_the_order_and_reads_the_venue_state(api):
    result = adapter_for(api).cancel_order("D-1")
    assert result["ok"] is True
    assert result["retcodeName"] == "CANCELLED"
    assert Handler.cancelled[-1]["order_id"] == ["D-1"]


def test_a_filled_order_is_not_reported_as_cancelled(api):
    Handler.cancel_state = "filled"
    result = adapter_for(api).cancel_order("D-1")
    assert result["ok"] is False
    assert result["retcodeName"] == "TOO_LATE"


def test_close_submits_the_opposite_reduce_only_order(api):
    result = adapter_for(api).close_position("BTC-PERPETUAL:sell")
    assert result["ok"] is True
    assert result["side"] == "buy"
    assert result["volume"] == 10
    assert result["reduceOnly"] is True
    # An open (unfilled) close is not verified as closed.
    assert result["verified"] is False
    path, params = Handler.submitted[-1]
    assert path == "/api/v2/private/buy"
    assert params["amount"] == ["10"]
    assert params["reduce_only"] == ["true"]


def test_cancel_all_only_touches_eclipse_labels(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert [params["order_id"][0] for params in Handler.cancelled] == ["D-1"]


def test_modify_order_edits_the_working_order(api):
    result = adapter_for(api).modify_order("D-1", price=60500, stop_loss=59000)
    assert result["ok"] is True
    assert result["retcodeName"] == "REPLACED"
    params = Handler.edited[-1]
    assert params["order_id"] == ["D-1"]
    assert params["price"] == ["60500"]
    assert params["stop_loss"] == ["59000"]


def test_modify_position_refuses_because_stops_live_on_orders(api):
    result = adapter_for(api).modify_position("BTC-PERPETUAL:sell", stop_loss=62000)
    assert result["ok"] is False
    assert result["retcodeName"] == "POSITION_BRACKET_UNSUPPORTED"
    assert Handler.edited == []


def test_order_status_and_symbol_info_come_from_the_venue(api):
    adapter = adapter_for(api)
    status = adapter.order_status("D-1")
    assert status["found"] is True
    assert status["state"] == "open"
    info = adapter.symbol_info("btc-perpetual")
    assert info["symbol"] == "BTC-PERPETUAL"
    assert info["tickSize"] == 0.5
    assert info["volumeMin"] == 10
    assert info["volumeStep"] == 0.0       # not published: reported as 0, not invented
