"""Zerodha execution: Kite form payload, tag ownership, close verification."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapters.zerodha import ZerodhaAdapter
from harness.trade.model import OrderIntent

PROFILE = {"user_id": "AB1234", "broker": "ZERODHA"}
MARGINS = {"equity": {"net": 250000.5, "available": {"cash": 120000.0},
                      "utilised": {"debits": 5000.0}}}
POSITIONS = {"net": [
    {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS",
     "quantity": 5, "average_price": 2400.0, "last_price": 2450.0, "pnl": 250.0},
], "day": []}
QUOTE = {"NSE:RELIANCE": {
    "instrument_token": 738561, "last_price": 2450.0,
    "depth": {"buy": [{"price": 2449.5}], "sell": [{"price": 2450.5}]},
}}
ORDERS = [
    {"order_id": "220101", "tradingsymbol": "RELIANCE", "order_type": "LIMIT",
     "pending_quantity": 5, "price": 2400.0, "status": "OPEN",
     "transaction_type": "BUY", "tag": "eclipse-77-abc12345", "order_timestamp": ""},
    {"order_id": "220105", "tradingsymbol": "RELIANCE", "order_type": "LIMIT",
     "pending_quantity": 5, "price": 2300.0, "status": "OPEN",
     "transaction_type": "BUY", "tag": "", "order_timestamp": ""},
]
CSV = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,Reliance Industries,2450,0,0,0.05,1,EQ,NSE,NSE\n"
)


class Handler(BaseHTTPRequestHandler):
    SENT: list = []
    DELETED: list = []
    PUT: list = []
    TAGS = True

    def log_message(self, *args):
        return

    def _path(self) -> str:
        return self.path.partition("?")[0]

    def _send(self, status, payload, content_type="application/json"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _data(self, payload):
        self._send(200, {"status": "success", "data": payload})

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        return {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                self.rfile.read(length).decode()
            ).items()
        }

    def do_GET(self):  # noqa: N802
        path = self._path()
        if path == "/user/profile":
            self._data(PROFILE)
        elif path == "/user/margins":
            self._data(MARGINS)
        elif path == "/portfolio/positions":
            self._data(POSITIONS)
        elif path == "/quote":
            self._data(QUOTE)
        elif path == "/instruments/NSE":
            self._send(200, CSV, "text/csv")
        elif path == "/orders":
            rows = ORDERS if self.TAGS else [{**row, "tag": ""} for row in ORDERS]
            self._data(rows)
        elif path == "/orders/220101":
            self._data([{"order_id": "220101", "status": "OPEN", "order_type": "LIMIT",
                         "filled_quantity": 0, "average_price": 0}])
        elif path == "/orders/220103":
            self._data([{"order_id": "220103", "status": "OPEN", "order_type": "SL-M",
                         "filled_quantity": 0, "average_price": 0}])
        elif path == "/orders/close-1":
            self._data([{"order_id": "close-1", "status": "COMPLETE",
                         "order_type": "MARKET", "filled_quantity": 5,
                         "average_price": 2451.0}])
        else:
            self._send(404, {"status": "error", "message": "not found"})

    def do_POST(self):  # noqa: N802
        assert self.path == "/orders/regular"
        assert self.headers.get("X-Kite-Version") == "3"
        assert self.headers.get("Authorization") == "token key123:access456"
        form = self._form()
        Handler.SENT.append(form)
        self._data({"order_id": "close-1" if form.get("transaction_type") == "SELL" else "220102"})

    def do_PUT(self):  # noqa: N802
        Handler.PUT.append({"path": self._path(), "form": self._form()})
        self._send(200, {"status": "success"})

    def do_DELETE(self):  # noqa: N802
        path = self._path()
        order_id = path.rsplit("/", 1)[-1]
        if order_id == "999":
            self._send(404, {"status": "error", "message": "not found"})
            return
        Handler.DELETED.append(order_id)
        self._send(200, {"status": "success"})


@pytest.fixture()
def api(monkeypatch):
    Handler.SENT = []
    Handler.DELETED = []
    Handler.PUT = []
    Handler.TAGS = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("TEST_KITE_TOKEN", "key123:access456")
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def adapter_for(url: str) -> ZerodhaAdapter:
    return ZerodhaAdapter({"baseUrl": url, "credentialRef": "TEST_KITE_TOKEN"})


def _intent(**overrides):
    values = dict(
        connection_id="conn", symbol="NSE:RELIANCE", side="buy", order_type="limit",
        volume=5, price=2400.0, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


# ── reads / mode ─────────────────────────────────────────────────────────────

def test_mode_is_real_here_and_demo_on_a_demo_host(api, monkeypatch):
    assert adapter_for(api).account()["mode"] == "real"
    demo = ZerodhaAdapter({
        "baseUrl": "https://sandbox.kite.trade", "credentialRef": "TEST_KITE_TOKEN",
    })
    payloads = {"/user/profile": PROFILE, "/user/margins": MARGINS}
    monkeypatch.setattr(demo, "_get", lambda path, params=None: payloads.get(path))
    assert demo.account()["mode"] == "demo"
    assert demo.account()["tradeAllowed"] is True


def test_positions_and_orders_carry_string_tickets(api):
    adapter = adapter_for(api)
    assert adapter.positions()[0]["ticket"] == "RELIANCE"
    assert adapter.orders()[0]["ticket"] == "220101"


def test_symbol_info_reads_the_instruments_csv(api):
    info = adapter_for(api).symbol_info("NSE:RELIANCE")
    assert info["symbol"] == "NSE:RELIANCE"
    assert info["point"] == pytest.approx(0.05)
    assert info["digits"] == 2
    assert info["contractSize"] == pytest.approx(1.0)
    assert info["instrumentToken"] == "738561"


# ── preview ──────────────────────────────────────────────────────────────────

def test_preview_normalises_quantity_and_product(api):
    preview = adapter_for(api).order_preview(_intent(volume=5.9))
    assert preview["ok"] is True
    assert preview["volume"] == 5
    assert preview["price"] == 2400.0
    assert preview["notional"] == 12000.0
    assert preview["margin"] == 12000.0
    assert preview["product"] == "CNC"
    assert preview["kiteOrderType"] == "LIMIT"


def test_a_day_order_uses_the_mis_product(api):
    preview = adapter_for(api).order_preview(_intent(time_in_force="day"))
    assert preview["product"] == "MIS"


def test_brackets_and_bare_symbols_are_refused_by_name(api):
    adapter = adapter_for(api)
    bracket = adapter.order_preview(_intent(stop_loss=2350.0))
    assert any("ZERODHA_BRACKET_UNSUPPORTED" in reason for reason in bracket["reasons"])
    bare = adapter.order_preview(_intent(symbol="RELIANCE"))
    assert any("ZERODHA_SYMBOL_AMBIGUOUS" in reason for reason in bare["reasons"])


# ── submit ───────────────────────────────────────────────────────────────────

def test_submit_sends_one_tagged_limit_order(api):
    result = adapter_for(api).submit_order(_intent())
    assert len(Handler.SENT) == 1
    form = Handler.SENT[0]
    assert form["tradingsymbol"] == "RELIANCE"
    assert form["exchange"] == "NSE"
    assert form["transaction_type"] == "BUY"
    assert form["order_type"] == "LIMIT"
    assert form["quantity"] == "5"
    assert form["product"] == "CNC"
    assert form["validity"] == "DAY"
    assert form["price"] == "2400.00"
    assert form["tag"].startswith("eclipse-77-")
    assert len(form["tag"]) <= 20
    assert result["ok"] is True
    assert result["status"] == "placed"
    assert result["ticket"] == "220102"


def test_a_stop_uses_sl_m_and_a_trigger_price(api):
    adapter_for(api).submit_order(_intent(order_type="stop", price=2500.0))
    form = Handler.SENT[0]
    assert form["order_type"] == "SL-M"
    assert form["trigger_price"] == "2500.00"
    assert "price" not in form


def test_preview_rejection_never_sends(api):
    result = adapter_for(api).submit_order(_intent(stop_loss=2350.0))
    assert result["ok"] is False
    assert result["retcodeName"] == "PREVIEW_REJECTED"
    assert Handler.SENT == []


# ── cancel / close ───────────────────────────────────────────────────────────

def test_cancel_and_a_404_is_not_success(api):
    adapter = adapter_for(api)
    assert adapter.cancel_order("220101")["ok"] is True
    missing = adapter.cancel_order("999")
    assert missing["ok"] is False
    assert missing["retcodeName"] == "ORDER_NOT_FOUND"


def test_cancel_all_only_cancels_tagged_eclipse_orders(api):
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert Handler.DELETED == ["220101"]


def test_cancel_all_refuses_when_no_order_carries_a_tag(api):
    Handler.TAGS = False
    result = adapter_for(api).cancel_all(77)
    assert result["ok"] is False
    assert result["retcodeName"] == "OWNERSHIP_UNAVAILABLE"
    assert Handler.DELETED == []


def test_close_sells_the_position_and_verifies_the_fill(api):
    result = adapter_for(api).close_position("RELIANCE")
    assert len(Handler.SENT) == 1
    form = Handler.SENT[0]
    assert form["transaction_type"] == "SELL"
    assert form["order_type"] == "MARKET"
    assert form["quantity"] == "5"
    assert form["product"] == "MIS"
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["price"] == pytest.approx(2451.0)
    assert result["volume"] == pytest.approx(5)


def test_partial_close_uses_the_requested_quantity(api):
    result = adapter_for(api).close_position("RELIANCE", volume=2)
    assert Handler.SENT[0]["quantity"] == "2"
    assert result["partial"] is True


def test_close_refuses_more_than_is_held(api):
    result = adapter_for(api).close_position("RELIANCE", volume=50)
    assert result["ok"] is False
    assert result["retcodeName"] == "INVALID_VOLUME"
    assert Handler.SENT == []


# ── modify / status ──────────────────────────────────────────────────────────

def test_modify_replaces_a_limit_price_but_not_a_stop(api):
    adapter = adapter_for(api)
    replaced = adapter.modify_order("220101", price=2399.0)
    assert replaced["ok"] is True
    assert Handler.PUT[0]["path"] == "/orders/220101"
    assert Handler.PUT[0]["form"]["price"] == "2399.00"
    refused = adapter.modify_order("220103", price=2399.0)
    assert refused["ok"] is False
    assert refused["retcodeName"] == "ORDER_MODIFY_UNSUPPORTED"


def test_order_status_reads_the_history(api):
    status = adapter_for(api).order_status("220101")
    assert status["found"] is True
    assert status["stateName"] == "OPEN"
    assert adapter_for(api).order_status("nope")["found"] is False
