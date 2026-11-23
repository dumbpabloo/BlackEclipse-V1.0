"""Coinbase Exchange execution: method+body signature, client_oid, spot close."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.coinbase import CoinbaseAdapter, sign_request
from harness.trade.model import OrderIntent

SECRET = base64.b64encode(b"top-secret").decode()

PRODUCT = {
    "id": "BTC-USD", "display_name": "BTC/USD", "status": "online",
    "base_currency": "BTC", "quote_currency": "USD",
    "base_min_size": "0.0001", "base_max_size": "9000",
    "base_increment": "0.00000001", "quote_increment": "0.01",
    "trading_disabled": False,
}

ACCOUNTS = [
    {"currency": "BTC", "balance": "0.5", "available": "0.4", "trading_enabled": True},
    {"currency": "USD", "balance": "1000", "available": "1000", "trading_enabled": True},
]

OPEN = [
    {"id": "cb-1", "client_oid": "eclipse-77-abc", "product_id": "BTC-USD",
     "side": "buy", "type": "limit", "size": "1", "filled_size": "0",
     "price": "50000", "status": "open", "created_at": "2026-11-01T12:00:00Z"},
    {"id": "cb-2", "client_oid": "manual-1", "product_id": "BTC-USD",
     "side": "buy", "type": "limit", "size": "1", "filled_size": "0",
     "price": "40000", "status": "open", "created_at": "2026-11-01T12:00:00Z"},
]


class Handler(BaseHTTPRequestHandler):
    CALLS: list = []
    WRITES: list = []

    def log_message(self, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode() if length else ""

    def _record(self, method):
        raw = self._raw()
        record = {
            "method": method, "path": self.path, "body": raw,
            "key": self.headers.get("CB-ACCESS-KEY"),
            "passphrase": self.headers.get("CB-ACCESS-PASSPHRASE"),
            "timestamp": self.headers.get("CB-ACCESS-TIMESTAMP"),
            "sign": self.headers.get("CB-ACCESS-SIGN"),
        }
        Handler.CALLS.append(record)
        return record

    def do_GET(self):  # noqa: N802
        self._record("GET")
        base, _, query = self.path.partition("?")
        if base == "/accounts":
            self._send(200, ACCOUNTS)
        elif base == "/products/BTC-USD":
            self._send(200, PRODUCT)
        elif base == "/products/BTC-USD/ticker":
            self._send(200, {"price": "60000", "bid": "59990", "ask": "60010"})
        elif base == "/orders" and "status=open" in query:
            self._send(200, OPEN)
        elif base.startswith("/orders/"):
            order_id = base.rsplit("/", 1)[-1]
            row = next((o for o in OPEN if o["id"] == order_id), None)
            self._send(200, row) if row else self._send(404, {"message": "NotFound"})
        else:
            self._send(404, {"message": "NotFound"})

    def do_POST(self):  # noqa: N802
        record = self._record("POST")
        if self.path != "/orders":
            self._send(404, {"message": "NotFound"})
            return
        body = json.loads(record["body"] or "{}")
        Handler.WRITES.append(record)
        self._send(200, {
            "id": "cb-new", "product_id": body.get("product_id"),
            "side": body.get("side"), "type": body.get("type"),
            "size": body.get("size"), "price": body.get("price", "0"),
            "filled_size": "0", "executed_value": "0", "status": "pending",
            "client_oid": body.get("client_oid"),
        })

    def do_DELETE(self):  # noqa: N802
        record = self._record("DELETE")
        base = self.path.partition("?")[0]
        order_id = base.rsplit("/", 1)[-1]
        if not base.startswith("/orders/") or not any(o["id"] == order_id for o in OPEN):
            self._send(404, {"message": "NotFound"})
            return
        Handler.WRITES.append(record)
        self._send(200, {"id": order_id, "status": "done"})


@pytest.fixture()
def api(monkeypatch):
    Handler.CALLS = []
    Handler.WRITES = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("COINBASE_TEST_KEYS", f"key-1:{SECRET}:pass-9")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url, **config):
    base = {"baseUrl": url, "credentialRef": "COINBASE_TEST_KEYS", "magic": 77}
    base.update(config)
    return CoinbaseAdapter(base)


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="BTC-USD", side="buy", order_type="market",
        volume=0.123456789, price=None, stop_loss=None, take_profit=None, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


def _signed_ok(record: dict) -> bool:
    return record["sign"] == sign_request(
        SECRET, record["timestamp"], record["method"], record["path"], record["body"]
    )


def test_account_mode_is_real_with_trading_allowed(api):
    account = adapter_for(api).account()
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True
    assert account["expertAllowed"] is True


def test_a_trading_disabled_profile_turns_trading_off(monkeypatch):
    monkeypatch.setenv("COINBASE_TEST_KEYS", f"key-1:{SECRET}:pass-9")
    adapter = adapter_for("http://127.0.0.1:1")
    rows = [dict(row) for row in ACCOUNTS]
    rows[0]["trading_enabled"] = False
    monkeypatch.setattr(adapter, "_accounts", lambda: rows)
    assert adapter.account()["tradeAllowed"] is False


def test_preview_uses_product_increments(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["volume"] == pytest.approx(0.12345678)
    assert preview["price"] == pytest.approx(60010)
    assert preview["volumeStep"] == pytest.approx(1e-8)
    assert preview["digits"] == 2
    assert Handler.WRITES == []


def test_an_attached_stop_is_refused_by_name(api):
    adapter = adapter_for(api)
    assert adapter.order_preview(_intent(stop_loss=55000))["retcodeName"] == "STOP_UNSUPPORTED"
    result = adapter.submit_order(_intent(take_profit=70000))
    assert result["ok"] is False
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.WRITES == []


def test_submit_posts_a_body_signed_limit_order_with_client_oid(api):
    result = adapter_for(api).submit_order(_intent(order_type="limit", price=59000))
    assert result["ok"] is True
    assert result["ticket"] == "cb-new"
    sent = Handler.WRITES[-1]
    assert sent["method"] == "POST"
    assert sent["key"] == "key-1"
    assert sent["passphrase"] == "pass-9"
    # The body is part of the signed message; a signature over an empty body fails.
    assert _signed_ok(sent) is True
    body = json.loads(sent["body"])
    assert body["product_id"] == "BTC-USD"
    assert body["side"] == "buy"
    assert body["type"] == "limit"
    assert body["size"] == "0.12345678"
    assert body["price"] == "59000"
    assert body["time_in_force"] == "GTC"
    assert body["client_oid"].startswith("eclipse-77-")


def test_a_market_order_carries_no_price(api):
    adapter_for(api).submit_order(_intent())
    body = json.loads(Handler.WRITES[-1]["body"])
    assert body["type"] == "market"
    assert "price" not in body


def test_cancel_signs_the_delete_verb(api):
    result = adapter_for(api).cancel_order("cb-1")
    assert result["ok"] is True
    sent = Handler.WRITES[-1]
    assert sent["method"] == "DELETE"
    assert sent["path"] == "/orders/cb-1"
    assert _signed_ok(sent) is True


def test_a_missing_order_is_not_reported_as_cancelled(api):
    result = adapter_for(api).cancel_order("cb-missing")
    assert result["ok"] is False
    assert result["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_only_touches_eclipse_orders(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert len(Handler.WRITES) == 1
    assert Handler.WRITES[0]["path"] == "/orders/cb-1"


def test_close_sells_the_available_base_balance(api):
    result = adapter_for(api).close_position("btc-usd")
    assert result["ok"] is True
    assert result["side"] == "sell"
    assert result["volume"] == pytest.approx(0.4)     # `available`, not `balance`
    assert result["verified"] is False
    body = json.loads(Handler.WRITES[-1]["body"])
    assert body["product_id"] == "BTC-USD"
    assert body["side"] == "sell"
    assert body["type"] == "market"
    assert body["size"] == "0.4"
    assert body["client_oid"].startswith("eclipse-77-close")


def test_close_refuses_an_unknown_product(api):
    result = adapter_for(api).close_position("DOGE-USD")
    assert result["ok"] is False
    assert result["retcodeName"] == "INVALID_SYMBOL"
    assert Handler.WRITES == []


def test_order_status_reads_the_order(api):
    status = adapter_for(api).order_status("cb-1")
    assert status["found"] is True
    assert status["state"] == "open"
