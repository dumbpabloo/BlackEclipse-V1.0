"""Bybit v5 execution against a local HTTP server: signed POSTs, scoped cancels."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.bybit import BybitAdapter, sign_request
from harness.trade.adapters.orders import client_order_id
from harness.trade.model import OrderIntent

SECRET = "secret-2"
KEYS = "key-1:secret-2"
MAGIC = 7


def _ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result}


def _routes(method, path, query, raw):
    body = json.loads(raw) if raw else {}
    if path == "/v5/account/wallet-balance":
        return _ok({"list": [{"accountType": "UNIFIED", "coin": [
            {"coin": "USDT", "walletBalance": "1000", "equity": "1010",
             "availableToWithdraw": "900"},
        ]}]})
    if path == "/v5/position/list":
        return _ok({"list": [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.5", "avgPrice": "60000",
             "markPrice": "61000", "unrealisedPnl": "500", "stopLoss": "59000",
             "takeProfit": "", "createdTime": "1700000000000", "positionIdx": 0},
            {"symbol": "ETHUSDT", "side": "Sell", "size": "0", "positionIdx": 2},
        ]})
    if path == "/v5/order/realtime":
        if query and "orderId=" in query:
            return _ok({"list": [{
                "orderId": "ord-1", "symbol": "BTCUSDT", "orderStatus": "New",
                "cumExecQty": "0.1", "avgPrice": "60000",
                "orderLinkId": "eclipse-7-abc",
            }]})
        return _ok({"list": [
            {"orderId": "ord-1", "symbol": "BTCUSDT", "side": "Sell",
             "orderType": "Limit", "qty": "1", "cumExecQty": "0.4", "price": "62000",
             "orderStatus": "New", "createdTime": "1700000000000",
             "orderLinkId": "eclipse-7-abc"},
            {"orderId": "manual-1", "symbol": "BTCUSDT", "side": "Buy",
             "orderType": "Limit", "qty": "1", "cumExecQty": "0", "price": "58000",
             "orderStatus": "New", "createdTime": "1700000000000",
             "orderLinkId": "typed-by-hand"},
        ]})
    if path == "/v5/market/tickers":
        return _ok({"list": [{"lastPrice": "60000.5", "bid1Price": "59999",
                              "ask1Price": "60001"}]})
    if path == "/v5/market/instruments-info":
        return _ok({"list": [{
            "symbol": "BTCUSDT", "contractType": "LinearPerpetual", "status": "Trading",
            "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                              "maxOrderQty": "100"},
            "priceFilter": {"tickSize": "0.1"},
        }]})
    if path == "/v5/order/create":
        return _ok({"orderId": "new-1", "orderLinkId": body.get("orderLinkId")})
    if path == "/v5/order/cancel":
        return _ok({"orderId": body.get("orderId")})
    if path == "/v5/order/amend":
        return _ok({"orderId": body.get("orderId")})
    if path == "/v5/position/trading-stop":
        return _ok({})
    return {"retCode": 10001, "retMsg": f"no route {method} {path}", "result": {}}


class _Venue:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[dict] = []
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                call = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": raw.decode("utf-8") if raw else "",
                }
                venue.calls.append(call)
                path, _, query = self.path.partition("?")
                data = json.dumps(venue.routes(self.command, path, query,
                                               call["body"])).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def writes(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET" and c["path"].startswith(path)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def venue(monkeypatch):
    monkeypatch.setenv("BYBIT_KEYS", KEYS)
    server = _Venue(_routes)
    yield server
    server.close()


def _adapter(venue, **config) -> BybitAdapter:
    rows = {"credentialRef": "BYBIT_KEYS", "baseUrl": venue.base, "category": "linear"}
    rows.update(config)
    return BybitAdapter(rows)


def _intent(**overrides) -> OrderIntent:
    rows = dict(connection_id="c1", symbol="BTCUSDT", side="buy", order_type="limit",
                volume=0.5, price=60000, stop_loss=59000, take_profit=62000, magic=MAGIC)
    rows.update(overrides)
    return OrderIntent(**rows)


def _check_sign(call: dict) -> None:
    text = call["body"] if call["method"] != "GET" else call["path"].partition("?")[2]
    expected = sign_request(SECRET, call["headers"]["x-bapi-timestamp"], "key-1", text)
    assert call["headers"]["x-bapi-sign"] == expected
    assert call["headers"]["x-bapi-recv-window"] == "5000"


def test_account_reports_the_configured_mode(venue):
    account = _adapter(venue).account()
    assert account["mode"] == "real" and account["tradeAllowed"] is True
    assert account["equity"] == pytest.approx(1010.0)


def test_a_testnet_host_is_a_demo_account(venue, monkeypatch):
    adapter = _adapter(venue, baseUrl="https://api-testnet.bybit.com")
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: {
        "list": [{"accountType": "UNIFIED", "coin": []}]})
    assert adapter.account()["mode"] == "demo"


def test_positions_ticket_carries_the_position_index(venue):
    rows = _adapter(venue).positions()
    assert [row["ticket"] for row in rows] == ["BTCUSDT:0"]


def test_preview_reads_the_lot_step(venue):
    preview = _adapter(venue).order_preview(_intent())
    assert preview["ok"] is True, preview["reasons"]
    assert preview["volume"] == pytest.approx(0.5)
    assert preview["volumeStep"] == pytest.approx(0.001)
    assert preview["derivative"] is True


def test_submit_posts_the_signed_order_with_tp_sl(venue):
    intent = _intent()
    result = _adapter(venue).submit_order(intent)
    assert result["ok"] is True and result["status"] == "placed"
    assert result["ticket"] == "new-1"
    call = venue.writes("/v5/order/create")[-1]
    _check_sign(call)
    body = json.loads(call["body"])
    assert body == {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy",
        "orderType": "Limit", "qty": "0.5",
        "orderLinkId": client_order_id(MAGIC, intent.digest),
        "price": "60000", "tpslMode": "Full",
        "stopLoss": "59000", "takeProfit": "62000",
    }


def test_spot_stops_are_refused_not_dropped(venue):
    adapter = _adapter(venue, category="spot")
    preview = adapter.order_preview(_intent())
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    assert preview["stopSupported"] is False


def test_cancel_posts_the_category_symbol_and_order(venue):
    result = _adapter(venue).cancel_order("ord-1")
    assert result["ok"] is True and result["retcodeName"] == "CANCELLED"
    call = venue.writes("/v5/order/cancel")[-1]
    _check_sign(call)
    assert json.loads(call["body"]) == {
        "category": "linear", "symbol": "BTCUSDT", "orderId": "ord-1",
    }


def test_close_position_reduces_the_venue_position(venue):
    result = _adapter(venue).close_position("BTCUSDT:0")
    assert result["ok"] is True
    body = json.loads(venue.writes("/v5/order/create")[-1]["body"])
    assert body["side"] == "Sell" and body["orderType"] == "Market"
    assert body["qty"] == "0.5" and body["reduceOnly"] is True
    assert body["positionIdx"] == 0


def test_close_refuses_an_unknown_position(venue):
    result = _adapter(venue).close_position("BTCUSDT:9")
    assert result["ok"] is False and result["retcodeName"] == "POSITION_NOT_FOUND"
    assert venue.writes("/v5/order/create") == []


def test_cancel_all_scopes_to_the_eclipse_link_id(venue):
    result = _adapter(venue).cancel_all(MAGIC)
    assert result["cancelled"] == 1
    ids = [json.loads(c["body"])["orderId"]
           for c in venue.writes("/v5/order/cancel")]
    assert ids == ["ord-1"]


def test_modify_position_posts_the_full_bracket(venue):
    result = _adapter(venue).modify_position("BTCUSDT:0", stop_loss=58500,
                                             take_profit=63000)
    assert result["ok"] is True and result["retcodeName"] == "PROTECTED"
    body = json.loads(venue.writes("/v5/position/trading-stop")[-1]["body"])
    assert body["stopLoss"] == "58500" and body["takeProfit"] == "63000"
    assert body["tpslMode"] == "Full"


def test_order_status_reads_the_realtime_order(venue):
    status = _adapter(venue).order_status("ord-1")
    assert status["found"] is True
    assert status["filledVolume"] == pytest.approx(0.1)
    assert status["clientOrderId"] == "eclipse-7-abc"
