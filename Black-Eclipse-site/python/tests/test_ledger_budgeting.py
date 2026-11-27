"""Budgets vs actuals, journal-line dimensions, and cash runway."""

from __future__ import annotations

import pytest

from harness.ledger.errors import LedgerError
from ledger_helpers import line, make_ledger, make_service, post


def _create_budget(service, ledger_id: str) -> dict:
    return service.create_budget(
        ledger_id,
        name="FY2026 plan",
        start="2026-01-01",
        end="2026-12-31",
        lines=[
            {"account_code": "6100", "amount": "1000",
             "dimensions": {"project": "A", "cost_center": "OPS"}},
            {"account_code": "6100", "amount": "500", "dimensions": {"project": "B"}},
            {"account_code": "6100", "amount": "999"},          # broad fallback
            {"account_code": "4000", "amount": "2000", "dimensions": {"project": "A"}},
            {"account_code": "6200", "amount": "100", "dimensions": {"project": "A"}},
        ],
    )


def test_budget_create_list_and_audit(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    budget = _create_budget(service, ledger_id)

    assert budget["id"].startswith("bud_")
    assert budget["currency"] == "USD"
    assert len(budget["lines"]) == 5
    assert budget["lines"][0]["dimensions"] == {"project": "A", "cost_center": "OPS"}

    listed = service.list_budgets(ledger_id)
    assert [row["id"] for row in listed] == [budget["id"]]
    assert listed[0]["lines"][0]["amount"] == "1000.00000000"
    assert "BUDGET_CREATED" in [event["kind"] for event in service.audit_log(ledger_id)]


def test_budget_actuals_match_account_and_dimensions_with_specificity(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    budget = _create_budget(service, ledger_id)

    post(service, ledger_id, "2026-02-01", [
        line("6100", debit="1200", dimensions={"project": "A", "cost_center": "OPS"}),
        line("1010", credit="1200"),
    ])
    post(service, ledger_id, "2026-02-02", [
        line("6100", debit="300", dimensions={"project": "B"}),
        line("1010", credit="300"),
    ])
    post(service, ledger_id, "2026-02-03", [
        line("6100", debit="100", dimensions={"project": "UNPLANNED"}),
        line("1010", credit="100"),
    ])
    post(service, ledger_id, "2026-02-04", [
        line("1010", debit="2500"),
        line("4000", credit="2500", dimensions={"project": "A"}),
    ])
    post(service, ledger_id, "2026-02-05", [
        line("6200", debit="50", dimensions={"project": "Z"}),
        line("1010", credit="50"),
    ])

    report = service.budget_vs_actual(ledger_id, budget["id"])
    rows = {(row["accountCode"], tuple(sorted(row["dimensions"].items()))): row
            for row in report["rows"]}

    specific = rows[("6100", (("cost_center", "OPS"), ("project", "A")))]
    assert float(specific["actual"]) == 1200.0
    assert float(specific["variance"]) == 200.0
    assert specific["favorable"] is False

    project_b = rows[("6100", (("project", "B"),))]
    assert float(project_b["actual"]) == 300.0
    assert float(project_b["variance"]) == -200.0
    assert project_b["favorable"] is True

    # The broad 6100 line did NOT also receive project A's 1200 (specificity);
    # it is the catch-all for the UNPLANNED project instead.
    broad = rows[("6100", ())]
    assert float(broad["actual"]) == 100.0

    revenue_a = rows[("4000", (("project", "A"),))]
    assert float(revenue_a["actual"]) == 2500.0
    assert float(revenue_a["variance"]) == 500.0
    assert revenue_a["favorable"] is True

    unbudgeted = {row["accountCode"]: row for row in report["unbudgeted"]}
    assert float(unbudgeted["6200"]["actual"]) == 50.0
    assert float(report["totals"]["budget"]) == 4599.0
    assert float(report["totals"]["actual"]) == 4100.0   # unbudgeted is reported separately


def test_budget_refuses_unknown_account_or_negative_amount(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    with pytest.raises(LedgerError) as info:
        service.create_budget(ledger_id, name="bad", start="2026-01-01",
                              end="2026-12-31",
                              lines=[{"account_code": "9998", "amount": "1"}])
    assert info.value.code == "ACCOUNT_NOT_FOUND"

    with pytest.raises(LedgerError) as info:
        service.create_budget(ledger_id, name="bad", start="2026-01-01",
                              end="2026-12-31",
                              lines=[{"account_code": "6100", "amount": "-1"}])
    assert info.value.code == "BUDGET_INVALID"


def test_dimension_summary_groups_values_and_marks_unassigned(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-02-01", [
        line("6100", debit="300", dimensions={"project": "A"}),
        line("1010", credit="300"),
    ])
    post(service, ledger_id, "2026-02-02", [
        line("6100", debit="200", dimensions={"project": "B"}),
        line("1010", credit="200"),
    ])
    post(service, ledger_id, "2026-02-03", [
        line("6100", debit="100"),                # no project dimension
        line("1010", credit="100"),
    ])

    report = service.dimension_summary(ledger_id, "project",
                                       start="2026-01-01", end="2026-12-31")
    rows = {row["value"]: row for row in report["rows"]}
    assert float(rows["A"]["expense"]) == 300.0
    assert float(rows["B"]["expense"]) == 200.0
    assert float(rows["(unassigned)"]["expense"]) == 100.0
    assert report["unassignedLines"] == 1   # the expense line with no project

    with pytest.raises(LedgerError) as info:
        service.dimension_summary(ledger_id, "  ")
    assert info.value.code == "DIMENSION_REQUIRED"


def test_cash_runway_uses_trailing_burn_and_open_ar_ap(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-09-01", [
        line("1010", debit="100000"), line("3000", credit="100000"),
    ])
    post(service, ledger_id, "2026-10-01", [
        line("6100", debit="1500"), line("1010", credit="1500"),
    ])
    post(service, ledger_id, "2026-11-01", [
        line("6100", debit="1500"), line("1010", credit="1500"),
    ])

    report = service.cash_runway(ledger_id, as_of="2026-11-26",
                                 history_days=90, horizon_days=90)
    assert float(report["openingCash"]) == 97000.0
    assert float(report["operatingCashFlow"]) == -3000.0
    assert float(report["monthlyBurn"]) > 0.0
    assert report["runwayMonths"] is not None
    assert float(report["runwayMonths"]) > 90.0
    assert float(report["openReceivables"]) == 0.0
    assert report["receivables"]["reconciled"] is True
    assert report["payables"]["reconciled"] is True
    assert report["balanced"] is True


def test_cash_runway_reports_unknown_when_operating_is_a_surplus(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-09-01", [
        line("1010", debit="10000"), line("3000", credit="10000"),
    ])
    post(service, ledger_id, "2026-10-01", [
        line("1010", debit="5000"), line("4000", credit="5000"),
    ])
    post(service, ledger_id, "2026-10-02", [
        line("6100", debit="1000"), line("1010", credit="1000"),
    ])

    report = service.cash_runway(ledger_id, as_of="2026-11-26", history_days=90)
    assert float(report["operatingCashFlow"]) == 4000.0
    assert float(report["monthlyBurn"]) < 0.0
    assert report["runwayMonths"] is None       # UNKNOWN, not infinite
