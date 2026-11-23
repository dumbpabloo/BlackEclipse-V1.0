"""Kraken adapter: signature input, spot semantics, refusals."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.kraken import KrakenAdapter, sign_request
from harness.trade.registry import get_kind, list_kinds

SECRET = base64.b64encode(b"top-secret").decode()


def _adapter(**config) -> KrakenAdapter:
    base = {"credentialRef": "KRAKEN_KEYS"}
    base.update(config)
    return KrakenAdapter(base)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("KRAKEN_KEYS", f"key-1:{SECRET}")


def test_availability_is_stdlib_only():
    assert KrakenAdapter.availability() == (True, None)


def test_the_signature_matches_the_documented_input():
    path, nonce, postdata = "/0/private/Balance", "1700000000000", "nonce=1700000000000"
    digest = hashlib.sha256((nonce + postdata).encode()).digest()
    expected = base64.b64encode(
        hmac.new(b"top-secret", path.encode() + digest, hashlib.sha512).digest()
    ).decode()
    assert sign_request(SECRET, path, nonce, postdata) == expected


def test_a_private_call_posts_with_the_key_header(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"error":[],"result":{"XXBT":"0.5"}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["method"] = request.get_method()
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["data"] = request.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._call("/0/private/Balance")
    assert captured["method"] == "POST"
    assert captured["h"]["api-key"] == "key-1"
    assert captured["h"]["api-sign"]
    assert b"nonce=" in captured["data"]


def test_a_public_call_is_an_unsigned_get(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Resp:
        def read(self, _n=None):
            return b'{"error":[],"result":{}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=0):
        captured["method"] = request.get_method()
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._call("/0/public/Ticker", {"pair": "XBTUSD"}, private=False)
    assert captured["method"] == "GET"
    assert "api-key" not in captured["h"]


def test_missing_credentials_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "KRAKEN_CREDENTIALS"


def test_an_error_array_is_a_refusal(monkeypatch):
    adapter = _adapter()

    class _Resp:
        def read(self, _n=None):
            return b'{"error":["EAPI:Invalid key"],"result":{}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._call("/0/private/Balance")
    assert exc.value.code == "KRAKEN_API_ERROR"
    assert "Invalid key" in exc.value.reason


def test_account_sums_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_balances", lambda: {"XXBT": 0.5, "ZUSD": 100.0})
    account = adapter.account()
    assert account["server"] == "kraken"
    assert account["equity"] == pytest.approx(100.5)
    assert account["tradeAllowed"] is True


def test_positions_are_spot_balances_not_marked(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_balances", lambda: {"XXBT": 0.5, "ZUSD": 0.0})
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["XXBT"]
    assert rows[0]["volume"] == pytest.approx(0.5)
    assert rows[0]["currentPrice"] == 0.0
    assert "not marked" in rows[0]["comment"]


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call", lambda path, params=None, private=True: {
        "open": {"OABC-1": {"descr": {"pair": "XBTUSD", "type": "buy",
                                      "ordertype": "limit", "price": "50000"},
                            "vol": "0.5", "vol_exec": "0.2", "opentm": 1700000000.0}}
    })
    order = adapter.orders()[0]
    assert order["ticket"] == "OABC-1"
    assert order["volumeCurrent"] == pytest.approx(0.3)
    assert "buy" in order["comment"]
    assert order["placedAt"] == 1700000000000


def test_quote_reads_the_ticker(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_call",
                        lambda path, params=None, private=True: {
                            "XXBTZUSD": {"c": ["60000.1"], "b": ["59990"], "a": ["60010"]}})
    quote = adapter.quote("XBTUSD")
    assert quote["last"] == pytest.approx(60000.1)
    assert quote["bid"] == pytest.approx(59990.0)


def test_registry_declares_the_kind_executable():
    kind = get_kind("kraken")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "kraken" for row in list_kinds())
