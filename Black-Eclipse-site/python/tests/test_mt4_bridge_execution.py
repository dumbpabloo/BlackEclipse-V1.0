"""MT4 bridge execution: the documented write contract, end to end.

The bridge is operator-run, so Eclipse must speak exactly the documented JSON and
must treat a bridge without the write endpoints as a named refusal rather than a
crash or a fake fill.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.mt4_bridge import MT4BridgeAdapter
from harness.trade.model import OrderIntent

ACCOUNT = {
    "login": 123456,
    "mode": "demo",
    "server": "Broker-Demo",
    "company": "Broker Ltd",
    "currency": "USD",
    "balance": 10000.0,
    "equity": 10050.0,
    "margin": 100.0,
    "marginFree": 9950.0,
    "marginLevel": 10050.0,
    "profit": 50.0,
    "leverage": 100,
    "marginMode": "hedging",
    "tradeAllowed": True,
    "expertAllowed": True,
}

POSITIONS = [
    {
        "ticket": 5,
        "symbol": "EURUSD",
        "side": "buy",
        "volume": 0.1,
        "openPrice": 1.1,
        "currentPrice": 1.11,
        "stopLoss": 1.09,
        "takeProfit": 1.12,
        "profit": 10.0,
        "swap": -0.5,
        "magic": 7,
        "comment": "bridge",
        "openedAt": 1_700_000_000_000,
    }
]

ORDERS = [
    {"ticket": 900, "symbol": "EURUSD", "type": "limit", "volumeCurrent": 0.1,
     "openPrice": 1.2, "magic": 7, "state": "placed"},
    {"ticket": 901, "symbol": "EURUSD", "type": "limit", "volumeCurrent": 0.1,
     "openPrice": 1.3, "magic": 9, "state": "placed"},
]


class Handler(BaseHTTPRequestHandler):
    token = None
    writes_supported = True
    mode = "demo"
    positions_rows: list = []
    orders_rows: list = []
    posts: list[tuple[str, dict]] = []
    symbol_supported = True

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

    def _find_position(self, ticket: str):
        return next(
            (row for row in Handler.positions_rows if str(row.get("ticket")) == ticket),
            None,
        )

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/account":
            self._send(200, {**ACCOUNT, "mode": Handler.mode})
        elif path == "/positions":
            self._send(200, {"positions": Handler.positions_rows})
        elif path == "/orders":
            self._send(200, {"orders": Handler.orders_rows})
        elif path == "/orders/9001/status":
            self._send(200, {"found": True, "ticket": 9001, "state": 4,
                             "stateName": "placed", "filledVolume": 0})
        elif path == "/orders/9002":
            self._send(200, {"ticket": 9002, "state": 4, "stateName": "placed"})
        elif path.startswith("/orders/"):
            self._send(404, {"error": "not found"})
        elif path == "/quote":
            self._send(200, {
                "symbol": "EURUSD", "bid": 1.1, "ask": 1.1001, "last": 1.10005,
                "spreadPoints": 1, "digits": 5, "point": 0.00001,
                "stopsLevelPoints": 0, "serverTimeMs": 1_700_000_000_000,
                "volumeMin": 0.01, "volumeMax": 100.0, "volumeStep": 0.01,
                "contractSize": 100000,
            })
        elif path == "/symbol":
            if not Handler.symbol_supported or "symbol=NONE" in f"?{query}":
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {
                    "symbol": "EURUSD", "description": "Euro vs US Dollar",
                    "digits": 5, "point": 0.00001, "tickSize": 0.00001,
                    "tickValue": 1.0, "contractSize": 100000, "volumeMin": 0.01,
                    "volumeMax": 100.0, "volumeStep": 0.01, "stopsLevelPoints": 0,
                })
        elif path == "/deals":
            self._send(200, {"deals": []})
        else:
            self._send(404, {"error": "unknown"})

    def do_POST(self):  # noqa: N802
        body = self._body()
        Handler.posts.append((self.path, body))
        if not Handler.writes_supported:
            self._send(404, {"error": "not found"})
            return
        if self.path == "/orders":
            pending = body.get("type") != "market"
            self._send(200, {
                "ok": True,
                "ticket": 501,
                "price": 0.0 if pending else 1.1001,
                "retcode": 10008 if pending else 10009,
                "retcodeName": "PLACED" if pending else "DONE",
                "reasons": [],
                "message": "",
            })
            return
        if self.path.endswith("/cancel"):
            ticket = int(self.path.split("/")[2])
            self._send(200, {"ok": True, "ticket": ticket, "retcode": 10009,
                             "retcodeName": "DONE", "reasons": []})
            return
        if self.path.startswith("/positions/") and self.path.endswith("/close"):
            ticket = self.path.split("/")[2]
            position = self._find_position(ticket)
            if position is None:
                self._send(404, {"ok": False, "retcodeName": "POSITION_NOT_FOUND",
                                 "reasons": [f"no position {ticket}"]})
                return
            held = float(position["volume"])
            wanted = float(body.get("volume") or held)
            if wanted >= held - 1e-9:
                Handler.positions_rows.remove(position)
            else:
                position["volume"] = round(held - wanted, 8)
            self._send(200, {"ok": True, "ticket": int(ticket), "volume": wanted,
                             "price": 1.1001, "retcode": 10009,
                             "retcodeName": "DONE", "reasons": []})
            return
        if self.path.startswith("/positions/") and self.path.endswith("/modify"):
            ticket = self.path.split("/")[2]
            position = self._find_position(ticket)
            if position is None:
                self._send(404, {"ok": False, "retcodeName": "POSITION_NOT_FOUND"})
                return
            for key in ("stopLoss", "takeProfit"):
                if key in body:
                    position[key] = body[key]
            self._send(200, {"ok": True, "ticket": int(ticket), "retcode": 10009,
                             "retcodeName": "DONE", **{
                                 key: position[key]
                                 for key in ("stopLoss", "takeProfit") if key in body
                             }})
            return
        if self.path.startswith("/orders/") and self.path.endswith("/modify"):
            ticket = int(self.path.split("/")[2])
            self._send(200, {"ok": True, "ticket": ticket, "retcode": 10009,
                             "retcodeName": "DONE", "price": body.get("price")})
            return
        self._send(404, {"error": "not found"})


def reset() -> None:
    Handler.writes_supported = True
    Handler.mode = "demo"
    Handler.positions_rows = [dict(row) for row in POSITIONS]
    Handler.orders_rows = [dict(row) for row in ORDERS]
    Handler.posts = []
    Handler.symbol_supported = True


@pytest.fixture()
def bridge():
    reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> MT4BridgeAdapter:
    return MT4BridgeAdapter({"bridgeUrl": url, "magic": 7})


def intent(**overrides) -> OrderIntent:
    payload = {"symbol": "EURUSD", "side": "buy", "type": "market", "volume": 0.1}
    payload.update(overrides)
    return OrderIntent.from_payload(payload, connection_id="conn", magic_default=7)


def test_account_mode_maps_the_bridge_vocabulary(bridge):
    adapter = adapter_for(bridge)
    for reported, expected in (
        ("demo", "demo"), ("contest", "demo"), ("live", "real"),
        ("real", "real"), ("", "unknown"), ("mystery", "unknown"),
    ):
        Handler.mode = reported
        assert adapter.account()["mode"] == expected, reported


def test_preview_normalises_volume_and_posts_nothing(bridge):
    preview = adapter_for(bridge).order_preview(intent(volume=0.013))
    assert preview["ok"] is True
    assert preview["volume"] == 0.01
    assert preview["volumeStep"] == 0.01
    assert preview["contractSize"] == 100000.0
    assert preview["price"] == 1.1001
    assert round(preview["notional"], 2) == 1100.10
    assert round(preview["margin"], 3) == 11.001
    assert Handler.posts == []


def test_submit_sends_the_documented_write_body(bridge):
    result = adapter_for(bridge).submit_order(intent(volume=0.1, comment="eclipse"))
    assert result["ok"] is True
    assert result["status"] == "FILLED"
    assert result["ticket"] == 501
    assert len(Handler.posts) == 1
    path, body = Handler.posts[0]
    assert path == "/orders"
    assert body == {
        "symbol": "EURUSD", "side": "buy", "volume": 0.1, "type": "market",
        "price": 0.0, "stopLoss": None, "takeProfit": None, "magic": 7,
        "comment": "eclipse", "deviation": 20,
    }


def test_pending_order_is_placed_not_reported_as_filled(bridge):
    result = adapter_for(bridge).submit_order(intent(type="limit", price=1.2))
    assert result["ok"] is True
    assert result["status"] == "placed"
    assert result["ticket"] == 501


def test_cancel_order_posts_to_the_ticket_endpoint(bridge):
    result = adapter_for(bridge).cancel_order(900)
    assert result["ok"] is True
    assert result["retcodeName"] == "DONE"
    assert Handler.posts[0][0] == "/orders/900/cancel"


def test_close_position_verifies_the_position_is_gone(bridge):
    result = adapter_for(bridge).close_position(5)
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["partial"] is False
    assert Handler.posts[0][0] == "/positions/5/close"
    assert Handler.posts[0][1] == {}
    assert Handler.positions_rows == []


def test_partial_close_leaves_the_position_and_is_not_verified(bridge):
    result = adapter_for(bridge).close_position(5, volume=0.05)
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["verified"] is False
    assert Handler.posts[0][1] == {"volume": 0.05}
    assert Handler.positions_rows[0]["volume"] == 0.05


def test_close_missing_position_is_a_named_refusal(bridge):
    result = adapter_for(bridge).close_position(999)
    assert result["ok"] is False
    assert result["retcodeName"] == "POSITION_NOT_FOUND"
    assert Handler.posts == []


def test_modify_position_updates_the_bracket_and_verifies_it(bridge):
    result = adapter_for(bridge).modify_position(5, stop_loss=1.095, take_profit=1.13)
    assert result["ok"] is True
    assert result["verified"] is True
    assert Handler.posts[0] == (
        "/positions/5/modify", {"stopLoss": 1.095, "takeProfit": 1.13}
    )


def test_modify_order_re_prices_a_working_order(bridge):
    result = adapter_for(bridge).modify_order(900, price=1.25)
    assert result["ok"] is True
    assert Handler.posts[0] == ("/orders/900/modify", {"price": 1.25})


def test_cancel_all_only_cancels_orders_with_the_connection_magic(bridge):
    result = adapter_for(bridge).cancel_all(7)
    assert result["ok"] is True
    assert result["cancelled"] == 1
    assert [path for path, _ in Handler.posts] == ["/orders/900/cancel"]


def test_cancel_all_refuses_a_zero_magic(bridge):
    result = adapter_for(bridge).cancel_all(0)
    assert result["ok"] is False
    assert result["retcodeName"] == "MAGIC_REQUIRED"
    assert Handler.posts == []


def test_bridge_without_write_endpoints_is_a_named_refusal(bridge):
    Handler.writes_supported = False
    adapter = adapter_for(bridge)
    submitted = adapter.submit_order(intent())
    assert submitted["ok"] is False
    assert submitted["retcodeName"] == "BRIDGE_EXECUTION_UNSUPPORTED"
    cancelled = adapter.cancel_order(900)
    assert cancelled["ok"] is False
    assert cancelled["retcodeName"] == "BRIDGE_EXECUTION_UNSUPPORTED"


def test_order_status_prefers_status_then_falls_back_to_the_read(bridge):
    adapter = adapter_for(bridge)
    assert adapter.order_status(9001)["stateName"] == "placed"
    assert adapter.order_status(9002)["stateName"] == "placed"
    assert adapter.order_status(999) == {"found": False, "ticket": 999}


def test_symbol_info_reads_the_optional_endpoint_or_refuses(bridge):
    info = adapter_for(bridge).symbol_info("EURUSD")
    assert info["symbol"] == "EURUSD"
    assert info["volumeStep"] == 0.01
    with pytest.raises(AdapterUnavailable) as excinfo:
        adapter_for(bridge).symbol_info("NONE")
    assert excinfo.value.code == "SYMBOL_INFO_NOT_SUPPORTED"
