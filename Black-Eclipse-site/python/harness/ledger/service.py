"""The General Ledger service — one facade over the double-entry core.

Callers (agent tools, HTTP routes, a future importer) all come through HERE, so
the UI and the agent can never disagree and a second posting path is never built.

What this layer owns:
  * identity (ledger/account/entry ids) and tenancy authorization,
  * the posting flow: validate → resolve the OPEN period → persist atomically →
    audit,
  * reversals (the only way to correct a posted entry),
  * period control (close / reopen / lock),
  * statement assembly (fetch the right postings, hand them to `statements.py`).

What it does NOT own: arithmetic. Money rules live in `money.py`, posting rules in
`journal.py`, statement maths in `statements.py`. This file only sequences them.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Optional, Sequence

from . import budgeting
from . import statements as stmts
from .accounts import accounts_from_template, build_account, template_accounts
from .errors import (
    ACCOUNT_EXISTS,
    ACCOUNT_NOT_FOUND,
    ALREADY_REVERSED,
    ARTIFACT_NOT_FOUND,
    BUDGET_INVALID,
    DIMENSION_REQUIRED,
    RETAINED_EARNINGS_MISSING,
    YEAR_END_NOT_READY,
    ARTIFACT_STORE_UNAVAILABLE,
    ARTIFACT_UNHASHED,
    ATTACHMENT_NOT_FOUND,
    ENTRY_NOT_FOUND,
    INVALID_ACCOUNT,
    INVALID_ENTRY,
    LedgerError,
    LEDGER_FORBIDDEN,
    LEDGER_NOT_FOUND,
    PERIOD_CLOSED,
    PERIOD_LOCKED,
    PERIOD_NOT_FOUND,
    PERIOD_NOT_READY,
    RULE_NOT_FOUND,
    UNSUPPORTED_DATE_RANGE,
)
from .journal import reverse_journal_lines, validate_lines
from .models import JournalEntry, JournalLine, Ledger, LineSpec, Period, money
from .money import D, ZERO, q_money
from .periods import (
    build_period,
    fiscal_position,
    iso,
    parse_date,
    period_bounds,
    period_bounds_for,
    period_id as make_period_id,
    require_open,
    today,
)
from .store import LedgerStore


def _ledger_id() -> str:
    return f"ledg_{uuid.uuid4().hex[:20]}"


def _account_id() -> str:
    return f"acct_{uuid.uuid4().hex[:20]}"


def _entry_id() -> str:
    return f"je_{uuid.uuid4().hex[:20]}"


def _line_id() -> str:
    return f"jl_{uuid.uuid4().hex[:20]}"


def _currency(value: object, *, default: str = "USD") -> str:
    text = str(value or default).strip().upper()
    if len(text) != 3 or not text.isalpha():
        raise LedgerError(
            INVALID_ACCOUNT,
            f"currency must be a 3-letter ISO code; got {value!r}",
            {"currency": str(value)},
        )
    return text


class LedgerService:
    def __init__(self, store: LedgerStore) -> None:
        self.store = store
        # The AR/AP subledger shares the ONE connection; it is a separate table
        # group, not a separate database.
        from .subledger_store import SubledgerStore  # noqa: PLC0415

        self.subledger = SubledgerStore(store)
        # Entity groups and the consolidation report. Neither posts: a group owns
        # no journal, it aggregates member journals.
        from .consolidation import ConsolidationService  # noqa: PLC0415
        from .group_store import GroupStore  # noqa: PLC0415

        self.groups = GroupStore(store)
        self.consolidation = ConsolidationService(self)

    # ── Ledgers ──────────────────────────────────────────────────────────────
    def create_ledger(
        self,
        name: str,
        *,
        base_currency: str = "USD",
        fiscal_year_start_month: int = 1,
        chart_template: str = "standard",
        owner_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise LedgerError(INVALID_ENTRY, "a ledger needs a name", {})
        template = str(chart_template or "standard").strip().lower()
        # Validates the template and raises UNSUPPORTED_CHART (listing the
        # available charts) before any row is written.
        template_accounts(template)
        ledger_id = _ledger_id()
        ledger = self.store.create_ledger(
            ledger_id,
            clean_name,
            base_currency=_currency(base_currency),
            owner_id=owner_id,
            fiscal_year_start_month=int(fiscal_year_start_month or 1),
            chart_template=template,
            metadata=metadata,
        )
        accounts = accounts_from_template(
            template,
            ledger_id=ledger_id,
            id_factory=_account_id,
        )
        self.store.insert_accounts(accounts)
        self.store.audit(
            ledger_id, "LEDGER_CREATED", subject_id=ledger_id, actor=actor,
            detail={"name": clean_name, "chartTemplate": template,
                    "baseCurrency": ledger.base_currency},
        )
        payload = ledger.to_dict()
        payload["accounts"] = len(accounts)
        return payload

    def list_ledgers(self, owner_id: Optional[str] = None) -> list[dict[str, Any]]:
        return self.store.list_ledgers(owner_id)

    def get_ledger(self, ledger_id: str, *, actor_id: Optional[str] = None) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        accounts = self.store.list_accounts(ledger_id)
        periods = self.store.list_periods(ledger_id)
        entries = self.store.list_entries(ledger_id, limit=1, include_lines=False)
        payload = ledger.to_dict()
        payload["accounts"] = [account.to_dict() for account in accounts]
        payload["periods"] = [period.to_dict() for period in periods]
        payload["summary"] = {
            "accountCount": len(accounts),
            "periodCount": len(periods),
            "entryCount": self.store.count_entries(ledger_id),
            "lastEntryDate": entries[0].entry_date if entries else None,
            "currentPeriod": self._current_period_view(ledger),
        }
        return payload

    def delete_ledger(self, ledger_id: str, *, actor_id: Optional[str] = None) -> bool:
        self._require(ledger_id, actor_id=actor_id)
        self.subledger.purge(ledger_id)
        self.groups.purge_ledger(ledger_id)
        return self.store.delete_ledger(ledger_id)

    # ── Accounts ─────────────────────────────────────────────────────────────
    def create_account(
        self,
        ledger_id: str,
        *,
        code: str,
        name: str,
        account_type: str,
        subtype: str = "",
        parent_code: Optional[str] = None,
        currency: str = "",
        cash_flow_category: str = "",
        is_cash: bool = False,
        is_contra: bool = False,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        if self.store.get_account(ledger_id, str(code).strip()):
            raise LedgerError(
                ACCOUNT_EXISTS,
                f"account {str(code).strip()!r} already exists in this ledger",
                {"accountCode": str(code).strip()},
            )
        if parent_code and self.store.get_account(ledger_id, str(parent_code).strip()) is None:
            raise LedgerError(
                ACCOUNT_NOT_FOUND,
                f"parent account {parent_code!r} does not exist",
                {"parentCode": parent_code},
            )
        account = build_account(
            ledger_id,
            account_id=_account_id(),
            code=code,
            name=name,
            account_type=account_type,
            subtype=subtype,
            parent_code=parent_code,
            currency=_currency(currency) if currency else "",
            cash_flow_category=cash_flow_category,
            is_cash=is_cash,
            is_contra=is_contra,
        )
        self.store.add_account(account)
        self.store.audit(
            ledger_id, "ACCOUNT_CREATED", subject_id=account.id, actor=actor_id,
            detail={"code": account.code, "type": account.type},
        )
        return account.to_dict()

    def list_accounts(self, ledger_id: str, *, include_inactive: bool = True,
                      actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return [a.to_dict() for a in self.store.list_accounts(ledger_id,
                                                              include_inactive=include_inactive)]

    def set_account_active(self, ledger_id: str, code: str, active: bool, *,
                           actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        if not self.store.set_account_active(ledger_id, str(code).strip(), active):
            raise LedgerError(ACCOUNT_NOT_FOUND, f"account {code!r} not found",
                              {"accountCode": code})
        self.store.audit(
            ledger_id, "ACCOUNT_STATUS_CHANGED", subject_id=str(code), actor=actor_id,
            detail={"active": bool(active)},
        )
        account = self.store.get_account(ledger_id, str(code).strip())
        assert account is not None
        return account.to_dict()

    # ── Posting ──────────────────────────────────────────────────────────────
    def post(
        self,
        ledger_id: str,
        *,
        entry_date: object,
        lines: Sequence[LineSpec | dict],
        memo: str = "",
        source: str = "manual",
        external_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        when = parse_date(entry_date, field="entry_date")
        source_key = str(source or "manual").strip() or "manual"
        external = str(external_id).strip() if external_id else None

        # Idempotent import: a retried batch returns the FIRST posting, never a
        # duplicate. An entry with no external id is deliberately not deduped.
        if external:
            existing = self.store.find_by_external(ledger_id, source_key, external)
            if existing is not None:
                payload = existing.to_dict()
                payload["duplicate"] = True
                return payload

        accounts = {a.code: a for a in self.store.list_accounts(ledger_id)}
        validated = validate_lines(lines, accounts, base_currency=ledger.base_currency)
        period = self._resolve_open_period(ledger, when)

        entry = JournalEntry(
            id=_entry_id(),
            ledger_id=ledger_id,
            entry_no=self.store.next_entry_no(ledger_id),
            entry_date=iso(when),
            period_id=period.id,
            memo=str(memo or "").strip()[:1000],
            source=source_key,
            external_id=external,
            status="POSTED",
            created_by=actor_id,
            lines=[
                JournalLine(
                    id=_line_id(),
                    entry_id="",  # filled below once the entry id is known
                    line_no=line.line_no,
                    account_code=line.account.code,
                    account_id=line.account.id,
                    debit=line.debit,
                    credit=line.credit,
                    base_debit=line.base_debit,
                    base_credit=line.base_credit,
                    currency=line.currency,
                    fx_rate=line.fx_rate,
                    memo=line.memo,
                    dimensions=line.dimensions,
                    base_only=line.base_only,
                )
                for line in validated
            ],
        )
        for line in entry.lines:
            line.entry_id = entry.id
        self.store.insert_entry(entry)
        self.store.touch_ledger(ledger_id)
        self.store.audit(
            ledger_id, "ENTRY_POSTED", subject_id=entry.id, actor=actor_id,
            detail={"entryNo": entry.entry_no, "entryDate": entry.entry_date,
                    "total": money(sum((line.base_debit for line in entry.lines), ZERO))},
        )
        payload = entry.to_dict()
        payload["duplicate"] = False
        return payload

    def reverse(
        self,
        ledger_id: str,
        entry_id: str,
        *,
        entry_date: object = None,
        memo: str = "",
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        original = self.store.require_entry(entry_id)
        if original.ledger_id != ledger_id:
            raise LedgerError(ENTRY_NOT_FOUND, f"entry {entry_id} is not in ledger {ledger_id}",
                              {"entryId": entry_id})
        if original.reversed_by_entry_id:
            raise LedgerError(
                ALREADY_REVERSED,
                f"entry {original.entry_no} was already reversed by "
                f"{original.reversed_by_entry_id}",
                {"entryId": entry_id, "reversedBy": original.reversed_by_entry_id},
            )
        when = parse_date(entry_date or original.entry_date, field="entry_date")
        accounts = {a.code: a for a in self.store.list_accounts(ledger_id)}
        mirrored = reverse_journal_lines(original.lines)
        period = self._resolve_open_period(ledger, when)
        reversal = JournalEntry(
            id=_entry_id(),
            ledger_id=ledger_id,
            entry_no=self.store.next_entry_no(ledger_id),
            entry_date=iso(when),
            period_id=period.id,
            memo=(str(memo).strip() or f"Reversal of entry {original.entry_no}")[:1000],
            source="reversal",
            external_id=None,
            status="POSTED",
            reverses_entry_id=original.id,
            created_by=actor_id,
            lines=[],
        )
        validated = validate_lines(mirrored, accounts, base_currency=ledger.base_currency)
        for line in validated:
            reversal.lines.append(
                JournalLine(
                    id=_line_id(),
                    entry_id=reversal.id,
                    line_no=line.line_no,
                    account_code=line.account.code,
                    account_id=line.account.id,
                    debit=line.debit,
                    credit=line.credit,
                    base_debit=line.base_debit,
                    base_credit=line.base_credit,
                    currency=line.currency,
                    fx_rate=line.fx_rate,
                    memo=line.memo,
                    dimensions=line.dimensions,
                    base_only=line.base_only,
                )
            )
        self.store.insert_entry(reversal)
        self.store.mark_reversed(original.id, reversal.id)
        self.store.audit(
            ledger_id, "ENTRY_REVERSED", subject_id=original.id, actor=actor_id,
            detail={"reversalId": reversal.id, "reversalNo": reversal.entry_no},
        )
        payload = reversal.to_dict()
        payload["reversedEntryId"] = original.id
        return payload

    # ── Reads ────────────────────────────────────────────────────────────────
    def list_entries(
        self,
        ledger_id: str,
        *,
        start: object = None,
        end: object = None,
        account_code: Optional[str] = None,
        limit: int = 100,
        actor_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        start_iso = iso(parse_date(start)) if start else None
        end_iso = iso(parse_date(end)) if end else None
        if start_iso and end_iso and start_iso > end_iso:
            raise LedgerError(UNSUPPORTED_DATE_RANGE, "`start` is after `end`",
                              {"start": start_iso, "end": end_iso})
        entries = self.store.list_entries(
            ledger_id, start=start_iso, end=end_iso, account_code=account_code,
            limit=limit, include_lines=True,
        )
        return [entry.to_dict() for entry in entries]

    def get_entry(self, ledger_id: str, entry_id: str, *,
                  actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        entry = self._require_entry_in_ledger(ledger_id, entry_id)
        payload = entry.to_dict()
        # Evidence travels with the entry it supports, so an auditor reading one
        # posting sees its source documents without a second lookup.
        payload["attachments"] = self.store.list_attachments(ledger_id, entry_id=entry.id)
        return payload

    def trial_balance(
        self,
        ledger_id: str,
        *,
        as_of: object = None,
        start: object = None,
        include_zero: bool = False,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        end_iso = iso(parse_date(as_of)) if as_of else iso(today())
        start_iso = iso(parse_date(start)) if start else None
        opening, movement = self._batches(ledger_id, start_iso, end_iso)
        accounts = self.store.list_accounts(ledger_id)
        return stmts.trial_balance(
            accounts, opening, movement, start=start_iso or "", end=end_iso,
            include_zero=include_zero,
        )

    def balance_sheet(self, ledger_id: str, *, as_of: object = None,
                      actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        end_iso = iso(parse_date(as_of)) if as_of else iso(today())
        accounts = self.store.list_accounts(ledger_id)
        postings = self.store.postings(ledger_id, end=end_iso)
        return stmts.balance_sheet(accounts, postings, as_of=end_iso)

    def income_statement(
        self,
        ledger_id: str,
        *,
        start: object = None,
        end: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        start_iso, end_iso = self._default_range(ledger_id, start, end)
        accounts = self.store.list_accounts(ledger_id)
        postings = self.store.postings(ledger_id, start=start_iso, end=end_iso)
        return stmts.income_statement(accounts, postings, start=start_iso, end=end_iso)

    def cash_flow(
        self,
        ledger_id: str,
        *,
        start: object = None,
        end: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        start_iso, end_iso = self._default_range(ledger_id, start, end)
        accounts = self.store.list_accounts(ledger_id)
        opening = self.store.postings(ledger_id, end=_day_before(start_iso))
        movement = self.store.postings(ledger_id, start=start_iso, end=end_iso)
        return stmts.cash_flow(accounts, movement, opening=opening,
                               start=start_iso, end=end_iso)

    def cash_flow_indirect(
        self,
        ledger_id: str,
        *,
        start: object = None,
        end: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        start_iso, end_iso = self._default_range(ledger_id, start, end)
        accounts = self.store.list_accounts(ledger_id)
        opening = self.store.postings(ledger_id, end=_day_before(start_iso))
        movement = self.store.postings(ledger_id, start=start_iso, end=end_iso)
        return stmts.cash_flow_indirect(accounts, movement, opening=opening,
                                        start=start_iso, end=end_iso)

    # ── Year-end close ───────────────────────────────────────────────────────
    def _default_fiscal_year(self, ledger: Ledger) -> int:
        """The most recently completed fiscal year as of today."""
        now = today()
        fiscal_year, _ = fiscal_position(ledger.fiscal_year_start_month, now)
        _, year_end = period_bounds(
            ledger.fiscal_year_start_month, fiscal_year, 12
        )
        return fiscal_year if now > year_end else fiscal_year - 1

    def _year_end_plan(self, ledger: Ledger, fiscal_year: int) -> dict[str, Any]:
        """Compute the closing entry without writing it.

        The closing entry is dated the FIRST day of the next fiscal year, so the
        closed year's income statement is never rewritten. P&L statements omit
        `source=year_end_close`, while the balance sheet and trial balance include
        it — the transfer is real, it just is not new-year activity.
        """
        start_month = ledger.fiscal_year_start_month
        year_start, _ = period_bounds(start_month, fiscal_year, 1)
        _, year_end = period_bounds(start_month, fiscal_year, 12)
        next_start, _ = period_bounds(start_month, fiscal_year + 1, 1)
        start_iso = iso(year_start)
        end_iso = iso(year_end)
        closing_date = iso(next_start)
        external_id = f"ye:{end_iso}"

        accounts = self.store.list_accounts(ledger.id)
        # Cumulative through the year end: PRIOR closing entries must be included
        # so their revenue/expense balances are already zero. The current year has
        # no closing entry yet, so what remains is this year's P&L.
        postings = self.store.postings(ledger.id, end=end_iso)
        balances = stmts.build_balances(accounts, (), postings)
        retained = next(
            (a for a in accounts
             if a.active and a.type == "EQUITY" and a.subtype == "RETAINED"),
            None,
        )
        if retained is None:
            raise LedgerError(
                RETAINED_EARNINGS_MISSING,
                "the chart has no active retained-earnings account (EQUITY/RETAINED)",
                {"ledgerId": ledger.id},
            )

        lines: list[dict[str, Any]] = []
        debit_total = ZERO
        credit_total = ZERO
        revenue_total = ZERO
        expense_total = ZERO
        for balance in sorted(balances.values(), key=lambda b: b.account.code):
            account = balance.account
            if account.type not in ("REVENUE", "EXPENSE"):
                continue
            if account.type == "REVENUE":
                revenue_total += -balance.net
            else:
                expense_total += balance.net
            zeroing = -balance.net
            if zeroing > ZERO:
                lines.append({"account_code": account.code, "debit": money(zeroing),
                              "memo": f"Close {account.code} {account.name}"})
                debit_total += zeroing
            elif zeroing < ZERO:
                lines.append({"account_code": account.code, "credit": money(-zeroing),
                              "memo": f"Close {account.code} {account.name}"})
                credit_total += -zeroing

        net_income = revenue_total - expense_total
        balance_due = debit_total - credit_total
        if balance_due > ZERO:
            lines.append({"account_code": retained.code, "credit": money(balance_due),
                          "memo": f"Retained earnings FY{fiscal_year}"})
            credit_total += balance_due
        elif balance_due < ZERO:
            lines.append({"account_code": retained.code, "debit": money(-balance_due),
                          "memo": f"Retained earnings FY{fiscal_year}"})
            debit_total += -balance_due
        existing = self.store.find_by_external(
            ledger.id, stmts.YEAR_END_CLOSE_SOURCE, external_id
        )
        if existing is not None and existing.status == "REVERSED":
            # A reversal is how a close is corrected. The original external id is
            # spent, so a re-close uses a fresh id rather than deduping to the
            # reversed entry. Date the re-close on/after its reversal so the
            # journal order stays chronological.
            reversal = (
                self.store.get_entry(existing.reversed_by_entry_id, include_lines=False)
                if existing.reversed_by_entry_id else None
            )
            if reversal is not None and reversal.entry_date > closing_date:
                closing_date = reversal.entry_date
            existing = None
            external_id = f"ye:{end_iso}:reclose:{self.store.next_entry_no(ledger.id)}"
        if not lines and existing is not None:
            # Already closed: the P&L is zero, so there is nothing left to move.
            return {
                "fiscalYear": int(fiscal_year),
                "startDate": start_iso,
                "endDate": end_iso,
                "closingDate": closing_date,
                "externalId": external_id,
                "netIncome": money(ZERO),
                "retainedAccount": {"code": retained.code, "name": retained.name},
                "lines": [],
                "totals": {"debit": money(ZERO), "credit": money(ZERO)},
                "alreadyClosed": True,
                "existingEntry": existing.to_dict(),
            }
        if not lines:
            raise LedgerError(
                YEAR_END_NOT_READY,
                f"no revenue or expense balances to close for FY{fiscal_year}",
                {"fiscalYear": fiscal_year, "startDate": start_iso, "endDate": end_iso},
            )
        if debit_total != credit_total:
            raise LedgerError(
                "UNBALANCED_ENTRY",
                f"year-end plan does not balance: debits {money(debit_total)} "
                f"vs credits {money(credit_total)}",
                {"fiscalYear": fiscal_year},
            )
        return {
            "fiscalYear": int(fiscal_year),
            "startDate": start_iso,
            "endDate": end_iso,
            "closingDate": closing_date,
            "externalId": external_id,
            "netIncome": money(q_money(net_income)),
            "retainedAccount": {"code": retained.code, "name": retained.name},
            "lines": lines,
            "totals": {
                "debit": money(q_money(debit_total)),
                "credit": money(q_money(credit_total)),
            },
            "alreadyClosed": existing is not None,
            "existingEntry": existing.to_dict() if existing else None,
        }

    def preview_fiscal_year_close(
        self,
        ledger_id: str,
        *,
        fiscal_year: Optional[int] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        year = int(fiscal_year) if fiscal_year not in (None, "") else self._default_fiscal_year(ledger)
        plan = self._year_end_plan(ledger, year)
        plan["status"] = "ALREADY_CLOSED" if plan["alreadyClosed"] else "READY"
        return plan

    def close_fiscal_year(
        self,
        ledger_id: str,
        *,
        fiscal_year: Optional[int] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Post the closing entry and roll current earnings into retained earnings.

        Idempotent by `(source, externalId)`: a second call returns the existing
        entry as `ALREADY_CLOSED` rather than double-posting. A correction is a
        reversal, like every other entry.
        """
        ledger = self._require(ledger_id, actor_id=actor_id)
        year = int(fiscal_year) if fiscal_year not in (None, "") else self._default_fiscal_year(ledger)
        plan = self._year_end_plan(ledger, year)
        if plan["alreadyClosed"]:
            return {**plan, "status": "ALREADY_CLOSED", "entry": plan["existingEntry"]}

        entry = self.post(
            ledger_id,
            entry_date=plan["closingDate"],
            memo=f"Year-end close FY{year}: transfer current earnings to retained earnings",
            lines=plan["lines"],
            source=stmts.YEAR_END_CLOSE_SOURCE,
            external_id=plan["externalId"],
            actor_id=actor_id,
        )
        self.store.audit(
            ledger_id, "YEAR_END_CLOSED", subject_id=entry.get("id"), actor=actor_id,
            detail={
                "fiscalYear": year,
                "startDate": plan["startDate"],
                "endDate": plan["endDate"],
                "netIncome": plan["netIncome"],
                "retainedAccount": plan["retainedAccount"]["code"],
            },
        )
        return {**plan, "status": "CLOSED", "entry": entry}

    # ── Budgets, dimensions, runway ──────────────────────────────────────────
    def create_budget(
        self,
        ledger_id: str,
        *,
        name: str,
        start: object,
        end: object,
        lines: Sequence[dict[str, Any]],
        note: Optional[str] = None,
        currency: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        start_iso = iso(parse_date(start, field="start"))
        end_iso = iso(parse_date(end, field="end"))
        if start_iso > end_iso:
            raise LedgerError(UNSUPPORTED_DATE_RANGE, "`start` is after `end`",
                              {"start": start_iso, "end": end_iso})
        accounts = {account.code: account for account in self.store.list_accounts(ledger_id)}
        if not lines:
            raise LedgerError(BUDGET_INVALID, "a budget needs at least one line", {})
        prepared = []
        for index, line in enumerate(lines):
            code = str(line.get("account_code") or line.get("accountCode") or "").strip()
            if code not in accounts:
                raise LedgerError(ACCOUNT_NOT_FOUND, f"unknown account {code!r}",
                                  {"accountCode": code, "line": index})
            raw_amount = line.get("amount")
            amount = D(raw_amount)
            if amount < ZERO:
                raise LedgerError(BUDGET_INVALID, "a budget amount cannot be negative",
                                  {"accountCode": code, "line": index})
            dimensions = line.get("dimensions") or {}
            if not isinstance(dimensions, dict):
                raise LedgerError(BUDGET_INVALID, "`dimensions` must be an object",
                                  {"accountCode": code, "line": index})
            prepared.append({
                "id": f"bl_{uuid.uuid4().hex[:20]}",
                "accountCode": code,
                "amount": money(amount),
                "dimensions": {
                    str(key).strip(): str(value).strip()
                    for key, value in dimensions.items() if str(key).strip()
                },
                "memo": line.get("memo"),
            })
        budget = self.store.insert_budget(
            f"bud_{uuid.uuid4().hex[:20]}", ledger_id,
            str(name or "").strip() or "Budget",
            start_iso, end_iso, lines=prepared,
            currency=str(currency or ledger.base_currency or "USD").upper(),
            note=note, created_by=actor_id,
        )
        self.store.audit(
            ledger_id, "BUDGET_CREATED", subject_id=budget["id"], actor=actor_id,
            detail={"name": budget["name"], "startDate": start_iso,
                    "endDate": end_iso, "lines": len(prepared)},
        )
        return budget

    def list_budgets(self, ledger_id: str, *, limit: int = 100,
                     actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return self.store.list_budgets(ledger_id, limit=limit)

    def get_budget(self, ledger_id: str, budget_id: str, *,
                   actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        return self.store.require_budget(ledger_id, budget_id)

    def budget_vs_actual(self, ledger_id: str, budget_id: str, *,
                         actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        budget = self.store.require_budget(ledger_id, budget_id)
        accounts = self.store.list_accounts(ledger_id)
        postings = self.store.postings(
            ledger_id, start=budget["startDate"], end=budget["endDate"]
        )
        return budgeting.budget_vs_actual(
            budget=budget, accounts=accounts, postings=postings
        )

    def dimension_summary(
        self,
        ledger_id: str,
        dimension: str,
        *,
        start: object = None,
        end: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        key = str(dimension or "").strip()
        if not key:
            raise LedgerError(DIMENSION_REQUIRED, "`dimension` is required",
                              {"dimension": dimension})
        start_iso, end_iso = self._default_range(ledger_id, start, end)
        accounts = self.store.list_accounts(ledger_id)
        postings = self.store.postings(ledger_id, start=start_iso, end=end_iso)
        result = budgeting.dimension_summary(
            accounts=accounts, postings=postings, dimension=key
        )
        result["startDate"] = start_iso
        result["endDate"] = end_iso
        return result

    def cash_runway(
        self,
        ledger_id: str,
        *,
        as_of: object = None,
        history_days: int = 90,
        horizon_days: int = 90,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        as_of_iso = iso(parse_date(as_of)) if as_of else iso(today())
        since = (date.fromisoformat(as_of_iso) - timedelta(days=max(1, int(history_days)))).isoformat()
        accounts = self.store.list_accounts(ledger_id)
        cash_codes = {account.code for account in accounts if account.is_cash}
        cash_rows = self.store.postings(ledger_id, end=as_of_iso)
        opening_cash = sum(
            (D(row.get("baseDebit")) - D(row.get("baseCredit"))
             for row in cash_rows if row.get("accountCode") in cash_codes),
            ZERO,
        )
        flow = self.cash_flow(ledger_id, start=since, end=as_of_iso, actor_id=actor_id)
        receivables = self.aging(ledger_id, kind="AR", as_of=as_of_iso, actor_id=actor_id)
        payables = self.aging(ledger_id, kind="AP", as_of=as_of_iso, actor_id=actor_id)
        result = budgeting.cash_runway(
            opening_cash=opening_cash,
            operating_cash_flow=D(flow["operating"]),
            open_receivables=D(receivables["openTotal"]),
            open_payables=D(payables["openTotal"]),
            history_days=int(history_days),
            horizon_days=int(horizon_days),
        )
        result["asOf"] = as_of_iso
        result["operatingWindow"] = {"startDate": since, "endDate": as_of_iso}
        result["receivables"] = {
            "openTotal": receivables["openTotal"],
            "buckets": receivables["buckets"],
            "reconciled": receivables["reconciled"],
        }
        result["payables"] = {
            "openTotal": payables["openTotal"],
            "buckets": payables["buckets"],
            "reconciled": payables["reconciled"],
        }
        result["checks"] = list(result["checks"]) + [
            {"name": "receivables-reconcile-to-control",
             "ok": bool(receivables["reconciled"]),
             "detail": f"AR open {receivables['openTotal']}"},
            {"name": "payables-reconcile-to-control",
             "ok": bool(payables["reconciled"]),
             "detail": f"AP open {payables['openTotal']}"},
        ]
        result["balanced"] = all(check["ok"] for check in result["checks"])
        return result

    # ── Periods ──────────────────────────────────────────────────────────────
    def list_periods(self, ledger_id: str, *, actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return [period.to_dict() for period in self.store.list_periods(ledger_id)]

    def close_period(
        self,
        ledger_id: str,
        *,
        period_id: Optional[str] = None,
        fiscal_year: Optional[int] = None,
        period_no: Optional[int] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ledger = self._require(ledger_id, actor_id=actor_id)
        period = self._locate_period(ledger, period_id, fiscal_year, period_no)
        if period.status == "LOCKED":
            raise LedgerError(PERIOD_LOCKED, "period is LOCKED and cannot be re-closed",
                              {"periodId": period.id})
        from .close import evaluate, persist  # noqa: PLC0415

        report = evaluate(self, ledger_id, start=period.start_date,
                          end=period.end_date, actor_id=actor_id)
        record = persist(self, report, period_id=period.id, actor_id=actor_id)
        if report["status"] == "BLOCKED":
            raise LedgerError(
                PERIOD_NOT_READY,
                f"period {period.start_date}..{period.end_date} has "
                f"{report['blockerCount']} blocking finding(s)",
                {"periodId": period.id, "findings": report["findings"],
                 "runId": record["id"], "checks": report["checks"]},
            )
        updated = self.store.set_period_status(period.id, "CLOSED", actor=actor_id)
        self.store.audit(
            ledger_id, "PERIOD_CLOSED", subject_id=period.id, actor=actor_id,
            detail={"startDate": period.start_date, "endDate": period.end_date,
                    "closeRunId": record["id"], "warnings": report["warningCount"]},
        )
        assert updated is not None
        payload = updated.to_dict()
        payload["close"] = report
        payload["runId"] = record["id"]
        return payload

    def reopen_period(self, ledger_id: str, period_id: str, *,
                      actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        period = self.store.get_period_by_id(period_id)
        if period is None or period.ledger_id != ledger_id:
            raise LedgerError(PERIOD_NOT_FOUND, f"period {period_id} not found",
                              {"periodId": period_id})
        if period.status == "LOCKED":
            raise LedgerError(PERIOD_LOCKED, "a LOCKED period can never be reopened",
                              {"periodId": period.id})
        if period.status == "OPEN":
            return period.to_dict()
        updated = self.store.set_period_status(period.id, "OPEN", actor=actor_id)
        self.store.audit(ledger_id, "PERIOD_REOPENED", subject_id=period.id, actor=actor_id,
                         detail={"startDate": period.start_date, "endDate": period.end_date})
        assert updated is not None
        return updated.to_dict()

    def lock_period(self, ledger_id: str, period_id: str, *,
                    actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        period = self.store.get_period_by_id(period_id)
        if period is None or period.ledger_id != ledger_id:
            raise LedgerError(PERIOD_NOT_FOUND, f"period {period_id} not found",
                              {"periodId": period_id})
        if period.status == "OPEN":
            raise LedgerError(PERIOD_CLOSED, "close the period before locking it",
                              {"periodId": period.id, "status": period.status})
        updated = self.store.set_period_status(period.id, "LOCKED", actor=actor_id)
        self.store.audit(ledger_id, "PERIOD_LOCKED", subject_id=period.id, actor=actor_id,
                         detail={"startDate": period.start_date, "endDate": period.end_date})
        assert updated is not None
        return updated.to_dict()

    def audit_log(self, ledger_id: str, *, limit: int = 100,
                  actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return self.store.list_audit(ledger_id, limit=limit)

    def verify_audit(self, ledger_id: str, *,
                     actor_id: Optional[str] = None) -> dict[str, Any]:
        """Prove the audit trail has not been edited, reordered or truncated.

        A broken chain names the first bad event; a legacy unsealed trail says
        so instead of claiming a verification it did not perform.
        """
        self._require(ledger_id, actor_id=actor_id)
        return self.store.verify_audit(ledger_id)

    # ── Source-document attachments ──────────────────────────────────────────
    def _require_entry_in_ledger(self, ledger_id: str, entry_id: str) -> JournalEntry:
        entry = self.store.require_entry(str(entry_id or ""))
        if entry.ledger_id != ledger_id:
            raise LedgerError(
                ENTRY_NOT_FOUND,
                f"entry {entry_id} is not in ledger {ledger_id}",
                {"entryId": entry_id, "ledgerId": ledger_id},
            )
        return entry

    def attach_document(
        self,
        ledger_id: str,
        entry_id: str,
        artifact_id: str,
        *,
        artifact_service: Any = None,
        note: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Attach an artifact (receipt/invoice/statement) to a posted entry.

        Only the artifact id and its content hash are stored; the bytes stay in
        the ONE artifact store. A document without a checksum is refused,
        because an unverifiable reference is not evidence.
        """
        self._require(ledger_id, actor_id=actor_id)
        entry = self._require_entry_in_ledger(ledger_id, entry_id)
        if artifact_service is None:
            raise LedgerError(
                ARTIFACT_STORE_UNAVAILABLE,
                "the artifact store is unavailable; cannot attach a document",
                {"artifactId": artifact_id},
            )
        try:
            artifact = artifact_service.get(str(artifact_id))
        except Exception as exc:  # noqa: BLE001 - a missing artifact is a named refusal
            raise LedgerError(
                ARTIFACT_NOT_FOUND,
                f"artifact {artifact_id} was not found",
                {"artifactId": artifact_id},
            ) from exc
        checksum = getattr(artifact, "checksum", None)
        if not checksum:
            raise LedgerError(
                ARTIFACT_UNHASHED,
                "the artifact has no content hash; it cannot be attached as evidence",
                {"artifactId": artifact_id},
            )
        attachment = self.store.insert_attachment(
            f"latt_{uuid.uuid4().hex[:20]}",
            ledger_id,
            entry.id,
            str(getattr(artifact, "id", artifact_id)),
            checksum=str(checksum),
            artifact_name=getattr(artifact, "name", None),
            mime_type=getattr(artifact, "mime_type", None),
            size_bytes=getattr(artifact, "size_bytes", None),
            note=str(note or "").strip()[:500] or None,
            attached_by=actor_id,
        )
        self.store.audit(
            ledger_id, "DOCUMENT_ATTACHED", subject_id=entry.id, actor=actor_id,
            detail={"attachmentId": attachment["id"],
                    "artifactId": attachment["artifactId"],
                    "checksum": attachment["checksum"]},
        )
        return attachment

    def list_attachments(
        self,
        ledger_id: str,
        *,
        entry_id: Optional[str] = None,
        limit: int = 200,
        actor_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        if entry_id:
            self._require_entry_in_ledger(ledger_id, entry_id)
        return self.store.list_attachments(ledger_id, entry_id=entry_id, limit=limit)

    def detach_document(
        self,
        ledger_id: str,
        attachment_id: str,
        *,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        attachment = self.store.require_attachment(ledger_id, attachment_id)
        removed = self.store.delete_attachment(ledger_id, attachment_id)
        self.store.audit(
            ledger_id, "DOCUMENT_DETACHED", subject_id=attachment["entryId"], actor=actor_id,
            detail={"attachmentId": attachment["id"],
                    "artifactId": attachment["artifactId"],
                    "checksum": attachment["checksum"]},
        )
        return {"removed": removed, "attachment": attachment}

    def verify_attachment(
        self,
        ledger_id: str,
        attachment_id: str,
        *,
        artifact_service: Any = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Compare the frozen checksum with the artifact's current content hash."""
        self._require(ledger_id, actor_id=actor_id)
        attachment = self.store.require_attachment(ledger_id, attachment_id)
        if artifact_service is None:
            raise LedgerError(
                ARTIFACT_STORE_UNAVAILABLE,
                "the artifact store is unavailable; cannot verify the document",
                {"attachmentId": attachment_id},
            )
        try:
            artifact = artifact_service.get(attachment["artifactId"])
        except Exception:  # noqa: BLE001 - a deleted artifact is evidence of loss
            return {
                "status": "MISSING", "ok": False,
                "attachmentId": attachment["id"], "entryId": attachment["entryId"],
                "artifactId": attachment["artifactId"],
                "storedChecksum": attachment["checksum"],
                "currentChecksum": None,
                "reason": "ARTIFACT_MISSING",
            }
        current = getattr(artifact, "checksum", None)
        matched = bool(current) and str(current) == attachment["checksum"]
        return {
            "status": "MATCHED" if matched else "CHANGED",
            "ok": matched,
            "attachmentId": attachment["id"],
            "entryId": attachment["entryId"],
            "artifactId": attachment["artifactId"],
            "storedChecksum": attachment["checksum"],
            "currentChecksum": current,
            "reason": None if matched else "CHECKSUM_MISMATCH",
        }

    # ── Categorisation rules ─────────────────────────────────────────────────
    def create_rule(
        self,
        ledger_id: str,
        *,
        name: str,
        target_account: str,
        priority: int = 100,
        payee_contains: Sequence[str] = (),
        payee_regex: str = "",
        amount_min: object = None,
        amount_max: object = None,
        direction: str = "ANY",
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .rules import build_rule  # noqa: PLC0415 - keeps import cost off boot

        self._require(ledger_id, actor_id=actor_id)
        if self.store.get_account(ledger_id, str(target_account).strip()) is None:
            raise LedgerError(
                ACCOUNT_NOT_FOUND,
                f"target account {target_account!r} does not exist in this ledger",
                {"accountCode": str(target_account)},
            )
        version = self.store.latest_rule_version(ledger_id, str(name).strip()) + 1
        rule = build_rule(
            ledger_id,
            rule_id=f"rule_{uuid.uuid4().hex[:20]}",
            name=name,
            target_account=target_account,
            priority=priority,
            payee_contains=payee_contains,
            payee_regex=payee_regex,
            amount_min=amount_min,
            amount_max=amount_max,
            direction=direction,
            version=version,
        )
        rule.created_by = actor_id
        # A named rule is a live policy: the previous version stops applying when
        # the next one lands, so evaluation is never ambiguous.
        self.store.deactivate_rule_name(ledger_id, rule.name)
        self.store.insert_rule(rule)
        self.store.audit(
            ledger_id, "RULE_CREATED", subject_id=rule.id, actor=actor_id,
            detail={"name": rule.name, "version": rule.version,
                    "targetAccount": rule.target_account},
        )
        return rule.to_dict()

    def list_rules(self, ledger_id: str, *, active_only: bool = True,
                   actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return [rule.to_dict() for rule in
                self.store.list_rules(ledger_id, active_only=active_only)]

    def deactivate_rule(self, ledger_id: str, rule_id: str, *,
                        actor_id: Optional[str] = None) -> dict[str, Any]:
        self._require(ledger_id, actor_id=actor_id)
        if not self.store.deactivate_rule(ledger_id, str(rule_id)):
            raise LedgerError(RULE_NOT_FOUND, f"rule {rule_id} not found",
                              {"ruleId": str(rule_id)})
        self.store.audit(ledger_id, "RULE_DEACTIVATED", subject_id=str(rule_id),
                         actor=actor_id, detail={})
        return {"id": rule_id, "active": False}

    # ── Statement import ─────────────────────────────────────────────────────
    def preview_import(
        self,
        ledger_id: str,
        *,
        account_code: str,
        content: str,
        statement_format: str = "csv",
        mapping: Any = None,
        fx_rate: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .imports import preview  # noqa: PLC0415

        return preview(
            self, ledger_id, account_code=account_code, content=content,
            statement_format=statement_format, mapping=_as_mapping(mapping),
            fx_rate=fx_rate, actor_id=actor_id,
        )

    def commit_import(
        self,
        ledger_id: str,
        *,
        account_code: str,
        content: str,
        statement_format: str = "csv",
        mapping: Any = None,
        fx_rate: object = None,
        batch_ref: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .imports import commit  # noqa: PLC0415

        return commit(
            self, ledger_id, account_code=account_code, content=content,
            statement_format=statement_format, mapping=_as_mapping(mapping),
            fx_rate=fx_rate, batch_ref=batch_ref, actor_id=actor_id,
        )

    def list_import_batches(self, ledger_id: str, *, limit: int = 50,
                            actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .imports import list_batches  # noqa: PLC0415

        return list_batches(self, ledger_id, limit=limit, actor_id=actor_id)

    # ── Reconciliation ──────────────────────────────────────────────────────
    def reconcile_statement(
        self,
        ledger_id: str,
        *,
        account_code: str,
        content: str,
        statement_format: str = "csv",
        mapping: Any = None,
        start: object = None,
        end: object = None,
        tolerance: object = None,
        window_days: int = 5,
        statement_balance: object = None,
        fx_rate: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .reconcile import run as _run  # noqa: PLC0415

        return _run(
            self, ledger_id, account_code=account_code, content=content,
            statement_format=statement_format, mapping=_as_mapping(mapping),
            start=start, end=end,
            tolerance=tolerance if tolerance not in (None, "") else "0.01",
            window_days=window_days, statement_balance=statement_balance,
            fx_rate=fx_rate, actor_id=actor_id,
        )

    def list_reconciliations(self, ledger_id: str, *, limit: int = 50,
                             actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .reconcile import list_runs  # noqa: PLC0415

        return list_runs(self, ledger_id, limit=limit, actor_id=actor_id)

    def get_reconciliation(self, ledger_id: str, run_id: str, *,
                           actor_id: Optional[str] = None) -> dict[str, Any]:
        from .reconcile import get_run  # noqa: PLC0415

        return get_run(self, ledger_id, run_id, actor_id=actor_id)

    # ── Schedules, accruals and revaluation (Phase 3) ────────────────────────
    def create_schedule(
        self,
        ledger_id: str,
        *,
        name: str,
        kind: str,
        cost: object,
        periods: int,
        expense_account: str,
        contra_account: str,
        start_date: object,
        salvage: object = None,
        method: str = "STRAIGHT_LINE",
        factor: object = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .schedules import build_schedule  # noqa: PLC0415

        ledger = self._require(ledger_id, actor_id=actor_id)
        for code in (expense_account, contra_account):
            account = self.store.get_account(ledger_id, str(code).strip())
            if account is None:
                raise LedgerError(
                    ACCOUNT_NOT_FOUND,
                    f"account {code!r} does not exist in this ledger",
                    {"accountCode": str(code)},
                )
            if not account.active:
                raise LedgerError(
                    "ACCOUNT_INACTIVE", f"account {code!r} is inactive",
                    {"accountCode": str(code)},
                )
        schedule = build_schedule(
            ledger_id,
            name=name,
            kind=kind,
            cost=cost,
            periods=int(periods),
            expense_account=str(expense_account).strip(),
            contra_account=str(contra_account).strip(),
            start_date=start_date,
            salvage=salvage if salvage is not None else "0",
            method=method,
            factor=factor if factor is not None else "2",
            currency=ledger.base_currency,
        )
        schedule.created_by = actor_id
        self.store.insert_schedule(schedule)
        self.store.audit(
            ledger_id, "SCHEDULE_CREATED", subject_id=schedule.id, actor=actor_id,
            detail={"kind": schedule.kind, "name": schedule.name,
                    "periods": schedule.periods, "cost": f"{schedule.cost:f}"},
        )
        return schedule.to_dict()

    def list_schedules(self, ledger_id: str, *, active_only: bool = True,
                       actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._require(ledger_id, actor_id=actor_id)
        return [schedule.to_dict()
                for schedule in self.store.list_schedules(ledger_id, active_only=active_only)]

    def post_schedule(self, ledger_id: str, schedule_id: str, *,
                      through: object = None,
                      actor_id: Optional[str] = None) -> dict[str, Any]:
        from .schedules import due_entries, schedule_lines  # noqa: PLC0415

        self._require(ledger_id, actor_id=actor_id)
        schedule = self.store.get_schedule(str(schedule_id))
        if schedule is None or schedule.ledger_id != ledger_id:
            raise LedgerError(SCHEDULE_NOT_FOUND, f"schedule {schedule_id} not found",
                              {"scheduleId": str(schedule_id)})
        through_iso = iso(parse_date(through)) if through else iso(today())
        due = due_entries(schedule, through_iso)
        posted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        processed = schedule.posted_through
        for entry in due:
            processed = entry.sequence
            if entry.amount <= ZERO:
                continue
            try:
                record = self.post(
                    ledger_id,
                    entry_date=entry.date,
                    lines=schedule_lines(schedule, entry),
                    memo=f"{schedule.name} — period {entry.sequence}/{schedule.periods}",
                    source="schedule",
                    external_id=f"sched:{schedule.id}:{entry.sequence}",
                    actor_id=actor_id,
                )
            except LedgerError as exc:
                errors.append({"sequence": entry.sequence, "date": entry.date,
                               "code": exc.code, "message": exc.message})
                processed = entry.sequence - 1
                break
            posted.append({"sequence": entry.sequence, "date": entry.date,
                           "amount": f"{entry.amount:f}", "entryId": record["id"],
                           "entryNo": record["entryNo"],
                           "duplicate": bool(record.get("duplicate"))})
        status = "COMPLETE" if processed >= schedule.periods else schedule.status
        if processed != schedule.posted_through or status != schedule.status:
            self.store.set_schedule_progress(schedule.id, processed, status)
        self.store.audit(
            ledger_id, "SCHEDULE_POSTED", subject_id=schedule.id, actor=actor_id,
            detail={"posted": len(posted), "errors": len(errors), "through": through_iso},
        )
        return {
            "scheduleId": schedule.id,
            "through": through_iso,
            "postedCount": len([item for item in posted if not item["duplicate"]]),
            "duplicateCount": len([item for item in posted if item["duplicate"]]),
            "errorCount": len(errors),
            "posted": posted,
            "errors": errors,
            "postedThrough": processed,
            "status": status,
        }

    def post_accrual(
        self,
        ledger_id: str,
        *,
        entry_date: object,
        amount: object,
        debit_account: str,
        credit_account: str,
        memo: str = "",
        reversal_date: object = None,
        reference: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .schedules import accrual_entries  # noqa: PLC0415

        self._require(ledger_id, actor_id=actor_id)
        entries = accrual_entries(
            entry_date=iso(parse_date(entry_date)),
            amount=amount,
            debit_account=debit_account,
            credit_account=credit_account,
            memo=memo,
            reversal_date=iso(parse_date(reversal_date)) if reversal_date else None,
            reference=reference,
        )
        posted = []
        for item in entries:
            record = self.post(
                ledger_id,
                entry_date=item["entry_date"],
                lines=item["lines"],
                memo=item["memo"],
                source=item["source"],
                external_id=item["external_id"],
                actor_id=actor_id,
            )
            posted.append({"entryId": record["id"], "entryNo": record["entryNo"],
                           "date": record["entryDate"],
                           "duplicate": bool(record.get("duplicate"))})
        return {
            "postedCount": len([item for item in posted if not item["duplicate"]]),
            "duplicateCount": len([item for item in posted if item["duplicate"]]),
            "entries": posted,
        }

    def preview_revaluation(
        self,
        ledger_id: str,
        *,
        as_of: object,
        rates: dict[str, Any],
        gain_account: str,
        loss_account: str,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .revaluation import preview  # noqa: PLC0415

        return preview(self, ledger_id, as_of=as_of, rates=rates,
                       gain_account=gain_account, loss_account=loss_account,
                       actor_id=actor_id)

    def post_revaluation(
        self,
        ledger_id: str,
        *,
        as_of: object,
        rates: dict[str, Any],
        gain_account: str,
        loss_account: str,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .revaluation import post as _post  # noqa: PLC0415

        return _post(self, ledger_id, as_of=as_of, rates=rates,
                     gain_account=gain_account, loss_account=loss_account,
                     actor_id=actor_id)

    # ── Close ───────────────────────────────────────────────────────────────
    def close_report(
        self,
        ledger_id: str,
        *,
        period_id: Optional[str] = None,
        fiscal_year: Optional[int] = None,
        period_no: Optional[int] = None,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .close import evaluate, persist  # noqa: PLC0415

        ledger = self._require(ledger_id, actor_id=actor_id)
        period = self._locate_period(ledger, period_id, fiscal_year, period_no)
        report = evaluate(self, ledger_id, start=period.start_date,
                          end=period.end_date, actor_id=actor_id)
        record = persist(self, report, period_id=period.id, actor_id=actor_id)
        return {**report, "runId": record["id"], "periodId": period.id}

    def list_close_runs(self, ledger_id: str, *, limit: int = 50,
                        actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .close import list_runs  # noqa: PLC0415

        return list_runs(self, ledger_id, limit=limit, actor_id=actor_id)

    def get_close_run(self, ledger_id: str, run_id: str, *,
                      actor_id: Optional[str] = None) -> dict[str, Any]:
        from .close import get_run  # noqa: PLC0415

        return get_run(self, ledger_id, run_id, actor_id=actor_id)

    # ── AR/AP subledger (Phase 4) ────────────────────────────────────────────
    def create_party(self, ledger_id: str, *, name: str, kind: str,
                     **kwargs: Any) -> dict[str, Any]:
        from .documents import create_party  # noqa: PLC0415

        return create_party(self, ledger_id, name=name, kind=kind, **kwargs)

    def list_parties(self, ledger_id: str, *, kind: Optional[str] = None,
                     active_only: bool = True,
                     actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .documents import list_parties  # noqa: PLC0415

        return list_parties(self, ledger_id, kind=kind, active_only=active_only,
                            actor_id=actor_id)

    def create_tax_code(self, ledger_id: str, *, code: str, name: str = "",
                        jurisdiction: str, rate: object, account_code: str,
                        kind: str = "VAT", price_includes_tax: bool = False,
                        actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import create_tax_code  # noqa: PLC0415

        return create_tax_code(
            self, ledger_id, code=code, name=name, jurisdiction=jurisdiction,
            rate=rate, account_code=account_code, kind=kind,
            price_includes_tax=price_includes_tax, actor_id=actor_id,
        )

    def list_tax_codes(self, ledger_id: str, *, active_only: bool = True,
                       actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .documents import list_tax_codes  # noqa: PLC0415

        return list_tax_codes(self, ledger_id, active_only=active_only,
                              actor_id=actor_id)

    def create_document(self, ledger_id: str, *, kind: str, party_id: str,
                        issue_date: object, lines: Sequence[dict[str, Any]],
                        due_date: object = None, number: Optional[str] = None,
                        memo: str = "", reference: Optional[str] = None,
                        receivable_account: Optional[str] = None,
                        payable_account: Optional[str] = None,
                        actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import create_document  # noqa: PLC0415

        return create_document(
            self, ledger_id, kind=kind, party_id=party_id, issue_date=issue_date,
            lines=lines, due_date=due_date, number=number, memo=memo,
            reference=reference, receivable_account=receivable_account,
            payable_account=payable_account, actor_id=actor_id,
        )

    def get_document(self, ledger_id: str, document_id: str, *,
                     actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import get_document  # noqa: PLC0415

        return get_document(self, ledger_id, document_id, actor_id=actor_id)

    def list_documents(self, ledger_id: str, *, kind: Optional[str] = None,
                       party_id: Optional[str] = None, status: Optional[str] = None,
                       limit: int = 100,
                       actor_id: Optional[str] = None) -> list[dict[str, Any]]:
        from .documents import list_documents  # noqa: PLC0415

        return list_documents(self, ledger_id, kind=kind, party_id=party_id,
                              status=status, limit=limit, actor_id=actor_id)

    def void_document(self, ledger_id: str, document_id: str, *, reason: str = "",
                      actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import void_document  # noqa: PLC0415

        return void_document(self, ledger_id, document_id, reason=reason,
                             actor_id=actor_id)

    def pay_document(self, ledger_id: str, document_id: str, *, date_paid: object,
                     amount: object, bank_account: str,
                     reference: Optional[str] = None,
                     actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import pay_document  # noqa: PLC0415

        return pay_document(self, ledger_id, document_id, date_paid=date_paid,
                            amount=amount, bank_account=bank_account,
                            reference=reference, actor_id=actor_id)

    def aging(self, ledger_id: str, *, kind: str = "AR", as_of: object = None,
              actor_id: Optional[str] = None) -> dict[str, Any]:
        from .documents import aging  # noqa: PLC0415

        return aging(self, ledger_id, kind=kind, as_of=as_of, actor_id=actor_id)

    # ── Internals ────────────────────────────────────────────────────────────
    def _require(self, ledger_id: str, *, actor_id: Optional[str] = None) -> Ledger:
        ledger = self.store.get_ledger(ledger_id)
        if ledger is None:
            raise LedgerError(LEDGER_NOT_FOUND, f"ledger {ledger_id} not found",
                              {"ledgerId": ledger_id})
        if actor_id and ledger.owner_id and actor_id != ledger.owner_id:
            raise LedgerError(LEDGER_FORBIDDEN, "this ledger belongs to another owner",
                              {"ledgerId": ledger_id})
        return ledger

    def _resolve_open_period(self, ledger: Ledger, when: date) -> Period:
        fiscal_year, period_no = fiscal_position(ledger.fiscal_year_start_month, when)
        period = self.store.get_period(ledger.id, fiscal_year, period_no)
        if period is None:
            period = build_period(ledger.id, ledger.fiscal_year_start_month,
                                  fiscal_year, period_no)
            self.store.upsert_period(period)
        return require_open(period, when=when)

    def _current_period_view(self, ledger: Ledger) -> Optional[dict[str, Any]]:
        """The current fiscal period, WITHOUT creating a row on a read."""
        fiscal_year, period_no = fiscal_position(ledger.fiscal_year_start_month, today())
        period = self.store.get_period(ledger.id, fiscal_year, period_no)
        if period is not None:
            return period.to_dict()
        start, end = period_bounds(ledger.fiscal_year_start_month, fiscal_year, period_no)
        return {
            "id": make_period_id(ledger.id, fiscal_year, period_no),
            "ledgerId": ledger.id,
            "fiscalYear": fiscal_year,
            "periodNo": period_no,
            "startDate": iso(start),
            "endDate": iso(end),
            "status": "OPEN",
            "materialized": False,
        }

    def _locate_period(self, ledger: Ledger, period_id: Optional[str],
                       fiscal_year: Optional[int], period_no: Optional[int]) -> Period:
        if period_id:
            period = self.store.get_period_by_id(period_id)
            if period is None or period.ledger_id != ledger.id:
                raise LedgerError(PERIOD_NOT_FOUND, f"period {period_id} not found",
                                  {"periodId": period_id})
            return period
        if fiscal_year is None or period_no is None:
            raise LedgerError(
                PERIOD_NOT_FOUND,
                "supply `period_id`, or both `fiscal_year` and `period_no`",
                {},
            )
        period = self.store.get_period(ledger.id, int(fiscal_year), int(period_no))
        if period is None:
            raise LedgerError(
                PERIOD_NOT_FOUND,
                f"period {fiscal_year}-{period_no:02d} does not exist in this ledger",
                {"fiscalYear": int(fiscal_year), "periodNo": int(period_no)},
            )
        return period

    def _batches(self, ledger_id: str, start_iso: Optional[str], end_iso: str):
        opening = self.store.postings(ledger_id, end=_day_before(start_iso)) if start_iso else []
        movement = self.store.postings(ledger_id, start=start_iso, end=end_iso)
        return opening, movement

    def _default_range(self, ledger_id: str, start: object, end: object) -> tuple[str, str]:
        if end:
            end_iso = iso(parse_date(end))
        else:
            end_iso = iso(today())
        if start:
            start_iso = iso(parse_date(start))
        else:
            ledger = self.store.get_ledger(ledger_id)
            assert ledger is not None
            _, _, first, _ = period_bounds_for(
                ledger.fiscal_year_start_month, parse_date(end_iso)
            )
            start_iso = iso(first)
        if start_iso > end_iso:
            raise LedgerError(UNSUPPORTED_DATE_RANGE, "`start` is after `end`",
                              {"start": start_iso, "end": end_iso})
        return start_iso, end_iso


def _day_before(iso_date: str) -> str:
    from datetime import timedelta

    return (date.fromisoformat(iso_date) - timedelta(days=1)).isoformat()


def _as_mapping(value: Any) -> Any:
    """Accept a mapping object or the plain dict an agent/HTTP body carries."""
    from .importers import CsvMapping  # noqa: PLC0415

    if value is None or isinstance(value, CsvMapping):
        return value
    if isinstance(value, dict):
        return CsvMapping.from_dict(value)
    raise LedgerError(INVALID_ENTRY, "`mapping` must be an object", {})


__all__ = ["LedgerService"]
