"""Bybit v5 adapter: prefix signature, retCode refusals, real positions."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.bybit import BybitAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> BybitAdapter:
    base = {"credentialRef": "BYBIT_KEYS"}
    base.update(config)
    return BybitAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("BYBIT_KEYS", "key-1:secret-2")


def test_availability_is_stdlib_only():
    assert BybitAdapter.availability() == (True, None)


def test_the_signature_is_over_ts_key_window_and_query():
    expected = hmac.new(
        b"secret-2", b"1700000000000key-15000symbol=BTCUSDT", hashlib.sha256
    ).hexdigest()
    assert sign_request("secret-2", "1700000000000", "key-1", "symbol=BTCUSDT") == expected


def test_the_signature_changes_when_the_query_changes():
    a = sign_request("s", "1", "k", "a=1")
    b = sign_request("s", "1", "k", "a=2")
    assert a != b


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_signed_call_carries_the_four_headers(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["url"] = request.full_url
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b'{"retCode":0,"result":{}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    assert captured["h"]["x-bapi-api-key"] == "key-1"
    assert captured["h"]["x-bapi-recv-window"] == "5000"
    assert captured["h"]["x-bapi-timestamp"].isdigit()
    assert captured["h"]["x-bapi-sign"]
    assert "accountType=UNIFIED" in captured["url"]


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b'{"retCode":0,"result":{"list":[]}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/v5/market/tickers", {"category": "spot"}, signed=False)
    assert not any(k.startswith("x-bapi") for k in captured["h"])


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("BYBIT_KEYS", "key-only")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    # The shared `parse_key_pair` helper owns this code; the adapter does not
    # fork it, so one shape failure is reported one way for every broker.
    assert exc.value.code == "BROKER_CREDENTIAL"


def test_a_non_zero_retcode_is_a_named_refusal(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"retCode":10003,"retMsg":"Invalid api key"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "BYBIT_API_ERROR"
    assert "Invalid api key" in exc.value.reason


def test_account_sums_the_unified_wallet(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "list": [{"accountType": "UNIFIED", "coin": [
            {"coin": "USDT", "walletBalance": "1000", "equity": "1010",
             "availableToWithdraw": "900"},
            {"coin": "BTC", "walletBalance": "0.5", "equity": "0.5",
             "availableToWithdraw": "0.5"},
        ]}],
    })
    account = adapter.account()
    assert account["server"] == "bybit"
    assert account["equity"] == pytest.approx(1010.5)
    assert account["marginFree"] == pytest.approx(900.5)
    assert account["tradeAllowed"] is True


def test_positions_are_real_and_skip_closed_ones(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "list": [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.5", "avgPrice": "60000",
             "markPrice": "61000", "unrealisedPnl": "500", "stopLoss": "59000",
             "takeProfit": "", "createdTime": "1700000000000"},
            {"symbol": "ETHUSDT", "side": "Sell", "size": "0"},
        ],
    })
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTCUSDT"]
    position = rows[0]
    assert position["side"] == "buy"
    assert position["volume"] == pytest.approx(0.5)
    assert position["currentPrice"] == pytest.approx(61000.0)
    assert position["profit"] == pytest.approx(500.0)
    assert position["stopLoss"] == pytest.approx(59000.0)
    assert position["takeProfit"] is None      # blank, not 0.0


def test_orders_normalize_the_remaining_quantity(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {
        "list": [{"orderId": "o-1", "symbol": "BTCUSDT", "side": "Sell",
                  "orderType": "Limit", "qty": "1", "cumExecQty": "0.4",
                  "price": "62000", "orderStatus": "New",
                  "createdTime": "1700000000000"}],
    })
    order = adapter.orders()[0]
    assert order["volumeCurrent"] == pytest.approx(0.6)
    assert "sell" in order["comment"]
    assert order["state"] == "New"


def test_quote_reads_the_spot_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: {"list": [
                            {"lastPrice": "60000.5", "bid1Price": "59999",
                             "ask1Price": "60001"}]})
    quote = adapter.quote("btcusdt")
    assert quote["symbol"] == "BTCUSDT"
    assert quote["last"] == pytest.approx(60000.5)


def test_a_empty_ticker_is_a_named_refusal(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: {"list": []})
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.quote("NOPE")
    assert exc.value.code == "BYBIT_NO_QUOTE"


def test_registry_declares_the_kind_executable_with_derivatives():
    kind = get_kind("bybit")
    assert kind.execution is True
    assert "derivatives" in kind.asset_classes
    assert any(row["id"] == "bybit" for row in list_kinds())
