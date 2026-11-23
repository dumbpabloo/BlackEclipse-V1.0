"""Deribit adapter: newline preimage, JSON-RPC errors, per-currency honesty."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.deribit import DeribitAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> DeribitAdapter:
    base = {"credentialRef": "DERIBIT_KEYS"}
    base.update(config)
    return DeribitAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("DERIBIT_KEYS", "client-1:secret-2")


def test_availability_is_stdlib_only():
    assert DeribitAdapter.availability() == (True, None)


def test_the_signature_is_newline_joined_ts_nonce_data_query():
    expected = hmac.new(
        b"secret-2", b"1700000000000\nnonce-1\n\ncurrency=BTC", hashlib.sha256
    ).hexdigest()
    assert sign_request("secret-2", "1700000000000", "nonce-1",
                        "currency=BTC") == expected


def test_the_signature_covers_the_query():
    assert sign_request("s", "1", "n", "currency=BTC") != sign_request(
        "s", "1", "n", "currency=ETH")


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_signed_call_sends_the_deri_hmac_header(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        return _Resp(b'{"jsonrpc":"2.0","result":{}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/private/get_account_summary", {"currency": "BTC"})
    auth = captured["h"]["authorization"]
    assert auth.startswith("deri-hmac-sha256 id=client-1,ts=")
    assert "nonce=" in auth and "sig=" in auth
    assert "currency=BTC" in captured["url"]


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b'{"jsonrpc":"2.0","result":{}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/public/ticker", {"instrument_name": "BTC-PERPETUAL"},
                     signed=False)
    assert "authorization" not in captured["h"]


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("DERIBIT_KEYS", "client-only")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BROKER_CREDENTIAL"


def test_a_jsonrpc_error_is_a_named_refusal(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"jsonrpc":"2.0","error":{"code":13001,"message":"bad credentials"}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "DERIBIT_API_ERROR"
    assert "bad credentials" in exc.value.reason


def test_account_reports_the_currency_it_actually_queried(monkeypatch):
    adapter = _adapter(currency="eth")
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "currency": "ETH", "balance": 2.0, "equity": 2.5,
        "available_funds": 2.0, "session_upl": 0.5,
    })
    account = adapter.account()
    assert account["server"] == "deribit"
    assert account["currency"] == "ETH"
    assert account["equity"] == pytest.approx(2.5)
    assert account["marginFree"] == pytest.approx(2.0)
    assert account["tradeAllowed"] is True


def test_positions_use_the_direction_and_skip_closed(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"instrument_name": "BTC-PERPETUAL", "kind": "future", "direction": "sell",
         "size": -10.0, "average_price": 60000, "mark_price": 61000,
         "floating_profit_loss": 100.0, "timestamp": 1700000000000},
        {"instrument_name": "ETH-PERPETUAL", "direction": "buy", "size": 0},
    ])
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC-PERPETUAL"]
    position = rows[0]
    assert position["side"] == "sell"
    assert position["volume"] == pytest.approx(10.0)   # abs(size)
    assert position["profit"] == pytest.approx(100.0)


def test_orders_report_the_remaining_amount(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"order_id": "o-1", "instrument_name": "BTC-PERPETUAL", "order_type": "limit",
         "amount": 10.0, "filled_amount": 4.0, "price": 60000, "order_state": "open",
         "direction": "buy", "creation_timestamp": 1700000000000},
    ])
    order = adapter.orders()[0]
    assert order["volumeCurrent"] == pytest.approx(6.0)
    assert "buy" in order["comment"]
    assert order["openPrice"] == pytest.approx(60000.0)


def test_quote_reads_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: {
                            "last_price": 60000.5, "best_bid_price": 59999,
                            "best_ask_price": 60001, "timestamp": 1700000000000})
    quote = adapter.quote("btc-perpetual")
    assert quote["symbol"] == "BTC-PERPETUAL"
    assert quote["last"] == pytest.approx(60000.5)
    assert quote["serverTimeMs"] == 1700000000000


def test_registry_declares_options_and_execution():
    kind = get_kind("deribit")
    assert kind.execution is True
    assert "options" in kind.asset_classes
    assert any(row["id"] == "deribit" for row in list_kinds())
