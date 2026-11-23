"""Bitstamp v2 execution: form-post order path, quote-sized market buys, close."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.bitstamp import BitstampAdapter, sign_request
from harness.trade.model import OrderIntent

BALANCE = {
    "btc_balance": "0.5", "btc_available": "0.5", "btc_fee": "0.5",
    "usd_balance": "1000.00", "usd_available": "900.00", "usd_fee": "0.2",
}

MARKETS = [{
    "name": "BTC/USD", "url_symbol": "btcusd", "base_decimals": 8,
    "counter_decimals": 2, "minimum_order_value": "10", "trading": "Enabled",
}]


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

    def do_GET(self):  # noqa: N802
        base = self.path.partition("?")[0]
        Handler.CALLS.append({"method": "GET", "path": base, "params": {}})
        if base.startswith("/api/v2/ticker/"):
            self._send(200, {"last": "60000", "bid": "59990", "ask": "60000",
                             "timestamp": "1700000000"})
        elif base == "/api/v2/markets":
            self._send(200, MARKETS)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        base = self.path.partition("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        params = urllib.parse.parse_qs(raw)
        record = {"method": "POST", "path": base, "params": params, "raw": raw}
        Handler.CALLS.append(record)
        if base == "/api/v2/balance/":
            self._send(200, BALANCE)
        elif base == "/api/v2/order/":
            Handler.WRITES.append(record)
            self._send(200, {"id": 999, "price": params.get("price", ["60000"])[0],
                             "amount": params.get("amount", ["0"])[0], "type": "1"})
        elif base == "/api/v2/cancel_order/":
            Handler.WRITES.append(record)
            if (params.get("id") or [""])[0] == "999":
                self._send(200, {"id": 999, "amount": "0.5", "price": "60000"})
            else:
                self._send(200, {"error": "Order not found"})
        elif base == "/api/v2/order_status/":
            self._send(200, {"status": "Finished", "id": 999,
                             "amount_transacted": "0.5", "price": "60000"})
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture()
def api(monkeypatch):
    Handler.CALLS = []
    Handler.WRITES = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("BITSTAMP_TEST_KEYS", "key-1:secret-2:cust-3")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url, **config):
    base = {"baseUrl": url, "credentialRef": "BITSTAMP_TEST_KEYS", "magic": 77}
    base.update(config)
    return BitstampAdapter(base)


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="btcusd", side="buy", order_type="market",
        volume=0.123456789, price=None, stop_loss=None, take_profit=None, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


def _signed_ok(record: dict) -> bool:
    nonce = (record["params"].get("nonce") or [""])[0]
    return (record["params"].get("signature") or [""])[0] == sign_request(
        "secret-2", nonce, "cust-3", "key-1"
    )


def test_account_mode_is_real_with_trading_allowed(api):
    account = adapter_for(api).account()
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True
    assert account["expertAllowed"] is True


def test_symbol_info_reads_the_market_metadata(api):
    info = adapter_for(api).symbol_info("BTCUSD")
    assert info["symbol"] == "BTCUSD"
    assert info["volumeStep"] == pytest.approx(1e-8)
    assert info["digits"] == 2
    assert info["tradeMode"] == "full"
    assert info["notionalMin"] == pytest.approx(10)


def test_preview_uses_the_market_decimals(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["volume"] == pytest.approx(0.12345678)
    assert preview["price"] == pytest.approx(60000)
    assert preview["volumeStep"] == pytest.approx(1e-8)
    assert Handler.WRITES == []


def test_an_attached_stop_is_refused_by_name(api):
    adapter = adapter_for(api)
    assert adapter.order_preview(_intent(stop_loss=55000))["retcodeName"] == "STOP_UNSUPPORTED"
    result = adapter.submit_order(_intent(take_profit=70000))
    assert result["ok"] is False
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.WRITES == []


def test_a_market_buy_sends_a_quote_notional(api):
    result = adapter_for(api).submit_order(_intent())
    assert result["ok"] is True
    assert result["ticket"] == "999"
    sent = Handler.WRITES[-1]
    assert _signed_ok(sent) is True
    assert sent["params"]["key"] == ["key-1"]
    assert sent["params"]["nonce"][0].isdigit()
    assert sent["params"]["currency_pair"] == ["btcusd"]
    assert sent["params"]["type"] == ["buy"]
    # Bitstamp reads a market BUY `amount` as QUOTE notional.
    assert float(sent["params"]["amount"][0]) == pytest.approx(0.12345678 * 60000)
    assert "price" not in sent["params"]


def test_a_market_sell_sends_the_base_volume(api):
    adapter_for(api).submit_order(_intent(side="sell"))
    params = Handler.WRITES[-1]["params"]
    assert params["type"] == ["sell"]
    assert float(params["amount"][0]) == pytest.approx(0.12345678)


def test_a_limit_order_carries_price_in_the_base_volume(api):
    adapter_for(api).submit_order(_intent(order_type="limit", price=59000, side="sell"))
    params = Handler.WRITES[-1]["params"]
    assert params["type"] == ["sell_limit"]
    assert params["price"] == ["59000"]
    assert float(params["amount"][0]) == pytest.approx(0.12345678)


def test_cancel_posts_the_order_id(api):
    result = adapter_for(api).cancel_order("999")
    assert result["ok"] is True
    sent = Handler.WRITES[-1]
    assert sent["path"] == "/api/v2/cancel_order/"
    assert sent["params"]["id"] == ["999"]
    assert _signed_ok(sent) is True


def test_an_unknown_order_is_not_reported_as_cancelled(api):
    result = adapter_for(api).cancel_order("123")
    assert result["ok"] is False
    assert result["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_refuses_because_bitstamp_has_no_ownership_tag(api):
    result = adapter_for(api).cancel_all(77)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert result["cancelled"] == 0
    assert Handler.WRITES == []


def test_close_sells_the_available_base_balance(api):
    result = adapter_for(api).close_position("btcusd")
    assert result["ok"] is True
    assert result["side"] == "sell"
    assert result["volume"] == pytest.approx(0.5)
    assert result["verified"] is False
    params = Handler.WRITES[-1]["params"]
    assert params["currency_pair"] == ["btcusd"]
    assert params["type"] == ["sell"]
    assert float(params["amount"][0]) == pytest.approx(0.5)


def test_close_refuses_an_unknown_market(api):
    result = adapter_for(api).close_position("dogeusd")
    assert result["ok"] is False
    assert result["retcodeName"] == "INVALID_SYMBOL"
    assert Handler.WRITES == []


def test_order_status_reads_the_order_status_endpoint(api):
    status = adapter_for(api).order_status("999")
    assert status["found"] is True
    assert status["state"] == "Finished"
    assert status["filledVolume"] == pytest.approx(0.5)
