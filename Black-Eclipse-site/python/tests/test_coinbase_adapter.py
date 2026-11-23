"""Coinbase Exchange adapter: prehash, unsigned public path, spot semantics."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.coinbase import CoinbaseAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds

SECRET = base64.b64encode(b"top-secret").decode()
CREDS = f"key-1:{SECRET}:pass-9"


def _adapter(**config) -> CoinbaseAdapter:
    base = {"credentialRef": "COINBASE_KEYS"}
    base.update(config)
    return CoinbaseAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("COINBASE_KEYS", CREDS)


def test_availability_is_stdlib_only():
    assert CoinbaseAdapter.availability() == (True, None)


def test_the_signature_is_hmac_over_timestamp_method_path_body():
    expected = base64.b64encode(hmac.new(
        b"top-secret", b"1700000000GET/accounts", hashlib.sha256).digest()).decode()
    assert sign_request(SECRET, "1700000000", "GET", "/accounts") == expected


def test_the_query_string_is_part_of_the_signed_path():
    a = sign_request(SECRET, "1", "GET", "/orders?status=open")
    b = sign_request(SECRET, "1", "GET", "/orders")
    assert a != b


def test_a_signed_call_sends_the_three_headers(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'[]'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["url"] = request.full_url
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/accounts")
    assert captured["h"]["cb-access-key"] == "key-1"
    assert captured["h"]["cb-access-passphrase"] == "pass-9"
    assert captured["h"]["cb-access-timestamp"].isdigit()
    assert captured["h"]["cb-access-sign"]


def test_the_public_ticker_carries_no_credentials(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"price":"60000"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter.quote("BTC-USD")
    assert not any(k.startswith("cb-access") for k in captured["h"])


def test_credentials_must_have_three_parts(monkeypatch):
    monkeypatch.setenv("COINBASE_KEYS", "key:SECRET")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "COINBASE_CREDENTIALS"


def test_missing_credentials_prints_the_expected_shape():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "COINBASE_CREDENTIALS"
    assert "KEY:SECRET:PASSPHRASE" in exc.value.reason


def test_account_sums_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_accounts", lambda: [
        {"currency": "BTC", "balance": "0.5"},
        {"currency": "USD", "balance": "1000.00"},
    ])
    account = adapter.account()
    assert account["server"] == "coinbase"
    assert account["equity"] == pytest.approx(1000.5)
    assert account["tradeAllowed"] is True


def test_positions_are_balances_not_marked_to_market(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_accounts", lambda: [
        {"currency": "BTC", "balance": "0.5"},
        {"currency": "ETH", "balance": "0.00000000"},
    ])
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC"]
    assert rows[0]["currentPrice"] == 0.0
    assert "not marked" in rows[0]["comment"]


def test_orders_normalize_and_parse_iso_timestamps(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [{
        "id": "abc-1", "product_id": "BTC-USD", "type": "limit", "size": "1.0",
        "filled_size": "0.25", "price": "59000", "status": "open", "side": "buy",
        "created_at": "2026-11-01T12:00:00.000000Z",
    }])
    order = adapter.orders()[0]
    assert order["ticket"] == "abc-1"
    assert order["volumeCurrent"] == pytest.approx(0.75)
    assert order["placedAt"] > 1_700_000_000_000
    assert "buy" in order["comment"]


def test_quote_uses_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: {
                            "price": "60000.5", "bid": "59999", "ask": "60001"})
    quote = adapter.quote("btc-usd")
    assert quote["symbol"] == "BTC-USD"
    assert quote["last"] == pytest.approx(60000.5)
    assert quote["bid"] == pytest.approx(59999.0)


def test_an_api_error_message_is_a_named_refusal(monkeypatch):
    adapter = _adapter()

    class _Resp:
        def read(self, _n=None):
            return b'{"message":"Invalid API Key"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.error
    def raising(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raising)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._request("/accounts")
    assert exc.value.code == "COINBASE_UNAUTHORIZED"


def test_registry_declares_the_kind_executable():
    kind = get_kind("coinbase")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "coinbase" for row in list_kinds())
