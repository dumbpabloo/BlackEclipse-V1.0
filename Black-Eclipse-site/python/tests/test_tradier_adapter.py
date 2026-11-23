"""Tradier broker adapter: envelope flattening, credential probe, refusals."""

from __future__ import annotations

import urllib.error

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.tradier import TradierAdapter, _rows
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> TradierAdapter:
    base = {"credentialRef": "TRADIER_TOKEN", "accountId": "VA123"}
    base.update(config)
    return TradierAdapter(base)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TRADIER_TOKEN", "tok-abc")


def test_availability_is_stdlib_only():
    assert TradierAdapter.availability() == (True, None)


def test_rows_flattens_tradiers_envelope():
    # Tradier returns `None`, one object, or a list for the same field.
    assert _rows(None) == []
    assert _rows({"symbol": "AAPL"}) == [{"symbol": "AAPL"}]
    assert _rows([{"symbol": "A"}, {"symbol": "B"}]) == [{"symbol": "A"}, {"symbol": "B"}]


def test_a_missing_token_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "TRADIER_CREDENTIALS"


def test_an_account_id_is_required_for_account_reads():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(accountId="").account()
    assert exc.value.code == "TRADIER_ACCOUNT"


def test_account_normalizes_balances(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "balances": {"total_equity": 55_000.0, "total_cash": 12_500.0,
                     "close_pl": 320.5, "margin": {"stock_buying_power": 25_000.0}},
    })
    account = adapter.account()
    assert account["server"] == "tradier"
    assert account["equity"] == pytest.approx(55_000.0)
    assert account["balance"] == pytest.approx(12_500.0)
    assert account["profit"] == pytest.approx(320.5)
    assert account["tradeAllowed"] is True


def test_sandbox_is_derived_from_the_base_url(monkeypatch):
    adapter = _adapter(baseUrl="https://sandbox.tradier.com")
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"balances": {}})
    assert adapter.account()["mode"] == "demo"


def test_positions_accept_a_single_object(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "positions": {"position": {"symbol": "AAPL", "quantity": 10.0, "cost_basis": 180.0}},
    })
    rows = adapter.positions()
    assert len(rows) == 1
    assert rows[0]["volume"] == pytest.approx(10)
    assert rows[0]["openPrice"] == pytest.approx(180.0)
    assert rows[0]["currentPrice"] == 0.0  # not marked to market by the adapter


def test_positions_skip_zero_quantity_and_map_short_side(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "positions": {"position": [
            {"symbol": "MSFT", "quantity": -5.0, "cost_basis": 400.0},
            {"symbol": "ZERO", "quantity": 0.0},
        ]},
    })
    rows = adapter.positions()
    assert [r["symbol"] for r in rows] == ["MSFT"]
    assert rows[0]["side"] == "sell"


def test_orders_normalize(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "orders": {"order": [{"id": 99, "symbol": "SPY", "type": "limit",
                              "remaining_quantity": 2.0, "price": 500.0,
                              "status": "open", "side": "buy"}]},
    })
    order = adapter.orders()[0]
    assert order["ticket"] == "99"
    assert order["volumeCurrent"] == pytest.approx(2)
    assert "buy" in order["comment"]


def test_quote_takes_the_first_row_of_an_envelope(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {
        "quotes": {"quote": [{"bid": 100.0, "ask": 100.2, "last": 100.1}]},
    })
    quote = adapter.quote("aapl")
    assert quote["symbol"] == "AAPL"
    assert quote["last"] == pytest.approx(100.1)


def test_quote_without_a_row_is_refused(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"quotes": None})
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.quote("NOPE")
    assert exc.value.code == "TRADIER_NO_QUOTE"


def test_probe_reports_disconnected_with_the_reason(monkeypatch):
    adapter = _adapter()

    def boom(path, params=None):
        raise AdapterUnavailable("TRADIER_UNAUTHORIZED", "rejected")

    monkeypatch.setattr(adapter, "_get", boom)
    probe = adapter.probe()
    assert probe["connected"] is False and probe["code"] == "TRADIER_UNAUTHORIZED"


def test_http_401_maps_to_unauthorized(monkeypatch):
    adapter = _adapter()

    def raising(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raising)
    with pytest.raises(AdapterUnavailable) as exc:
        adapter._get("/v1/markets/quotes")
    assert exc.value.code == "TRADIER_UNAUTHORIZED"


def test_registry_declares_the_kind_with_quotes_and_execution():
    kind = get_kind("tradier")
    assert kind.execution is True
    assert "quote" in kind.capabilities
    assert any(row["id"] == "tradier" for row in list_kinds())
