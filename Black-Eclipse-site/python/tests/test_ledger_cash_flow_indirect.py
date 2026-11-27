"""Indirect-method operating cash flow, reconciled to the direct statement.

The indirect figure is derived twice on purpose: net income + non-cash add-backs
+ working-capital movement must equal the direct operating bucket. A difference
is reported as a failed check, never plugged.
"""

from __future__ import annotations

from ledger_helpers import line, make_ledger, make_service, post


def _amount(value: str) -> float:
    return float(value)


def test_indirect_reconciles_to_direct_on_a_trading_month(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)

    def entry(day: int, lines: list[dict]) -> None:
        post(service, ledger_id, f"2026-01-{day:02d}", lines)

    entry(2, [line("1010", debit="100000"), line("3000", credit="100000")])
    entry(3, [line("1200", debit="9000"), line("2000", credit="9000")])
    entry(4, [line("1100", debit="24000"), line("4000", credit="24000")])
    entry(5, [line("5000", debit="9000"), line("1200", credit="9000")])
    entry(6, [line("6100", debit="2000"), line("1010", credit="2000")])
    entry(7, [line("1010", debit="12000"), line("1100", credit="12000")])
    entry(8, [line("1500", debit="15000"), line("1010", credit="15000")])
    entry(9, [line("1010", debit="30000"), line("2500", credit="30000")])

    report = service.cash_flow_indirect(ledger_id, start="2026-01-01", end="2026-01-31")
    assert report["kind"] == "cash_flow_indirect"
    assert _amount(report["netIncome"]) == 13000.0
    assert _amount(report["operatingCashFlow"]) == 10000.0
    assert _amount(report["directOperatingCashFlow"]) == 10000.0
    assert _amount(report["difference"]) == 0.0
    assert report["reconciled"] is True
    assert report["balanced"] is True

    # Investing/financing come from the same direct statement, not a second rule.
    assert _amount(report["investing"]) == -15000.0
    assert _amount(report["financing"]) == 130000.0
    assert _amount(report["closingCash"]) == 125000.0

    wc = {row["code"]: _amount(row["amount"]) for row in report["workingCapital"]["rows"]}
    assert wc["1100"] == -12000.0   # receivables increased → cash used
    assert wc["2000"] == 9000.0     # payables increased → cash provided
    assert "1200" not in wc         # inventory rose and fell back to zero


def test_depreciation_is_added_back(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-02-01", [
        line("1500", debit="10000"), line("1010", credit="10000"),
    ])
    post(service, ledger_id, "2026-02-02", [
        line("6900", debit="1000"), line("1510", credit="1000"),
    ])
    post(service, ledger_id, "2026-02-03", [
        line("1010", debit="5000"), line("4000", credit="5000"),
    ])

    report = service.cash_flow_indirect(ledger_id, start="2026-02-01", end="2026-02-28")
    assert _amount(report["netIncome"]) == 4000.0
    assert _amount(report["depreciation"]) == 1000.0
    assert _amount(report["operatingCashFlow"]) == 5000.0
    assert _amount(report["directOperatingCashFlow"]) == 5000.0
    assert report["reconciled"] is True


def test_non_cash_investing_on_credit_is_reported_not_plugged(tmp_path):
    # Equipment bought on credit: no cash moves, but the AP increase looks like
    # operating cash to a simple working-capital adjustment. The difference must
    # be visible and the check must fail, never be force-balanced.
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-03-01", [
        line("1500", debit="5000"), line("2000", credit="5000"),
    ])

    report = service.cash_flow_indirect(ledger_id, start="2026-03-01", end="2026-03-31")
    assert _amount(report["directOperatingCashFlow"]) == 0.0
    assert _amount(report["operatingCashFlow"]) == 5000.0
    assert _amount(report["difference"]) == -5000.0
    assert report["reconciled"] is False
    failed = [check for check in report["checks"] if not check["ok"]]
    assert [check["name"] for check in failed] == ["indirect-operating-reconciles-to-direct"]
