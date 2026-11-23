"""Bitstamp v2 adapter: body-borne auth, monotonic nonce, spot balances."""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.bitstamp import BitstampAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> BitstampAdapter:
    base = {"credentialRef": "BITSTAMP_KEYS"}
    base.update(config)
    return BitstampAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("BITSTAMP_KEYS", "key-1:secret-2:cust-3")


def test_availability_is_stdlib_only():
    assert BitstampAdapter.availability() == (True, None)


def test_the_signature_covers_nonce_customer_id_and_key():
    expected = hmac.new(b"secret-2", b"1700000000cust-3key-1", hashlib.sha256).hexdigest()
    assert sign_request("secret-2", "1700000000", "cust-3", "key-1") == expected


def test_the_customer_id_is_part_of_the_preimage():
    assert sign_request("s", "1", "cust-A", "k") != sign_request("s", "1", "cust-B", "k")


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_private_call_posts_the_credential_in_the_body(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["method"] = request.get_method()
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = urllib.parse.parse_qs((request.data or b"").decode())
        return _Resp(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._call("/api/v2/balance/")
    assert captured["method"] == "POST"
    assert captured["body"]["key"] == ["key-1"]
    assert captured["body"]["signature"][0]
    assert captured["body"]["nonce"][0].isdigit()
    # Auth is in the body: no Authorization header is invented.
    assert "authorization" not in captured["h"]


def test_the_public_ticker_is_an_unsigned_get(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["method"] = request.get_method()
        captured["body"] = request.data
        return _Resp(b'{"last": "60000"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._call("/api/v2/ticker/btcusd/", private=False)
    assert captured["method"] == "GET"
    assert captured["body"] is None


def test_the_nonce_is_strictly_increasing(monkeypatch):
    adapter = _adapter()
    first = adapter._nonce()
    second = adapter._nonce()
    third = adapter._nonce()
    assert int(second) > int(first) and int(third) > int(second)


def test_credentials_must_have_three_parts(monkeypatch):
    monkeypatch.setenv("BITSTAMP_KEYS", "key:secret")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BITSTAMP_CREDENTIALS"


def test_account_sums_balances_from_the_flat_payload(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call", lambda path, private=True: {
        "btc_balance": "0.5", "btc_available": "0.4", "btc_fee": "0.5",
        "usd_balance": "1000.00", "usd_available": "900.00", "usd_fee": "0.2",
    })
    account = adapter.account()
    assert account["server"] == "bitstamp"
    assert account["balance"] == pytest.approx(1000.5)
    assert account["marginFree"] == pytest.approx(900.4)
    assert account["tradeAllowed"] is True


def test_positions_are_balances_and_not_marked(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_balances", lambda: {
        "BTC": {"balance": 0.5, "available": 0.5},
        "USD": {"balance": 0.0, "available": 0.0},
    })
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC"]
    assert rows[0]["currentPrice"] == 0.0
    assert "not marked" in rows[0]["comment"]


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call", lambda path, private=True: [
        {"id": 77, "currency_pair": "btcusd", "type": "1", "amount": "0.25",
         "price": "60000", "datetime": "1700000000"},
    ])
    order = adapter.orders()[0]
    assert order["ticket"] == "77"
    assert order["symbol"] == "BTCUSD"
    assert order["volumeCurrent"] == pytest.approx(0.25)
    assert order["placedAt"] == 1700000000000


def test_quote_strips_the_pair_of_separators(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call",
                        lambda path, private=True: {"last": "60000.5", "bid": "59999",
                                                    "ask": "60001", "timestamp": "1700000000"})
    quote = adapter.quote("BTC/USD")
    assert quote["symbol"] == "BTCUSD"
    assert quote["last"] == pytest.approx(60000.5)
    assert quote["serverTimeMs"] == 1700000000000


def test_registry_declares_the_kind_executable():
    kind = get_kind("bitstamp")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "bitstamp" for row in list_kinds())
