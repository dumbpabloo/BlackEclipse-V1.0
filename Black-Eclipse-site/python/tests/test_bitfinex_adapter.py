"""Bitfinex v2 adapter: SHA-384 signature, signed path, positional arrays."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.bitfinex import (
    BitfinexAdapter,
    sign_request,
    symbol_for,
)
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> BitfinexAdapter:
    base = {"credentialRef": "BITFINEX_KEYS"}
    base.update(config)
    return BitfinexAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("BITFINEX_KEYS", "key-1:secret-2")


def test_availability_is_stdlib_only():
    assert BitfinexAdapter.availability() == (True, None)


def test_the_signature_is_hmac_sha384_of_path_nonce_body():
    expected = hmac.new(b"secret-2", b"/api/v2/r/wallets1700000000000",
                        hashlib.sha384).hexdigest()
    assert sign_request("secret-2", "/api/v2/r/wallets", "1700000000000") == expected
    assert len(expected) == 96          # SHA-384, not SHA-256's 64


def test_the_signed_path_and_body_both_change_the_signature():
    a = sign_request("s", "/api/v2/r/wallets", "1")
    assert a != sign_request("s", "/api/v2/auth/r/wallets", "1")
    assert a != sign_request("s", "/api/v2/r/wallets", "2")
    assert a != sign_request("s", "/api/v2/r/wallets", "1", '{"x":1}')


def test_symbols_drop_the_t_and_f_class_prefix():
    assert symbol_for("tBTCUSD") == "BTCUSD"
    assert symbol_for("fUSD") == "USD"
    assert symbol_for("BTCUSD") == "BTCUSD"


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
        return _Resp(b"[]")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/r/wallets", signed=True)
    assert captured["h"]["bfx-apikey"] == "key-1"
    assert captured["h"]["bfx-nonce"].isdigit()
    assert len(captured["h"]["bfx-signature"]) == 96
    assert captured["url"].endswith("/api/v2/r/wallets")


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b"[1,2,3,4,5,6,7]")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/ticker/tBTCUSD")
    assert not any(k.startswith("bfx") for k in captured["h"])


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("BITFINEX_KEYS", "key-only")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BROKER_CREDENTIAL"


def test_an_error_object_is_a_refusal(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"error":"Invalid api key"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "BITFINEX_API_ERROR"
    assert "Invalid api key" in exc.value.reason


def test_account_reads_the_wallet_array_by_index(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, signed=False, body="": [
        ["exchange", "BTC", 0.5, 0.0, 0.4, None],
        ["exchange", "USD", 1000.0, 0.0, 900.0, None],
    ])
    account = adapter.account()
    assert account["server"] == "bitfinex"
    assert account["balance"] == pytest.approx(1000.5)
    assert account["marginFree"] == pytest.approx(900.4)
    assert account["tradeAllowed"] is True


def test_positions_use_the_sign_of_amount_for_the_side(monkeypatch):
    adapter = _adapter()
    row = ["tBTCUSD", "ACTIVE", -0.5, 60000, -1.0, 0.0, 0.0, 25.5, 0.01,
           0.0, 10, None, 42, 1700000000000, None, None, None, 61000]
    monkeypatch.setattr(adapter, "_request", lambda path, signed=False, body="": [row])
    position = adapter.positions()[0]
    assert position["symbol"] == "BTCUSD"
    assert position["side"] == "sell"
    assert position["volume"] == pytest.approx(0.5)
    assert position["openPrice"] == pytest.approx(60000.0)
    assert position["currentPrice"] == pytest.approx(61000.0)
    assert position["profit"] == pytest.approx(25.5)


def test_closed_positions_are_skipped(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, signed=False, body="": [
        ["tETHUSD", "CLOSED", 0, 1, 0, 0, 0, 0, 0, 0, 1],
    ])
    assert adapter.positions() == []


def test_orders_read_the_array_and_sign_of_amount(monkeypatch):
    adapter = _adapter()
    row = [111, None, 222, "tBTCUSD", 1700000000000, -1.0, 62000, None, None,
           "LIMIT", None, None, "ACTIVE"]
    monkeypatch.setattr(adapter, "_request", lambda path, signed=False, body="": [row])
    order = adapter.orders()[0]
    assert order["ticket"] == "111"
    assert order["symbol"] == "BTCUSD"
    assert order["volumeCurrent"] == pytest.approx(1.0)
    assert "sell" in order["comment"]
    assert order["state"] == "ACTIVE"


def test_quote_reads_the_public_ticker_array(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, signed=False, body="": [
                            59999, 1, 60001, 1, 10, 0.0001, 60000.5, 100, 60100, 59000])
    quote = adapter.quote("btc/usd")
    assert quote["symbol"] == "BTCUSD"
    assert quote["last"] == pytest.approx(60000.5)
    assert quote["bid"] == pytest.approx(59999.0)


def test_registry_declares_the_kind_executable():
    kind = get_kind("bitfinex")
    assert kind.execution is True
    assert "derivatives" in kind.asset_classes
    assert any(row["id"] == "bitfinex" for row in list_kinds())
