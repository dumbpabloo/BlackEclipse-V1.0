"""Binance spot adapter: signature, public/signed split, honest spot semantics."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.binance import BinanceAdapter, sign_query
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> BinanceAdapter:
    base = {"credentialRef": "BINANCE_KEYS"}
    base.update(config)
    return BinanceAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("BINANCE_KEYS", "key-1:secret-2")


def test_availability_is_stdlib_only():
    assert BinanceAdapter.availability() == (True, None)


def test_the_signature_is_hex_hmac_over_the_query():
    assert sign_query("secret-2", "symbol=BTCUSDT") == hmac.new(
        b"secret-2", b"symbol=BTCUSDT", hashlib.sha256).hexdigest()


def test_a_signed_request_carries_the_key_and_signature(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"canTrade":true,"balances":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["url"] = request.full_url
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v3/account")
    assert captured["h"]["x-mbx-apikey"] == "key-1"
    assert "timestamp=" in captured["url"] and "signature=" in captured["url"]


def test_a_public_request_carries_no_key(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"symbol":"BTCUSDT","price":"60000"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter.quote("BTCUSDT")
    assert "x-mbx-apikey" not in captured["h"]


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("BINANCE_KEYS", "key-only")
    with pytest.raises(AdapterUnavailable):
        _adapter().account()


def test_missing_credentials_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "BINANCE_CREDENTIALS"


def test_account_sums_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "canTrade": True,
        "balances": [{"asset": "BTC", "free": "0.5", "locked": "0"},
                     {"asset": "USDT", "free": "100", "locked": "50"}],
    })
    account = adapter.account()
    assert account["server"] == "binance"
    assert account["balance"] == pytest.approx(150.5)
    assert account["tradeAllowed"] is True


def test_positions_are_spot_balances_and_not_marked(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"},
                     {"asset": "ETH", "free": "0", "locked": "0"}],
    })
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC"]
    assert rows[0]["volume"] == pytest.approx(0.6)
    assert rows[0]["currentPrice"] == 0.0   # deliberately not marked to market
    assert "not marked" in rows[0]["comment"]


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"orderId": 123, "symbol": "BTCUSDT", "type": "LIMIT", "origQty": "1.0",
         "executedQty": "0.4", "price": "59000", "status": "NEW", "side": "BUY",
         "time": 1700000000000},
    ])
    order = adapter.orders()[0]
    assert order["ticket"] == "123"
    assert order["volumeCurrent"] == pytest.approx(0.6)
    assert "buy" in order["comment"]
    assert order["placedAt"] == 1700000000000


def test_quote_reads_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: {"price": "60000.5"})
    quote = adapter.quote("btcusdt")
    assert quote["symbol"] == "BTCUSDT"
    assert quote["last"] == pytest.approx(60000.5)


def test_a_restricted_jurisdiction_is_named(monkeypatch):
    import urllib.error
    adapter = _adapter()

    def raising(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 451, "Unavailable", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raising)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._request("/api/v3/ticker/price", {"symbol": "BTCUSDT"}, signed=False)
    assert exc.value.code == "BINANCE_RESTRICTED"


def test_registry_declares_the_kind_executable():
    kind = get_kind("binance")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "binance" for row in list_kinds())
