"""Bitfinex v2 execution: /w/ URL vs /r/ signed path, signed amounts, cid ownership."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import TradingAdapter
from harness.trade.adapters.bitfinex import (
    BitfinexAdapter,
    owns_cid,
    sign_request,
    tagged_cid,
)
from harness.trade.model import OrderIntent

BIG_ID = 111111111111111111
POSITION_ID = 987654321098765432


def order_row(order_id=BIG_ID, cid=12345, amount=-1.0, status="ACTIVE", price=62000):
    return [order_id, None, cid, "tBTCUSD", 1700000000000, 1700000000000, amount,
            amount, "EXCHANGE LIMIT", None, None, None, 0, status, None, None,
            price, 0, 0, 0, None, None, None, 0, None, None, None, "API>BFX",
            None, None, None]


POSITION_ROW = ["tBTCUSD", "ACTIVE", -0.5, 60000, -1.0, 0.0, 0.0, 25.5, 0.01,
                0.0, 10, None, POSITION_ID, 1700000000000, None, None, None, 61000]


class Handler(BaseHTTPRequestHandler):
    submits: list = []
    cancels: list = []
    updates: list = []
    orders: list = []
    account_real = True

    def log_message(self, *args):
        return

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return raw, (json.loads(raw) if raw else {})

    def _envelope(self, kind, row):
        return [1700000000.0, kind, 1, None, [[row]], 0, "SUCCESS", "ok"]

    def do_GET(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if path.startswith("/api/v2/r/") and not self.headers.get("bfx-apikey"):
            self._send({"error": "missing auth"}, 401)
            return
        if path == "/api/v2/r/wallets":
            self._send([["exchange", "BTC", 0.5, 0.0, 0.4, None],
                        ["exchange", "USD", 1000.0, 0.0, 900.0, None]])
        elif path == "/api/v2/r/positions":
            self._send([POSITION_ROW])
        elif path == "/api/v2/r/orders":
            self._send(Handler.orders)
        elif path == "/api/v2/ticker/tBTCUSD":
            self._send([59999, 1, 60001, 1, 10, 0.0001, 60000.5, 100, 60100, 59000])
        elif path == "/api/v2/conf/pub:list:pair:exchange":
            self._send([["BTCUSD", "ETHUSD"]])
        else:
            self._send({"error": "not found"}, 404)

    def _headers(self):
        return {key.lower(): value for key, value in self.headers.items()}

    def do_POST(self):  # noqa: N802
        raw, body = self._payload()
        path = self.path.partition("?")[0]
        if path == "/api/v2/w/order/submit":
            Handler.submits.append((path, self._headers(), raw))
            row = order_row(
                order_id=BIG_ID, cid=body.get("cid"),
                amount=float(body.get("amount") or 0),
                price=float(body.get("price") or 0))
            self._send(self._envelope("on-req", row))
        elif path == "/api/v2/w/order/cancel":
            Handler.cancels.append((path, self._headers(), raw, body))
            self._send(self._envelope("oc-req", order_row(
                order_id=int(body["id"]), cid=0, amount=0.0, status="CANCELED")))
        elif path == "/api/v2/w/order/update":
            Handler.updates.append((path, self._headers(), raw, body))
            self._send(self._envelope("ou-req", order_row(
                order_id=int(body["id"]), cid=0, amount=-0.5, status="ACTIVE",
                price=float(body.get("price") or 0))))
        elif path == "/api/v2/r/orders/hist":
            self._send([])
        else:
            self._send({"error": "not found"}, 404)


@pytest.fixture()
def api(monkeypatch):
    Handler.submits, Handler.cancels, Handler.updates = [], [], []
    Handler.orders = [order_row(cid=tagged_cid(77, "abc"), amount=-1.0, price=62000)]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_BITFINEX_EXEC", "key-1:secret-2")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> BitfinexAdapter:
    return BitfinexAdapter({"baseUrl": url, "credentialRef": "TEST_BITFINEX_EXEC"})


def _intent(**overrides) -> OrderIntent:
    values = dict(connection_id="conn", symbol="BTCUSD", side="buy",
                  order_type="limit", volume=0.5, price=60000, magic=77)
    values.update(overrides)
    return OrderIntent(**values)


def test_module_tag_is_inside_int45_and_scoped_to_the_magic():
    cid = tagged_cid(77, "abc")
    assert owns_cid(cid, 77) is True
    assert owns_cid(cid, 78) is False
    assert cid < 2**45


def test_account_is_real_and_trade_allowed(api):
    adapter = adapter_for(api)
    account = adapter.account()
    assert isinstance(adapter, TradingAdapter)
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True
    assert account["balance"] == pytest.approx(1000.5)


def test_positions_keep_the_big_position_id_as_a_string(api):
    position = adapter_for(api).positions()[0]
    assert position["ticket"] == str(POSITION_ID)
    assert position["side"] == "sell"
    assert position["volume"] == pytest.approx(0.5)


def test_preview_prices_a_market_buy_from_the_ask(api):
    preview = adapter_for(api).order_preview(_intent(order_type="market", price=None))
    assert preview["ok"] is True
    assert preview["price"] == 60001
    assert preview["notional"] == pytest.approx(30000.5)
    assert preview["openPositions"] == 1


def test_submit_signs_the_r_path_but_posts_to_w(api):
    result = adapter_for(api).submit_order(_intent())
    assert result["ok"] is True
    assert result["status"] == "placed"
    assert result["ticket"] == str(BIG_ID)
    path, headers, raw = Handler.submits[-1]
    assert path == "/api/v2/w/order/submit"
    nonce = headers["bfx-nonce"]
    assert headers["bfx-signature"] == sign_request(
        "secret-2", "/api/v2/r/order/submit", nonce, raw)
    body = json.loads(raw)
    assert body["type"] == "EXCHANGE LIMIT"
    assert body["symbol"] == "tBTCUSD"
    assert body["amount"] == "0.5"
    assert body["price"] == "60000"
    assert body["flags"] == 0            # no implicit OCO/close flag
    assert owns_cid(body["cid"], 77)


def test_submit_uses_a_signed_negative_amount_for_a_sell(api):
    adapter_for(api).submit_order(_intent(side="sell", price=61000))
    body = json.loads(Handler.submits[-1][2])
    assert body["amount"] == "-0.5"


def test_a_stop_entry_uses_price_aux_limit(api):
    result = adapter_for(api).submit_order(
        _intent(order_type="stop", price=59000))
    assert result["ok"] is True
    body = json.loads(Handler.submits[-1][2])
    assert body["type"] == "EXCHANGE STOP LIMIT"
    assert body["price"] == "59000"
    assert body["price_aux_limit"] == "59000"


def test_an_order_bracket_is_refused_before_sending(api):
    result = adapter_for(api).submit_order(_intent(stop_loss=59000))
    assert result["ok"] is False
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.submits == []


def test_cancel_posts_the_numeric_id_and_reads_canceled(api):
    result = adapter_for(api).cancel_order(str(BIG_ID))
    assert result["ok"] is True
    path, headers, raw, body = Handler.cancels[-1]
    assert path == "/api/v2/w/order/cancel"
    assert body["id"] == BIG_ID
    assert headers["bfx-signature"] == sign_request(
        "secret-2", "/api/v2/r/order/cancel", headers["bfx-nonce"], raw)


def test_cancel_all_only_touches_eclipse_cids(api):
    Handler.orders = [
        order_row(order_id=111, cid=tagged_cid(77, "abc")),
        order_row(order_id=222, cid=12345),
    ]
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert [body["id"] for *_rest, body in Handler.cancels] == [111]


def test_close_sends_the_opposite_market_amount(api):
    result = adapter_for(api).close_position(str(POSITION_ID))
    assert result["ok"] is True
    assert result["side"] == "buy"
    assert result["volume"] == pytest.approx(0.5)
    assert result["verified"] is False       # accepted, not necessarily filled
    body = json.loads(Handler.submits[-1][2])
    assert body["type"] == "EXCHANGE MARKET"
    assert body["amount"] == "0.5"


def test_modify_order_updates_the_price_only(api):
    result = adapter_for(api).modify_order("111", price=60500)
    assert result["ok"] is True
    path, headers, raw, body = Handler.updates[-1]
    assert path == "/api/v2/w/order/update"
    assert body["price"] == "60500"
    assert headers["bfx-signature"] == sign_request(
        "secret-2", "/api/v2/r/order/update", headers["bfx-nonce"], raw)


def test_modify_position_refuses_because_stops_are_separate_orders(api):
    result = adapter_for(api).modify_position(str(POSITION_ID), stop_loss=62000)
    assert result["ok"] is False
    assert result["retcodeName"] == "POSITION_BRACKET_UNSUPPORTED"


def test_order_status_finds_an_active_order(api):
    status = adapter_for(api).order_status(str(BIG_ID))
    assert status["found"] is True


def test_symbol_info_validates_the_pair_and_never_invents_a_step(api):
    info = adapter_for(api).symbol_info("btc/usd")
    assert info["symbol"] == "BTCUSD"
    assert info["volumeStep"] == 0.0
    assert info["volumeMin"] == 0.0
