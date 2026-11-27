"""Year-end close: zero the P&L and roll net income into retained earnings.

The closing entry is dated the first day of the next fiscal year. That keeps the
closed year's income statement intact; P&L readers omit `source=year_end_close`,
while the trial balance and balance sheet include the transfer.
"""

from __future__ import annotations

import pytest

from harness.ledger.errors import LedgerError
from ledger_helpers import line, make_ledger, make_service, post


def _post_year(service, ledger_id: str) -> None:
    post(service, ledger_id, "2026-02-01", [
        line("1010", debit="1000"), line("4000", credit="1000"),
    ])
    post(service, ledger_id, "2026-03-01", [
        line("6100", debit="400"), line("1010", credit="400"),
    ])


def _account_rows(report: dict) -> dict[str, dict]:
    return {row["code"]: row for row in report["rows"]}


def test_preview_shows_the_closing_entry_and_retained_target(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    _post_year(service, ledger_id)

    plan = service.preview_fiscal_year_close(ledger_id, fiscal_year=2026)
    assert plan["status"] == "READY"
    assert plan["startDate"] == "2026-01-01"
    assert plan["endDate"] == "2026-12-31"
    assert plan["closingDate"] == "2027-01-01"
    assert float(plan["netIncome"]) == 600.0
    assert plan["retainedAccount"]["code"] == "3100"
    assert plan["alreadyClosed"] is False
    assert float(plan["totals"]["debit"]) == float(plan["totals"]["credit"]) == 1000.0

    lines = {row["account_code"]: row for row in plan["lines"]}
    assert float(lines["4000"]["debit"]) == 1000.0
    assert float(lines["6100"]["credit"]) == 400.0
    assert float(lines["3100"]["credit"]) == 600.0

    # Preview writes nothing.
    entries = service.list_entries(ledger_id, limit=100)
    assert all(entry["source"] != "year_end_close" for entry in entries)


def test_close_posts_on_next_year_open_and_rolls_retained_earnings(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    _post_year(service, ledger_id)

    result = service.close_fiscal_year(ledger_id, fiscal_year=2026)
    assert result["status"] == "CLOSED"
    entry = result["entry"]
    assert entry["entryDate"] == "2027-01-01"
    assert entry["source"] == "year_end_close"
    assert entry["externalId"] == "ye:2026-12-31"

    trial = _account_rows(service.trial_balance(ledger_id, as_of="2027-01-01"))
    assert float(trial["4000"]["closingDebit"]) == 0.0
    assert float(trial["4000"]["closingCredit"]) == 0.0
    assert float(trial["6100"]["closingDebit"]) == 0.0
    assert float(trial["6100"]["closingCredit"]) == 0.0
    assert float(trial["3100"]["closingCredit"]) == 600.0

    sheet = service.balance_sheet(ledger_id, as_of="2027-01-01")
    assert sheet["balanced"] is True
    assert float(sheet["currentEarnings"]) == 0.0
    assert float(sheet["assets"]["total"]) == 600.0
    assert float(sheet["equity"]["total"]) == 600.0

    # The closed year is not rewritten.
    closed_year = service.income_statement(ledger_id, start="2026-01-01", end="2026-12-31")
    assert float(closed_year["netIncome"]) == 600.0
    # The closing entry is not new-year P&L activity.
    next_year = service.income_statement(ledger_id, start="2027-01-01", end="2027-12-31")
    assert float(next_year["netIncome"]) == 0.0
    assert float(next_year["revenue"]["total"]) == 0.0

    # Audit trail records the act and still verifies.
    kinds = [event["kind"] for event in service.audit_log(ledger_id)]
    assert "YEAR_END_CLOSED" in kinds
    assert service.verify_audit(ledger_id)["ok"] is True


def test_second_close_is_idempotent_and_posts_nothing(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    _post_year(service, ledger_id)

    first = service.close_fiscal_year(ledger_id, fiscal_year=2026)
    before = len(service.list_entries(ledger_id, limit=100))
    second = service.close_fiscal_year(ledger_id, fiscal_year=2026)
    after = len(service.list_entries(ledger_id, limit=100))

    assert first["status"] == "CLOSED"
    assert second["status"] == "ALREADY_CLOSED"
    assert second["entry"]["id"] == first["entry"]["id"]
    assert after == before


def test_reversing_the_close_allows_a_fresh_close(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    _post_year(service, ledger_id)

    first = service.close_fiscal_year(ledger_id, fiscal_year=2026)
    service.reverse(ledger_id, first["entry"]["id"], entry_date="2027-01-02",
                    memo="correct the close")

    second = service.close_fiscal_year(ledger_id, fiscal_year=2026)
    assert second["status"] == "CLOSED"
    assert second["entry"]["id"] != first["entry"]["id"]
    assert second["externalId"] != first["externalId"]
    # The re-close is dated on/after the reversal, so the journal stays ordered.
    assert second["entry"]["entryDate"] >= "2027-01-02"
    trial = _account_rows(service.trial_balance(ledger_id, as_of="2027-01-31"))
    assert float(trial["4000"]["closingDebit"]) == 0.0
    assert float(trial["3100"]["closingCredit"]) == 600.0


def test_net_loss_debits_retained_earnings(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-05-01", [
        line("1010", debit="100"), line("4000", credit="100"),
    ])
    post(service, ledger_id, "2026-06-01", [
        line("6100", debit="300"), line("1010", credit="300"),
    ])

    plan = service.preview_fiscal_year_close(ledger_id, fiscal_year=2026)
    assert float(plan["netIncome"]) == -200.0
    lines = {row["account_code"]: row for row in plan["lines"]}
    assert float(lines["3100"]["debit"]) == 200.0

    service.close_fiscal_year(ledger_id, fiscal_year=2026)
    trial = _account_rows(service.trial_balance(ledger_id, as_of="2027-01-01"))
    assert float(trial["3100"]["closingDebit"]) == 200.0


def test_fiscal_year_start_month_changes_the_bounds(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service, month=4)   # fiscal year starts in April
    post(service, ledger_id, "2026-05-01", [
        line("1010", debit="1000"), line("4000", credit="1000"),
    ])

    plan = service.preview_fiscal_year_close(ledger_id, fiscal_year=2026)
    assert plan["startDate"] == "2026-04-01"
    assert plan["endDate"] == "2027-03-31"
    assert plan["closingDate"] == "2027-04-01"


def test_close_refuses_without_activity_or_a_retained_account(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    with pytest.raises(LedgerError) as info:
        service.preview_fiscal_year_close(ledger_id, fiscal_year=2026)
    assert info.value.code == "YEAR_END_NOT_READY"

    post(service, ledger_id, "2026-05-01", [
        line("1010", debit="10"), line("4000", credit="10"),
    ])
    service.set_account_active(ledger_id, "3100", False)
    with pytest.raises(LedgerError) as info:
        service.preview_fiscal_year_close(ledger_id, fiscal_year=2026)
    assert info.value.code == "RETAINED_EARNINGS_MISSING"
