"""Coincheck execution: form bodies, full-URL signature, ownership refusal."""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import AdapterUnavailable, TradingAdapter
from harness.trade.adapters.coincheck import CoincheckAdapter, sign_request
from harness.trade.model import OrderIntent

OPEN_ORDERS = [
    {"id": 9, "pair": "btc_jpy", "order_type": "buy", "amount": "1",
     "executed_amount": "0.25", "rate": "5000000"},
]


class Handler(BaseHTTPRequestHandler):
    base = ""
    posts: list = []
    deletes: list = []

    def log_message(self, *args):
        return

    def _send(self, payload, status=200):
        import json

        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self):
        return {key.lower(): value for key, value in self.headers.items()}

    def _url(self):
        return f"{Handler.base}{self.path}"

    def do_GET(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/accounts/balance":
            self._send({"success": True, "jpy": "100000", "btc": "0.5"})
        elif path == "/api/ticker":
            self._send({"last": "6000000", "bid": "5999000", "ask": "6001000"})
        elif path == "/api/exchange/orders/opens":
            self._send({"success": True, "orders": OPEN_ORDERS})
        elif path.startswith("/api/exchange/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            self._send({"success": True, "id": int(order_id), "amount": "0.1",
                        "executed_amount": "0", "rate": "6000000"})
        else:
            self._send({"success": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        body = dict(urllib.parse.parse_qsl(raw))
        if self.path == "/api/exchange/orders":
            Handler.posts.append({"url": self._url(), "headers": self._headers(),
                                  "body": body, "raw": raw})
            self._send({"success": True, "id": 987, "amount": body.get("amount"),
                        "rate": body.get("rate"), "order_type": body.get("order_type")})
        else:
            self._send({"success": False, "error": "not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        path = self.path.partition("?")[0]
        if path.startswith("/api/exchange/orders/"):
            Handler.deletes.append({"url": self._url(), "headers": self._headers()})
            self._send({"success": True, "id": int(path.rsplit("/", 1)[-1])})
        else:
            self._send({"success": False, "error": "not found"}, 404)


@pytest.fixture()
def api(monkeypatch):
    Handler.posts, Handler.deletes = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_COINCHECK_EXEC", "key-1:secret-2")
    try:
        yield Handler.base
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> CoincheckAdapter:
    return CoincheckAdapter({"baseUrl": url, "credentialRef": "TEST_COINCHECK_EXEC"})


def _intent(**overrides) -> OrderIntent:
    values = dict(connection_id="conn", symbol="btc_jpy", side="buy",
                  order_type="limit", volume=0.1, price=6000000, magic=77)
    values.update(overrides)
    return OrderIntent(**values)


def test_account_reports_real_mode_and_is_tradeable(api):
    adapter = adapter_for(api)
    account = adapter.account()
    assert isinstance(adapter, TradingAdapter)
    assert account["mode"] == "real"
    assert account["tradeAllowed"] is True


def test_a_sandbox_host_is_the_demo_account(monkeypatch):
    adapter = CoincheckAdapter({
        "baseUrl": "https://demo.coincheck.com",
        "credentialRef": "TEST_COINCHECK_EXEC",
    })
    monkeypatch.setattr(adapter, "_balances", lambda: {"JPY": 1.0})
    assert adapter.account()["mode"] == "demo"


def test_positions_and_orders_carry_closeable_tickets(api):
    adapter = adapter_for(api)
    assert [row["ticket"] for row in adapter.positions()] == ["JPY", "BTC"]
    order = adapter.orders()[0]
    assert order["ticket"] == "9"
    assert order["volumeCurrent"] == pytest.approx(0.75)


def test_preview_prices_a_limit_order(api):
    preview = adapter_for(api).order_preview(_intent(stop_loss=5900000))
    assert preview["ok"] is True
    assert preview["price"] == 6000000
    assert preview["notional"] == 600000
    assert preview["openPositions"] == 2


def test_submit_posts_a_signed_form_body_with_the_stop_rate(api):
    result = adapter_for(api).submit_order(_intent(stop_loss=5900000))
    assert result["ok"] is True
    assert result["status"] == "placed"
    assert result["ticket"] == "987"
    sent = Handler.posts[-1]
    assert sent["body"]["pair"] == "btc_jpy"
    assert sent["body"]["order_type"] == "buy"
    assert sent["body"]["rate"] == "6000000"
    assert sent["body"]["amount"] == "0.1"
    assert sent["body"]["stop_loss_rate"] == "1.67"
    headers = sent["headers"]
    assert headers["access-key"] == "key-1"
    assert headers["access-sign"] == sign_request(
        "secret-2", headers["access-nonce"], sent["url"])


def test_a_market_buy_spends_a_jpy_amount(api):
    adapter_for(api).submit_order(_intent(order_type="market", price=None))
    body = Handler.posts[-1]["body"]
    assert body["order_type"] == "market_buy"
    assert body["market_buy_amount"] == "600100"
    assert "amount" not in body


def test_a_market_sell_sends_the_asset_amount(api):
    adapter_for(api).submit_order(
        _intent(order_type="market", price=None, side="sell"))
    body = Handler.posts[-1]["body"]
    assert body["order_type"] == "market_sell"
    assert body["amount"] == "0.1"


def test_take_profit_is_refused_before_sending(api):
    result = adapter_for(api).submit_order(
        _intent(order_type="market", price=None, take_profit=6200000))
    assert result["ok"] is False
    assert result["retcodeName"] == "STOP_UNSUPPORTED"
    assert Handler.posts == []


def test_cancel_deletes_the_order_and_signs_the_full_url(api):
    result = adapter_for(api).cancel_order("987")
    assert result["ok"] is True
    sent = Handler.deletes[-1]
    assert sent["url"].endswith("/api/exchange/orders/987")
    headers = sent["headers"]
    assert headers["access-sign"] == sign_request(
        "secret-2", headers["access-nonce"], sent["url"])


def test_close_sells_the_held_balance(api):
    result = adapter_for(api).close_position("BTC")
    assert result["ok"] is True
    assert result["side"] == "sell"
    assert result["volume"] == pytest.approx(0.5)
    assert result["verified"] is False      # accepted, not proven filled
    body = Handler.posts[-1]["body"]
    assert body["order_type"] == "market_sell"
    assert body["amount"] == "0.5"


def test_cancel_all_refuses_without_an_ownership_tag(api):
    result = adapter_for(api).cancel_all(77)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert Handler.deletes == []


def test_order_status_reads_the_single_order(api):
    status = adapter_for(api).order_status("987")
    assert status["found"] is True
    assert status["state"] == "open"


def test_symbol_info_declines_instead_of_inventing_a_step(api):
    with pytest.raises(AdapterUnavailable) as exc:
        adapter_for(api).symbol_info("btc_jpy")
    assert exc.value.code == "SYMBOL_INFO_NOT_SUPPORTED"


def test_modify_order_and_position_are_honest_refusals(api):
    adapter = adapter_for(api)
    assert adapter.modify_order("987", price=6100000)["retcodeName"] == "ORDER_MODIFY_NOT_SUPPORTED"
    assert adapter.modify_position("BTC", stop_loss=1)["retcodeName"] == "MODIFY_NOT_SUPPORTED"
