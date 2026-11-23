"""MT4 bridge adapter: the documented JSON contract.

A real local HTTP server proves the contract end-to-end without MT4, a bridge,
or the network. The adapter must map the JSON into the normal broker shapes and
must report a refused bridge honestly (never a fake connection).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import os

import pytest

from harness.persistence import AgentMemoryStore
from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.mt4_bridge import MT4BridgeAdapter
from harness.trade.reconcile import BrokerReconciler
from harness.trade.service import BrokerService

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
        "symbol": "eurusd",
        "side": "buy",
        "volume": 0.1,
        "openPrice": 1.1,
        "currentPrice": 1.11,
        "stopLoss": 1.09,
        "takeProfit": 1.12,
        "profit": 10.0,
        "swap": -0.5,
        "magic": 99,
        "comment": "bridge",
        "openedAt": 1_700_000_000_000,
    }
]

DEALS = [
    {
        "dealTicket": 11,
        "orderTicket": 901,
        "positionTicket": 5,
        "symbol": "eurusd",
        "side": "buy",
        "entry": "in",
        "volume": 0.1,
        "price": 1.1,
        "commission": -1.0,
        "swap": 0.0,
        "profit": 0.0,
        "magic": 99,
        "comment": "",
        "dealtAt": 1_700_000_000_000,
    }
]


class Handler(BaseHTTPRequestHandler):
    token = None

    def log_message(self, *args):  # noqa: D401 - silence the test server
        return

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib contract
        if Handler.token and self.headers.get("Authorization") != f"Bearer {Handler.token}":
            self._send(401, {"error": "unauthorized"})
            return

        path, _, query = self.path.partition("?")
        if path == "/account":
            self._send(200, ACCOUNT)
        elif path == "/positions":
            self._send(200, {"positions": POSITIONS})
        elif path == "/orders":
            self._send(200, {"orders": [{"ticket": 900, "symbol": "EURUSD"}]})
        elif path == "/orders/900":
            self._send(200, {"found": True, "ticket": 900, "state": 4, "stateName": "filled"})
        elif path in ("/orders/999", "/orders/123"):
            self._send(404, {"error": "not found"})
        elif path == "/quote":
            if "symbol=FAIL" in f"?{query}":
                self._send(401, {"error": "unauthorized"})
            else:
                self._send(200, {
                    "symbol": "EURUSD", "bid": 1.1, "ask": 1.1001, "last": 1.10005,
                    "spreadPoints": 1, "digits": 5, "point": 0.00001,
                    "stopsLevelPoints": 0, "serverTimeMs": 1_700_000_000_000,
                })
        elif path == "/deals":
            self._send(200, {"deals": DEALS})
        else:
            self._send(404, {"error": "unknown"})


@pytest.fixture()
def bridge(monkeypatch):
    Handler.token = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        monkeypatch.delenv("TEST_BRIDGE_TOKEN", raising=False)


def test_contract_reads_map_to_broker_shapes(bridge):
    adapter = MT4BridgeAdapter({"bridgeUrl": bridge, "magic": 99})
    assert adapter.account()["balance"] == 10000.0
    positions = adapter.positions()
    assert positions[0]["symbol"] == "EURUSD"
    assert positions[0]["side"] == "buy"
    orders = adapter.orders()
    assert orders[0]["ticket"] == 900
    quote = adapter.quote("eurusd")
    assert quote["bid"] == 1.1
    assert quote["symbol"] == "EURUSD"

    deals = adapter.deals_window(1)
    assert deals[0]["dealTicket"] == 11
    assert deals[0]["eclipseOwned"] is True  # magic matches the config default 0? no
    assert adapter.order_status(900)["stateName"] == "filled"
    assert adapter.order_status(999) == {"found": False, "ticket": 999}


def test_probe_is_honest_when_the_bridge_is_missing():
    adapter = MT4BridgeAdapter({"bridgeUrl": "http://127.0.0.1:1"})
    probe = adapter.probe()
    assert probe["available"] is True
    assert probe["connected"] is False
    assert probe["code"] == "BRIDGE_UNREACHABLE"


def test_secret_is_a_name_and_is_sent_as_a_bearer(bridge, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "s3cret")
    Handler.token = "s3cret"
    adapter = MT4BridgeAdapter({"bridgeUrl": bridge, "bridgeSecretRef": "TEST_BRIDGE_TOKEN"})
    assert adapter.account()["login"] == 123456
    with pytest.raises(AdapterUnavailable):
        adapter.quote("FAIL")


def test_missing_secret_env_is_a_named_refusal(bridge):
    adapter = MT4BridgeAdapter({"bridgeUrl": bridge, "bridgeSecretRef": "NO_SUCH_TOKEN"})
    with pytest.raises(AdapterUnavailable) as info:
        adapter.account()
    assert info.value.code == "BRIDGE_SECRET"


def test_read_only_bridge_still_feeds_reconciliation(bridge, tmp_path):
    store = AgentMemoryStore(os.path.join(str(tmp_path), "bridge.db"))
    service = BrokerService(store)
    connection = service.create_connection(
        kind="mt4_bridge",
        label="MT4 bridge",
        mode="demo",
        config={"bridgeUrl": bridge, "magic": 99},
    )
    service.connections.set_status(connection["id"], status="connected")

    result = BrokerReconciler(service).sync(connection["id"])
    assert result["ok"] is True
    assert result["fillsAdded"] == 1
    assert service.connections.list_fills(connection["id"])[0]["dealTicket"] == 11


def test_bad_url_is_refused():
    adapter = MT4BridgeAdapter({"bridgeUrl": "file:///etc/passwd"})
    with pytest.raises(AdapterUnavailable) as info:
        adapter.account()
    assert info.value.code == "BRIDGE_CONFIG"
