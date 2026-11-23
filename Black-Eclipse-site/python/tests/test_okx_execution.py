"""OKX v5 execution against a local HTTP server: no real network, no fake fills."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.okx import OkxAdapter, sign_request
from harness.trade.adapters.orders import client_order_id
from harness.trade.model import OrderIntent

SECRET = "secret-2"
KEYS = "key-1:secret-2:pass-3"
MAGIC = 7


def _account():
    return {"code": "0", "data": [{"totalEq": "1000", "availEq": "900",
                                   "imr": "10", "upl": "5"}]}


def _instrument(inst_id, tick="0.1", lot="0.01", ct_val="0.01"):
    return {"code": "0", "data": [{
        "instId": inst_id, "tickSz": tick, "lotSz": lot, "minSz": lot,
        "maxMktSz": "100", "ctVal": ct_val, "state": "live",
        "settleCcy": "USDT", "baseCcy": inst_id.split("-")[0],
        "quoteCcy": "USDT", "ctValCcy": inst_id.split("-")[0],
    }]}


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
                payload = venue.routes(self.command, path, query, call["body"])
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = _do

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


def _routes(method, path, query, raw):
    body = json.loads(raw) if raw else {}
    if path == "/api/v5/account/balance":
        return _account()
    if path == "/api/v5/account/positions":
        return {"code": "0", "data": [
            {"posId": "pos-1", "instId": "BTC-USDT-SWAP", "pos": "2",
             "posSide": "long", "avgPx": "59000", "markPx": "60000", "upl": "20"},
            {"posId": "pos-2", "instId": "ETH-USDT-SWAP", "pos": "0",
             "posSide": "long"},
        ]}
    if path == "/api/v5/trade/orders-pending":
        return {"code": "0", "data": [
            {"ordId": "ord-1", "instId": "BTC-USDT-SWAP", "ordType": "limit",
             "sz": "0.5", "px": "59000", "state": "live", "side": "buy",
             "clOrdId": "eclipse7abc", "cTime": "1700000000000"},
            {"ordId": "manual-1", "instId": "BTC-USDT-SWAP", "ordType": "limit",
             "sz": "0.5", "px": "59000", "state": "live", "side": "buy",
             "clOrdId": "hand-placed"},
        ]}
    if path == "/api/v5/trade/orders-history":
        return {"code": "0", "data": []}
    if path == "/api/v5/public/instruments":
        return _instrument("BTC-USDT-SWAP" if "SWAP" in query else "BTC-USDT")
    if path == "/api/v5/market/ticker":
        return {"code": "0", "data": [{"bidPx": "59990", "askPx": "60010",
                                       "last": "60000", "ts": "1700000000000"}]}
    if path == "/api/v5/trade/order" and method == "POST":
        return {"code": "0", "data": [{"ordId": "new-1", "sCode": "0", "sMsg": "",
                                       "clOrdId": body.get("clOrdId")}]}
    if path == "/api/v5/trade/order":
        return {"code": "0", "data": [{
            "instId": "BTC-USDT-SWAP", "state": "partially_filled", "accFillSz": "0.02",
            "avgPx": "59950", "clOrdId": "eclipse7abc",
        }]}
    if path == "/api/v5/trade/cancel-order":
        return {"code": "0", "data": [{"ordId": body.get("ordId"), "sCode": "0"}]}
    if path == "/api/v5/trade/amend-order":
        return {"code": "0", "data": [{"ordId": body.get("ordId"), "sCode": "0"}]}
    if path == "/api/v5/trade/order-algo":
        return {"code": "0", "data": [{"algoId": "algo-1", "sCode": "0"}]}
    return {"code": "59999", "msg": f"no route {method} {path}"}


@pytest.fixture
def venue(monkeypatch):
    monkeypatch.setenv("OKX_KEYS", KEYS)
    server = _Venue(_routes)
    yield server
    server.close()


def _adapter(venue, **config) -> OkxAdapter:
    rows = {"credentialRef": "OKX_KEYS", "baseUrl": venue.base, "instType": "SWAP"}
    rows.update(config)
    return OkxAdapter(rows)


def _intent(**overrides) -> OrderIntent:
    rows = dict(connection_id="c1", symbol="BTC-USDT-SWAP", side="buy",
                order_type="limit", volume=0.05, price=60000,
                stop_loss=59000, take_profit=62000, magic=MAGIC)
    rows.update(overrides)
    return OrderIntent(**rows)


def _client_id(intent: OrderIntent) -> str:
    return client_order_id(intent.magic, intent.digest).replace("-", "")[:32]


def _check_sign(venue, call, method):
    path = call["path"]
    expected = sign_request(SECRET, call["headers"]["ok-access-timestamp"],
                            method, path, call["body"])
    assert call["headers"]["ok-access-sign"] == expected
    assert call["headers"]["ok-access-key"] == "key-1"
    assert call["headers"]["ok-access-passphrase"] == "pass-3"


def test_account_reports_the_configured_mode(venue):
    account = _adapter(venue).account()
    assert account["mode"] == "real" and account["tradeAllowed"] is True
    assert account["equity"] == pytest.approx(1000.0)


def test_a_sandbox_host_is_a_demo_account(venue, monkeypatch):
    adapter = _adapter(venue, baseUrl="https://sandbox.okx.com")
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: _account()["data"])
    assert adapter.account()["mode"] == "demo"


def test_preview_uses_the_instrument_step_and_ownership_tag(venue):
    intent = _intent()
    preview = _adapter(venue).order_preview(intent)
    assert preview["ok"] is True, preview["reasons"]
    assert preview["volume"] == pytest.approx(0.05)
    assert preview["volumeStep"] == pytest.approx(0.01)
    assert preview["clientOrderId"] == _client_id(intent)
    assert preview["derivative"] is True
    assert preview["tdMode"] == "cross"


def test_submit_posts_the_signed_order_with_stops(venue):
    adapter = _adapter(venue)
    intent = _intent()
    result = adapter.submit_order(intent)
    assert result["ok"] is True and result["status"] == "placed"
    assert result["ticket"] == "new-1"
    call = venue.writes("/api/v5/trade/order")[-1]
    _check_sign(venue, call, "POST")
    body = json.loads(call["body"])
    assert body == {
        "instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "buy",
        "ordType": "limit", "sz": "0.05", "clOrdId": _client_id(intent),
        "px": "60000", "tpTriggerPx": "62000", "tpOrdPx": "-1",
        "slTriggerPx": "59000", "slOrdPx": "-1",
    }


def test_bad_stop_direction_is_refused_before_any_send(venue):
    result = _adapter(venue).submit_order(_intent(stop_loss=61000))
    assert result["ok"] is False and result["status"] == "REFUSED"
    assert venue.writes("/api/v5/trade/order") == []


def test_spot_stops_are_refused_not_dropped(venue):
    preview = _adapter(venue).order_preview(_intent(symbol="BTC-USDT"))
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    assert preview["stopSupported"] is False
    assert any("instType=SPOT" in call["path"] for call in venue.calls)


def test_a_spot_market_buy_is_sized_in_the_base_currency(venue):
    result = _adapter(venue).submit_order(_intent(
        symbol="BTC-USDT", order_type="market", stop_loss=None, take_profit=None))
    assert result["ok"] is True
    body = json.loads(venue.writes("/api/v5/trade/order")[-1]["body"])
    assert body["tdMode"] == "cash" and body["tgtCcy"] == "base_ccy"
    assert "px" not in body and body["sz"] == "0.05"


def test_cancel_posts_the_instid_and_ordid(venue):
    result = _adapter(venue).cancel_order("ord-1")
    assert result["ok"] is True and result["retcodeName"] == "CANCELLED"
    call = venue.writes("/api/v5/trade/cancel-order")[-1]
    _check_sign(venue, call, "POST")
    assert json.loads(call["body"]) == {"instId": "BTC-USDT-SWAP", "ordId": "ord-1"}


def test_cancel_refuses_an_unknown_order(venue):
    result = _adapter(venue).cancel_order("nope")
    assert result["ok"] is False and result["retcodeName"] == "ORDER_NOT_FOUND"
    assert venue.writes("/api/v5/trade/cancel-order") == []


def test_close_position_sends_the_opposite_reduce_only_market_order(venue):
    result = _adapter(venue).close_position("pos-1")
    assert result["ok"] is True
    body = json.loads(venue.writes("/api/v5/trade/order")[-1]["body"])
    assert body["side"] == "sell" and body["ordType"] == "market"
    assert body["sz"] == "2" and body["instId"] == "BTC-USDT-SWAP"
    assert body["posSide"] == "long"


def test_close_refuses_a_position_the_venue_does_not_report(venue):
    result = _adapter(venue).close_position("pos-9")
    assert result["ok"] is False and result["retcodeName"] == "POSITION_NOT_FOUND"
    assert venue.writes("/api/v5/trade/order") == []


def test_cancel_all_only_cancels_eclipse_orders(venue):
    result = _adapter(venue).cancel_all(MAGIC)
    assert result["cancelled"] == 1
    bodies = [json.loads(call["body"]) for call in venue.writes("/api/v5/trade/cancel-order")]
    assert bodies == [{"instId": "BTC-USDT-SWAP", "ordId": "ord-1"}]


def test_order_status_reads_the_order_endpoint(venue):
    status = _adapter(venue).order_status("ord-1")
    assert status["found"] is True
    assert status["state"] == "partially_filled"
    assert status["filledVolume"] == pytest.approx(0.02)
    assert status["clientOrderId"] == "eclipse7abc"


def test_cancel_all_refuses_without_a_client_order_id(venue, monkeypatch):
    adapter = _adapter(venue)
    monkeypatch.setattr(adapter, "orders", lambda: [
        {"ticket": "x", "symbol": "BTC-USDT-SWAP", "clientOrderId": ""}])
    result = adapter.cancel_all(MAGIC)
    assert result["ok"] is False and result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"


def test_amend_posts_the_new_trigger_prices(venue):
    result = _adapter(venue).modify_order("ord-1", price=59500, stop_loss=58000)
    assert result["ok"] is True and result["retcodeName"] == "AMENDED"
    body = json.loads(venue.writes("/api/v5/trade/amend-order")[-1]["body"])
    assert body == {"instId": "BTC-USDT-SWAP", "ordId": "ord-1",
                    "newPx": "59500", "newSlTriggerPx": "58000"}


def test_the_signature_is_over_the_sent_body(venue):
    _adapter(venue).submit_order(_intent())
    call = venue.writes("/api/v5/trade/order")[-1]
    manual = base64.b64encode(hmac.new(
        b"secret-2",
        f"{call['headers']['ok-access-timestamp']}POST{call['path']}"
        .encode() + call["body"].encode(),
        hashlib.sha256,
    ).digest()).decode()
    assert call["headers"]["ok-access-sign"] == manual
