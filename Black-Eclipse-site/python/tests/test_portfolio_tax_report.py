"""Realized-gains reporting by tax year, term and currency; wash-sale candidates.

The report must never silently apply a jurisdiction's rule. These tests pin the
facts (gain, year, term, currency) and that a suspected wash sale is reported as
a candidate with the disposal's gain still shown unchanged.
"""

from __future__ import annotations

from portfolio_helpers import buy, make_portfolio, make_service, sell


def _disposal(report, symbol: str) -> dict:
    return next(row for row in report["disposals"] if row["symbol"] == symbol)


def test_long_term_gain_is_grouped_by_tax_year(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    buy(service, pid, "AAPL", "10", "100", date="2025-01-01")
    sell(service, pid, "AAPL", "10", "150", date="2026-02-01")

    report = service.realized_gains(pid)
    row = _disposal(report, "AAPL")
    assert row["taxYear"] == "2026"
    assert row["term"] == "LONG"
    assert float(row["realizedPnl"]) == 500.0
    assert float(row["proceeds"]) == 1500.0
    assert float(row["costBasis"]) == 1000.0

    year = report["years"][0]
    assert year["taxYear"] == "2026"
    assert year["currency"] == "USD"
    assert float(year["longTermGain"]) == 500.0
    assert float(year["totalGain"]) == 500.0


def test_short_term_gain_and_custom_long_term_threshold(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    buy(service, pid, "AAPL", "10", "100", date="2026-01-01")
    sell(service, pid, "AAPL", "10", "120", date="2026-02-01")

    row = _disposal(service.realized_gains(pid), "AAPL")
    assert row["term"] == "SHORT"
    assert float(row["realizedPnl"]) == 200.0

    # The threshold is caller-supplied, not a bundled jurisdiction rule.
    custom = _disposal(service.realized_gains(pid, long_term_days=30), "AAPL")
    assert custom["term"] == "LONG"


def test_wash_sale_candidate_is_reported_but_not_applied(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    buy(service, pid, "AAPL", "10", "100", date="2026-01-01")
    sell(service, pid, "AAPL", "10", "80", date="2026-02-01")      # -200 loss
    buy(service, pid, "AAPL", "10", "85", date="2026-02-10")       # replacement in window

    report = service.realized_gains(pid)
    row = _disposal(report, "AAPL")
    assert row["washSaleSuspected"] is True
    assert float(row["realizedPnl"]) == -200.0
    assert float(row["candidateDisallowedLoss"]) == 200.0
    assert any(r["transactionId"] for r in row["replacementTransactions"])
    assert report["washSale"]["applied"] is False
    assert "does not apply" in report["washSale"]["note"]
    # The loss is still the loss: a candidate is a flag, never a rewrite.
    assert float(report["years"][0]["totalGain"]) == -200.0


def test_no_candidate_outside_the_window(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    buy(service, pid, "AAPL", "10", "100", date="2026-01-01")
    sell(service, pid, "AAPL", "10", "80", date="2026-02-01")
    buy(service, pid, "AAPL", "10", "85", date="2026-04-01")       # 59 days later

    row = _disposal(service.realized_gains(pid), "AAPL")
    assert row["washSaleSuspected"] is False
    assert row["replacementTransactions"] == []
    assert row["candidateDisallowedLoss"] is None


def test_currencies_and_years_are_never_summed_together(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service, base="USD")
    buy(service, pid, "AAPL", "10", "100", date="2025-06-01", currency="USD")
    service.add_transaction(pid, {"type": "SELL", "symbol": "AAPL", "quantity": "10",
                                  "price": "110", "trade_date": "2025-07-01", "currency": "USD"})
    buy(service, pid, "NESN", "10", "100", date="2026-01-01", currency="EUR")
    service.add_transaction(pid, {"type": "SELL", "symbol": "NESN", "quantity": "10",
                                  "price": "130", "trade_date": "2026-02-01", "currency": "EUR"})

    report = service.realized_gains(pid)
    assert {(row["taxYear"], row["currency"]) for row in report["years"]} == {
        ("2025", "USD"), ("2026", "EUR"),
    }
    totals = {row["currency"]: float(row["realizedPnl"]) for row in report["totalsByCurrency"]}
    assert totals == {"USD": 100.0, "EUR": 300.0}


def test_tax_year_filter_and_empty_report(tmp_path):
    service = make_service(tmp_path)
    pid = make_portfolio(service)
    buy(service, pid, "AAPL", "10", "100", date="2024-01-01")
    sell(service, pid, "AAPL", "10", "150", date="2025-02-01")

    assert service.realized_gains(pid, tax_year=2025)["disposals"]
    assert service.realized_gains(pid, tax_year=2024)["disposals"] == []
    assert service.realized_gains(pid, tax_year=2024)["years"] == []
