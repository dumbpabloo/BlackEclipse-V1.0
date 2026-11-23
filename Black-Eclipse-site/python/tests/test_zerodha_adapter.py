"""Zerodha Kite broker adapter: auth shape, normalization, honest refusals."""

from __future__ import annotations

import urllib.error

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.zerodha import ZerodhaAdapter
from harness.trade.registry import get_kind, list_kinds, make_adapter


def _adapter(**config) -> ZerodhaAdapter:
    base = {"credentialRef": "KITE_TOKEN"}
    base.update(config)
    return ZerodhaAdapter(base)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("KITE_TOKEN", "key123:access456")


def test_availability_is_stdlib_only():
    assert ZerodhaAdapter.availability() == (True, None)


def test_the_authorization_header_is_the_documented_token_form(monkeypatch):
    adapter = _adapter()
    captured: dict = {}

    class _Response:
        status_code = 200

        def read(self, _n=None):
            return b'{"status":"success","data":{}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        captured["headers"] = dict(request.headers)
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    adapter._get("/user/profile")
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"] == "token key123:access456"
    assert headers["x-kite-version"] == "3"


def test_a_missing_credential_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "ZERODHA_CREDENTIALS"


def test_account_normalizes_profile_and_margins(monkeypatch):
    adapter = _adapter()
    payloads = {
        "/user/profile": {"user_id": "AB1234", "broker": "ZERODHA"},
        "/user/margins": {"equity": {"net": 250000.5, "available": {"cash": 120000.0},
                                     "utilised": {"debits": 5000.0}}},
    }
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: payloads.get(path))
    account = adapter.account()
    assert account["server"] == "zerodha"
    assert account["currency"] == "INR"
    assert account["equity"] == pytest.approx(250000.5)
    assert account["balance"] == pytest.approx(120000.0)
    assert account["tradeAllowed"] is True


def test_positions_use_the_net_book_only(monkeypatch):
    adapter = _adapter()
    payload = {"net": [
        {"tradingsymbol": "RELIANCE", "quantity": 5, "average_price": 2400.0,
         "last_price": 2450.0, "pnl": 250.0},
        {"tradingsymbol": "INFY", "quantity": -2, "average_price": 1500.0,
         "last_price": 1490.0, "pnl": 20.0},
        {"tradingsymbol": "FLAT", "quantity": 0},
    ], "day": [{"tradingsymbol": "IGNORED", "quantity": 9}]}
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: payload)
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["RELIANCE", "INFY"]
    assert rows[1]["side"] == "sell"
    assert rows[0]["profit"] == pytest.approx(250.0)


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: [
        {"order_id": "220101", "tradingsymbol": "TCS", "order_type": "LIMIT",
         "pending_quantity": 3, "price": 3400.0, "status": "OPEN",
         "transaction_type": "BUY"},
    ])
    order = adapter.orders()[0]
    assert order["ticket"] == "220101"
    assert order["volumeCurrent"] == pytest.approx(3)
    assert "BUY" in order["comment"]


def test_quote_requires_an_exchange_prefix():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().quote("RELIANCE")
    assert exc.value.code == "ZERODHA_SYMBOL_AMBIGUOUS"


def test_quote_normalizes_the_snapshot(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "NSE:RELIANCE": {"last_price": 2450.5,
                         "depth": {"buy": [{"price": 2450.0}],
                                   "sell": [{"price": 2451.0}]}},
    })
    quote = adapter.quote("NSE:RELIANCE")
    assert quote["last"] == pytest.approx(2450.5)
    assert quote["bid"] == pytest.approx(2450.0)
    assert quote["ask"] == pytest.approx(2451.0)


def test_probe_reports_disconnected_with_the_reason(monkeypatch):
    adapter = _adapter()

    def boom(path, params=None):
        raise AdapterUnavailable("ZERODHA_UNAUTHORIZED", "rejected")

    monkeypatch.setattr(adapter, "_get", boom)
    probe = adapter.probe()
    assert probe["connected"] is False
    assert probe["code"] == "ZERODHA_UNAUTHORIZED"


def test_http_401_maps_to_unauthorized(monkeypatch):
    adapter = _adapter()

    def raising(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://x", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raising)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._get("/user/profile")
    assert exc.value.code == "ZERODHA_UNAUTHORIZED"


def test_an_api_error_payload_is_a_refusal(monkeypatch):
    adapter = _adapter()

    class _Response:
        def read(self, _n=None):
            return b'{"status":"error","message":"Invalid token"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Response())
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._get("/orders")
    assert exc.value.code == "ZERODHA_API_ERROR"


def test_registry_declares_an_executable_kind_with_quotes():
    kind = get_kind("zerodha")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "zerodha" for row in list_kinds())
    assert isinstance(make_adapter("zerodha", {"credentialRef": "KITE_TOKEN"}),
                      ZerodhaAdapter)
