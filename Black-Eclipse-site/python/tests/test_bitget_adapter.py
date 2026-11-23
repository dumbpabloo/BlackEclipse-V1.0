"""Bitget v2 adapter: base64 signature, three-part credential, code refusals."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.bitget import BitgetAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> BitgetAdapter:
    base = {"credentialRef": "BITGET_KEYS"}
    base.update(config)
    return BitgetAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("BITGET_KEYS", "key-1:secret-2:pass-3")


def test_availability_is_stdlib_only():
    assert BitgetAdapter.availability() == (True, None)


def test_the_signature_is_base64_hmac_over_ts_method_path_body():
    expected = base64.b64encode(hmac.new(
        b"secret-2", b"1700000000000GET/api/v2/spot/account/assets", hashlib.sha256
    ).digest()).decode()
    assert sign_request("secret-2", "1700000000000", "GET",
                        "/api/v2/spot/account/assets") == expected


def test_the_signature_is_not_hex():
    # Coinbase also uses base64, Kraken and Deribit use hex; this pins which one.
    signed = sign_request("s", "1", "GET", "/x")
    assert "=" in signed or len(signed) == 44
    assert not all(c in "0123456789abcdef" for c in signed)


def test_the_query_string_is_part_of_the_signature():
    a = sign_request("s", "1", "GET", "/api/v2/spot/market/tickers?symbol=BTCUSDT")
    b = sign_request("s", "1", "GET", "/api/v2/spot/market/tickers")
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
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        return _Resp(b'{"code":"00000","msg":"success","data":[]}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/spot/account/assets")
    assert captured["h"]["access-key"] == "key-1"
    assert captured["h"]["access-passphrase"] == "pass-3"
    assert captured["h"]["access-timestamp"].isdigit()
    assert captured["h"]["access-sign"]


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b'{"code":"00000","data":[]}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v2/spot/market/tickers", {"symbol": "BTCUSDT"}, signed=False)
    assert not any(k.startswith("access-") for k in captured["h"])


def test_a_two_part_credential_is_refused(monkeypatch):
    monkeypatch.setenv("BITGET_KEYS", "key:secret")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BITGET_CREDENTIALS"


def test_a_non_zero_code_is_a_refusal_even_with_an_empty_list(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"code":"40001","msg":"invalid signature","data":[]}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.positions()
    assert exc.value.code == "BITGET_API_ERROR"
    assert "invalid signature" in exc.value.reason


def test_account_sums_spot_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"coin": "USDT", "balance": "1000", "available": "900", "frozen": "100"},
        {"coin": "BTC", "balance": "0.5", "available": "0.5"},
    ])
    account = adapter.account()
    assert account["server"] == "bitget"
    assert account["balance"] == pytest.approx(1000.5)
    assert account["marginFree"] == pytest.approx(900.5)
    assert account["tradeAllowed"] is True


def test_positions_map_hold_side_and_skip_closed(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5", "openPriceAvg": "60000",
         "markPrice": "61000", "unrealizedPL": "500", "stopLoss": "59000",
         "totalFee": "-1.2", "cTime": "1700000000000"},
        {"symbol": "ETHUSDT", "holdSide": "short", "total": "0"},
    ])
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTCUSDT"]
    position = rows[0]
    assert position["side"] == "buy"
    assert position["profit"] == pytest.approx(500.0)
    assert position["stopLoss"] == pytest.approx(59000.0)


def test_orders_report_the_remaining_size(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"orderId": "o-1", "symbol": "BTCUSDT", "side": "sell", "orderType": "limit",
         "size": "1", "baseVolume": "0.25", "price": "62000", "status": "live",
         "cTime": "1700000000000"},
    ])
    order = adapter.orders()[0]
    assert order["volumeCurrent"] == pytest.approx(0.75)
    assert "sell" in order["comment"]


def test_quote_reads_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request",
                        lambda path, params=None, signed=True: [
                            {"lastPr": "60000.5", "bidPr": "59999", "askPr": "60001",
                             "ts": "1700000000000"}])
    quote = adapter.quote("btcusdt")
    assert quote["symbol"] == "BTCUSDT"
    assert quote["last"] == pytest.approx(60000.5)
    assert quote["serverTimeMs"] == 1700000000000


def test_registry_declares_the_kind_executable():
    kind = get_kind("bitget")
    assert kind.execution is True
    assert "derivatives" in kind.asset_classes
    assert any(row["id"] == "bitget" for row in list_kinds())
