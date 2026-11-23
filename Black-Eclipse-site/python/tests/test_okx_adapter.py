"""OKX broker adapter: signature correctness, normalization, refusals."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.okx import OkxAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> OkxAdapter:
    base = {"credentialRef": "OKX_KEYS"}
    base.update(config)
    return OkxAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("OKX_KEYS", "key-1:secret-2:pass-3")


def test_availability_is_stdlib_only():
    assert OkxAdapter.availability() == (True, None)


def test_the_signature_matches_the_documented_formula():
    ts = "2024-01-02T03:04:05.678Z"
    path = "/api/v5/account/balance"
    expected = base64.b64encode(hmac.new(
        b"secret-2", f"{ts}GET{path}".encode(), hashlib.sha256).digest()).decode()
    assert sign_request("secret-2", ts, "GET", path) == expected


def test_the_query_string_is_part_of_the_signed_path():
    ts = "2024-01-02T03:04:05.678Z"
    path = "/api/v5/market/ticker?instId=BTC-USDT"
    expected = base64.b64encode(hmac.new(
        b"s", f"{ts}GET{path}".encode(), hashlib.sha256).digest()).decode()
    assert sign_request("s", ts, "GET", path) == expected


def test_the_headers_carry_all_four_okx_fields(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"code":"0","data":[{"totalEq":"1000"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._request("/api/v5/account/balance")
    assert captured["h"]["ok-access-key"] == "key-1"
    assert captured["h"]["ok-access-passphrase"] == "pass-3"
    assert captured["h"]["ok-access-timestamp"].endswith("Z")
    assert captured["h"]["ok-access-sign"]


def test_credentials_must_have_three_parts(monkeypatch):
    monkeypatch.setenv("OKX_KEYS", "key-1:secret-2")
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().account()
    assert exc.value.code == "OKX_CREDENTIALS"


def test_missing_credentials_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "OKX_CREDENTIALS"


def test_account_normalizes_the_balance_row(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None: [
        {"totalEq": "10000.5", "availBal": "2500.0", "imr": "500.0", "upl": "120.0"}])
    account = adapter.account()
    assert account["server"] == "okx"
    assert account["equity"] == pytest.approx(10000.5)
    assert account["balance"] == pytest.approx(2500.0)
    assert account["tradeAllowed"] is True


def test_positions_skip_flat_rows_and_keep_the_side(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None: [
        {"instId": "BTC-USDT-SWAP", "pos": "2", "posSide": "long",
         "avgPx": "60000", "markPx": "61000", "upl": "200"},
        {"instId": "ETH-USDT-SWAP", "pos": "-1", "posSide": "short",
         "avgPx": "3000", "markPx": "2950", "upl": "50"},
        {"instId": "FLAT", "pos": "0"},
    ])
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert rows[1]["side"] == "short"
    assert rows[0]["profit"] == pytest.approx(200.0)


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None: [
        {"ordId": "abc123", "instId": "BTC-USDT", "ordType": "limit",
         "sz": "0.5", "px": "59000", "state": "live", "side": "buy"}])
    order = adapter.orders()[0]
    assert order["ticket"] == "abc123"
    assert order["volumeCurrent"] == pytest.approx(0.5)
    assert "buy" in order["comment"]


def test_quote_uses_the_ticker_snapshot(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_request", lambda path, params=None: [
        {"bidPx": "60000", "askPx": "60010", "last": "60005", "ts": "1700000000000"}])
    quote = adapter.quote("btc-usdt")
    assert quote["symbol"] == "BTC-USDT"
    assert quote["last"] == pytest.approx(60005.0)
    assert quote["serverTimeMs"] == 1700000000000


def test_an_api_error_code_is_a_refusal(monkeypatch):
    adapter = _adapter()

    class _Resp:
        def read(self, _n=None):
            return b'{"code":"50111","msg":"Invalid OK-ACCESS-KEY"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._request("/api/v5/account/balance")
    assert exc.value.code == "OKX_API_ERROR"
    assert "50111" in exc.value.reason


def test_probe_reports_disconnected_with_the_reason(monkeypatch):
    adapter = _adapter()

    def boom(path, params=None):
        raise AdapterUnavailable("OKX_UNAUTHORIZED", "rejected")

    monkeypatch.setattr(adapter, "_request", boom)
    probe = adapter.probe()
    assert probe["connected"] is False and probe["code"] == "OKX_UNAUTHORIZED"


def test_registry_declares_the_kind_executable():
    kind = get_kind("okx")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "okx" for row in list_kinds())
