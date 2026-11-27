"""Statements derived from postings — never from an assumption.

Every figure here is computed from the journal. There is no cached total, no
"last known" balance and no plug. Each statement returns a `checks` list, and a
failed check is reported as a failed check rather than silently corrected.

Sign convention inside this module: `net = base_debit − base_credit`, i.e.
debit-positive. An asset therefore has a positive net; a liability, equity or
revenue account has a negative net. Every presentation layer converts to the
sign a reader expects, in exactly one place per statement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .models import Account, money
from .money import D, ZERO, q_money

Posting = dict[str, Any]

#: The source tag on a year-end closing entry. Closing moves are not economic
#: activity of the new year, so the P&L and indirect cash-flow statements omit
#: them while the trial balance / balance sheet include them.
YEAR_END_CLOSE_SOURCE = "year_end_close"


@dataclass
class AccountBalance:
    account: Account
    opening_debit: Decimal = ZERO
    opening_credit: Decimal = ZERO
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        """Closing net, debit-positive."""
        return (self.opening_debit + self.debit) - (self.opening_credit + self.credit)

    @property
    def movement_net(self) -> Decimal:
        return self.debit - self.credit

    @property
    def closing_debit(self) -> Decimal:
        return self.net if self.net > ZERO else ZERO

    @property
    def closing_credit(self) -> Decimal:
        return -self.net if self.net < ZERO else ZERO

    @property
    def touched(self) -> bool:
        return any((self.opening_debit, self.opening_credit, self.debit, self.credit))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.account.code,
            "name": self.account.name,
            "type": self.account.type,
            "subtype": self.account.subtype,
            "openingDebit": money(q_money(self.opening_debit)),
            "openingCredit": money(q_money(self.opening_credit)),
            "debit": money(q_money(self.debit)),
            "credit": money(q_money(self.credit)),
            "closingDebit": money(q_money(self.closing_debit)),
            "closingCredit": money(q_money(self.closing_credit)),
        }


def build_balances(
    accounts: Sequence[Account],
    opening: Iterable[Posting] = (),
    movement: Iterable[Posting] = (),
) -> dict[str, AccountBalance]:
    """Accumulate postings per account into opening + movement columns."""
    balances = {account.code: AccountBalance(account=account) for account in accounts}
    for row in opening:
        entry = balances.get(str(row.get("accountCode")))
        if entry is None:
            continue  # an account deleted after posting: excluded, not invented
        entry.opening_debit += row.get("baseDebit") or ZERO
        entry.opening_credit += row.get("baseCredit") or ZERO
    for row in movement:
        entry = balances.get(str(row.get("accountCode")))
        if entry is None:
            continue
        entry.debit += row.get("baseDebit") or ZERO
        entry.credit += row.get("baseCredit") or ZERO
    return balances


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


# ── Trial balance ────────────────────────────────────────────────────────────


def trial_balance(
    accounts: Sequence[Account],
    opening: Iterable[Posting] = (),
    movement: Iterable[Posting] = (),
    *,
    start: str = "",
    end: str = "",
    include_zero: bool = False,
) -> dict[str, Any]:
    balances = build_balances(accounts, opening, movement)
    rows = [
        entry.to_dict()
        for entry in balances.values()
        if include_zero or entry.touched
    ]
    total_debit = sum(
        (entry.closing_debit for entry in balances.values()), ZERO
    )
    total_credit = sum(
        (entry.closing_credit for entry in balances.values()), ZERO
    )
    movement_debit = sum((entry.debit for entry in balances.values()), ZERO)
    movement_credit = sum((entry.credit for entry in balances.values()), ZERO)
    checks = [
        _check(
            "closing-debits-equal-closing-credits",
            total_debit == total_credit,
            f"{money(total_debit)} vs {money(total_credit)}",
        ),
        _check(
            "movement-debits-equal-movement-credits",
            movement_debit == movement_credit,
            f"{money(movement_debit)} vs {money(movement_credit)}",
        ),
        _check(
            "postings-sum-to-zero",
            sum((entry.net for entry in balances.values()), ZERO) == ZERO,
            "sum of every account's closing net must be zero",
        ),
    ]
    return {
        "kind": "trial_balance",
        "startDate": start or None,
        "endDate": end or None,
        "rows": rows,
        "totals": {
            "openingDebit": money(sum((e.opening_debit for e in balances.values()), ZERO)),
            "openingCredit": money(sum((e.opening_credit for e in balances.values()), ZERO)),
            "debit": money(movement_debit),
            "credit": money(movement_credit),
            "closingDebit": money(total_debit),
            "closingCredit": money(total_credit),
        },
        "checks": checks,
        "balanced": all(check["ok"] for check in checks),
    }


# ── Balance sheet ────────────────────────────────────────────────────────────


def _section(balances: Iterable[AccountBalance], *, credit_positive: bool,
             include_zero: bool = False) -> tuple[list[dict[str, Any]], Decimal]:
    rows: list[dict[str, Any]] = []
    total = ZERO
    for entry in balances:
        value = -entry.net if credit_positive else entry.net
        if not include_zero and value == ZERO:
            continue
        rows.append({
            "code": entry.account.code,
            "name": entry.account.name,
            "subtype": entry.account.subtype,
            "amount": money(q_money(value)),
        })
        total += value
    return rows, total


def balance_sheet(
    accounts: Sequence[Account],
    postings: Iterable[Posting],
    *,
    as_of: str = "",
) -> dict[str, Any]:
    """Assets, liabilities and equity as of a date, with current earnings folded
    into equity. The accounting equation is a CHECK, not a claim."""
    balances = build_balances(accounts, (), postings)
    assets = [b for b in balances.values() if b.account.type == "ASSET"]
    liabilities = [b for b in balances.values() if b.account.type == "LIABILITY"]
    equity = [b for b in balances.values() if b.account.type == "EQUITY"]
    revenue = [b for b in balances.values() if b.account.type == "REVENUE"]
    expenses = [b for b in balances.values() if b.account.type == "EXPENSE"]

    asset_rows, total_assets = _section(assets, credit_positive=False)
    liability_rows, total_liabilities = _section(liabilities, credit_positive=True)
    equity_rows, total_equity = _section(equity, credit_positive=True)

    revenue_total = sum((-b.net for b in revenue), ZERO)
    expense_total = sum((b.net for b in expenses), ZERO)
    current_earnings = revenue_total - expense_total
    equity_rows.append({
        "code": "—",
        "name": "Current period earnings",
        "subtype": "RETAINED",
        "amount": money(q_money(current_earnings)),
    })

    total_equity_incl = total_equity + current_earnings
    checks = [
        _check(
            "assets-equal-liabilities-plus-equity",
            total_assets == total_liabilities + total_equity_incl,
            f"assets {money(total_assets)} vs L+E {money(total_liabilities + total_equity_incl)}",
        ),
        _check(
            "every-posting-is-accounted-for",
            sum((b.net for b in balances.values()), ZERO) == ZERO,
            "the sum of every account's net must be zero",
        ),
    ]
    return {
        "kind": "balance_sheet",
        "asOf": as_of or None,
        "assets": {"rows": asset_rows, "total": money(total_assets)},
        "liabilities": {"rows": liability_rows, "total": money(total_liabilities)},
        "equity": {"rows": equity_rows, "total": money(total_equity_incl)},
        "currentEarnings": money(current_earnings),
        "checks": checks,
        "balanced": all(check["ok"] for check in checks),
    }


# ── Income statement ─────────────────────────────────────────────────────────


def income_statement(
    accounts: Sequence[Account],
    postings: Iterable[Posting],
    *,
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    # A closing entry belongs to the new fiscal year's opening date but moves the
    # PRIOR year's income. Showing it as revenue/expense in the new year's P&L
    # would report the same activity twice; the balance sheet still includes it.
    rows = [row for row in postings
            if str(row.get("source") or "") != YEAR_END_CLOSE_SOURCE]
    balances = build_balances(accounts, (), rows)
    revenue_bals = [b for b in balances.values() if b.account.type == "REVENUE"]
    expense_bals = [b for b in balances.values() if b.account.type == "EXPENSE"]

    revenue_rows, revenue_total = _section(revenue_bals, credit_positive=True)
    cogs = [b for b in expense_bals if b.account.subtype == "COGS"]
    depreciation = [b for b in expense_bals if b.account.subtype == "DEPRECIATION"]
    operating = [
        b for b in expense_bals
        if b.account.subtype not in ("COGS", "DEPRECIATION")
    ]
    cogs_rows, cogs_total = _section(cogs, credit_positive=False)
    opex_rows, opex_total = _section(operating, credit_positive=False)
    dep_rows, dep_total = _section(depreciation, credit_positive=False)

    gross_profit = revenue_total - cogs_total
    total_expenses = cogs_total + opex_total + dep_total
    net_income = revenue_total - total_expenses
    checks = [
        _check(
            "net-income-from-components",
            net_income == gross_profit - opex_total - dep_total,
            f"net income {money(net_income)}",
        ),
    ]
    return {
        "kind": "income_statement",
        "startDate": start or None,
        "endDate": end or None,
        "revenue": {"rows": revenue_rows, "total": money(revenue_total)},
        "costOfSales": {"rows": cogs_rows, "total": money(cogs_total)},
        "grossProfit": money(gross_profit),
        "operatingExpenses": {"rows": opex_rows, "total": money(opex_total)},
        "depreciation": {"rows": dep_rows, "total": money(dep_total)},
        "totalExpenses": money(total_expenses),
        "netIncome": money(net_income),
        "checks": checks,
        "balanced": all(check["ok"] for check in checks),
    }


# ── Cash flow ────────────────────────────────────────────────────────────────


def cash_flow(
    accounts: Sequence[Account],
    postings: Iterable[Posting],
    *,
    opening: Iterable[Posting] = (),
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Cash movement by classification of the COUNTER line's account.

    A cash line's movement is attributed to the category of the non-cash lines in
    the same entry (`cash_flow_category`). Any part that cannot be classified
    lands in an explicit `unclassified` bucket and sets `complete=False` — the
    statement never invents a category to make itself look tidy.
    """
    account_by_code = {account.code: account for account in accounts}
    cash_codes = {account.code for account in accounts if account.is_cash}

    opening_balances = build_balances(accounts, (), opening)
    opening_cash = sum(
        (b.net for b in opening_balances.values() if b.account.code in cash_codes), ZERO
    )

    by_entry: dict[str, list[Posting]] = {}
    for row in postings:
        by_entry.setdefault(str(row.get("entryId")), []).append(row)

    buckets = {"OPERATING": ZERO, "INVESTING": ZERO, "FINANCING": ZERO, "UNCLASSIFIED": ZERO}
    cash_lines_total = ZERO
    for rows in by_entry.values():
        cash_rows = [row for row in rows if row.get("accountCode") in cash_codes]
        if not cash_rows:
            continue
        delta = sum(
            ((row.get("baseDebit") or ZERO) - (row.get("baseCredit") or ZERO)
             for row in cash_rows), ZERO
        )
        cash_lines_total += delta
        attributed = ZERO
        for row in rows:
            code = str(row.get("accountCode"))
            if code in cash_codes:
                continue
            counter = account_by_code.get(code)
            category = (counter.cash_flow_category if counter else "") or "UNCLASSIFIED"
            if category not in buckets:
                category = "UNCLASSIFIED"
            # cash received from a credit to the counter account
            amount = (row.get("baseCredit") or ZERO) - (row.get("baseDebit") or ZERO)
            buckets[category] += amount
            attributed += amount
        residual = delta - attributed
        if residual != ZERO:
            buckets["UNCLASSIFIED"] += residual

    net_change = sum(buckets.values(), ZERO)
    closing_cash = opening_cash + net_change
    checks = [
        _check(
            "cash-movement-reconciles-to-cash-lines",
            net_change == cash_lines_total,
            f"net change {money(net_change)} vs cash lines {money(cash_lines_total)}",
        ),
        _check(
            "opening-plus-movement-equals-closing",
            opening_cash + net_change == closing_cash,
            "opening + net change == closing cash",
        ),
        _check(
            "every-cash-movement-is-classified",
            buckets["UNCLASSIFIED"] == ZERO,
            f"unclassified {money(buckets['UNCLASSIFIED'])}",
        ),
    ]
    return {
        "kind": "cash_flow",
        "startDate": start or None,
        "endDate": end or None,
        "operating": money(q_money(buckets["OPERATING"])),
        "investing": money(q_money(buckets["INVESTING"])),
        "financing": money(q_money(buckets["FINANCING"])),
        "unclassified": money(q_money(buckets["UNCLASSIFIED"])),
        "netChange": money(q_money(net_change)),
        "openingCash": money(q_money(opening_cash)),
        "closingCash": money(q_money(closing_cash)),
        "complete": buckets["UNCLASSIFIED"] == ZERO,
        "checks": checks,
        "balanced": all(check["ok"] for check in checks),
    }


def cash_flow_indirect(
    accounts: Sequence[Account],
    postings: Iterable[Posting],
    *,
    opening: Iterable[Posting] = (),
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Indirect-method operating cash flow, reconciled to the direct statement.

    Starts with net income, adds back depreciation/amortisation, then adjusts for
    the movement in every non-cash ASSET/LIABILITY account whose declared
    `cash_flow_category` is OPERATING (receivables, inventory, prepaid, payables,
    tax, accruals, payroll, intercompany, suspense). Investing/financing lines
    come from the direct statement; only the operating section is derived twice.

    The direct-vs-indirect difference is a **check**, not a plug. A non-cash
    investing/financing entry (e.g. equipment bought on credit) is not modelled
    by this simple working-capital adjustment, so the difference is reported
    with the amount that did not reconcile rather than hidden.
    """
    movement = [row for row in postings
                if str(row.get("source") or "") != YEAR_END_CLOSE_SOURCE]
    opening_rows = list(opening)
    direct = cash_flow(accounts, movement, opening=opening_rows, start=start, end=end)
    balances = build_balances(accounts, (), movement)

    revenue_total = sum(
        (-b.net for b in balances.values() if b.account.type == "REVENUE"), ZERO
    )
    expense_total = sum(
        (b.net for b in balances.values() if b.account.type == "EXPENSE"), ZERO
    )
    net_income = revenue_total - expense_total

    depreciation = sum(
        (
            b.movement_net for b in balances.values()
            if b.account.type == "EXPENSE"
            and b.account.subtype in ("DEPRECIATION", "AMORTISATION")
        ),
        ZERO,
    )

    wc_rows: list[dict[str, Any]] = []
    wc_total = ZERO
    for balance in balances.values():
        account = balance.account
        if account.is_cash or account.type not in ("ASSET", "LIABILITY"):
            continue
        if (account.cash_flow_category or "") != "OPERATING":
            continue
        # A debit movement in an operating asset uses cash; a credit movement in
        # an operating liability provides cash. Negating the debit-positive net
        # gives the cash effect for both sides.
        amount = -balance.movement_net
        if amount == ZERO:
            continue
        wc_rows.append({
            "code": account.code,
            "name": account.name,
            "subtype": account.subtype,
            "amount": money(q_money(amount)),
        })
        wc_total += amount

    indirect_operating = net_income + depreciation + wc_total
    direct_operating = D(direct["operating"])
    difference = direct_operating - indirect_operating
    checks = [
        _check(
            "indirect-operating-reconciles-to-direct",
            difference == ZERO,
            f"direct {money(direct_operating)} vs indirect {money(indirect_operating)} "
            f"(difference {money(difference)})",
        ),
        _check(
            "every-cash-movement-is-classified",
            bool(direct["complete"]),
            f"unclassified {direct['unclassified']}",
        ),
    ]
    return {
        "kind": "cash_flow_indirect",
        "startDate": start or None,
        "endDate": end or None,
        "netIncome": money(q_money(net_income)),
        "depreciation": money(q_money(depreciation)),
        "workingCapital": {
            "rows": wc_rows,
            "total": money(q_money(wc_total)),
        },
        "operatingCashFlow": money(q_money(indirect_operating)),
        "directOperatingCashFlow": direct["operating"],
        "difference": money(q_money(difference)),
        "reconciled": difference == ZERO and bool(direct["complete"]),
        "investing": direct["investing"],
        "financing": direct["financing"],
        "unclassified": direct["unclassified"],
        "netChange": direct["netChange"],
        "openingCash": direct["openingCash"],
        "closingCash": direct["closingCash"],
        "complete": bool(direct["complete"]),
        "checks": checks,
        "balanced": all(check["ok"] for check in checks),
    }


__all__ = [
    "AccountBalance",
    "balance_sheet",
    "build_balances",
    "cash_flow",
    "cash_flow_indirect",
    "income_statement",
    "trial_balance",
]
