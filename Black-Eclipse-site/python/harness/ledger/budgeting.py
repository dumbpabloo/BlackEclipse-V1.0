"""Budgets, dimension reporting and a simple cash runway.

Pure functions over postings and account metadata. A budget is planning data;
actuals are always derived from the journal, never stored. Dimensions are the
`dimensions` map already validated on every journal line. The runway is a
labelled model (trailing operating burn + open AR/AP), not a promise.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .models import Account, money
from .money import D, ZERO, q_money

CREDIT_NORMAL = frozenset({"REVENUE", "LIABILITY", "EQUITY"})
_DAYS_PER_MONTH = Decimal("30.4375")


def _signed(account: Account, net: Decimal) -> Decimal:
    """Present a debit-positive net in the account's natural direction."""
    return -net if account.type in CREDIT_NORMAL else net


def _dimensions(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key).strip(): str(val).strip() for key, val in value.items() if str(key).strip()}


def _matches(line_dims: Mapping[str, str], posting_dims: Mapping[str, str]) -> bool:
    return all(posting_dims.get(key) == value for key, value in line_dims.items())


def budget_vs_actual(
    *,
    budget: Mapping[str, Any],
    accounts: Sequence[Account],
    postings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare a budget's account + dimension targets with journal actuals.

    A posting is assigned to the MOST SPECIFIC matching budget line (the one with
    the most dimension keys), so a broad "all projects" line and a specific
    project line do not double-count. Actuals that match no line are reported in
    `unbudgeted`, not hidden.
    """
    account_by_code = {account.code: account for account in accounts}
    lines = []
    for index, line in enumerate(budget.get("lines") or []):
        account = account_by_code.get(str(line.get("accountCode")))
        dims = _dimensions(line.get("dimensions"))
        lines.append({
            "index": index,
            "accountCode": str(line.get("accountCode")),
            "accountName": account.name if account else "(unknown account)",
            "accountType": account.type if account else "",
            "dimensions": dims,
            "budget": D(line.get("amount")),
            "specificity": len(dims),
            "actual": ZERO,
        })
    budgeted_codes = {line["accountCode"] for line in lines}
    best: dict[int, list[dict[str, Any]]] = {}
    for posting in postings:
        if str(posting.get("source") or "") == "year_end_close":
            continue
        code = str(posting.get("accountCode") or "")
        account = account_by_code.get(code)
        if account is None:
            continue
        # Only accounts the budget actually names can be "unbudgeted"; otherwise
        # every cash/AP line would flood the report.
        if code not in budgeted_codes:
            continue
        net = D(posting.get("baseDebit")) - D(posting.get("baseCredit"))
        signed = _signed(account, net)
        posting_dims = _dimensions(posting.get("dimensions"))
        candidates = [
            line for line in lines
            if line["accountCode"] == code and _matches(line["dimensions"], posting_dims)
        ]
        if not candidates:
            bucket = best.setdefault(-1, [])
            bucket.append({**posting, "_signed": signed, "_code": code})
            continue
        winner = max(candidates, key=lambda line: line["specificity"])
        winner["actual"] += signed

    rows = []
    for line in lines:
        variance = line["actual"] - line["budget"]
        account_type = line["accountType"]
        # Revenue: exceeding target is favourable. Cost/asset: staying under is.
        favorable = (line["actual"] >= line["budget"]
                     if account_type in ("REVENUE", "LIABILITY", "EQUITY")
                     else line["actual"] <= line["budget"])
        rows.append({
            "accountCode": line["accountCode"],
            "accountName": line["accountName"],
            "accountType": account_type,
            "dimensions": line["dimensions"],
            "budget": money(q_money(line["budget"])),
            "actual": money(q_money(line["actual"])),
            "variance": money(q_money(variance)),
            "variancePct": (
                float(q_money(variance / line["budget"] * 100))
                if line["budget"] != ZERO else None
            ),
            "favorable": favorable,
        })

    unbudgeted_by_account: dict[str, dict[str, Any]] = {}
    for entry in best.get(-1, []):
        code = entry["_code"]
        account = account_by_code.get(code)
        bucket = unbudgeted_by_account.setdefault(code, {
            "accountCode": code,
            "accountName": account.name if account else "(unknown account)",
            "accountType": account.type if account else "",
            "actual": ZERO,
            "lines": 0,
        })
        bucket["actual"] += entry["_signed"]
        bucket["lines"] += 1
    unbudgeted = [
        {**row, "actual": money(q_money(row["actual"]))}
        for row in sorted(unbudgeted_by_account.values(), key=lambda r: r["accountCode"])
    ]

    by_account: dict[str, dict[str, Any]] = {}
    for line in lines:
        bucket = by_account.setdefault(line["accountCode"], {
            "accountCode": line["accountCode"],
            "accountName": line["accountName"],
            "budget": ZERO, "actual": ZERO,
        })
        bucket["budget"] += line["budget"]
        bucket["actual"] += line["actual"]
    account_rows = [
        {**row, "budget": money(q_money(row["budget"])),
         "actual": money(q_money(row["actual"])),
         "variance": money(q_money(row["actual"] - row["budget"]))}
        for row in sorted(by_account.values(), key=lambda r: r["accountCode"])
    ]

    total_budget = sum((line["budget"] for line in lines), ZERO)
    total_actual = sum((line["actual"] for line in lines), ZERO)
    return {
        "kind": "budget_vs_actual",
        "budgetId": budget.get("id"),
        "budgetName": budget.get("name"),
        "startDate": budget.get("startDate"),
        "endDate": budget.get("endDate"),
        "currency": budget.get("currency"),
        "rows": rows,
        "byAccount": account_rows,
        "unbudgeted": unbudgeted,
        "totals": {
            "budget": money(q_money(total_budget)),
            "actual": money(q_money(total_actual)),
            "variance": money(q_money(total_actual - total_budget)),
        },
        "note": (
            "Budget is planning data. Actuals are journal movements in the budget "
            "period, signed in each account's natural direction."
        ),
    }


def dimension_summary(
    *,
    accounts: Sequence[Account],
    postings: Iterable[Mapping[str, Any]],
    dimension: str,
) -> dict[str, Any]:
    """Group journal movement by one dimension value (e.g. project, cost center)."""
    account_by_code = {account.code: account for account in accounts}
    key = str(dimension or "").strip()
    if not key:
        raise ValueError("dimension is required")
    groups: dict[str, dict[str, Any]] = {}
    for posting in postings:
        account = account_by_code.get(str(posting.get("accountCode") or ""))
        if account is None or account.is_cash:
            continue
        dims = _dimensions(posting.get("dimensions"))
        value = dims.get(key) or "(unassigned)"
        bucket = groups.setdefault(value, {
            "value": value,
            "debit": ZERO, "credit": ZERO,
            "revenue": ZERO, "expense": ZERO,
            "assets": ZERO, "liabilities": ZERO,
            "lines": 0,
        })
        debit = D(posting.get("baseDebit"))
        credit = D(posting.get("baseCredit"))
        bucket["debit"] += debit
        bucket["credit"] += credit
        bucket["lines"] += 1
        if account.type == "REVENUE":
            bucket["revenue"] += credit - debit
        elif account.type == "EXPENSE":
            bucket["expense"] += debit - credit
        elif account.type == "ASSET":
            bucket["assets"] += debit - credit
        elif account.type == "LIABILITY":
            bucket["liabilities"] += credit - debit
    rows = []
    for bucket in groups.values():
        rows.append({
            "value": bucket["value"],
            "debit": money(q_money(bucket["debit"])),
            "credit": money(q_money(bucket["credit"])),
            "revenue": money(q_money(bucket["revenue"])),
            "expense": money(q_money(bucket["expense"])),
            "assets": money(q_money(bucket["assets"])),
            "liabilities": money(q_money(bucket["liabilities"])),
            "lines": bucket["lines"],
        })
    rows.sort(key=lambda row: abs(D(row["debit"]) - D(row["credit"])), reverse=True)
    return {
        "kind": "dimension_summary",
        "dimension": key,
        "rows": rows,
        "unassignedLines": next(
            (row["lines"] for row in rows if row["value"] == "(unassigned)"), 0
        ),
    }


def cash_runway(
    *,
    opening_cash: Decimal,
    operating_cash_flow: Decimal,
    open_receivables: Decimal,
    open_payables: Decimal,
    history_days: int,
    horizon_days: int,
) -> dict[str, Any]:
    """Trailing-burn runway plus a labelled AR/AP-adjusted projection.

    `operating_cash_flow` is the direct operating figure for the trailing window.
    A positive figure is a surplus, so `monthlyBurn` is negative and runway is
    UNKNOWN rather than infinite.
    """
    history_days = max(1, int(history_days))
    horizon_days = max(0, int(horizon_days))
    months = Decimal(history_days) / _DAYS_PER_MONTH
    monthly_burn = -operating_cash_flow / months
    adjusted_cash = opening_cash + open_receivables - open_payables

    def _runway(cash: Decimal) -> str | None:
        if monthly_burn <= ZERO:
            return None
        return money(q_money(cash / monthly_burn))

    projected = adjusted_cash - monthly_burn * (Decimal(horizon_days) / _DAYS_PER_MONTH)
    checks = [
        {
            "name": "runway-uses-trailing-operating-burn",
            "ok": months > ZERO,
            "detail": f"{history_days} days = {months:.2f} months",
        },
        {
            "name": "opening-cash-not-negative",
            "ok": opening_cash >= ZERO,
            "detail": f"opening cash {money(q_money(opening_cash))}",
        },
    ]
    return {
        "kind": "cash_runway",
        "historyDays": history_days,
        "horizonDays": horizon_days,
        "openingCash": money(q_money(opening_cash)),
        "operatingCashFlow": money(q_money(operating_cash_flow)),
        "monthlyBurn": money(q_money(monthly_burn)),
        "openReceivables": money(q_money(open_receivables)),
        "openPayables": money(q_money(open_payables)),
        "adjustedCash": money(q_money(adjusted_cash)),
        "runwayMonths": _runway(opening_cash),
        "adjustedRunwayMonths": _runway(adjusted_cash),
        "projectedCashAtHorizon": money(q_money(projected)),
        "checks": checks,
        "assumptions": (
            "Trailing operating cash flow is extrapolated; open AR/AP are added "
            "at face value. This is a planning model, not a forecast of collections."
        ),
    }
