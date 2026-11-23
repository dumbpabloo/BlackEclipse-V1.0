"""OANDA v20 execution: preview rules, one-send submit, cancel, close, ownership."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.oanda import OandaAdapter
from harness.trade.model import OrderIntent

ACCOUNT_ID = "101-001-1"
SUMMARY = {
    "account": {
        "NAV": "10500.0", "balance": "10000.0", "marginAvailable": "9000.0",
        "marginUsed": "1500.0", "unrealizedPL": "500.0", "currency": "USD",
        "marginRate": "0.05", "marginCloseoutPercent": "0.10",
    }
}
PRICING = {"prices": [{
    "instrument": "EUR_USD", "tradeable": True,
    "bids": [{"price": "1.10000"}], "asks": [{"price": "1.10020"}],
}]}
POSITIONS = {"positions": [{
    "instrument": "EUR_USD",
    "long": {"units": "10000", "averagePrice": "1.0950", "unrealizedPL": "52.0", "financing": "-0.5"},
    "short": {"units": "0", "averagePrice": "0", "unrealizedPL": "0", "financing": "0"},
}]}
INSTRUMENTS = {"instruments": [{
    "name": "EUR_USD", "displayName": "EUR/USD", "type": "CURRENCY",
    "displayPrecision": 5, "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
    "maximumOrderUnits": "100000000", "marginRate": "0.05",
}]}
PENDING = {"orders": [
    {"id": "77", "instrument": "EUR_USD", "type": "LIMIT", "units": "1000",
     "price": "1.09000", "state": "PENDING", "createTime": "1700000000000000",
     "clientExtensions": {"id": "eclipse-77-abc123456789"}},
    {"id": "88", "instrument": "EUR_USD", "type": "LIMIT", "units": "1000",
     "price": "1.08000", "state": "PENDING", "clientExtensions": {"id": "manual-1"}},
]}


class Handler(BaseHTTPRequestHandler):
    SENT: list = []
    CANCELLED: list = []

    def log_message(self, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == f"/v3/accounts/{ACCOUNT_ID}/summary":
            self._send(200, SUMMARY)
        elif path == f"/v3/accounts/{ACCOUNT_ID}/pricing":
            assert "EUR_USD" in query
            self._send(200, PRICING)
        elif path == f"/v3/accounts/{ACCOUNT_ID}/openPositions":
            self._send(200, POSITIONS)
        elif path == f"/v3/accounts/{ACCOUNT_ID}/instruments":
            self._send(200, INSTRUMENTS)
        elif path == f"/v3/accounts/{ACCOUNT_ID}/pendingOrders":
            self._send(200, PENDING)
        elif path == f"/v3/accounts/{ACCOUNT_ID}/orders/77":
            self._send(200, {"order": {"id": "77", "state": "PENDING",
                                       "units": "1000",
                                       "clientExtensions": {"id": "eclipse-77-abc123456789"}}})
        else:
            self._send(404, {"errorMessage": "not found"})

    def do_POST(self):  # noqa: N802
        assert self.path == f"/v3/accounts/{ACCOUNT_ID}/orders"
        assert self.headers.get("Authorization") == "Bearer tok-123"
        body = self._body()
        Handler.SENT.append(body)
        order = body.get("order") or {}
        client_id = (order.get("clientExtensions") or {}).get("id")
        if order.get("type") == "MARKET":
            self._send(201, {"orderFillTransaction": {
                "id": "fill-1", "orderID": "ord-1", "price": "1.10030",
                "units": order.get("units"), "clientExtensions": {"id": client_id},
            }})
        else:
            self._send(201, {"orderCreateTransaction": {
                "id": "ord-2", "clientExtensions": {"id": client_id},
            }})

    def do_PUT(self):  # noqa: N802
        path, _, _query = self.path.partition("?")
        body = self._body() if self.headers.get("Content-Length") else {}
        if path.endswith("/cancel"):
            order_id = path.split("/")[-2]
            if order_id == "404":
                self._send(404, {"errorMessage": "no such order"})
                return
            Handler.CANCELLED.append(order_id)
            self._send(200, {"orderCancelTransaction": {"id": order_id, "reason": "CANCELLED"}})
        elif "/positions/" in path and path.endswith("/close"):
            units = body.get("longUnits") or body.get("shortUnits")
            signed = units if body.get("longUnits") else f"-{units}"
            self._send(200, {"longOrderFillTransaction": {
                "id": "close-1", "orderID": "close-1", "price": "1.09980",
                "units": "10000" if units == "ALL" else signed,
            }})
        else:
            self._send(200, {"orderCreateTransaction": {"id": "ord-3"}})


@pytest.fixture()
def api(monkeypatch):
    Handler.SENT = []
    Handler.CANCELLED = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("TEST_OANDA_TOKEN", "tok-123")
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> OandaAdapter:
    return OandaAdapter({
        "baseUrl": url, "accountId": ACCOUNT_ID, "credentialRef": "TEST_OANDA_TOKEN",
    })


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="EUR_USD", side="buy", order_type="limit",
        volume=1234.7, price=1.09, stop_loss=1.08, take_profit=1.11, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


# ── reads / mode ─────────────────────────────────────────────────────────────

def test_mode_is_demo_on_the_practice_host_and_real_elsewhere(api, monkeypatch):
    assert adapter_for(api).account()["mode"] == "real"
    practice = OandaAdapter({
        "baseUrl": "https://api-fxpractice.oanda.com", "accountId": ACCOUNT_ID,
        "credentialRef": "TEST_OANDA_TOKEN",
    })
    monkeypatch.setattr(practice, "_get", lambda path, params=None: SUMMARY)
    account = practice.account()
    assert account["mode"] == "demo"
    assert account["tradeAllowed"] is True


def test_positions_carry_an_instrument_and_side_ticket(api):
    rows = adapter_for(api).positions()
    assert rows[0]["ticket"] == "EUR_USD:LONG"
    assert rows[0]["symbol"] == "EURUSD"
    assert adapter_for(api).orders()[0]["ticket"] == "77"


# ── preview ──────────────────────────────────────────────────────────────────

def test_preview_normalises_units_and_computes_margin(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["volume"] == 1234          # rounded DOWN to whole units
    assert preview["price"] == 1.09
    assert preview["notional"] == pytest.approx(1234 * 1.09)
    assert preview["margin"] == pytest.approx(1234 * 1.09 * 0.05)
    assert preview["openPositions"] == 1
    assert preview["digits"] == 5


def test_a_limit_on_the_wrong_side_is_refused_by_name(api):
    preview = adapter_for(api).order_preview(_intent(price=1.20, stop_loss=None, take_profit=None))
    assert preview["ok"] is False
    assert any("buy limit must be below" in reason for reason in preview["reasons"])


def test_day_orders_are_refused_rather_than_widened(api):
    preview = adapter_for(api).order_preview(_intent(time_in_force="day"))
    assert any("no day/session order" in reason for reason in preview["reasons"])


# ── submit ───────────────────────────────────────────────────────────────────

def test_submit_sends_exactly_one_market_order(api):
    result = adapter_for(api).submit_order(
        _intent(order_type="market", price=None, volume=1000)
    )
    assert len(Handler.SENT) == 1
    order = Handler.SENT[0]["order"]
    assert order["type"] == "MARKET"
    assert order["units"] == "1000"
    assert order["timeInForce"] == "FOK"
    assert order["takeProfitOnFill"]["price"] == "1.11000"
    assert order["clientExtensions"]["id"].startswith("eclipse-77-")
    assert result["ok"] is True
    assert result["status"] == "FILLED"
    assert result["ticket"] == "ord-1"
    assert result["price"] == pytest.approx(1.1003)


def test_submit_sends_exactly_one_pending_limit_order(api):
    result = adapter_for(api).submit_order(_intent())
    assert len(Handler.SENT) == 1
    order = Handler.SENT[0]["order"]
    assert order["type"] == "LIMIT"
    assert order["price"] == "1.09000"
    assert order["stopLossOnFill"]["price"] == "1.08000"
    assert result["status"] == "placed"
    assert result["ticket"] == "ord-2"


def test_preview_rejection_never_reaches_the_venue(api):
    result = adapter_for(api).submit_order(_intent(price=1.30))
    assert result["ok"] is False
    assert result["retcodeName"] == "PREVIEW_REJECTED"
    assert Handler.SENT == []


# ── cancel / close ───────────────────────────────────────────────────────────

def test_cancel_by_full_id_and_a_404_is_not_success(api):
    adapter = adapter_for(api)
    assert adapter.cancel_order("77")["ok"] is True
    missing = adapter.cancel_order("404")
    assert missing["ok"] is False
    assert missing["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_only_touches_eclipse_orders(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert Handler.CANCELLED == ["77"]


def test_close_full_position_by_side_ticket(api):
    result = adapter_for(api).close_position("EUR_USD:LONG")
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["volume"] == pytest.approx(10000)
    assert result["price"] == pytest.approx(1.0998)


def test_close_partial_position_sends_units(api):
    result = adapter_for(api).close_position("EUR_USD:LONG", volume=4000)
    assert result["ok"] is True
    assert result["partial"] is True


def test_close_without_a_side_uses_the_only_open_side(api):
    result = adapter_for(api).close_position("EURUSD")
    assert result["ok"] is True
    assert result["side"] == "buy"


def test_order_status_reads_the_pending_order(api):
    status = adapter_for(api).order_status("77")
    assert status["found"] is True
    assert status["stateName"] == "PENDING"
    assert adapter_for(api).order_status("999")["found"] is False
