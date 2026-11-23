"""Gate.io v4 adapter: SHA-512 body hash, HMAC-SHA512 signature, refusals."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.gateio import GateioAdapter, payload_hash, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> GateioAdapter:
    base = {"credentialRef": "GATEIO_KEYS"}
    base.update(config)
    return GateioAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GATEIO_KEYS", "key-1:secret-2")


def test_availability_is_stdlib_only():
    assert GateioAdapter.availability() == (True, None)


def test_the_body_hash_is_sha512_of_the_empty_string_for_a_get():
    assert payload_hash("") == hashlib.sha512(b"").hexdigest()
    assert len(payload_hash("")) == 128          # SHA-512, not SHA-256


def test_the_signature_is_hmac_sha512_over_the_five_line_preimage():
    ts = "1700000000000"
    preimage = "\n".join(["GET", "/api/v4/spot/accounts", "", payload_hash(""), ts])
    expected = hmac.new(b"secret-2", preimage.encode(), hashlib.sha512).hexdigest()
    assert sign_request("secret-2", ts, "GET", "/api/v4/spot/accounts") == expected
    assert len(expected) == 128


def test_the_query_is_signed_separately_from_the_path():
    a = sign_request("s", "1", "GET", "/api/v4/spot/tickers", "currency_pair=BTC_USDT")
    b = sign_request("s", "1", "GET", "/api/v4/spot/tickers", "")
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


def test_a_signed_call_carries_key_timestamp_and_sign(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        return _Resp(b"[]")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v4/spot/accounts")
    assert captured["h"]["key"] == "key-1"
    assert captured["h"]["timestamp"].isdigit()
    assert len(captured["h"]["sign"]) == 128


def test_the_public_ticker_is_unsigned(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp(b"[]")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v4/spot/tickers", {"currency_pair": "BTC_USDT"}, signed=False)
    assert "key" not in captured["h"] and "sign" not in captured["h"]


def test_credentials_must_be_two_parts(monkeypatch):
    monkeypatch.setenv("GATEIO_KEYS", "key-only")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "BROKER_CREDENTIAL"


def test_a_labelled_error_body_is_a_refusal(monkeypatch):
    adapter = _adapter()

    def fake(*_a, **_k):
        return _Resp(b'{"label":"INVALID_KEY","message":"Invalid key"}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "GATEIO_API_ERROR"
    assert "Invalid key" in exc.value.reason


def test_account_sums_spot_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"currency": "USDT", "total": "1000", "available": "900", "freeze": "100"},
        {"currency": "BTC", "total": "0.5", "available": "0.5"},
    ])
    account = adapter.account()
    assert account["server"] == "gateio"
    assert account["balance"] == pytest.approx(1000.5)
    assert account["marginFree"] == pytest.approx(900.5)
    assert account["tradeAllowed"] is True


def test_positions_use_the_sign_of_size_for_the_side(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"contract": "BTC_USDT", "size": "-10", "entry_price": "60000",
         "mark_price": "61000", "unrealised_pnl": "100", "leverage": "5",
         "funding_rate": "0.0001", "open_time": 1700000000},
        {"contract": "ETH_USDT", "size": "0"},
    ])
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC_USDT"]
    position = rows[0]
    assert position["side"] == "sell"
    assert position["volume"] == pytest.approx(10.0)
    assert position["profit"] == pytest.approx(100.0)
    assert "leverage 5" in position["comment"]


def test_orders_report_the_remaining_amount(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"currency_pair": "BTC_USDT", "orders": [
            {"id": "o-1", "currency_pair": "BTC_USDT", "type": "limit", "side": "sell",
             "amount": "1", "left": "0.4", "price": "62000", "status": "open",
             "create_time_ms": 1700000000000}]},
    ])
    order = adapter.orders()[0]
    assert order["volumeCurrent"] == pytest.approx(0.4)
    assert "sell" in order["comment"]
    assert order["placedAt"] == 1700000000000


def test_quote_reads_the_public_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None, signed=True: [
        {"currency_pair": "BTC_USDT", "last": "60000.5",
         "highest_bid": "59999", "lowest_ask": "60001"}])
    quote = adapter.quote("btc_usdt")
    assert quote["symbol"] == "BTC_USDT"
    assert quote["last"] == pytest.approx(60000.5)


def test_registry_declares_the_kind_executable():
    kind = get_kind("gateio")
    assert kind.execution is True
    assert "derivatives" in kind.asset_classes
    assert any(row["id"] == "gateio" for row in list_kinds())
