"""Tradier execution: form payload, one send, tag-scoped cancel, close by symbol."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.tradier import TradierAdapter
from harness.trade.model import OrderIntent

ACCOUNT_ID = "VA123"
BALANCES = {"balances": {
    "total_equity": 55000.0, "total_cash": 12500.0, "close_pl": 320.5,
    "margin": {"stock_buying_power": 25000.0}, "trading_blocked": False,
}}
POSITIONS = {"positions": {"position": [
    {"symbol": "AAPL", "quantity": 10.0, "cost_basis": 100.0},
]}}
QUOTES = {"quotes": {"quote": {"bid": 109.9, "ask": 110.1, "last": 110.0}}}
OPEN_ORDERS = [
    {"id": 1, "symbol": "AAPL", "side": "buy", "type": "limit", "quantity": 2,
     "remaining_quantity": 2, "price": 100.0, "status": "open", "tag": "eclipse-77-abc"},
    {"id": 2, "symbol": "MSFT", "side": "buy", "type": "limit", "quantity": 1,
     "remaining_quantity": 1, "price": 400.0, "status": "open", "tag": "manual"},
]


class Handler(BaseHTTPRequestHandler):
    SENT: list = []
    CANCELLED: list = []
    TAGS = True
    CLOSE_QTY = 10.0

    def log_message(self, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == f"/v1/accounts/{ACCOUNT_ID}/balances":
            self._send(200, BALANCES)
        elif path == f"/v1/accounts/{ACCOUNT_ID}/positions":
            self._send(200, POSITIONS)
        elif path == "/v1/markets/quotes":
            if "HALT" in query:
                self._send(200, {"quotes": {"quote": {
                    "bid": 0, "ask": 0, "last": 0, "symbol": "HALT"}}})
            else:
                self._send(200, QUOTES)
        elif path == f"/v1/accounts/{ACCOUNT_ID}/orders":
            rows = OPEN_ORDERS if self.TAGS else [
                {**row, "tag": None} for row in OPEN_ORDERS
            ]
            self._send(200, {"orders": {"order": rows}})
        elif path.startswith(f"/v1/accounts/{ACCOUNT_ID}/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            if order_id == "9002":
                self._send(200, {"order": {
                    "id": 9002, "status": "filled", "symbol": "AAPL",
                    "exec_quantity": Handler.CLOSE_QTY, "avg_fill_price": 110.0,
                }})
            elif order_id == "1":
                self._send(200, {"order": {"id": 1, "status": "open", "type": "limit",
                                           "symbol": "AAPL", "price": 100.0}})
            else:
                self._send(404, {"errors": {"error": "not found"}})
        else:
            self._send(404, {"errors": {"error": "not found"}})

    def do_POST(self):  # noqa: N802
        assert self.path == f"/v1/accounts/{ACCOUNT_ID}/orders"
        assert self.headers.get("Authorization") == "Bearer tok-abc"
        length = int(self.headers.get("Content-Length") or 0)
        form = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                self.rfile.read(length).decode()
            ).items()
        }
        Handler.SENT.append({
            "form": form,
            "contentType": self.headers.get("Content-Type"),
        })
        if form.get("side") == "sell":
            Handler.CLOSE_QTY = float(form.get("quantity") or 10)
            self._send(200, {"order": {"id": 9002, "status": "ok"}})
        else:
            self._send(200, {"order": {"id": 9001, "status": "ok"}})

    def do_DELETE(self):  # noqa: N802
        path, _, _query = self.path.partition("?")
        order_id = path.rsplit("/", 1)[-1]
        if order_id == "9999":
            self._send(404, {"errors": {"error": "not found"}})
            return
        Handler.CANCELLED.append(order_id)
        self._send(200, {"order": {"id": int(order_id), "status": "ok"}})


@pytest.fixture()
def api(monkeypatch):
    Handler.SENT = []
    Handler.CANCELLED = []
    Handler.TAGS = True
    Handler.CLOSE_QTY = 10.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("TEST_TRADIER_TOKEN", "tok-abc")
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> TradierAdapter:
    return TradierAdapter({
        "baseUrl": url, "accountId": ACCOUNT_ID, "credentialRef": "TEST_TRADIER_TOKEN",
    })


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="AAPL", side="buy", order_type="limit",
        volume=2, price=100.0, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


# ── reads / mode ─────────────────────────────────────────────────────────────

def test_mode_maps_sandbox_to_demo(api, monkeypatch):
    assert adapter_for(api).account()["mode"] == "real"
    sandbox = TradierAdapter({
        "baseUrl": "https://sandbox.tradier.com", "accountId": ACCOUNT_ID,
        "credentialRef": "TEST_TRADIER_TOKEN",
    })
    monkeypatch.setattr(sandbox, "_get", lambda path, params=None: BALANCES)
    assert sandbox.account()["mode"] == "demo"
    assert sandbox.account()["tradeAllowed"] is True


def test_positions_and_orders_carry_string_tickets(api):
    adapter = adapter_for(api)
    assert adapter.positions()[0]["ticket"] == "AAPL"
    assert adapter.orders()[0]["ticket"] == "1"


# ── preview ──────────────────────────────────────────────────────────────────

def test_preview_prices_the_order_from_the_quote(api):
    preview = adapter_for(api).order_preview(_intent(order_type="market", price=None))
    assert preview["ok"] is True
    assert preview["price"] == pytest.approx(110.1)
    assert preview["volume"] == 2
    assert preview["margin"] == pytest.approx(220.2)


def test_no_quote_is_named_market_closed(api):
    preview = adapter_for(api).order_preview(
        _intent(symbol="HALT", order_type="market", price=None)
    )
    assert preview["ok"] is False
    assert any("TRADIER_MARKET_CLOSED" in reason for reason in preview["reasons"])


def test_brackets_are_refused_by_name(api):
    preview = adapter_for(api).order_preview(_intent(stop_loss=95.0))
    assert any("TRADIER_BRACKET_UNSUPPORTED" in reason for reason in preview["reasons"])


def test_options_use_the_contract_multiplier(api, monkeypatch):
    adapter = adapter_for(api)
    monkeypatch.setattr(adapter, "quote", lambda symbol: {
        "symbol": symbol, "bid": 1.0, "ask": 1.1, "last": 1.05, "digits": 2, "point": 0.01,
    })
    preview = adapter.order_preview(_intent(
        symbol="AAPL230120C00150000", order_type="limit", price=1.0, volume=2,
    ))
    assert preview["contractSize"] == 100.0
    assert preview["notional"] == pytest.approx(200.0)


# ── submit ───────────────────────────────────────────────────────────────────

def test_submit_sends_one_form_encoded_order_with_the_tag(api):
    result = adapter_for(api).submit_order(_intent())
    assert len(Handler.SENT) == 1
    sent = Handler.SENT[0]
    assert "application/x-www-form-urlencoded" in (sent["contentType"] or "")
    form = sent["form"]
    assert form["class"] == "equity"
    assert form["symbol"] == "AAPL"
    assert form["side"] == "buy"
    assert form["quantity"] == "2"
    assert form["type"] == "limit"
    assert form["duration"] == "gtc"
    assert form["price"] == "100.00"
    assert form["tag"].startswith("eclipse-77-")
    assert result["ok"] is True
    assert result["ticket"] == "9001"
    assert result["status"] == "placed"


def test_preview_rejection_never_sends(api):
    result = adapter_for(api).submit_order(_intent(stop_loss=101.0))
    assert result["ok"] is False
    assert result["retcodeName"] == "PREVIEW_REJECTED"
    assert Handler.SENT == []


# ── cancel / close ───────────────────────────────────────────────────────────

def test_cancel_and_a_404_is_not_success(api):
    adapter = adapter_for(api)
    assert adapter.cancel_order("1")["ok"] is True
    missing = adapter.cancel_order("9999")
    assert missing["ok"] is False
    assert missing["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_only_cancels_tagged_eclipse_orders(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert Handler.CANCELLED == ["1"]


def test_cancel_all_refuses_when_no_order_carries_a_tag(api):
    Handler.TAGS = False
    result = adapter_for(api).cancel_all(77)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert Handler.CANCELLED == []


def test_close_sells_the_whole_position_and_verifies_the_fill(api):
    result = adapter_for(api).close_position("AAPL")
    assert len(Handler.SENT) == 1
    form = Handler.SENT[0]["form"]
    assert form["side"] == "sell"
    assert form["quantity"] == "10"
    assert form["type"] == "market"
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["price"] == pytest.approx(110.0)
    assert result["volume"] == pytest.approx(10)


def test_partial_close_uses_the_requested_quantity(api):
    result = adapter_for(api).close_position("AAPL", volume=4)
    assert Handler.SENT[0]["form"]["quantity"] == "4"
    assert result["partial"] is True


def test_close_refuses_more_than_is_held(api):
    result = adapter_for(api).close_position("AAPL", volume=50)
    assert result["ok"] is False
    assert result["retcodeName"] == "INVALID_VOLUME"
    assert Handler.SENT == []
