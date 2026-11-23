"""Alpaca broker adapter: REST mapping + FIFO deal normalization."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.alpaca import AlpacaAdapter

ACCOUNT = {
    "account_number": "PA123",
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "5000",
    "equity": "10500",
    "last_equity": "10400",
    "initial_margin": "1000",
    "buying_power": "4000",
    "multiplier": "4",
    "trading_blocked": False,
}

ORDERS = [
    {
        "id": "ord-open", "symbol": "AAPL", "side": "buy", "filled_qty": "10",
        "filled_avg_price": "100", "filled_at": "2026-11-01T14:00:00Z",
        "status": "filled", "type": "market", "qty": "10",
        "submitted_at": "2026-11-01T14:00:00Z",
    },
    {
        "id": "ord-close", "symbol": "AAPL", "side": "sell", "filled_qty": "10",
        "filled_avg_price": "110", "filled_at": "2026-11-01T15:00:00Z",
        "status": "filled", "type": "market", "qty": "10",
        "submitted_at": "2026-11-01T15:00:00Z",
    },
]

POSITIONS = [
    {"symbol": "AAPL", "qty": "10", "avg_entry_price": "100", "current_price": "110",
     "unrealized_pl": "100"}
]

OPEN_ORDERS = [
    {"id": "order-eclipse", "symbol": "AAPL", "side": "buy", "qty": "1",
     "status": "new", "type": "limit", "limit_price": "100",
     "client_order_id": "eclipse-77-abc123456789", "submitted_at": "2026-11-01T14:00:00Z"},
    {"id": "order-manual", "symbol": "AAPL", "side": "buy", "qty": "1",
     "status": "new", "type": "limit", "limit_price": "100",
     "client_order_id": "manual-1", "submitted_at": "2026-11-01T14:00:00Z"},
]
CLOSED_ORDERS = ORDERS


class Handler(BaseHTTPRequestHandler):
    token = None
    SUBMITTED: list = []
    CANCELLED: list = []

    def log_message(self, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if Handler.token:
            if self.headers.get("APCA-API-KEY-ID") != "key" or self.headers.get(
                "APCA-API-SECRET-KEY"
            ) != "secret":
                self._send(401, {"message": "unauthorized"})
                return
        path, _, query = self.path.partition("?")
        if path == "/v2/account":
            self._send(200, ACCOUNT)
        elif path == "/v2/positions":
            self._send(200, POSITIONS)
        elif path == "/v2/orders":
            if "status=open" in query:
                self._send(200, OPEN_ORDERS)
            else:
                self._send(200, ORDERS)
        elif path.startswith("/v2/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            row = next((o for o in CLOSED_ORDERS if o["id"] == order_id), None)
            self._send(200, row) if row else self._send(404, {"message": "not found"})
        elif path == "/v2/stocks/AAPL/quotes/latest":
            self._send(200, {"quote": {"bp": 109.9, "ap": 110.1, "t": "2026-11-01T15:00:00Z"}})
        else:
            self._send(404, {"message": "not found"})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):  # noqa: N802
        if self.headers.get("APCA-API-KEY-ID") != "key":
            self._send(401, {"message": "unauthorized"})
            return
        body = self._body()
        if self.path == "/v2/orders" and body.get("symbol") == "AAPL":
            Handler.SUBMITTED.append(body)
            self._send(200, {
                "id": "order-new", "symbol": "AAPL", "side": body.get("side"),
                "qty": body.get("qty"), "status": "accepted",
                "filled_qty": "0", "filled_avg_price": None,
                "client_order_id": body.get("client_order_id"),
            })
        else:
            self._send(422, {"message": "rejected by test venue"})

    def do_DELETE(self):  # noqa: N802
        if self.headers.get("APCA-API-KEY-ID") != "key":
            self._send(401, {"message": "unauthorized"})
            return
        path, _, _query = self.path.partition("?")
        if path == "/v2/positions/AAPL":
            self._send(200, {"symbol": "AAPL", "qty": "10", "side": "long",
                             "avg_entry_price": "100"})
        elif path.startswith("/v2/orders/"):
            Handler.CANCELLED.append(path.rsplit("/", 1)[-1])
            self._send(204, None)
        else:
            self._send(404, {"message": "not found"})

    def do_PATCH(self):  # noqa: N802
        body = self._body()
        self._send(200, {"id": self.path.rsplit("/", 1)[-1], "symbol": "AAPL",
                         "status": "new", "limit_price": body.get("limit_price")})


@pytest.fixture()
def api(monkeypatch):
    Handler.token = None
    Handler.SUBMITTED = []
    Handler.CANCELLED = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("TEST_ALPACA_KEYS", raising=False)


def adapter_for(url: str) -> AlpacaAdapter:
    return AlpacaAdapter({
        "baseUrl": url,
        "dataUrl": url,
        "credentialRef": "TEST_ALPACA_KEYS",
    })


def test_account_positions_quote(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    adapter = adapter_for(api)
    assert adapter.account()["equity"] == 10500.0
    # The trade layer's account-mode vocabulary is demo|real (MT5's own words).
    assert adapter.account()["mode"] == "real"
    positions = adapter.positions()
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["ticket"] == "AAPL"
    assert positions[0]["side"] == "buy"
    quote = adapter.quote("aapl")
    assert quote["bid"] == 109.9
    assert adapter.probe()["connected"] is True


def test_filled_orders_become_fifo_round_trips(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    deals = adapter_for(api).deals_window(2)
    entries = [d for d in deals if d["entry"] == "in"]
    exits = [d for d in deals if d["entry"] == "out"]
    assert len(entries) == 1
    assert len(exits) == 1
    assert entries[0]["price"] == 100
    assert exits[0]["price"] == 110
    assert exits[0]["profit"] == 100
    assert exits[0]["pnlBasis"] == "computed"


def test_missing_and_bad_credentials_are_named_refusals(api):
    with pytest.raises(AdapterUnavailable) as info:
        adapter_for(api).account()
    assert info.value.code == "ALPACA_CREDENTIALS"


# ── execution ────────────────────────────────────────────────────────────────

def _intent(**overrides):
    from harness.trade.model import OrderIntent

    values = dict(
        connection_id="conn", symbol="AAPL", side="buy", order_type="market",
        volume=2, price=None, stop_loss=None, take_profit=None, magic=77,
    )
    values.update(overrides)
    return OrderIntent(**values)


def test_preview_normalises_the_order_and_bracket(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    preview = adapter_for(api).order_preview(
        _intent(order_type="limit", price=100, stop_loss=95, take_profit=110)
    )
    assert preview["ok"] is True
    assert preview["symbol"] == "AAPL"
    assert preview["volume"] == 2
    assert preview["price"] == 100
    assert preview["notional"] == 200
    assert preview["margin"] == 200
    assert preview["openPositions"] == 1


def test_a_bracket_on_the_wrong_side_is_refused_before_sending(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    preview = adapter_for(api).order_preview(
        _intent(order_type="limit", price=100, stop_loss=105, take_profit=110)
    )
    assert preview["ok"] is False
    assert any("stop loss must be below" in reason for reason in preview["reasons"])


def test_submit_sends_one_bracketed_order_with_the_ownership_tag(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    result = adapter_for(api).submit_order(
        _intent(order_type="limit", price=100, stop_loss=95, take_profit=110)
    )
    assert result["ok"] is True
    assert result["ticket"] == "order-new"
    assert result["status"] == "placed"
    sent = Handler.SUBMITTED[-1]
    assert sent["symbol"] == "AAPL"
    assert sent["order_class"] == "bracket"
    assert sent["client_order_id"].startswith("eclipse-77-")
    assert sent["stop_loss"]["stop_price"] == "95.000000"
    assert sent["take_profit"]["limit_price"] == "110.000000"


def test_cancel_all_only_touches_eclipse_orders(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    result = adapter_for(api).cancel_all(77)
    assert result["cancelled"] == 1
    assert Handler.CANCELLED == ["order-eclipse"]


def test_close_by_symbol_is_risk_reducing(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    result = adapter_for(api).close_position("AAPL")
    assert result["ok"] is True
    assert result["ticket"] == "AAPL"
    assert result["verified"] is True


def test_replace_price_and_refuse_a_bracket_edit(api, monkeypatch):
    monkeypatch.setenv("TEST_ALPACA_KEYS", "key:secret")
    Handler.token = "key"
    adapter = adapter_for(api)
    replaced = adapter.modify_order("order-eclipse", price=101.5)
    assert replaced["ok"] is True
    refused = adapter.modify_order("order-eclipse", stop_loss=90)
    assert refused["ok"] is False
    assert refused["retcodeName"] == "ORDER_BRACKET_UNSUPPORTED"
