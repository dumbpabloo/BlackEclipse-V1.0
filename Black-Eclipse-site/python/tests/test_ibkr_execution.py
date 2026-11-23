"""IBKR Client Portal execution: preview, submit, confirmation, cancel, close.

A local HTTP gateway proves the CPAPI contract end-to-end. The reply-confirmation
path is exercised explicitly: exactly one `/iserver/reply` post is allowed, and a
second question must become a named refusal instead of a loop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.ibkr import IBKRAdapter
from harness.trade.model import OrderIntent

SUMMARY = {
    "currency": "USD",
    "availablefunds": "5000",
    "netliquidation": "10500",
    "initialmargin": "1000",
    "unrealizedpnl": "100",
}

POSITIONS = [
    {
        "conid": 265598,
        "contractDesc": "AAPL",
        "position": "10",
        "avgCost": "100",
        "mktPrice": "110",
        "unrealizedPnl": "100",
    }
]

LIVE_ORDERS = {
    "orders": [
        {"orderId": 9001, "ticker": "AAPL", "orderType": "LMT", "side": "BUY",
         "totalSize": 10, "price": 100.0, "tif": "DAY", "status": "Submitted",
         "remainingQuantity": 10},
    ]
}


class Handler(BaseHTTPRequestHandler):
    cookie = None
    order_posts: list[dict] = []
    modify_posts: list[dict] = []
    reply_posts: list[tuple[str, dict]] = []
    deletes: list[str] = []
    order_response: list = []
    reply_response: list = []
    positions_rows: list = []
    on_order = None

    def log_message(self, *args):
        return

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        if Handler.cookie and self.headers.get("Cookie") != Handler.cookie:
            self._send(401, {"error": "unauthorized"})
            return
        path, _, query = self.path.partition("?")
        if path == "/iserver/accounts":
            self._send(200, {"accounts": ["DU123"], "selectedAccount": "DU123"})
        elif path in ("/portfolio/DU123/summary", "/portfolio/U123456/summary"):
            self._send(200, SUMMARY)
        elif path.endswith("/positions/0"):
            self._send(200, Handler.positions_rows)
        elif path == "/iserver/account/orders":
            self._send(200, LIVE_ORDERS)
        elif path == "/iserver/secdef/search":
            symbol = "NONE" if "symbol=NONE" in f"?{query}" else "AAPL"
            conid = 111222 if symbol == "NONE" else 265598
            self._send(200, [{"conid": conid, "symbol": symbol,
                              "assetClass": "STK", "companyHeader": symbol}])
        elif path == "/iserver/marketdata/snapshot":
            self._send(200, [{"31": "110", "84": "109.9", "86": "110.1"}])
        elif path == "/iserver/account/order/status/9001":
            self._send(200, {"order_status": "Filled", "filledQuantity": 10,
                             "averagePrice": 110.1, "remainingQuantity": 0})
        elif path == "/iserver/contract/265598/info":
            self._send(200, {"symbol": "AAPL", "company_name": "Apple Inc",
                             "currency": "USD", "tickSize": 0.01, "multiplier": "1"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        body = self._body()
        if self.path.startswith("/iserver/reply/"):
            Handler.reply_posts.append((self.path, body))
            self._send(200, Handler.reply_response)
            return
        if self.path.startswith("/iserver/account/DU123/order/"):
            Handler.modify_posts.append(body)
            self._send(200, [{"order_id": self.path.rsplit("/", 1)[-1],
                              "order_status": "Submitted"}])
            return
        if self.path == "/iserver/account/DU123/orders":
            Handler.order_posts.append(body)
            if Handler.on_order is not None:
                Handler.on_order(body)
            self._send(200, Handler.order_response)
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        Handler.deletes.append(self.path)
        if self.path == "/iserver/account/DU123/order/9001":
            self._send(200, {"msg": "Request was submitted", "order_id": 9001})
        else:
            self._send(404, {"error": "not found"})


def reset() -> None:
    Handler.cookie = None
    Handler.order_posts = []
    Handler.modify_posts = []
    Handler.reply_posts = []
    Handler.deletes = []
    Handler.order_response = [{"order_id": "9001", "order_status": "Submitted"}]
    Handler.reply_response = [{"order_id": "9001", "order_status": "Submitted"}]
    Handler.positions_rows = list(POSITIONS)
    Handler.on_order = None


@pytest.fixture()
def gateway(monkeypatch):
    reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_IBKR_COOKIE", "SESSION=abc")
    Handler.cookie = "SESSION=abc"
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("TEST_IBKR_COOKIE", raising=False)


def adapter_for(url: str, account: str = "DU123") -> IBKRAdapter:
    return IBKRAdapter({
        "baseUrl": url,
        "accountId": account,
        "credentialRef": "TEST_IBKR_COOKIE",
    })


def intent(**overrides) -> OrderIntent:
    payload = {"symbol": "AAPL", "side": "buy", "type": "market", "volume": 10}
    payload.update(overrides)
    return OrderIntent.from_payload(payload, connection_id="conn")


def test_account_mode_is_demo_for_du_and_real_otherwise(gateway):
    assert adapter_for(gateway).account()["mode"] == "demo"
    assert adapter_for(gateway, "U123456").account()["mode"] == "real"


def test_positions_use_conid_and_orders_use_full_id(gateway):
    adapter = adapter_for(gateway)
    assert adapter.positions()[0]["ticket"] == "265598"
    assert adapter.orders()[0]["ticket"] == 9001


def test_preview_posts_nothing(gateway):
    preview = adapter_for(gateway).order_preview(intent())
    assert preview["ok"] is True
    assert preview["conid"] == 265598
    assert preview["volume"] == 10
    assert preview["price"] == 110.1  # buy pays the ask
    assert preview["notional"] == 1101.0
    assert Handler.order_posts == []


def test_submit_market_order_sends_the_cpapi_body(gateway):
    result = adapter_for(gateway).submit_order(intent())
    assert result["ok"] is True
    assert result["status"] == "placed"  # Submitted is never reported as a fill
    assert result["ticket"] == "9001"
    assert len(Handler.order_posts) == 1
    order = Handler.order_posts[0]["orders"][0]
    assert order["conid"] == 265598
    assert order["secType"] == "STK"
    assert order["orderType"] == "MKT"
    assert order["side"] == "BUY"
    assert order["quantity"] == 10
    assert order["tif"] == "GTC"
    assert order["outsideRth"] is True
    assert order["cOID"].startswith("eclipse-")


def test_submit_limit_order_carries_its_price(gateway):
    result = adapter_for(gateway).submit_order(
        intent(type="limit", price=105.5, timeInForce="day")
    )
    assert result["ok"] is True
    order = Handler.order_posts[0]["orders"][0]
    assert order["orderType"] == "LMT"
    assert order["price"] == 105.5
    assert order["tif"] == "DAY"


def test_one_confirmation_question_is_answered_exactly_once(gateway):
    Handler.order_response = [{"id": "q1", "message": ["Confirm this order"]}]
    result = adapter_for(gateway).submit_order(intent())
    assert result["ok"] is True
    assert result["ticket"] == "9001"
    assert result["confirmed"] is True
    assert len(Handler.reply_posts) == 1
    path, body = Handler.reply_posts[0]
    assert path == "/iserver/reply/q1"
    assert body == {"confirmed": True}


def test_second_confirmation_question_is_a_refusal_not_a_loop(gateway):
    Handler.order_response = [{"id": "q1", "message": ["Confirm this order"]}]
    Handler.reply_response = [{"id": "q2", "message": ["Confirm it again"]}]
    result = adapter_for(gateway).submit_order(intent())
    assert result["ok"] is False
    assert result["status"] == "REFUSED"
    assert result["retcodeName"] == "IBKR_CONFIRMATION_REQUIRED"
    # Exactly one reply was sent; a second question stops the sequence.
    assert len(Handler.reply_posts) == 1
    assert len(Handler.order_posts) == 1
    assert result["questions"][1] == ["Confirm it again"]


def test_attached_stop_is_a_named_refusal_and_nothing_is_posted(gateway):
    result = adapter_for(gateway).submit_order(intent(stopLoss=95))
    assert result["ok"] is False
    assert result["retcodeName"] == "BRACKET_UNSUPPORTED"
    assert "separate order" in result["reasons"][0]
    assert Handler.order_posts == []


def test_cancel_order_reports_the_request_without_claiming_verified(gateway):
    result = adapter_for(gateway).cancel_order("9001")
    assert result["ok"] is True
    assert result["retcodeName"] == "CANCEL_REQUESTED"
    assert result["verified"] is False
    assert Handler.deletes == ["/iserver/account/DU123/order/9001"]


def test_cancel_unknown_order_is_order_not_found(gateway):
    result = adapter_for(gateway).cancel_order("404")
    assert result["ok"] is False
    assert result["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_refuses_without_touching_orders(gateway):
    result = adapter_for(gateway).cancel_all(7)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert Handler.deletes == []


def test_close_position_submits_the_opposite_market_order(gateway):
    Handler.on_order = lambda body: setattr(Handler, "positions_rows", [])
    result = adapter_for(gateway).close_position("265598")
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["side"] == "sell"
    order = Handler.order_posts[0]["orders"][0]
    assert order["orderType"] == "MKT"
    assert order["side"] == "SELL"
    assert order["quantity"] == 10


def test_close_missing_position_is_a_named_refusal(gateway):
    result = adapter_for(gateway).close_position("999999")
    assert result["ok"] is False
    assert result["retcodeName"] == "POSITION_NOT_FOUND"
    assert Handler.order_posts == []


def test_modify_position_is_refused_because_cpapi_has_no_position_bracket(gateway):
    result = adapter_for(gateway).modify_position("265598", stop_loss=95)
    assert result["ok"] is False
    assert result["retcodeName"] == "POSITION_BRACKET_UNSUPPORTED"


def test_modify_order_re_prices_a_working_order(gateway):
    result = adapter_for(gateway).modify_order(9001, price=101.5)
    assert result["ok"] is True
    assert result["retcodeName"] == "MODIFY_SUBMITTED"
    assert Handler.modify_posts[0]["price"] == 101.5
    assert Handler.modify_posts[0]["conid"] == 265598


def test_order_status_reads_the_status_endpoint(gateway):
    status = adapter_for(gateway).order_status("9001")
    assert status["found"] is True
    assert status["stateName"] == "FILLED"
    assert status["filledVolume"] == 10
    assert adapter_for(gateway).order_status("999") == {"found": False, "ticket": "999"}


def test_symbol_info_uses_contract_info_or_refuses(gateway):
    info = adapter_for(gateway).symbol_info("AAPL")
    assert info["symbol"] == "AAPL"
    assert info["digits"] == 2
    assert info["contractSize"] == 1.0
    with pytest.raises(AdapterUnavailable) as excinfo:
        adapter_for(gateway).symbol_info("NONE")
    assert excinfo.value.code == "SYMBOL_INFO_NOT_SUPPORTED"
