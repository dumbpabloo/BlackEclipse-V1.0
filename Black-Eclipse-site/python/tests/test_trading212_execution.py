"""Trading212 execution: price-reference previews, one send, honest refusals."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.trading212 import Trading212Adapter
from harness.trade.model import OrderIntent

CASH = {"free": 1250.5, "total": 9800.0, "invested": 8500.0, "ppl": 49.5}
PORTFOLIO = [
    {"ticker": "AAPL_US_EQ", "quantity": 3, "averagePrice": 180.0,
     "currentPrice": 190.0, "ppl": 30.0, "fxPpl": 0.0},
]
ORDERS = [
    {"id": 11, "ticker": "VUSA_LSE_EQ", "quantity": -2, "type": "LIMIT",
     "limitPrice": 80.5, "status": "NEW"},
]
INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "name": "Apple Inc", "isin": "US0378331005",
     "type": "STOCK", "currencyCode": "USD", "maxOpenQuantity": 1000},
]


class Handler(BaseHTTPRequestHandler):
    SENT: list = []
    DELETED: list = []

    def log_message(self, *args):
        return

    def _path(self) -> str:
        path = self.path.partition("?")[0]
        return path[len("/api/v0"):] if path.startswith("/api/v0") else path

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self._path()
        if path == "/equity/account/cash":
            self._send(200, CASH)
        elif path == "/equity/portfolio":
            self._send(200, PORTFOLIO)
        elif path == "/equity/orders":
            self._send(200, ORDERS)
        elif path == "/equity/metadata/instruments":
            self._send(200, INSTRUMENTS)
        elif path.startswith("/equity/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            row = next((o for o in ORDERS if str(o["id"]) == order_id), None)
            self._send(200, row) if row else self._send(404, {"message": "not found"})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        assert self.headers.get("Authorization", "").startswith("Basic ")
        Handler.SENT.append({"path": path, "body": body})
        self._send(200, {
            "id": "ord-1", "type": "LIMIT" if path.endswith("limit") else "MARKET",
            "ticker": body.get("ticker"), "quantity": body.get("quantity"),
            "status": "NEW", "filledQuantity": 0,
        })

    def do_DELETE(self):  # noqa: N802
        path = self._path()
        order_id = path.rsplit("/", 1)[-1]
        if order_id == "999":
            self._send(404, {"message": "not found"})
            return
        Handler.DELETED.append(order_id)
        self._send(200, {})


@pytest.fixture()
def api(monkeypatch):
    Handler.SENT = []
    Handler.DELETED = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/v0"
    monkeypatch.setenv("TEST_T212_KEY", "demo-key-123")
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> Trading212Adapter:
    return Trading212Adapter({"baseUrl": url, "credentialRef": "TEST_T212_KEY"})


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="AAPL_US_EQ", side="buy", order_type="limit",
        volume=2, price=100.0, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


# ── reads / mode ─────────────────────────────────────────────────────────────

def test_mode_maps_the_demo_host_and_absence_of_a_block_is_tradeable(api, monkeypatch):
    assert adapter_for(api).account()["mode"] == "real"
    demo = Trading212Adapter({
        "baseUrl": "https://demo.trading212.com/api/v0", "credentialRef": "TEST_T212_KEY",
    })
    monkeypatch.setattr(demo, "_get", lambda path: CASH)
    assert demo.account()["mode"] == "demo"
    assert demo.account()["tradeAllowed"] is True
    monkeypatch.setattr(demo, "_get", lambda path: {**CASH, "blocked": True})
    assert demo.account()["tradeAllowed"] is False


def test_positions_and_orders_carry_string_tickets(api):
    adapter = adapter_for(api)
    assert adapter.positions()[0]["ticket"] == "AAPL_US_EQ"
    assert adapter.orders()[0]["ticket"] == "11"


def test_symbol_info_comes_from_instrument_metadata(api):
    info = adapter_for(api).symbol_info("AAPL_US_EQ")
    assert info["volumeMax"] == 1000
    assert info["currencyBase"] == "USD"
    assert info["contractSize"] == 1.0


# ── preview ──────────────────────────────────────────────────────────────────

def test_a_limit_preview_prices_from_the_intent_reference(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["price"] == 100.0
    assert preview["notional"] == 200.0
    assert preview["margin"] == 200.0
    assert preview["quote"]["priceSource"] == "intent"


def test_a_market_preview_without_a_reference_is_refused(api):
    preview = adapter_for(api).order_preview(
        _intent(order_type="market", price=None)
    )
    assert preview["ok"] is False
    assert any("TRADING212_QUOTE_UNSUPPORTED" in reason for reason in preview["reasons"])


def test_stop_entries_and_brackets_are_refused_by_name(api):
    adapter = adapter_for(api)
    stop = adapter.order_preview(_intent(order_type="stop", price=101.0))
    assert any("TRADING212_STOP_ENTRY_UNSUPPORTED" in reason for reason in stop["reasons"])
    bracket = adapter.order_preview(_intent(stop_loss=90.0))
    assert any("TRADING212_BRACKET_UNSUPPORTED" in reason for reason in bracket["reasons"])


# ── submit ───────────────────────────────────────────────────────────────────

def test_submit_sends_one_limit_order(api):
    result = adapter_for(api).submit_order(_intent())
    assert len(Handler.SENT) == 1
    sent = Handler.SENT[0]
    assert sent["path"] == "/equity/orders/limit"
    assert sent["body"] == {"ticker": "AAPL_US_EQ", "quantity": 2.0, "limitPrice": 100.0}
    assert result["ok"] is True
    assert result["ticket"] == "ord-1"
    assert result["status"] == "placed"


def test_a_sell_market_order_is_signed_negative(api):
    adapter_for(api).submit_order(
        _intent(side="sell", order_type="market", price=190.0, volume=2)
    )
    sent = Handler.SENT[0]
    assert sent["path"] == "/equity/orders/market"
    assert sent["body"] == {"ticker": "AAPL_US_EQ", "quantity": -2.0}


def test_preview_rejection_never_sends(api):
    result = adapter_for(api).submit_order(_intent(order_type="market", price=None))
    assert result["ok"] is False
    assert result["retcodeName"] == "PREVIEW_REJECTED"
    assert Handler.SENT == []


# ── cancel / close ───────────────────────────────────────────────────────────

def test_cancel_by_id_and_a_404_is_not_success(api):
    adapter = adapter_for(api)
    assert adapter.cancel_order("ord-1")["ok"] is True
    missing = adapter.cancel_order("999")
    assert missing["ok"] is False
    assert missing["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_refuses_without_ownership(api):
    result = adapter_for(api).cancel_all(77)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert Handler.DELETED == []


def test_close_sends_the_opposite_market_order(api):
    result = adapter_for(api).close_position("AAPL_US_EQ")
    assert Handler.SENT[0]["path"] == "/equity/orders/market"
    assert Handler.SENT[0]["body"] == {"ticker": "AAPL_US_EQ", "quantity": -3.0}
    assert result["ok"] is True
    assert result["verified"] is False       # the venue reported no fill
    assert result["volume"] == pytest.approx(3)


def test_partial_close_uses_the_requested_volume(api):
    result = adapter_for(api).close_position("AAPL_US_EQ", volume=1)
    assert Handler.SENT[0]["body"]["quantity"] == -1.0
    assert result["partial"] is True


def test_modify_paths_are_named_refusals(api):
    adapter = adapter_for(api)
    assert adapter.modify_order("11", price=79.0)["retcodeName"] == "ORDER_MODIFY_UNSUPPORTED"
    assert adapter.modify_position("AAPL_US_EQ", stop_loss=170.0)["retcodeName"] == (
        "POSITION_BRACKET_UNSUPPORTED"
    )
    assert Handler.SENT == []
