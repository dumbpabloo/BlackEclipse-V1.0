"""Numeric parity, fourth tier: the non-bar cash-flow spine.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/src/entities/cashflow.py`).
Its contract is stated in three sentences and all three are checkable:

* **sign convention** — ``KIND_DIRECTION`` maps fifteen canonical kinds to the sign
  the holder's perspective requires, and construction **enforces** it. A kind missing
  from the table is a flow whose sign nobody checks, so the table's *contents* matter.
* **valuations are not cash** — ``nav``/``valuation``/``residual_value`` are marks and
  are excluded from ``total()`` by default; summing a NAV into distributions
  overstates what was received.
* **translation uses each flow's own date** — never one period-end rate, and a gap
  raises rather than forward-filling silently.

Comparing Eclipse against that found three gaps, all fixed here: the kind table was
missing six of the fifteen kinds, kind labels were **not normalized** (so
``"Capital Call"`` was silently unconstrained — or rejected, depending on the caller),
and valuation status was only ever set by the caller rather than derived from the
kind. The per-flow-date FX translation did not exist at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from harness.entities import cashflow as CF


# -- the reference contract, restated from source -------------------------

REFERENCE_KINDS = {
    "contribution": -1,
    "capital_call": -1,
    "purchase": -1,
    "subscription": -1,
    "transfer_in": -1,
    "fee": -1,
    "expense": -1,
    "distribution": 1,
    "dividend": 1,
    "coupon": 1,
    "interest": 1,
    "principal": 1,
    "redemption": 1,
    "transfer_out": 1,
    "proceeds": 1,
}
REFERENCE_VALUATIONS = {"nav", "residual_value", "valuation"}


def reference_normalize(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


# -- the kind table is the reference's ------------------------------------


def test_the_kind_table_is_exactly_the_references():
    assert dict(CF.KIND_DIRECTION) == REFERENCE_KINDS


def test_every_reference_kind_enforces_its_sign():
    for kind, sign in REFERENCE_KINDS.items():
        good = 10.0 * sign
        # the correct direction is accepted
        assert CF.CashFlow(date="2024-01-02", amount=good, kind=kind).amount == good
        # the wrong one is refused
        with pytest.raises(ValueError):
            CF.CashFlow(date="2024-01-02", amount=-good, kind=kind)


@pytest.mark.parametrize("spelling", [
    "Capital Call", "capital-call", "CAPITAL_CALL", " capital  call ",
    "Distribution", "TRANSFER-OUT", "Interest",
])
def test_kind_labels_are_normalised_like_the_reference(spelling):
    assert CF.normalize_kind(spelling) == reference_normalize(spelling)


def test_a_spelled_out_kind_is_still_sign_checked():
    """The bug this closes: "Capital Call" with a positive amount was unconstrained."""
    with pytest.raises(ValueError):
        CF.CashFlow(date="2024-01-02", amount=+5000, kind="Capital Call")
    flow = CF.CashFlow(date="2024-01-02", amount=-5000, kind="Capital Call")
    assert flow.kind == "capital_call"


# -- valuations are not cash ---------------------------------------------


def test_valuation_kinds_are_the_references():
    assert set(CF.VALUATION_KINDS) == REFERENCE_VALUATIONS


@pytest.mark.parametrize("kind", sorted(REFERENCE_VALUATIONS))
def test_a_valuation_kind_is_a_mark_even_when_the_caller_says_otherwise(kind):
    flow = CF.CashFlow(date="2024-06-30", amount=2500, kind=kind)
    assert flow.is_valuation is True


def test_a_nav_never_lands_in_the_total():
    series = CF.CashFlowSeries.of(
        CF.CashFlow(date="2024-01-02", amount=-1000, kind="capital_call"),
        CF.CashFlow(date="2024-03-01", amount=300, kind="distribution"),
        CF.CashFlow(date="2024-06-30", amount=2500, kind="nav"),
    )
    assert series.total() == pytest.approx(-700.0)
    assert len(series.valuations) == 1
    assert len(series.cash) == 2


def test_tvpi_is_undefined_without_paid_in_capital():
    series = CF.CashFlowSeries.of(
        CF.CashFlow(date="2024-03-01", amount=300, kind="distribution")
    )
    assert series.tvpi() is None           # not infinity, not a fabricated 0


# -- FX: each flow at its own date ---------------------------------------


RATES = {
    ("EUR", date(2024, 1, 2)): 1.08,
    ("EUR", date(2024, 1, 5)): 1.11,
}


def _entry(currency, day, amount):
    return {"currency": currency, "date": day, "amount": amount}


def test_translation_uses_the_flows_own_date_not_a_period_rate():
    rows = CF.translate_flows(
        [_entry("EUR", "2024-01-02", 100), _entry("EUR", "2024-01-05", 100)],
        RATES, "USD",
    )
    assert [row["amount"] for row in rows] == pytest.approx([108.0, 111.0])
    assert [row["fxRateDate"] for row in rows] == ["2024-01-02", "2024-01-05"]


def test_a_missing_rate_raises_instead_of_forward_filling():
    with pytest.raises(CF.MissingExchangeRateError):
        CF.translate_flows([_entry("EUR", "2024-01-04", 100)], RATES, "USD")


def test_stale_reuse_is_opt_in_and_flagged():
    rows = CF.translate_flows(
        [_entry("EUR", "2024-01-04", 100)], RATES, "USD", allow_stale=True
    )
    assert rows[0]["fxRate"] == pytest.approx(1.08)     # the 01-02 rate
    assert rows[0]["fxRateDate"] == "2024-01-02"
    assert rows[0]["fxRateIsStale"] is True


def test_staleness_can_be_bounded():
    with pytest.raises(CF.MissingExchangeRateError):
        CF.translate_flows(
            [_entry("EUR", "2024-01-10", 100)], RATES, "USD",
            allow_stale=True, max_staleness_days=2,
        )


def test_the_quote_currency_needs_no_rate():
    rows = CF.translate_flows([_entry("USD", "2024-01-03", 50)], RATES, "USD")
    assert rows[0]["amount"] == pytest.approx(50.0)
    assert rows[0]["fxRate"] == 1.0
    assert rows[0]["fxRateIsStale"] is False


def test_every_translated_flow_keeps_its_provenance():
    row = CF.translate_flows([_entry("EUR", "2024-01-02", 100)], RATES, "USD")[0]
    assert row["originalAmount"] == pytest.approx(100.0)
    assert row["originalCurrency"] == "EUR"
    assert row["currency"] == "USD"
    assert row["fxRate"] == pytest.approx(1.08)


@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_a_non_positive_or_non_finite_rate_is_refused(bad):
    with pytest.raises(ValueError):
        CF.translate_flows(
            [_entry("EUR", "2024-01-02", 100)], {("EUR", date(2024, 1, 2)): bad}, "USD"
        )


def test_an_entry_without_a_currency_is_refused():
    with pytest.raises(ValueError):
        CF.translate_flows([{"date": "2024-01-02", "amount": 10}], RATES, "USD")
