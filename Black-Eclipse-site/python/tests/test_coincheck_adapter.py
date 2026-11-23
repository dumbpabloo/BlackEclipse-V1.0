"""Coincheck adapter: full-URL signature, monotonic nonce, spot balances."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.coincheck import CoincheckAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> CoincheckAdapter:
    base = {"credentialRef": "COINCHECK_KEYS"}
    base.update(config)
    return CoincheckAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("COINCHECK_KEYS", "key-1:secret-2")


def test_availability_is_stdlib_only():
    assert CoincheckAdapter.availability() == (True, None)


def test_the_signature_covers_nonce_and_the_full_url():
    url = "https://coincheck.com/api/accounts/balance"
    expected = hmac.new(b"secret-2", f"1700000000000{url}".encode(),
                        hashlib.sha256).hexdigest()
    assert sign_request("secret-2", "1700000000000", url) == expected


def test_signing_the_path_alone_would_not_match():
    # The mistake this pins: a path-only preimage validates locally, fails remotely.
    url = "https://coincheck.com/api/accounts/balance"
    assert sign_request("s", "1", url) != sign_request("s", "1", "/api/accounts/balance")


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_signed_call_sends_the_three_headers(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        return _Resp(b'{"success":true,"jpy":"1000"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/accounts/balance")
    assert captured["h"]["access-key"] == "key-1"
    assert captured["h"]["access-nonce"].isdigit()
    assert len(captured["h"]["access-sign"]) == 64      # hex SHA-256


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b'{"last":"60000"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/ticker?pair=btcjpy", signed=False)
    assert not any(k.startswith("access-") for k in captured["h"])


def test_the_nonce_increases(monkeypatch):
    adapter = _adapter()
    first, second = adapter._nonce(), adapter._nonce()
    assert int(second) > int(first)


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("COINCHECK_KEYS", "key-only")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BROKER_CREDENTIAL"


def test_success_false_is_a_refusal(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"success":false,"error":"invalid authentication"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "COINCHECK_API_ERROR"
    assert "invalid authentication" in exc.value.reason


def test_account_sums_only_numeric_balance_fields(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "success": True, "jpy": "1000", "btc": "0.5", "note": "not a balance",
    })
    account = adapter.account()
    assert account["server"] == "coincheck"
    assert account["balance"] == pytest.approx(1000.5)
    assert account["currency"] == "JPY"


def test_positions_are_balances_and_not_marked(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_balances", lambda: {"BTC": 0.5, "JPY": 0.0})
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC"]
    assert rows[0]["currentPrice"] == 0.0
    assert "not marked" in rows[0]["comment"]


def test_orders_report_the_remaining_amount(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "success": True, "orders": [
            {"id": 9, "pair": "btc_jpy", "order_type": "buy", "amount": "1",
             "executed_amount": "0.25", "rate": "5000000"}],
    })
    order = adapter.orders()[0]
    assert order["ticket"] == "9"
    assert order["symbol"] == "BTC_JPY"
    assert order["volumeCurrent"] == pytest.approx(0.75)


def test_quote_reads_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: {
                            "last": "60000.5", "bid": "59999", "ask": "60001"})
    quote = adapter.quote("btc/jpy")
    assert quote["symbol"] == "BTCJPY"
    assert quote["last"] == pytest.approx(60000.5)


def test_registry_declares_the_kind_executable():
    kind = get_kind("coincheck")
    assert kind.execution is True
    assert any(row["id"] == "coincheck" for row in list_kinds())
