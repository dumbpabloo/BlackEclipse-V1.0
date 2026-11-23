"""Bitget v2 execution against a local HTTP server: base64 signatures, mix orders."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.bitget import BitgetAdapter, sign_request
from harness.trade.adapters.orders import client_order_id
from harness.trade.model import OrderIntent

SECRET = "secret-2"
KEYS = "key-1:secret-2:pass-3"
MAGIC = 7


def _ok(data):
    return {"code": "00000", "msg": "success", "data": data}


def _routes(method, path, query, raw):
    body = json.loads(raw) if raw else {}
    if path == "/api/v2/spot/account/assets":
        return _ok([{"coin": "USDT", "balance": "1000", "available": "900"},
                    {"coin": "BTC", "balance": "0.5", "available": "0.5"}])
    if path == "/api/v2/mix/position/all-position":
        return _ok([{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                     "openPriceAvg": "60000", "markPrice": "61000",
                     "unrealizedPL": "500", "stopLoss": "59000", "totalFee": "-1.2",
                     "cTime": "1700000000000", "marginMode": "crossed"},
                    {"symbol": "ETHUSDT", "holdSide": "short", "total": "0"}])
    if path == "/api/v2/mix/order/orders-pending":
        return _ok([{"orderId": "ord-1", "symbol": "BTCUSDT", "side": "sell",
                     "orderType": "limit", "size": "1", "baseVolume": "0.25",
                     "price": "62000", "status": "live", "cTime": "1700000000000",
                     "clientOid": "eclipse-7-abc"},
                    {"orderId": "manual-1", "symbol": "BTCUSDT", "side": "buy",
                     "orderType": "limit", "size": "1", "baseVolume": "0",
                     "price": "58000", "status": "live", "cTime": "1700000000000",
                     "clientOid": "typed-by-hand"}])
    if path == "/api/v2/mix/order/detail":
        return _ok({"orderId": "ord-1", "symbol": "BTCUSDT", "status": "partial_fill",
                    "baseVolume": "0.25", "priceAvg": "61900",
                    "clientOid": "eclipse-7-abc"})
    if path == "/api/v2/mix/market/ticker":
        return _ok([{"lastPr": "60000.5", "bidPr": "59999", "askPr": "60001",
                     "ts": "1700000000000"}])
    if path == "/api/v2/mix/market/contracts":
        return _ok([{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
                     "pricePlace": "1", "volumePlace": "3", "minTradeNum": "0.001",
                     "maxTradeNum": "100", "symbolStatus": "normal"}])
    if path == "/api/v2/spot/public/symbols":
        return _ok([{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
                     "pricePrecision": "2", "quantityPrecision": "6",
                     "minTradeAmount": "0.0001", "maxTradeAmount": "100",
                     "status": "online"}])
    if path == "/api/v2/spot/market/tickers":
        return _ok([{"lastPr": "60000.5", "bidPr": "59999", "askPr": "60001",
                     "ts": "1700000000000"}])
    if path == "/api/v2/mix/order/place-order":
        return _ok({"orderId": "new-1", "clientOid": body.get("clientOid")})
    if path == "/api/v2/mix/order/cancel-order":
        return _ok({"orderId": body.get("orderId")})
    if path == "/api/v2/mix/order/place-tpsl-order":
        return _ok({"orderId": "tpsl-1", "clientOid": "tpsl"})
    return {"code": "40001", "msg": f"no route {method} {path}", "data": []}


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
    monkeypatch.setenv("BITGET_KEYS", KEYS)
    server = _Venue(_routes)
    yield server
    server.close()


def _adapter(venue, **config) -> BitgetAdapter:
    rows = {"credentialRef": "BITGET_KEYS", "baseUrl": venue.base,
            "productType": "USDT-FUTURES"}
    rows.update(config)
    return BitgetAdapter(rows)


def _intent(**overrides) -> OrderIntent:
    rows = dict(connection_id="c1", symbol="BTCUSDT", side="buy", order_type="limit",
                volume=0.5, price=60000, stop_loss=59000, take_profit=62000, magic=MAGIC)
    rows.update(overrides)
    return OrderIntent(**rows)


def _check_sign(call: dict) -> None:
    path = call["path"]
    expected = sign_request(SECRET, call["headers"]["access-timestamp"], "POST",
                            path, call["body"])
    assert call["headers"]["access-sign"] == expected
    assert call["headers"]["access-key"] == "key-1"
    assert call["headers"]["access-passphrase"] == "pass-3"


def test_account_reports_the_configured_mode(venue):
    account = _adapter(venue).account()
    assert account["mode"] == "real" and account["tradeAllowed"] is True
    assert account["balance"] == pytest.approx(1000.5)


def test_a_sandbox_host_is_a_demo_account(venue, monkeypatch):
    adapter = _adapter(venue, baseUrl="https://sandbox.bitget.com")
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: [
        {"coin": "USDT", "balance": "1", "available": "1"}])
    assert adapter.account()["mode"] == "demo"


def test_preview_reads_the_volume_place_and_product(venue):
    preview = _adapter(venue).order_preview(_intent())
    assert preview["ok"] is True, preview["reasons"]
    assert preview["volume"] == pytest.approx(0.5)
    assert preview["volumeStep"] == pytest.approx(0.001)
    assert preview["productType"] == "USDT-FUTURES"
    assert preview["stopSupported"] is True


def test_submit_posts_the_signed_mix_order_with_presets(venue):
    intent = _intent()
    result = _adapter(venue).submit_order(intent)
    assert result["ok"] is True and result["status"] == "placed"
    assert result["ticket"] == "new-1"
    call = venue.writes("/api/v2/mix/order/place-order")[-1]
    _check_sign(call)
    body = json.loads(call["body"])
    assert body == {
        "symbol": "BTCUSDT", "side": "buy", "orderType": "limit", "force": "gtc",
        "size": "0.5", "price": "60000",
        "clientOid": client_order_id(MAGIC, intent.digest),
        "productType": "USDT-FUTURES", "marginMode": "crossed", "marginCoin": "USDT",
        "presetStopSurplusPrice": "62000", "presetStopLossPrice": "59000",
    }


def test_the_signature_covers_method_path_and_body(venue):
    _adapter(venue).submit_order(_intent())
    call = venue.writes("/api/v2/mix/order/place-order")[-1]
    manual = base64.b64encode(hmac.new(
        b"secret-2",
        f"{call['headers']['access-timestamp']}POST{call['path']}".encode()
        + call["body"].encode(),
        hashlib.sha256,
    ).digest()).decode()
    assert call["headers"]["access-sign"] == manual


def test_spot_stops_are_refused_not_dropped(venue):
    preview = _adapter(venue, productType="SPOT").order_preview(_intent())
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    assert preview["stopSupported"] is False


def test_cancel_posts_the_symbol_and_order(venue):
    result = _adapter(venue).cancel_order("ord-1")
    assert result["ok"] is True and result["retcodeName"] == "CANCELLED"
    call = venue.writes("/api/v2/mix/order/cancel-order")[-1]
    _check_sign(call)
    assert json.loads(call["body"]) == {
        "symbol": "BTCUSDT", "orderId": "ord-1", "productType": "USDT-FUTURES",
    }


def test_close_position_sends_a_reduce_only_market_order(venue):
    result = _adapter(venue).close_position("BTCUSDT:long")
    assert result["ok"] is True
    body = json.loads(venue.writes("/api/v2/mix/order/place-order")[-1]["body"])
    assert body["side"] == "sell" and body["orderType"] == "market"
    assert body["size"] == "0.5" and body["reduceOnly"] == "YES"


def test_close_refuses_an_unknown_position(venue):
    result = _adapter(venue).close_position("SOLUSDT:long")
    assert result["ok"] is False and result["retcodeName"] == "POSITION_NOT_FOUND"
    assert venue.writes("/api/v2/mix/order/place-order") == []


def test_cancel_all_scopes_to_the_eclipse_client_oid(venue):
    result = _adapter(venue).cancel_all(MAGIC)
    assert result["cancelled"] == 1
    ids = [json.loads(c["body"])["orderId"]
           for c in venue.writes("/api/v2/mix/order/cancel-order")]
    assert ids == ["ord-1"]


def test_modify_position_places_a_position_tpsl_leg(venue):
    result = _adapter(venue).modify_position("BTCUSDT:long", stop_loss=58500)
    assert result["ok"] is True and result["retcodeName"] == "PROTECTED"
    body = json.loads(venue.writes("/api/v2/mix/order/place-tpsl-order")[-1]["body"])
    assert body["planType"] == "pos_loss" and body["triggerPrice"] == "58500"
    assert body["holdSide"] == "long"


def test_order_status_reads_the_mix_detail(venue):
    status = _adapter(venue).order_status("ord-1")
    assert status["found"] is True
    assert status["state"] == "partial_fill"
    assert status["filledVolume"] == pytest.approx(0.25)
