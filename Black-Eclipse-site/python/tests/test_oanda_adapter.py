"""OANDA v20 adapter: bearer auth, discovered account id, two-sided positions."""

from __future__ import annotations

import pytest

from harness.trade.adapter import AdapterUnavailable
from harness.trade.adapters.oanda import OandaAdapter, instrument_symbol
from harness.trade.registry import get_kind, list_kinds


def _adapter(**config) -> OandaAdapter:
    base = {"credentialRef": "OANDA_TOKEN"}
    base.update(config)
    return OandaAdapter(base)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("OANDA_TOKEN", "tok-123")


def test_availability_is_stdlib_only():
    assert OandaAdapter.availability() == (True, None)


def test_instrument_names_are_normalised_for_the_house_shape():
    assert instrument_symbol("EUR_USD") == "EURUSD"
    assert instrument_symbol("xau_usd") == "XAUUSD"


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_call_carries_the_bearer_token(monkeypatch):
    adapter = _adapter(accountId="101-001-1")
    captured: dict = {}

    def fake(request, timeout=0):
        captured["h"] = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        return _Resp(b'{"account":{}}')

    monkeypatch.setattr("urllib.request.urlopen", fake)
    adapter._get("/v3/accounts/101-001-1/summary")
    assert captured["h"]["authorization"] == "Bearer tok-123"
    assert "101-001-1" in captured["url"]


def test_a_missing_token_is_a_named_refusal():
    with pytest.raises(AdapterUnavailable) as exc:
        _adapter(credentialRef="").account()
    assert exc.value.code == "OANDA_CREDENTIALS"


def test_the_account_id_is_discovered_when_not_configured(monkeypatch):
    adapter = _adapter()
    seen: list = []

    def fake(path, params=None):
        seen.append(path)
        if path == "/v3/accounts":
            return {"accounts": [{"id": "101-002-2"}]}
        assert path == "/v3/accounts/101-002-2/summary"
        return {"account": {"NAV": 1000.0, "balance": 900.0, "marginAvailable": 800.0,
                            "marginUsed": 200.0, "unrealizedPL": 100.0, "currency": "USD"}}

    monkeypatch.setattr(adapter, "_get", fake)
    account = adapter.account()
    assert seen[0] == "/v3/accounts"
    assert account["equity"] == pytest.approx(1000.0)
    assert account["marginFree"] == pytest.approx(800.0)
    assert account["currency"] == "USD"
    assert account["tradeAllowed"] is True


def test_an_empty_account_list_is_a_refusal(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"accounts": []})
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "OANDA_NO_ACCOUNT"


def test_positions_report_both_halves_separately(monkeypatch):
    adapter = _adapter(accountId="A")
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"positions": [
        {"instrument": "EUR_USD",
         "long": {"units": "1000", "averagePrice": "1.1000", "unrealizedPL": "12.5",
                  "financing": "-0.4"},
         "short": {"units": "-500", "averagePrice": "1.1050", "unrealizedPL": "-3.0",
                   "financing": "0.1"}},
        {"instrument": "XAU_USD", "long": {"units": "0"}, "short": {"units": "0"}},
    ]})
    rows = adapter.positions()
    assert [(r["symbol"], r["side"]) for r in rows] == [
        ("EURUSD", "buy"), ("EURUSD", "sell")]
    long_row, short_row = rows
    assert long_row["volume"] == pytest.approx(1000.0)
    assert short_row["volume"] == pytest.approx(500.0)      # abs(units)
    assert long_row["currentPrice"] == 0.0                  # not marked, declared
    assert long_row["swap"] == pytest.approx(-0.4)


def test_orders_normalize_units_and_side(monkeypatch):
    adapter = _adapter(accountId="A")
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"orders": [
        {"id": "77", "instrument": "GBP_USD", "type": "LIMIT", "units": "-2000",
         "price": "1.2500", "state": "PENDING", "createTime": "1700000000000000"},
    ]})
    order = adapter.orders()[0]
    assert order["ticket"] == "77"
    assert order["symbol"] == "GBPUSD"
    assert order["volumeCurrent"] == pytest.approx(2000.0)
    assert "sell" in order["comment"]


def test_quote_reads_pricing_and_spells_the_pair(monkeypatch):
    adapter = _adapter(accountId="A")
    monkeypatch.setattr(adapter, "_get", lambda path, params=None: {"prices": [
        {"instrument": "EUR_USD", "bids": [{"price": "1.10000"}],
         "asks": [{"price": "1.10020"}],
         "closeoutBid": "1.09990", "closeoutAsk": "1.10030"}]})
    quote = adapter.quote("EURUSD")
    assert quote["symbol"] == "EURUSD"
    assert quote["bid"] == pytest.approx(1.10)
    assert quote["ask"] == pytest.approx(1.1002)
    # `spreadPoints` is in the quote's own point (10^-5 here, matching `digits`),
    # so a 2-pip spread on a 5-digit pair is 20 points — not 2.
    assert quote["spreadPoints"] == pytest.approx(20.0)
    assert quote["point"] == pytest.approx(0.00001)


def test_an_api_error_message_is_a_named_refusal(monkeypatch):
    adapter = _adapter(accountId="A")

    class _Err:
        def read(self, _n=None):
            return b'{"errorMessage":"Invalid access token"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Err())
    with pytest.raises(AdapterUnavailable) as exc:
        adapter.account()
    assert exc.value.code == "OANDA_API_ERROR"
    assert "Invalid access token" in exc.value.reason


def test_registry_declares_fx_and_execution():
    kind = get_kind("oanda")
    assert kind.execution is True
    assert "fx" in kind.asset_classes
    assert any(row["id"] == "oanda" for row in list_kinds())
