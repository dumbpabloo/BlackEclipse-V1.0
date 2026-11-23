"""Kraken spot execution: signed body order path, userref ownership, spot close."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.kraken import KrakenAdapter, sign_request
from harness.trade.model import OrderIntent

SECRET = base64.b64encode(b"top-secret").decode()

PAIRS = {"XXBTZUSD": {
    "altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD",
    "lot_decimals": 8, "pair_decimals": 1, "ordermin": "0.0001",
    "costmin": "0.5", "status": "online",
}}

OPEN = {
    "OID-1": {"userref": 77, "opentm": 1700000000.0, "vol": "0.5", "vol_exec": "0",
              "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit",
                        "price": "50000"}},
    "OID-2": {"opentm": 1700000000.0, "vol": "0.1", "vol_exec": "0",
              "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit",
                        "price": "40000"}},
}

BALANCE = {"XXBT": "0.5", "ZUSD": "1000.0"}


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

    def _body(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode() if length else ""

    def do_GET(self):  # noqa: N802
        base, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        Handler.CALLS.append({"method": "GET", "path": base, "params": params, "body": ""})
        if base == "/0/public/Ticker":
            self._send(200, {"error": [], "result": {"XXBTZUSD": {
                "c": ["60000"], "b": ["59990"], "a": ["60010"]}}})
        elif base == "/0/public/AssetPairs":
            pair = (params.get("pair") or [""])[0].upper()
            rows = PAIRS if pair in ("XBTUSD", "XXBTZUSD") else {}
            self._send(200, {"error": [], "result": rows})
        else:
            self._send(404, {"error": ["not found"], "result": {}})

    def do_POST(self):  # noqa: N802
        base, _, _query = self.path.partition("?")
        body = self._body()
        record = {"method": "POST", "path": base, "body": body,
                  "key": self.headers.get("API-Key"),
                  "sign": self.headers.get("API-Sign"),
                  "params": urllib.parse.parse_qs(body)}
        Handler.CALLS.append(record)
        if base == "/0/private/Balance":
            self._send(200, {"error": [], "result": BALANCE})
        elif base == "/0/private/OpenOrders":
            self._send(200, {"error": [], "result": {"open": OPEN}})
        elif base == "/0/private/AddOrder":
            Handler.WRITES.append(record)
            self._send(200, {"error": [], "result": {
                "descr": {"order": "sell 0.5 XBTUSD @ market"}, "txid": ["OID-NEW"]}})
        elif base == "/0/private/CancelOrder":
            Handler.WRITES.append(record)
            txid = (record["params"].get("txid") or [""])[0]
            count = 1 if txid in OPEN else 0
            self._send(200, {"error": [], "result": {"count": count, "pending": False}})
        elif base == "/0/private/QueryOrders":
            self._send(200, {"error": [], "result": {"OID-1": {
                "status": "open", "vol_exec": "0.2", "price": "50000"}}})
        else:
            self._send(404, {"error": ["not found"], "result": {}})


@pytest.fixture()
def api(monkeypatch):
    Handler.CALLS = []
    Handler.WRITES = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("KRAKEN_TEST_KEYS", f"key-1:{SECRET}")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url, **config):
    base = {"baseUrl": url, "credentialRef": "KRAKEN_TEST_KEYS", "magic": 77}
    base.update(config)
    return KrakenAdapter(base)


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="XBTUSD", side="buy", order_type="market",
        volume=0.123456789, price=None, stop_loss=None, take_profit=None, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


def _signed_ok(record: dict) -> bool:
    nonce = (record["params"].get("nonce") or [""])[0]
    return record["sign"] == sign_request(SECRET, record["path"], nonce, record["body"])


def test_account_mode_is_real_with_trading_allowed(api):
    account = adapter_for(api).account()
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True
    assert account["expertAllowed"] is True


def test_preview_uses_the_pair_lot_decimals(api):
    preview = adapter_for(api).order_preview(_intent())
    assert preview["ok"] is True
    assert preview["volume"] == pytest.approx(0.12345678)
    assert preview["price"] == pytest.approx(60010)       # buy pays the ask
    assert preview["volumeStep"] == pytest.approx(1e-8)
    assert preview["digits"] == 1
    assert Handler.WRITES == []


def test_an_attached_stop_is_refused_by_name(api):
    adapter = adapter_for(api)
    preview = adapter.order_preview(_intent(stop_loss=55000))
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    result = adapter.submit_order(_intent(take_profit=70000))
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.WRITES == []


def test_submit_posts_a_signed_add_order_with_userref(api):
    result = adapter_for(api).submit_order(_intent())
    assert result["ok"] is True
    assert result["ticket"] == "OID-NEW"
    sent = Handler.WRITES[-1]
    assert sent["key"] == "key-1"
    assert _signed_ok(sent) is True
    assert sent["params"]["pair"] == ["XBTUSD"]
    assert sent["params"]["type"] == ["buy"]
    assert sent["params"]["ordertype"] == ["market"]
    assert sent["params"]["volume"] == ["0.12345678"]
    assert sent["params"]["userref"] == ["77"]


def test_a_limit_order_carries_price_and_time_in_force(api):
    adapter_for(api).submit_order(_intent(order_type="limit", price=59000))
    params = Handler.WRITES[-1]["params"]
    assert params["ordertype"] == ["limit"]
    assert params["price"] == ["59000"]
    assert params["timeinforce"] == ["gtc"]


def test_cancel_posts_the_full_txid(api):
    result = adapter_for(api).cancel_order("OID-1")
    assert result["ok"] is True
    sent = Handler.WRITES[-1]
    assert sent["params"]["txid"] == ["OID-1"]
    assert _signed_ok(sent) is True


def test_an_unknown_txid_is_not_reported_as_cancelled(api):
    result = adapter_for(api).cancel_order("OID-NOPE")
    assert result["ok"] is False
    assert result["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_only_touches_orders_with_the_magic_userref(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert len(Handler.WRITES) == 1
    assert Handler.WRITES[0]["params"]["txid"] == ["OID-1"]


def test_close_sells_the_venue_reported_base_balance(api):
    result = adapter_for(api).close_position("XBTUSD")
    assert result["ok"] is True
    assert result["side"] == "sell"
    assert result["volume"] == pytest.approx(0.5)
    assert result["verified"] is False
    params = Handler.WRITES[-1]["params"]
    assert params["pair"] == ["XBTUSD"]
    assert params["type"] == ["sell"]
    assert params["ordertype"] == ["market"]
    assert params["volume"] == ["0.5"]


def test_close_refuses_a_ticket_that_is_not_a_pair(api):
    result = adapter_for(api).close_position("BTC")
    assert result["ok"] is False
    assert result["retcodeName"] == "INVALID_SYMBOL"
    assert Handler.WRITES == []


def test_order_status_reads_query_orders(api):
    status = adapter_for(api).order_status("OID-1")
    assert status["found"] is True
    assert status["filledVolume"] == pytest.approx(0.2)
