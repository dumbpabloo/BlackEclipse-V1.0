"""Trading212 broker adapter: normalization, honest refusals, registration."""

from __future__ import annotations

import base64
import urllib.error

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.trading212 import Trading212Adapter
from harness.trade.registry import get_kind, list_kinds, make_adapter


def _adapter(**config) -> Trading212Adapter:
    base = {"credentialRef": "T212_KEY", "currency": "GBP"}
    base.update(config)
    return Trading212Adapter(base)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("T212_KEY", "demo-key-123")


def test_availability_is_stdlib_only():
    assert Trading212Adapter.availability() == (True, None)


def test_authorization_is_basic_with_an_empty_password():
    header = _adapter()._auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "demo-key-123:"


def test_auth_style_key_omits_the_colon():
    header = _adapter(authStyle="key")._auth_header()
    assert base64.b64decode(header.split(" ", 1)[1]).decode() == "demo-key-123"


def test_a_missing_credential_is_a_named_refusal():
    adapter = _adapter(credentialRef="")
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "TRADING212_CREDENTIALS"


def test_account_normalizes_the_cash_payload(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path: {
        "free": 1_250.5, "total": 9_800.0, "invested": 8_500.0, "ppl": 49.5,
    } if path == "/equity/account/cash" else None)
    account = adapter.account()
    assert account["server"] == "trading212"
    assert account["currency"] == "GBP"
    assert account["balance"] == pytest.approx(1_250.5)
    assert account["equity"] == pytest.approx(9_800.0)
    assert account["profit"] == pytest.approx(49.5)
    assert account["tradeAllowed"] is True


def test_demo_mode_is_derived_from_the_base_url(monkeypatch):
    adapter = _adapter(baseUrl="https://demo.trading212.com/api/v0")
    monkeypatch.setattr(adapter, "_get", lambda path: {"free": 0, "total": 0})
    assert adapter.account()["mode"] == "demo"


def test_positions_normalize_and_skip_zero_quantity(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path: [
        {"ticker": "AAPL_US_EQ", "quantity": 3.5, "averagePrice": 180.0,
         "currentPrice": 190.0, "ppl": 35.0, "fxPpl": 1.2},
        {"ticker": "MSFT_US_EQ", "quantity": -1.0, "averagePrice": 400.0,
         "currentPrice": 410.0, "ppl": -10.0},
        {"ticker": "EMPTY", "quantity": 0},
    ] if path == "/equity/portfolio" else None)
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["AAPL_US_EQ", "MSFT_US_EQ"]
    assert rows[0]["side"] == "buy" and rows[0]["volume"] == pytest.approx(3.5)
    assert rows[1]["side"] == "sell" and rows[1]["profit"] == pytest.approx(-10.0)


def test_orders_normalize_the_open_book(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path: [
        {"id": 77, "ticker": "VUSA_LSE_EQ", "quantity": -2, "type": "LIMIT",
         "limitPrice": 80.5, "status": "NEW"},
    ] if path == "/equity/orders" else None)
    orders = adapter.orders()
    assert orders[0]["ticket"] == "77"
    assert orders[0]["symbol"] == "VUSA_LSE_EQ"
    assert orders[0]["volumeCurrent"] == pytest.approx(2)
    assert orders[0]["openPrice"] == pytest.approx(80.5)


def test_probe_reports_disconnected_with_the_reason(monkeypatch):
    adapter = _adapter()

    def boom(_path):
        raise AdapterUnavailable("TRADING212_UNAUTHORIZED", "rejected")

    monkeypatch.setattr(adapter, "_get", boom)
    probe = adapter.probe()
    assert probe["available"] is True and probe["connected"] is False
    assert probe["code"] == "TRADING212_UNAUTHORIZED"


def test_quote_is_refused_rather_than_faked():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter().quote("AAPL_US_EQ")
    assert exc.value.code == "TRADING212_QUOTE_UNSUPPORTED"


def test_http_401_maps_to_unauthorized(monkeypatch):
    adapter = _adapter()

    def raising(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://x", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raising)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._get("/equity/account/cash")
    assert exc.value.code == "TRADING212_UNAUTHORIZED"


def test_registry_declares_the_kind_without_quote_with_execution():
    kind = get_kind("trading212")
    assert kind.execution is True
    assert "quote" not in kind.capabilities
    assert {"account", "positions", "orders"} <= set(kind.capabilities)
    assert any(row["id"] == "trading212" for row in list_kinds())


def test_make_adapter_returns_the_execution_adapter():
    assert isinstance(make_adapter("trading212", {"credentialRef": "T212_KEY"}),
                      Trading212Adapter)
