"""Binance spot execution: signed query order path, ownership, spot close."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.binance import BinanceAdapter
from harness.trade.model import OrderIntent

SECRET = "secret-2"

ACCOUNT = {
    "canTrade": True,
    "balances": [
        {"asset": "BTC", "free": "0.5", "locked": "0.0"},
        {"asset": "USDT", "free": "1000", "locked": "0"},
    ],
}

EXCHANGE = {"symbols": [{
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000",
         "stepSize": "0.00001"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "10"},
    ],
}]}

OPEN = [
    {"orderId": 111, "clientOrderId": "eclipse-77-limit", "symbol": "BTCUSDT",
     "side": "BUY", "type": "LIMIT", "origQty": "0.5", "executedQty": "0",
     "price": "50000", "status": "NEW", "time": 1700000000000},
    {"orderId": 222, "clientOrderId": "manual-1", "symbol": "BTCUSDT",
     "side": "BUY", "type": "LIMIT", "origQty": "0.1", "executedQty": "0",
     "price": "40000", "status": "NEW", "time": 1700000000000},
]


class Handler(BaseHTTPRequestHandler):
    PREFIX = ""
    SIGNED: list = []
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

    def _route(self):
        path = self.path
        if Handler.PREFIX and path.startswith(Handler.PREFIX):
            path = path[len(Handler.PREFIX):]
        base, _, query = path.partition("?")
        return base, urllib.parse.parse_qs(query)

    def _record(self, method, base, query):
        raw = (self.path.split("?", 1)[1]) if "?" in self.path else ""
        Handler.SIGNED.append({
            "method": method, "path": base, "query": query, "raw": raw,
            "key": self.headers.get("X-MBX-APIKEY"), "body": self.rfile.read(
                int(self.headers.get("Content-Length") or 0)) if method != "GET" else b"",
        })
        return Handler.SIGNED[-1]

    def do_GET(self):  # noqa: N802
        base, query = self._route()
        self._record("GET", base, query)
        if base == "/api/v3/account":
            self._send(200, ACCOUNT)
        elif base == "/api/v3/ticker/price":
            self._send(200, {"symbol": query.get("symbol", [""])[0], "price": "60000"})
        elif base == "/api/v3/exchangeInfo":
            pair = query.get("symbol", [""])[0]
            if pair and pair != "BTCUSDT":
                self._send(200, {"symbols": []})
            else:
                self._send(200, EXCHANGE)
        elif base == "/api/v3/openOrders":
            self._send(200, OPEN)
        else:
            self._send(404, {"code": -1, "msg": "not found"})

    def do_POST(self):  # noqa: N802
        base, query = self._route()
        record = self._record("POST", base, query)
        if base != "/api/v3/order":
            self._send(404, {"code": -1, "msg": "not found"})
            return
        Handler.WRITES.append(record)
        self._send(200, {
            "symbol": "BTCUSDT", "orderId": 333,
            "clientOrderId": query.get("newClientOrderId", [""])[0],
            "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0",
            "price": query.get("price", ["0"])[0], "transactTime": 1700000000000,
        })

    def do_DELETE(self):  # noqa: N802
        base, query = self._route()
        record = self._record("DELETE", base, query)
        if base != "/api/v3/order":
            self._send(404, {"code": -2011, "msg": "Unknown order sent."})
            return
        Handler.WRITES.append(record)
        self._send(200, {"symbol": "BTCUSDT", "status": "CANCELED"})


@pytest.fixture()
def api(monkeypatch):
    Handler.PREFIX = ""
    Handler.SIGNED = []
    Handler.WRITES = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("BINANCE_TEST_KEYS", f"key-1:{SECRET}")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url, **config):
    base = {"baseUrl": url, "credentialRef": "BINANCE_TEST_KEYS", "magic": 77}
    base.update(config)
    return BinanceAdapter(base)


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="BTCUSDT", side="buy", order_type="market",
        volume=0.123456789, price=None, stop_loss=None, take_profit=None, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


def _signature_ok(raw: str) -> bool:
    unsigned, _, signature = raw.rpartition("&signature=")
    expected = hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    return signature == expected


def test_account_mode_is_real_with_trading_allowed(api):
    account = adapter_for(api).account()
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True
    assert account["expertAllowed"] is True


def test_a_sandbox_host_is_a_demo_account(monkeypatch):
    Handler.PREFIX = "/sandbox"
    monkeypatch.setenv("BINANCE_TEST_KEYS", f"key-1:{SECRET}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/sandbox"
        assert adapter_for(url).account()["mode"] == "demo"
    finally:
        server.shutdown()
        server.server_close()
        Handler.PREFIX = ""


def test_preview_uses_exchange_filters_and_never_sends_an_order(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["volume"] == pytest.approx(0.12345)      # floored to stepSize
    assert preview["price"] == pytest.approx(60000)
    assert preview["margin"] == pytest.approx(0.12345 * 60000)
    assert preview["volumeStep"] == pytest.approx(0.00001)
    assert preview["digits"] == 2
    assert Handler.WRITES == []


def test_an_attached_stop_is_refused_by_name(api):
    adapter = adapter_for(api)
    preview = adapter.order_preview(_intent(stop_loss=55000))
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    result = adapter.submit_order(_intent(take_profit=70000))
    assert result["ok"] is False
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.WRITES == []          # nothing was sent, nothing was dropped


def test_submit_posts_a_signed_query_order_with_the_ownership_tag(api):
    result = adapter_for(api).submit_order(_intent())
    assert result["ok"] is True
    assert result["ticket"] == "333"
    sent = Handler.WRITES[-1]
    assert sent["method"] == "POST"
    assert sent["key"] == "key-1"
    assert sent["body"] == b""                    # signed in the query, no body
    assert _signature_ok(sent["raw"]) is True
    query = sent["query"]
    assert query["symbol"] == ["BTCUSDT"]
    assert query["side"] == ["BUY"]
    assert query["type"] == ["MARKET"]
    assert query["quantity"] == ["0.12345"]
    assert query["newClientOrderId"][0].startswith("eclipse-77-")


def test_a_limit_order_carries_price_and_time_in_force(api):
    adapter_for(api).submit_order(_intent(order_type="limit", price=59000))
    query = Handler.WRITES[-1]["query"]
    assert query["type"] == ["LIMIT"]
    assert query["price"] == ["59000"]
    assert query["timeInForce"] == ["GTC"]


def test_cancel_resolves_the_symbol_from_the_open_order(api):
    result = adapter_for(api).cancel_order("111")
    assert result["ok"] is True
    sent = Handler.WRITES[-1]
    assert sent["method"] == "DELETE"
    assert sent["query"]["symbol"] == ["BTCUSDT"]
    assert sent["query"]["orderId"] == ["111"]
    assert _signature_ok(sent["raw"]) is True


def test_an_unknown_ticket_is_not_reported_as_cancelled(api):
    result = adapter_for(api).cancel_order("999")
    assert result["ok"] is False
    assert result["retcodeName"] == "ORDER_NOT_FOUND"
    assert Handler.WRITES == []


def test_cancel_all_only_touches_eclipse_orders(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert len(Handler.WRITES) == 1
    assert Handler.WRITES[0]["query"]["orderId"] == ["111"]


def test_close_sells_the_venue_reported_base_balance(api):
    result = adapter_for(api).close_position("btcusdt")
    assert result["ok"] is True
    assert result["side"] == "sell"
    assert result["volume"] == pytest.approx(0.5)
    assert result["verified"] is False          # no fill was reported
    query = Handler.WRITES[-1]["query"]
    assert query["symbol"] == ["BTCUSDT"]
    assert query["side"] == ["SELL"]
    assert query["quantity"] == ["0.5"]
    assert query["newClientOrderId"][0].startswith("eclipse-77-close")


def test_close_resolves_an_asset_ticket_to_the_venue_pair(api):
    result = adapter_for(api).close_position("BTC")
    assert result["ok"] is True
    assert result["ticket"] == "BTCUSDT"
    assert Handler.WRITES[-1]["path"].startswith("/api/v3/order")
    assert Handler.WRITES[-1]["query"]["symbol"] == ["BTCUSDT"]


def test_order_status_reads_open_orders(api):
    status = adapter_for(api).order_status("111")
    assert status["found"] is True
    assert status["stateName"] == "NEW"
