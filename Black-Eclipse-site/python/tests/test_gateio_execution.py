"""Gate.io v4 execution against a local HTTP server: five-line HMAC-SHA512."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.gateio import GateioAdapter, payload_hash, sign_request
from harness.trade.adapters.orders import client_order_id
from harness.trade.model import OrderIntent

SECRET = "secret-2"
KEYS = "key-1:secret-2"
MAGIC = 7


def _routes(method, path, query, raw):
    body = json.loads(raw) if raw else {}
    if path == "/api/v4/spot/accounts":
        return [{"currency": "USDT", "total": "1000", "available": "900"},
                {"currency": "BTC", "total": "0.5", "available": "0.5"}]
    if path == "/api/v4/futures/usdt/positions":
        return [{"contract": "BTC_USDT", "size": "-2", "entry_price": "60000",
                 "mark_price": "61000", "unrealised_pnl": "100", "leverage": "5",
                 "funding_rate": "0.0001", "open_time": 1700000000},
                {"contract": "ETH_USDT", "size": "0"}]
    if path == "/api/v4/futures/usdt/orders" and method == "GET":
        return [{"id": "ord-1", "contract": "BTC_USDT", "type": "limit",
                 "size": "1", "left": "0.4", "price": "62000", "status": "open",
                 "text": "t-eclipse-7-abc", "create_time_ms": 1700000000000},
                {"id": "manual-1", "contract": "BTC_USDT", "type": "limit",
                 "size": "1", "left": "1", "price": "58000", "status": "open",
                 "text": "typed-by-hand", "create_time_ms": 1700000000000}]
    if path == "/api/v4/futures/usdt/tickers":
        return [{"contract": "BTC_USDT", "last": "60000.5",
                 "highest_bid": "59999", "lowest_ask": "60001"}]
    if path == "/api/v4/spot/tickers":
        return [{"currency_pair": "BTC_USDT", "last": "60000.5",
                 "highest_bid": "59999", "lowest_ask": "60001"}]
    if path == "/api/v4/futures/usdt/contracts/BTC_USDT":
        return {"name": "BTC_USDT", "type": "direct", "quanto_multiplier": "0.0001",
                "order_price_round": "0.1", "order_size_min": 1,
                "order_size_max": 1000000, "in_delivery": False}
    if path == "/api/v4/spot/currency_pairs/BTC_USDT":
        return {"id": "BTC_USDT", "base": "BTC", "quote": "USDT",
                "precision": 2, "amount_precision": 6,
                "min_base_amount": "0.0001", "trade_status": "tradable"}
    if path == "/api/v4/futures/usdt/orders" and method == "POST":
        return {"id": "new-1", "contract": body.get("contract"),
                "text": body.get("text"), "size": body.get("size")}
    if path == "/api/v4/spot/orders" and method == "POST":
        return {"id": "spot-1", "currency_pair": body.get("currency_pair"),
                "text": body.get("text"), "amount": body.get("amount")}
    if path == "/api/v4/spot/open_orders":
        return []
    if path.startswith("/api/v4/futures/usdt/orders/") and method == "DELETE":
        return {"id": path.rsplit("/", 1)[-1]}
    if path == "/api/v4/futures/usdt/price_orders":
        return {"id": "price-1", "rule": body.get("rule")}
    return {"label": "NOT_FOUND", "message": f"no route {method} {path}"}


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

            do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = _do

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
    monkeypatch.setenv("GATEIO_KEYS", KEYS)
    server = _Venue(_routes)
    yield server
    server.close()


def _adapter(venue, **config) -> GateioAdapter:
    rows = {"credentialRef": "GATEIO_KEYS", "baseUrl": venue.base,
            "market": "futures", "settle": "usdt"}
    rows.update(config)
    return GateioAdapter(rows)


def _intent(**overrides) -> OrderIntent:
    rows = dict(connection_id="c1", symbol="BTC_USDT", side="buy", order_type="limit",
                volume=2, price=60000, magic=MAGIC)
    rows.update(overrides)
    return OrderIntent(**rows)


def _check_sign(call: dict) -> None:
    path, _, query = call["path"].partition("?")
    expected = sign_request(SECRET, call["headers"]["timestamp"], call["method"],
                            path, query, call["body"])
    assert call["headers"]["sign"] == expected
    assert call["headers"]["key"] == "key-1"


def test_account_reports_the_configured_mode(venue):
    account = _adapter(venue).account()
    assert account["mode"] == "real" and account["tradeAllowed"] is True
    assert account["balance"] == pytest.approx(1000.5)


def test_a_sandbox_host_is_a_demo_account(venue, monkeypatch):
    adapter = _adapter(venue, baseUrl="https://fx-api-testnet.gateio.ws")
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: [
        {"currency": "USDT", "total": "1", "available": "1"}])
    assert adapter.account()["mode"] == "demo"


def test_symbol_info_reads_the_contract_metadata(venue):
    info = _adapter(venue).symbol_info("BTC_USDT")
    assert info["market"] == "futures"
    assert info["contractSize"] == pytest.approx(0.0001)
    assert info["volumeStep"] == pytest.approx(1.0)
    assert info["point"] == pytest.approx(0.1)


def test_preview_refuses_an_unattachable_stop(venue):
    preview = _adapter(venue).order_preview(_intent(stop_loss=59000))
    assert preview["ok"] is False
    assert preview["retcodeName"] == "STOP_UNSUPPORTED"
    assert preview["stopSupported"] is False


def test_submit_posts_the_signed_futures_order(venue):
    intent = _intent(side="sell")
    result = _adapter(venue).submit_order(intent)
    assert result["ok"] is True and result["status"] == "placed"
    assert result["ticket"] == "new-1"
    call = venue.writes("/api/v4/futures/usdt/orders")[-1]
    _check_sign(call)
    body = json.loads(call["body"])
    assert body == {
        "contract": "BTC_USDT", "size": "-2", "price": "60000", "tif": "gtc",
        "text": "t-" + client_order_id(MAGIC, intent.digest),
    }


def test_cancel_deletes_the_order(venue):
    result = _adapter(venue).cancel_order("ord-1")
    assert result["ok"] is True and result["retcodeName"] == "CANCELLED"
    call = venue.writes("/api/v4/futures/usdt/orders/")[-1]
    assert call["method"] == "DELETE" and call["path"].endswith("/orders/ord-1")
    _check_sign(call)


def test_close_position_sends_a_signed_close_order(venue):
    result = _adapter(venue).close_position("BTC_USDT:sell")
    assert result["ok"] is True
    body = json.loads(venue.writes("/api/v4/futures/usdt/orders")[-1]["body"])
    assert body["close"] is True and body["size"] == "2" and body["price"] == "0"


def test_close_refuses_an_unknown_position(venue):
    result = _adapter(venue).close_position("SOL_USDT:buy")
    assert result["ok"] is False and result["retcodeName"] == "POSITION_NOT_FOUND"
    assert venue.writes("/api/v4/futures/usdt/orders") == []


def test_cancel_all_only_deletes_eclipse_text(venue):
    result = _adapter(venue).cancel_all(MAGIC)
    assert result["cancelled"] == 1
    deletes = [c["path"] for c in venue.calls if c["method"] == "DELETE"]
    assert deletes == ["/api/v4/futures/usdt/orders/ord-1"]


def test_cancel_all_refuses_without_client_text(venue, monkeypatch):
    adapter = _adapter(venue)
    monkeypatch.setattr(adapter, "orders", lambda: [
        {"ticket": "x", "symbol": "BTC_USDT", "clientOrderId": ""}])
    result = adapter.cancel_all(MAGIC)
    assert result["ok"] is False and result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"


def test_modify_position_places_a_price_triggered_stop(venue):
    result = _adapter(venue).modify_position("BTC_USDT:sell", stop_loss=61500)
    assert result["ok"] is True and result["retcodeName"] == "PROTECTED"
    body = json.loads(venue.writes("/api/v4/futures/usdt/price_orders")[-1]["body"])
    assert body["rule"] == 1
    assert body["trigger"]["price"] == "61500"
    assert body["order_type"] == "close-short-position"


def test_a_spot_market_buy_is_quote_sized(venue):
    adapter = _adapter(venue, market="spot")
    preview = adapter.order_preview(_intent(order_type="market"))
    assert preview["quoteSized"] is True
    result = adapter.submit_order(_intent(order_type="market"))
    assert result["ok"] is True and result["ticket"] == "spot-1"
    call = venue.writes("/api/v4/spot/orders")[-1]
    _check_sign(call)
    body = json.loads(call["body"])
    assert body["type"] == "market" and body["side"] == "buy"
    assert body["amount"] == "120002"   # 2 base at the 60001 ask
    assert "price" not in body
