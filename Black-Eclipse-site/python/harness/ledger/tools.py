"""Agent-facing general-ledger tools.

Flat `ledger_*` names (the ONE registry rejects dotted names). The conceptual
names map one-to-one:

    ledger.list              → ledger_list
    ledger.create            → ledger_create
    ledger.accounts          → ledger_list_accounts
    ledger.post              → ledger_post
    ledger.reverse           → ledger_reverse
    ledger.trial_balance     → ledger_trial_balance
    ledger.balance_sheet     → ledger_balance_sheet
    ledger.income_statement  → ledger_income_statement
    ledger.cash_flow         → ledger_cash_flow
    ledger.close_period      → ledger_close_period

Safety classification:
  * read         — list/get/statements. No effect, free to run.
  * mutating     — create, create_account, set_account_active, post, reverse,
                   close/reopen/lock period. effect `ledger.write`, admitted only
                   by a single-use, digest-bound operator approval.

A posting is a WRITE to the book of record. It is not a payment: nothing here
moves money. That is why it needs exactly one approval and no money guardrail.
"""

from __future__ import annotations

from typing import Any

from .errors import LedgerError
from .money import D, ZERO

READ = "read"
WRITE = "write"

WRITE_EFFECT = "ledger.write"


def _service(ctx: Any) -> Any:
    service = getattr(ctx, "ledger_service", None)
    if service is None:
        raise LedgerError(
            "LEDGER_UNAVAILABLE",
            "the general ledger is not available in this run",
        )
    return service


def _actor(ctx: Any) -> Any:
    return getattr(ctx, "user_id", None)


def _fail(error: Any) -> tuple[str, Any]:
    payload = error.to_dict() if isinstance(error, LedgerError) else {
        "code": "LEDGER_ERROR", "message": str(error), "details": {}, "retryable": False,
    }
    return f"Ledger error ({payload['code']}): {payload['message']}", {
        "status": "FAILED",
        "error": payload,
    }


def _ledger_id(args: dict[str, Any]) -> str:
    ledger_id = str(args.get("ledger_id") or args.get("ledgerId") or "").strip()
    if not ledger_id:
        raise LedgerError("INVALID_ENTRY", "`ledger_id` is required", {})
    return ledger_id


def _checks_line(payload: dict[str, Any]) -> str:
    checks = payload.get("checks") or []
    if not checks:
        return ""
    failed = [c for c in checks if not c.get("ok")]
    if not failed:
        return " All checks passed."
    return " FAILED CHECKS: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failed) + "."


# ── Read tools ────────────────────────────────────────────────────────────────


async def ledger_list(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_ledgers(args.get("owner_id") or getattr(ctx, "user_id", None))
    if not rows:
        return ("No ledgers yet. Create one with `ledger_create` (a chart of accounts "
                "is installed automatically)."), {"ledgers": []}
    lines = [f"{len(rows)} ledger(s):"]
    for row in rows:
        lines.append(
            f"  - {row['id']}  {row['name']}  ({row['baseCurrency']}, {row['chartTemplate']}, "
            f"fiscal year starts month {row['fiscalYearStartMonth']})"
        )
    return "\n".join(lines), {"ledgers": rows}


async def ledger_get(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = service.get_ledger(_ledger_id(args), actor_id=_actor(ctx))
    summary = payload.get("summary") or {}
    return (
        f"Ledger {payload['name']} ({payload['baseCurrency']}): "
        f"{summary.get('accountCount', 0)} accounts, {summary.get('entryCount', 0)} entries, "
        f"{summary.get('periodCount', 0)} periods."
    ), payload


async def ledger_list_accounts(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_accounts(
        _ledger_id(args),
        include_inactive=bool(args.get("include_inactive", True)),
        actor_id=_actor(ctx),
    )
    lines = [f"{len(rows)} account(s):"]
    for row in rows:
        marker = "" if row["active"] else " [inactive]"
        lines.append(f"  {row['code']}  {row['name']}  ({row['type']}){marker}")
    return "\n".join(lines), {"accounts": rows}


async def ledger_list_entries(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_entries(
        _ledger_id(args),
        start=args.get("start"), end=args.get("end"),
        account_code=args.get("account_code"),
        limit=int(args.get("limit") or 100),
        actor_id=_actor(ctx),
    )
    lines = [f"{len(rows)} journal entr{'y' if len(rows) == 1 else 'ies'}:"]
    for row in rows:
        lines.append(
            f"  #{row['entryNo']}  {row['entryDate']}  {row['memo'] or '(no memo)'}  "
            f"Dr {row['totalDebit']} / Cr {row['totalCredit']}  [{row['status']}]"
        )
    return "\n".join(lines), {"entries": rows}


async def ledger_get_entry(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    entry = service.get_entry(_ledger_id(args), str(args.get("entry_id") or ""),
                              actor_id=_actor(ctx))
    lines = [f"Entry #{entry['entryNo']} on {entry['entryDate']} — {entry['memo'] or '(no memo)'}"]
    for line in entry.get("lines", []):
        debit = D(line["baseDebit"])
        side = "Dr" if debit != ZERO else "Cr"
        amount = line["baseDebit"] if side == "Dr" else line["baseCredit"]
        lines.append(f"  {side} {line['accountCode']}  {amount}")
    return "\n".join(lines), entry


async def ledger_trial_balance(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = service.trial_balance(
        _ledger_id(args), as_of=args.get("as_of"), start=args.get("start"),
        include_zero=bool(args.get("include_zero", False)), actor_id=_actor(ctx),
    )
    totals = payload["totals"]
    text = (
        f"Trial balance as of {payload['endDate']}: closing debits {totals['closingDebit']} "
        f"vs credits {totals['closingCredit']}." + _checks_line(payload)
    )
    return text, payload


async def ledger_balance_sheet(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = service.balance_sheet(_ledger_id(args), as_of=args.get("as_of"),
                                    actor_id=_actor(ctx))
    text = (
        f"Balance sheet as of {payload['asOf']}: assets {payload['assets']['total']}, "
        f"liabilities {payload['liabilities']['total']}, equity {payload['equity']['total']}."
        + _checks_line(payload)
    )
    return text, payload


async def ledger_income_statement(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = service.income_statement(
        _ledger_id(args), start=args.get("start"), end=args.get("end"), actor_id=_actor(ctx)
    )
    text = (
        f"P&L {payload['startDate']}..{payload['endDate']}: revenue "
        f"{payload['revenue']['total']}, expenses {payload['totalExpenses']}, "
        f"net income {payload['netIncome']}." + _checks_line(payload)
    )
    return text, payload


async def ledger_cash_flow(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    method = str(args.get("method") or "direct").strip().lower()
    if method == "indirect":
        payload = service.cash_flow_indirect(
            _ledger_id(args), start=args.get("start"), end=args.get("end"),
            actor_id=_actor(ctx),
        )
        text = (
            f"Indirect cash flow {payload['startDate']}..{payload['endDate']}: net income "
            f"{payload['netIncome']} + depreciation {payload['depreciation']} + working "
            f"capital {payload['workingCapital']['total']} = operating "
            f"{payload['operatingCashFlow']} (direct says {payload['directOperatingCashFlow']}, "
            f"difference {payload['difference']})." + _checks_line(payload)
        )
        if not payload.get("reconciled"):
            text += (" The indirect operating figure does NOT reconcile to the direct "
                     "statement; the difference is reported, not plugged.")
        return text, payload
    if method != "direct":
        return _fail(LedgerError(
            "INVALID_STATEMENT_METHOD",
            "`method` must be `direct` or `indirect`",
            {"method": method},
        ))
    payload = service.cash_flow(
        _ledger_id(args), start=args.get("start"), end=args.get("end"), actor_id=_actor(ctx)
    )
    text = (
        f"Cash flow {payload['startDate']}..{payload['endDate']}: operating "
        f"{payload['operating']}, investing {payload['investing']}, financing "
        f"{payload['financing']}, net change {payload['netChange']}, closing cash "
        f"{payload['closingCash']}." + _checks_line(payload)
    )
    if not payload.get("complete"):
        text += " Some cash movement is UNCLASSIFIED — see the unclassified bucket."
    return text, payload


async def ledger_list_periods(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_periods(_ledger_id(args), actor_id=_actor(ctx))
    lines = [f"{len(rows)} period(s):"]
    for row in rows:
        lines.append(f"  {row['startDate']}..{row['endDate']}  {row['status']}")
    return "\n".join(lines), {"periods": rows}


async def ledger_audit_log(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.audit_log(_ledger_id(args), limit=int(args.get("limit") or 100),
                             actor_id=_actor(ctx))
    return f"{len(rows)} audit event(s).", {"events": rows}


async def ledger_audit_verify(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    result = service.verify_audit(_ledger_id(args), actor_id=_actor(ctx))
    if result.get("ok"):
        summary = f"Audit chain verified ({result['checked']} event(s), head {result['head'][:12]}…)."
    elif result.get("status") == "UNSEALED":
        summary = (f"Audit chain has {result.get('unsealed', 0)} legacy unsealed event(s); "
                   "the sealed portion cannot be verified as a whole.")
    else:
        summary = (f"Audit chain BROKEN at {result.get('brokenAt')} "
                   f"({result.get('reason')}).")
    return summary, result


# ── Budgets, dimensions, runway ─────────────────────────────────────────────


async def ledger_budget_create(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.create_budget(
            _ledger_id(args),
            name=str(args.get("name") or ""),
            start=args.get("start"),
            end=args.get("end"),
            lines=args.get("lines") or [],
            note=args.get("note"),
            currency=args.get("currency"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Created budget {payload['name']!r} ({payload['id']}) "
        f"{payload['startDate']}..{payload['endDate']} with {len(payload['lines'])} line(s)."
    ), payload


async def ledger_budget_list(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_budgets(_ledger_id(args), limit=int(args.get("limit") or 100),
                               actor_id=_actor(ctx))
    return f"{len(rows)} budget(s).", {"budgets": rows}


async def ledger_budget_variance(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.budget_vs_actual(
            _ledger_id(args), str(args.get("budget_id") or args.get("budgetId") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Budget {payload['budgetName']!r} {payload['startDate']}..{payload['endDate']}: "
        f"budget {payload['totals']['budget']}, actual {payload['totals']['actual']}, "
        f"variance {payload['totals']['variance']}."
    )
    return text, payload


async def ledger_dimension_summary(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.dimension_summary(
            _ledger_id(args),
            str(args.get("dimension") or ""),
            start=args.get("start"), end=args.get("end"), actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Dimension {payload['dimension']!r}: {len(payload['rows'])} value(s) "
        f"over {payload['startDate']}..{payload['endDate']}."
    ), payload


async def ledger_cash_runway(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.cash_runway(
            _ledger_id(args), as_of=args.get("as_of") or args.get("asOf"),
            history_days=int(args.get("history_days") or 90),
            horizon_days=int(args.get("horizon_days") or 90),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    runway = payload["runwayMonths"]
    text = (
        f"Cash runway as of {payload['asOf']}: opening {payload['openingCash']}, "
        f"monthly burn {payload['monthlyBurn']}, runway "
        f"{runway + ' months' if runway is not None else 'UNKNOWN (no net burn)'}, "
        f"adjusted runway {payload['adjustedRunwayMonths'] or 'UNKNOWN'}."
    )
    return text, payload


# ── Year-end close ──────────────────────────────────────────────────────────


async def ledger_year_end_preview(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.preview_fiscal_year_close(
            _ledger_id(args),
            fiscal_year=args.get("fiscal_year") or args.get("fiscalYear"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"FY{payload['fiscalYear']} close preview ({payload['status']}): net income "
        f"{payload['netIncome']} → retained earnings {payload['retainedAccount']['code']}; "
        f"{len(payload['lines'])} closing line(s), dated {payload['closingDate']}."
    )
    return text, payload


async def ledger_close_fiscal_year(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.close_fiscal_year(
            _ledger_id(args),
            fiscal_year=args.get("fiscal_year") or args.get("fiscalYear"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    if payload["status"] == "ALREADY_CLOSED":
        entry = payload.get("entry") or {}
        return (
            f"FY{payload['fiscalYear']} is already closed by entry {entry.get('id')}; "
            "nothing was posted again."
        ), payload
    entry = payload.get("entry") or {}
    return (
        f"Closed FY{payload['fiscalYear']}: posted entry {entry.get('id')} on "
        f"{payload['closingDate']}, moving net income {payload['netIncome']} to "
        f"retained earnings {payload['retainedAccount']['code']}."
    ), payload


# ── Source-document attachments ─────────────────────────────────────────────


def _artifacts(ctx: Any) -> Any:
    return getattr(ctx, "artifact_service", None)


async def ledger_attach_document(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.attach_document(
            _ledger_id(args),
            str(args.get("entry_id") or args.get("entryId") or ""),
            str(args.get("artifact_id") or args.get("artifactId") or ""),
            artifact_service=_artifacts(ctx),
            note=args.get("note"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    name = payload.get("artifactName") or payload["artifactId"]
    return (
        f"Attached {name!r} to entry {payload['entryId']} "
        f"(sha256 {payload['checksum'][:12]}…)."
    ), payload


async def ledger_list_attachments(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        rows = service.list_attachments(
            _ledger_id(args),
            entry_id=args.get("entry_id") or args.get("entryId"),
            limit=int(args.get("limit") or 200),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return f"{len(rows)} attachment(s).", {"attachments": rows}


async def ledger_detach_document(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.detach_document(
            _ledger_id(args),
            str(args.get("attachment_id") or args.get("attachmentId") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        "Detached the document from the entry (the artifact itself is unchanged)."
        if payload.get("removed") else "No attachment was removed."
    ), payload


async def ledger_verify_attachment(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.verify_attachment(
            _ledger_id(args),
            str(args.get("attachment_id") or args.get("attachmentId") or ""),
            artifact_service=_artifacts(ctx),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    if payload["status"] == "MATCHED":
        summary = f"Attachment verified: stored sha256 still matches the artifact."
    elif payload["status"] == "MISSING":
        summary = "Attachment BROKEN: the referenced artifact no longer exists."
    else:
        summary = "Attachment CHANGED: the artifact content no longer matches the posted checksum."
    return summary, payload


# ── Import / categorisation ─────────────────────────────────────────────────


async def ledger_import_preview(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.preview_import(
            _ledger_id(args),
            account_code=str(args.get("account_code") or args.get("accountCode") or ""),
            content=str(args.get("content") or ""),
            statement_format=str(args.get("format") or "csv"),
            mapping=args.get("mapping"),
            fx_rate=args.get("fx_rate") or args.get("fxRate"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    lines = [
        f"{payload['rowCount']} statement row(s): {payload['newCount']} new, "
        f"{payload['duplicateCount']} already posted, "
        f"{payload['suspenseCount']} uncategorised.",
        f"Net {payload['net']} (in {payload['totalIn']} / out {payload['totalOut']}).",
    ]
    if payload.get("requiresFx"):
        lines.append("Some rows are in another currency: supply fx_rate before committing.")
    return " ".join(lines), payload


async def ledger_import_commit(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.commit_import(
            _ledger_id(args),
            account_code=str(args.get("account_code") or args.get("accountCode") or ""),
            content=str(args.get("content") or ""),
            statement_format=str(args.get("format") or "csv"),
            mapping=args.get("mapping"),
            fx_rate=args.get("fx_rate") or args.get("fxRate"),
            batch_ref=args.get("batch_ref") or args.get("batchRef"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Imported batch {payload['batchId']}: {payload['postedCount']} posted, "
        f"{payload['duplicateCount']} duplicate(s), {payload['errorCount']} error(s) — "
        f"{payload['status']}."
    )
    if payload["suspenseCount"]:
        text += (f" {payload['suspenseCount']} row(s) went to suspense; they block a close "
                 "until categorised.")
    if payload["errorRows"]:
        first = payload["errorRows"][0]
        text += f" First error (row {first['rowNo']}): {first['message']}"
    return text, {"status": payload["status"], **payload}


async def ledger_list_imports(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_import_batches(_ledger_id(args), limit=int(args.get("limit") or 50),
                                       actor_id=_actor(ctx))
    lines = [f"{len(rows)} import batch(es):"]
    for row in rows:
        lines.append(
            f"  {row['id']}  {row['createdAt'][:10]}  {row['accountCode']}  "
            f"{row['postedCount']}/{row['rowCount']} posted  [{row['status']}]"
        )
    return "\n".join(lines), {"batches": rows}


async def ledger_rule_create(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.create_rule(
            _ledger_id(args),
            name=str(args.get("name") or ""),
            target_account=str(args.get("target_account") or args.get("targetAccount") or ""),
            priority=int(args.get("priority") or 100),
            payee_contains=args.get("payee_contains") or args.get("payeeContains") or (),
            payee_regex=str(args.get("payee_regex") or args.get("payeeRegex") or ""),
            amount_min=args.get("amount_min") or args.get("amountMin"),
            amount_max=args.get("amount_max") or args.get("amountMax"),
            direction=str(args.get("direction") or "ANY"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Created rule {payload['name']!r} v{payload['version']} → account "
        f"{payload['targetAccount']}."
    ), payload


async def ledger_rule_list(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_rules(_ledger_id(args),
                              active_only=bool(args.get("active_only", True)),
                              actor_id=_actor(ctx))
    lines = [f"{len(rows)} categorisation rule(s):"]
    for row in rows:
        match = row["match"]
        bits = []
        if match["payeeContains"]:
            bits.append("payee ~ " + ", ".join(match["payeeContains"]))
        if match["payeeRegex"]:
            bits.append(f"payee =~ {match['payeeRegex']}")
        if match["amountMin"] or match["amountMax"]:
            bits.append(f"amount {match['amountMin'] or '0'}..{match['amountMax'] or '∞'}")
        lines.append(
            f"  {row['name']!r} v{row['version']} (prio {row['priority']}) → "
            f"{row['targetAccount']}  [{'; '.join(bits)}]"
        )
    return "\n".join(lines), {"rules": rows}


async def ledger_rule_deactivate(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.deactivate_rule(
            _ledger_id(args), str(args.get("rule_id") or args.get("ruleId") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return f"Rule {payload['id']} is now inactive.", payload


# ── Reconciliation ──────────────────────────────────────────────────────────


async def ledger_reconcile_statement(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.reconcile_statement(
            _ledger_id(args),
            account_code=str(args.get("account_code") or args.get("accountCode") or ""),
            content=str(args.get("content") or ""),
            statement_format=str(args.get("format") or "csv"),
            mapping=args.get("mapping"),
            start=args.get("start"),
            end=args.get("end"),
            tolerance=args.get("tolerance") or "0.01",
            window_days=int(args.get("window_days") or 5),
            statement_balance=args.get("statement_balance") or args.get("statementBalance"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Reconciliation {payload['id']}: {payload['status']} — "
        f"{payload['matchedCount']} matched, {payload['mismatchCount']} mismatched, "
        f"{payload['missingCount']} missing, {payload['unknownCount']} unknown; "
        f"statement {payload['statementBalance'] or 'n/a'} vs ledger "
        f"{payload['closingBalance']} (difference {payload['difference'] or 'n/a'})."
    )
    if payload["status"] == "INCOMPLETE":
        text += " Supply `statement_balance` to prove the balance, not just the rows."
    return text, payload


async def ledger_list_reconciliations(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_reconciliations(_ledger_id(args),
                                        limit=int(args.get("limit") or 50),
                                        actor_id=_actor(ctx))
    lines = [f"{len(rows)} reconciliation(s):"]
    for row in rows:
        lines.append(
            f"  {row['id']}  {row['startDate']}..{row['endDate']}  {row['accountCode']}  "
            f"[{row['status']}]"
        )
    return "\n".join(lines), {"reconciliations": rows}


async def ledger_get_reconciliation(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.get_reconciliation(
            _ledger_id(args), str(args.get("run_id") or args.get("runId") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Reconciliation {payload['id']}: {payload['status']} with "
        f"{len(payload.get('items') or [])} item(s)."
    ), payload


# ── Schedules, accruals, revaluation, close (Phase 3) ───────────────────────


async def ledger_schedule_create(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.create_schedule(
            _ledger_id(args),
            name=str(args.get("name") or ""),
            kind=str(args.get("kind") or "DEPRECIATION"),
            cost=args.get("cost"),
            periods=int(args.get("periods") or 0),
            expense_account=str(args.get("expense_account") or args.get("expenseAccount") or ""),
            contra_account=str(args.get("contra_account") or args.get("contraAccount") or ""),
            start_date=args.get("start_date") or args.get("startDate"),
            salvage=args.get("salvage"),
            method=str(args.get("method") or "STRAIGHT_LINE"),
            factor=args.get("factor"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    total = sum((D(entry["amount"]) for entry in payload["entries"]), ZERO)
    return (
        f"Created {payload['kind'].lower()} schedule {payload['name']!r} "
        f"({payload['periods']} periods, total {total:f}): first entry "
        f"{payload['entries'][0]['date'] if payload['entries'] else 'n/a'}."
    ), payload


async def ledger_schedule_list(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_schedules(_ledger_id(args),
                                  active_only=bool(args.get("active_only", True)),
                                  actor_id=_actor(ctx))
    lines = [f"{len(rows)} schedule(s):"]
    for row in rows:
        lines.append(
            f"  {row['name']!r}  {row['kind']}  {row['postedThrough']}/{row['periods']} "
            f"posted  [{row['status']}]"
        )
    return "\n".join(lines), {"schedules": rows}


async def ledger_schedule_post(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.post_schedule(
            _ledger_id(args), str(args.get("schedule_id") or args.get("scheduleId") or ""),
            through=args.get("through"), actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Posted {payload['postedCount']} schedule period(s) through {payload['through']} "
        f"({payload['postedThrough']} total, {payload['status']})."
    )
    if payload["errors"]:
        first = payload["errors"][0]
        text += f" Stopped at period {first['sequence']}: {first['message']}"
    return text, payload


async def ledger_accrue(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.post_accrual(
            _ledger_id(args),
            entry_date=args.get("entry_date") or args.get("entryDate"),
            amount=args.get("amount"),
            debit_account=str(args.get("debit_account") or args.get("debitAccount") or ""),
            credit_account=str(args.get("credit_account") or args.get("creditAccount") or ""),
            memo=str(args.get("memo") or ""),
            reversal_date=args.get("reversal_date") or args.get("reversalDate"),
            reference=args.get("reference"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Posted {payload['postedCount']} accrual entr"
        f"{'y' if payload['postedCount'] == 1 else 'ies'}."
    ), payload


async def ledger_revalue_preview(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.preview_revaluation(
            _ledger_id(args),
            as_of=args.get("as_of") or args.get("asOf"),
            rates=args.get("rates") or {},
            gain_account=str(args.get("gain_account") or args.get("gainAccount") or ""),
            loss_account=str(args.get("loss_account") or args.get("lossAccount") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = f"FX revaluation at {payload['asOf']}: {payload['revalueCount']} account(s) to adjust."
    if payload["rateRequired"]:
        text += f" No rate for: {', '.join(payload['rateRequired'])}."
    return text, payload


async def ledger_revalue_post(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.post_revaluation(
            _ledger_id(args),
            as_of=args.get("as_of") or args.get("asOf"),
            rates=args.get("rates") or {},
            gain_account=str(args.get("gain_account") or args.get("gainAccount") or ""),
            loss_account=str(args.get("loss_account") or args.get("lossAccount") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Posted {payload['postedCount']} FX revaluation entr"
        f"{'y' if payload['postedCount'] == 1 else 'ies'} at {payload['asOf']}."
    )
    if payload["rateRequired"]:
        text += f" Skipped (no rate): {', '.join(payload['rateRequired'])}."
    if payload["errors"]:
        text += f" {payload['errorCount']} error(s); first: {payload['errors'][0]['message']}"
    return text, payload


async def ledger_close_preview(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.close_report(
            _ledger_id(args),
            period_id=args.get("period_id") or args.get("periodId"),
            fiscal_year=args.get("fiscal_year") or args.get("fiscalYear"),
            period_no=args.get("period_no") or args.get("periodNo"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    text = (
        f"Close check {payload['startDate']}..{payload['endDate']}: {payload['status']} — "
        f"{payload['blockerCount']} blocker(s), {payload['warningCount']} warning(s)."
    )
    for finding in payload["findings"][:5]:
        text += f"\n  [{finding['severity']}] {finding['code']}: {finding['detail']}"
    return text, payload


async def ledger_list_close_runs(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_close_runs(_ledger_id(args), limit=int(args.get("limit") or 50),
                                   actor_id=_actor(ctx))
    lines = [f"{len(rows)} close run(s):"]
    for row in rows:
        lines.append(
            f"  {row['id']}  {row['startDate']}..{row['endDate']}  [{row['status']}]  "
            f"{row['blockerCount']} blocker(s), {row['warningCount']} warning(s)"
        )
    return "\n".join(lines), {"runs": rows}


async def ledger_get_close_run(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.get_close_run(
            _ledger_id(args), str(args.get("run_id") or args.get("runId") or ""),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Close run {payload['id']}: {payload['status']} "
        f"({payload['blockerCount']} blocker(s), {payload['warningCount']} warning(s))."
    ), payload


# ── Write tools ───────────────────────────────────────────────────────────────


async def ledger_create(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.create_ledger(
            args.get("name") or "Books",
            base_currency=args.get("base_currency") or "USD",
            fiscal_year_start_month=int(args.get("fiscal_year_start_month") or 1),
            chart_template=args.get("chart_template") or "standard",
            owner_id=getattr(ctx, "user_id", None),
            metadata=args.get("metadata"),
            actor=getattr(ctx, "user_id", None),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Created ledger {payload['id']} ({payload['name']}, {payload['baseCurrency']}) "
        f"with {payload['accounts']} accounts."
    ), {"status": "SUCCESS", "ledger": payload}


async def ledger_create_account(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.create_account(
            _ledger_id(args),
            code=str(args.get("code") or ""),
            name=str(args.get("name") or ""),
            account_type=str(args.get("type") or args.get("account_type") or ""),
            subtype=args.get("subtype") or "",
            parent_code=args.get("parent_code"),
            currency=args.get("currency") or "",
            cash_flow_category=args.get("cash_flow_category") or "",
            is_cash=bool(args.get("is_cash", False)),
            is_contra=bool(args.get("is_contra", False)),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return f"Created account {payload['code']} — {payload['name']} ({payload['type']}).", payload


async def ledger_set_account_active(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.set_account_active(
            _ledger_id(args), str(args.get("code") or ""), bool(args.get("active", True)),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    state = "active" if payload["active"] else "inactive"
    return f"Account {payload['code']} is now {state}.", payload


async def ledger_post(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.post(
            _ledger_id(args),
            entry_date=args.get("entry_date"),
            lines=args.get("lines") or [],
            memo=args.get("memo") or "",
            source=args.get("source") or "manual",
            external_id=args.get("external_id"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    verb = "already posted as" if payload.get("duplicate") else "Posted"
    return (
        f"{verb} entry #{payload['entryNo']} on {payload['entryDate']}: "
        f"Dr {payload['totalDebit']} / Cr {payload['totalCredit']} "
        f"across {payload['lineCount']} lines."
    ), {"status": "SUCCESS", "entry": payload}


async def ledger_reverse(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.reverse(
            _ledger_id(args),
            str(args.get("entry_id") or ""),
            entry_date=args.get("entry_date"),
            memo=args.get("memo") or "",
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Reversed entry {payload['reversedEntryId']} with reversing entry "
        f"#{payload['entryNo']} on {payload['entryDate']}."
    ), {"status": "SUCCESS", "entry": payload}


async def ledger_close_period(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.close_period(
            _ledger_id(args),
            period_id=args.get("period_id"),
            fiscal_year=args.get("fiscal_year"),
            period_no=args.get("period_no"),
            actor_id=_actor(ctx),
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Closed period {payload['startDate']}..{payload['endDate']}."
    ), {"status": "SUCCESS", "period": payload}


async def ledger_reopen_period(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.reopen_period(
            _ledger_id(args), str(args.get("period_id") or ""), actor_id=_actor(ctx)
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Reopened period {payload['startDate']}..{payload['endDate']}."
    ), {"status": "SUCCESS", "period": payload}


async def ledger_lock_period(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        payload = service.lock_period(
            _ledger_id(args), str(args.get("period_id") or ""), actor_id=_actor(ctx)
        )
    except LedgerError as exc:
        return _fail(exc)
    return (
        f"Locked period {payload['startDate']}..{payload['endDate']} permanently."
    ), {"status": "SUCCESS", "period": payload}


# ── Registration ──────────────────────────────────────────────────────────────

LEDGER_READ_TOOLS = [
    "ledger_list", "ledger_get", "ledger_list_accounts", "ledger_list_entries",
    "ledger_get_entry", "ledger_trial_balance", "ledger_balance_sheet",
    "ledger_income_statement", "ledger_cash_flow", "ledger_list_periods",
    "ledger_audit_log", "ledger_audit_verify", "ledger_year_end_preview",
    "ledger_budget_list", "ledger_budget_variance", "ledger_dimension_summary",
    "ledger_cash_runway",
    "ledger_list_attachments", "ledger_verify_attachment",
    # Phase 2: import preview / rules / reconciliation are read-class — none of
    # them can change the journal.
    "ledger_import_preview", "ledger_list_imports", "ledger_rule_list",
    "ledger_reconcile_statement", "ledger_list_reconciliations",
    "ledger_get_reconciliation",
    # Phase 3: a close check, a schedule list, an FX preview and the close log are
    # all read-class; none of them can change the journal.
    "ledger_schedule_list", "ledger_revalue_preview", "ledger_close_preview",
    "ledger_list_close_runs", "ledger_get_close_run",
]

LEDGER_WRITE_TOOLS = [
    "ledger_create", "ledger_create_account", "ledger_set_account_active",
    "ledger_post", "ledger_reverse", "ledger_close_period", "ledger_reopen_period",
    "ledger_lock_period", "ledger_attach_document", "ledger_detach_document",
    "ledger_close_fiscal_year", "ledger_budget_create",
    # Phase 2: an import posts entries; a rule changes how future imports post.
    "ledger_import_commit", "ledger_rule_create", "ledger_rule_deactivate",
    # Phase 3: a schedule, an accrual and a revaluation all post entries.
    "ledger_schedule_create", "ledger_schedule_post", "ledger_accrue",
    "ledger_revalue_post",
]

LEDGER_TOOLS = LEDGER_READ_TOOLS + LEDGER_WRITE_TOOLS


def _guarded(handler: Any) -> Any:
    """Wrap a handler so no ledger failure can escape as an exception.

    The tool loop must receive a structured refusal (`status: FAILED` + a named
    code), never a raised exception it would render as an opaque run error.
    """

    async def run(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
        try:
            return await handler(args, ctx)
        except LedgerError as exc:
            return _fail(exc)
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the loop
            return _fail(exc)

    run.__name__ = getattr(handler, "__name__", "ledger_tool")
    return run


def register_ledger_tools(registry: Any) -> None:
    from ..tools import ToolDefinition

    def _def(name: str, description: str, handler: Any, schema: dict[str, Any],
             permission: str, effect: Any = None, timeout: int = 20_000) -> None:
        registry.register(ToolDefinition(
            name=name, description=description, input_schema=schema,
            handler=_guarded(handler),
            permission=permission, risk="low" if permission == READ else "medium",
            timeout_ms=timeout, effect=effect,
        ))

    lid = {"ledger_id": {"type": "string", "description": "Ledger id."}}

    _def("ledger_list", "List accounting ledgers (books) for the current user.",
         ledger_list, {"type": "object", "properties": {"owner_id": {"type": "string"}}}, READ)
    _def("ledger_create",
         "Create a double-entry ledger (a company's book of record) and install a chart "
         "of accounts. `chart_template` is `standard`, `sole_trader` or `minimal`. "
         "`fiscal_year_start_month` is 1-12.",
         ledger_create,
         {"type": "object", "properties": {
             "name": {"type": "string"},
             "base_currency": {"type": "string", "description": "3-letter ISO code."},
             "fiscal_year_start_month": {"type": "integer", "minimum": 1, "maximum": 12},
             "chart_template": {"type": "string",
                                "enum": ["standard", "sole_trader", "minimal"]},
             "metadata": {"type": "object"}},
          "required": ["name"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_get",
         "Read a ledger: its chart of accounts, periods and summary.",
         ledger_get, {"type": "object", "properties": {**lid}, "required": ["ledger_id"]},
         READ)
    _def("ledger_list_accounts", "List the chart of accounts for a ledger.",
         ledger_list_accounts,
         {"type": "object", "properties": {
             **lid, "include_inactive": {"type": "boolean"}}, "required": ["ledger_id"]},
         READ)
    _def("ledger_create_account",
         "Add an account to the chart. `type` is ASSET, LIABILITY, EQUITY, REVENUE or "
         "EXPENSE. Set `is_cash` for an account the cash-flow statement treats as cash.",
         ledger_create_account,
         {"type": "object", "properties": {
             **lid, "code": {"type": "string"}, "name": {"type": "string"},
             "type": {"type": "string",
                      "enum": ["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]},
             "subtype": {"type": "string"}, "parent_code": {"type": "string"},
             "currency": {"type": "string"},
             "cash_flow_category": {"type": "string",
                                    "enum": ["OPERATING", "INVESTING", "FINANCING",
                                             "UNCLASSIFIED"]},
             "is_cash": {"type": "boolean"}, "is_contra": {"type": "boolean"}},
          "required": ["ledger_id", "code", "name", "type"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_set_account_active",
         "Activate or deactivate an account. Deactivated accounts cannot receive postings; "
         "history is untouched.",
         ledger_set_account_active,
         {"type": "object", "properties": {
             **lid, "code": {"type": "string"}, "active": {"type": "boolean"}},
          "required": ["ledger_id", "code"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_post",
         "Post a balanced journal entry. `lines` is an array of objects with "
         "`account_code` and exactly one of `debit`/`credit` (decimal strings). The entry "
         "is refused unless total debits equal total credits exactly. Optional per line: "
         "`currency` + `fx_rate` (required when the line is not in the base currency), "
         "`memo`, `dimensions`. Supply `external_id` with `source` for idempotent imports.",
         ledger_post,
         {"type": "object", "properties": {
             **lid,
             "entry_date": {"type": "string", "description": "YYYY-MM-DD."},
             "memo": {"type": "string"},
             "source": {"type": "string", "description": "manual | import | ..."},
             "external_id": {"type": "string"},
             "lines": {"type": "array", "minItems": 2, "items": {
                 "type": "object",
                 "properties": {
                     "account_code": {"type": "string"},
                     "debit": {"type": "string"},
                     "credit": {"type": "string"},
                     "currency": {"type": "string"},
                     "fx_rate": {"type": "string"},
                     "memo": {"type": "string"},
                     "dimensions": {"type": "object"},
                 },
                 "required": ["account_code"],
             }}},
          "required": ["ledger_id", "entry_date", "lines"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_reverse",
         "Reverse a posted entry by posting its mirror image and linking the two. History "
         "is never edited.",
         ledger_reverse,
         {"type": "object", "properties": {
             **lid, "entry_id": {"type": "string"}, "entry_date": {"type": "string"},
             "memo": {"type": "string"}},
          "required": ["ledger_id", "entry_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_list_entries",
         "List journal entries, optionally filtered by date range and account code.",
         ledger_list_entries,
         {"type": "object", "properties": {
             **lid, "start": {"type": "string"}, "end": {"type": "string"},
             "account_code": {"type": "string"}, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_get_entry", "Read one journal entry with all of its lines.",
         ledger_get_entry,
         {"type": "object", "properties": {**lid, "entry_id": {"type": "string"}},
          "required": ["ledger_id", "entry_id"]},
         READ)
    _def("ledger_trial_balance",
         "Trial balance (opening + movement + closing) derived from postings, with "
         "balance checks.",
         ledger_trial_balance,
         {"type": "object", "properties": {
             **lid, "as_of": {"type": "string"}, "start": {"type": "string"},
             "include_zero": {"type": "boolean"}}, "required": ["ledger_id"]},
         READ)
    _def("ledger_balance_sheet",
         "Balance sheet as of a date: assets, liabilities, equity (including current "
         "earnings), with the accounting-equation check.",
         ledger_balance_sheet,
         {"type": "object", "properties": {**lid, "as_of": {"type": "string"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_income_statement",
         "Income statement (revenue, cost of sales, gross profit, operating expenses, "
         "depreciation, net income) for a date range.",
         ledger_income_statement,
         {"type": "object", "properties": {
             **lid, "start": {"type": "string"}, "end": {"type": "string"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_cash_flow",
         "Cash flow for a date range. `method=direct` (default) classifies operating/"
         "investing/financing from the counter account; `method=indirect` starts from net "
         "income, adds depreciation and working-capital movements, and reconciles the "
         "result to the direct operating figure. Unclassifiable movement is explicit, "
         "never guessed.",
         ledger_cash_flow,
         {"type": "object", "properties": {
             **lid, "start": {"type": "string"}, "end": {"type": "string"},
             "method": {"type": "string", "enum": ["direct", "indirect"]}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_year_end_preview",
         "Preview the year-end closing entry: net income, retained-earnings target, "
         "closing lines and date. Writes nothing.",
         ledger_year_end_preview,
         {"type": "object", "properties": {**lid, "fiscal_year": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_close_fiscal_year",
         "Post the year-end closing entry on the first day of the next fiscal year: "
         "zero revenue/expense and roll net income into retained earnings. Idempotent; "
         "a second call returns ALREADY_CLOSED.",
         ledger_close_fiscal_year,
         {"type": "object", "properties": {**lid, "fiscal_year": {"type": "integer"}},
          "required": ["ledger_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_budget_create",
         "Create a budget: a named set of account + optional dimension targets over a "
         "period. Planning data only; actuals are derived from the journal.",
         ledger_budget_create,
         {"type": "object", "properties": {
             **lid, "name": {"type": "string"},
             "start": {"type": "string"}, "end": {"type": "string"},
             "currency": {"type": "string"}, "note": {"type": "string"},
             "lines": {"type": "array", "items": {"type": "object", "properties": {
                 "account_code": {"type": "string"}, "amount": {"type": "string"},
                 "dimensions": {"type": "object"}, "memo": {"type": "string"}},
                 "required": ["account_code", "amount"]}}},
          "required": ["ledger_id", "name", "start", "end", "lines"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_budget_list", "List budgets and their lines.",
         ledger_budget_list,
         {"type": "object", "properties": {**lid, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_budget_variance",
         "Budget vs actual: per-line budget/actual/variance and favourable flag, "
         "matched by account and dimension. Unmatched actuals are reported as unbudgeted.",
         ledger_budget_variance,
         {"type": "object", "properties": {
             **lid, "budget_id": {"type": "string"}}, "required": ["ledger_id", "budget_id"]},
         READ)
    _def("ledger_dimension_summary",
         "Group journal movement by one dimension value (project, cost center, …), "
         "over a date range; unassigned lines are explicit.",
         ledger_dimension_summary,
         {"type": "object", "properties": {
             **lid, "dimension": {"type": "string"},
             "start": {"type": "string"}, "end": {"type": "string"}},
          "required": ["ledger_id", "dimension"]},
         READ)
    _def("ledger_cash_runway",
         "Cash runway from trailing operating burn plus open AR/AP: opening cash, "
         "monthly burn, runway months and an AR/AP-adjusted projection. A surplus "
         "reports UNKNOWN runway, never infinite.",
         ledger_cash_runway,
         {"type": "object", "properties": {
             **lid, "as_of": {"type": "string"},
             "history_days": {"type": "integer"}, "horizon_days": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_list_periods", "List fiscal periods and their OPEN/CLOSED/LOCKED status.",
         ledger_list_periods, {"type": "object", "properties": {**lid}, "required": ["ledger_id"]},
         READ)
    _def("ledger_close_period",
         "Close a fiscal period. Refused when the period has blocking findings (for "
         "example a non-zero suspense balance).",
         ledger_close_period,
         {"type": "object", "properties": {
             **lid, "period_id": {"type": "string"},
             "fiscal_year": {"type": "integer"}, "period_no": {"type": "integer"}},
          "required": ["ledger_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_reopen_period", "Reopen a CLOSED period so postings are accepted again.",
         ledger_reopen_period,
         {"type": "object", "properties": {**lid, "period_id": {"type": "string"}},
          "required": ["ledger_id", "period_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_lock_period",
         "Lock a CLOSED period permanently. A LOCKED period can never be reopened.",
         ledger_lock_period,
         {"type": "object", "properties": {**lid, "period_id": {"type": "string"}},
          "required": ["ledger_id", "period_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_audit_log", "The ledger's append-only audit trail.",
         ledger_audit_log,
         {"type": "object", "properties": {**lid, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_audit_verify",
         "Verify the audit trail's hash chain: OK, UNSEALED (legacy rows) or BROKEN "
         "at the first edited/reordered event.",
         ledger_audit_verify,
         {"type": "object", "properties": {**lid}, "required": ["ledger_id"]},
         READ)
    _def("ledger_attach_document",
         "Attach an artifact (receipt, invoice, bank statement) to a posted entry. "
         "Stores the artifact id and its content hash; the bytes stay in the artifact "
         "store. A changed artifact no longer verifies against the posted entry.",
         ledger_attach_document,
         {"type": "object", "properties": {
             **lid, "entry_id": {"type": "string"},
             "artifact_id": {"type": "string"},
             "note": {"type": "string"}},
          "required": ["ledger_id", "entry_id", "artifact_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_list_attachments",
         "List the source-document references on a ledger or one entry.",
         ledger_list_attachments,
         {"type": "object", "properties": {
             **lid, "entry_id": {"type": "string"},
             "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_detach_document",
         "Remove an attachment reference (the artifact itself is unchanged).",
         ledger_detach_document,
         {"type": "object", "properties": {
             **lid, "attachment_id": {"type": "string"}},
          "required": ["ledger_id", "attachment_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_verify_attachment",
         "Compare an attachment's frozen content hash with the artifact's current "
         "hash: MATCHED, CHANGED (artifact replaced) or MISSING (artifact deleted).",
         ledger_verify_attachment,
         {"type": "object", "properties": {
             **lid, "attachment_id": {"type": "string"}},
          "required": ["ledger_id", "attachment_id"]},
         READ)

    # ── Phase 2: import, categorisation, reconciliation ────────────────────
    mapping_schema = {
        "type": "object",
        "description": "CSV column mapping. Declare the date and amount columns; the "
                       "format and date pattern are declared, never guessed.",
        "properties": {
            "date_column": {"type": "string"},
            "description_columns": {"type": "array", "items": {"type": "string"}},
            "amount_column": {"type": "string", "description": "Signed amount (single column)."},
            "debit_column": {"type": "string", "description": "Money out (positive magnitude)."},
            "credit_column": {"type": "string", "description": "Money in (positive magnitude)."},
            "currency_column": {"type": "string"},
            "balance_column": {"type": "string"},
            "delimiter": {"type": "string"},
            "date_format": {"type": "string", "description": "strftime pattern, e.g. %d/%m/%Y."},
            "decimal_separator": {"type": "string", "enum": [".", ","]},
            "thousands_separator": {"type": "string"},
            "skip_rows": {"type": "integer"},
            "default_currency": {"type": "string"},
        },
        "required": ["date_column"],
    }
    statement_fields = {
        **lid,
        "account_code": {"type": "string",
                         "description": "The cash/bank account the statement belongs to."},
        "content": {"type": "string", "description": "The raw statement text."},
        "format": {"type": "string", "enum": ["csv", "ofx", "qif"]},
        "mapping": mapping_schema,
        "fx_rate": {"type": "string",
                    "description": "Required when the statement currency is not the "
                                   "ledger's base currency."},
    }

    _def("ledger_import_preview",
         "Parse a bank/CSV/OFX/QIF statement for one cash account, categorise each row "
         "with the ledger's rules, and show what WOULD be posted. Writes nothing. Rows "
         "already posted are reported as duplicates; unmatched rows go to suspense.",
         ledger_import_preview,
         {"type": "object", "properties": statement_fields,
          "required": ["ledger_id", "account_code", "content"]},
         READ)
    _def("ledger_import_commit",
         "Post a parsed statement through the normal journal path. Idempotent per row "
         "(a re-import returns the existing entries). Rows that cannot post are reported "
         "individually; the batch is COMPLETE, PARTIAL or FAILED.",
         ledger_import_commit,
         {"type": "object", "properties": {
             **statement_fields, "batch_ref": {"type": "string"}},
          "required": ["ledger_id", "account_code", "content"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_list_imports", "Import batches for a ledger, newest first.",
         ledger_list_imports,
         {"type": "object", "properties": {**lid, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_rule_create",
         "Create (or version) a categorisation rule. Conditions inside a rule are ANDed; "
         "the needles inside `payee_contains` are ORed. Lower `priority` runs first. "
         "Creating a rule with an existing name deactivates the previous version.",
         ledger_rule_create,
         {"type": "object", "properties": {
             **lid,
             "name": {"type": "string"},
             "target_account": {"type": "string", "description": "Account code to post to."},
             "priority": {"type": "integer", "default": 100},
             "payee_contains": {"type": "array", "items": {"type": "string"}},
             "payee_regex": {"type": "string"},
             "amount_min": {"type": "string", "description": "Compared against |amount|."},
             "amount_max": {"type": "string"},
             "direction": {"type": "string", "enum": ["IN", "OUT", "ANY"]}},
          "required": ["ledger_id", "name", "target_account"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_rule_list", "Categorisation rules in evaluation order.",
         ledger_rule_list,
         {"type": "object", "properties": {
             **lid, "active_only": {"type": "boolean"}}, "required": ["ledger_id"]},
         READ)
    _def("ledger_rule_deactivate", "Stop a categorisation rule from applying.",
         ledger_rule_deactivate,
         {"type": "object", "properties": {**lid, "rule_id": {"type": "string"}},
          "required": ["ledger_id", "rule_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_reconcile_statement",
         "Reconcile a statement against the ledger for one cash account: per-row "
         "MATCHED/MISMATCH/MISSING/UNKNOWN plus a balance check. Supply "
         "`statement_balance` to prove the balance; without it the run is INCOMPLETE. "
         "Never mutates the journal.",
         ledger_reconcile_statement,
         {"type": "object", "properties": {
             **statement_fields,
             "start": {"type": "string"}, "end": {"type": "string"},
             "tolerance": {"type": "string"},
             "window_days": {"type": "integer"},
             "statement_balance": {"type": "string"}},
          "required": ["ledger_id", "account_code", "content"]},
         READ)
    _def("ledger_list_reconciliations", "Saved reconciliation runs, newest first.",
         ledger_list_reconciliations,
         {"type": "object", "properties": {**lid, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_get_reconciliation", "One reconciliation run with every item.",
         ledger_get_reconciliation,
         {"type": "object", "properties": {**lid, "run_id": {"type": "string"}},
          "required": ["ledger_id", "run_id"]},
         READ)

    # ── Phase 3: schedules, accruals, revaluation, close ────────────────────
    _def("ledger_schedule_create",
         "Create a depreciation or amortisation schedule. Every period's entry is "
         "derived up front (straight line gives the last period the rounding remainder; "
         "declining balance never goes below salvage). Nothing is posted until "
         "`ledger_schedule_post`.",
         ledger_schedule_create,
         {"type": "object", "properties": {
             **lid,
             "name": {"type": "string"},
             "kind": {"type": "string", "enum": ["DEPRECIATION", "AMORTISATION"]},
             "method": {"type": "string",
                        "enum": ["STRAIGHT_LINE", "DECLINING_BALANCE"]},
             "cost": {"type": "string"}, "salvage": {"type": "string"},
             "periods": {"type": "integer", "description": "Number of monthly periods."},
             "start_date": {"type": "string", "description": "YYYY-MM-DD."},
             "expense_account": {"type": "string"},
             "contra_account": {"type": "string",
                                "description": "Accumulated depreciation / prepaid."},
             "factor": {"type": "string",
                        "description": "Declining-balance factor (default 2)."}},
          "required": ["ledger_id", "name", "cost", "periods", "expense_account",
                       "contra_account", "start_date"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_schedule_list", "Depreciation/amortisation schedules and their progress.",
         ledger_schedule_list,
         {"type": "object", "properties": {
             **lid, "active_only": {"type": "boolean"}}, "required": ["ledger_id"]},
         READ)
    _def("ledger_schedule_post",
         "Post every schedule period due on/before `through` (default today). Stops at "
         "the first entry that cannot post; already-posted periods are not repeated.",
         ledger_schedule_post,
         {"type": "object", "properties": {
             **lid, "schedule_id": {"type": "string"}, "through": {"type": "string"}},
          "required": ["ledger_id", "schedule_id"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_accrue",
         "Post a month-end accrual (Dr debit / Cr credit) and, optionally, its automatic "
         "reversal on `reversal_date`. Supply `reference` to make a retried run idempotent.",
         ledger_accrue,
         {"type": "object", "properties": {
             **lid, "entry_date": {"type": "string"}, "amount": {"type": "string"},
             "debit_account": {"type": "string"}, "credit_account": {"type": "string"},
             "memo": {"type": "string"}, "reversal_date": {"type": "string"},
             "reference": {"type": "string"}},
          "required": ["ledger_id", "entry_date", "amount", "debit_account",
                       "credit_account"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_revalue_preview",
         "Show the period-end FX revaluation for foreign-currency accounts at supplied "
         "rates. An account with no rate is reported as RATE_REQUIRED and skipped. "
         "Writes nothing.",
         ledger_revalue_preview,
         {"type": "object", "properties": {
             **lid, "as_of": {"type": "string"},
             "rates": {"type": "object", "description": "currency → rate, e.g. {\"EUR\":\"1.09\"}"},
             "gain_account": {"type": "string"}, "loss_account": {"type": "string"}},
          "required": ["ledger_id", "as_of", "rates", "gain_account", "loss_account"]},
         READ)
    _def("ledger_revalue_post",
         "Post the period-end FX revaluation. Idempotent per account+date+rate, so "
         "re-running at the same rate posts nothing and a rate correction trues up.",
         ledger_revalue_post,
         {"type": "object", "properties": {
             **lid, "as_of": {"type": "string"},
             "rates": {"type": "object"}, "gain_account": {"type": "string"},
             "loss_account": {"type": "string"}},
          "required": ["ledger_id", "as_of", "rates", "gain_account", "loss_account"]},
         WRITE, WRITE_EFFECT)
    _def("ledger_close_preview",
         "Run the period close checklist (trial balance, suspense, cash, imports, "
         "schedules, reconciliation, FX, duplicates) with blocker/warning severities. "
         "Records the run; changes no posting.",
         ledger_close_preview,
         {"type": "object", "properties": {
             **lid, "period_id": {"type": "string"},
             "fiscal_year": {"type": "integer"}, "period_no": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_list_close_runs", "Saved close evaluations, newest first.",
         ledger_list_close_runs,
         {"type": "object", "properties": {**lid, "limit": {"type": "integer"}},
          "required": ["ledger_id"]},
         READ)
    _def("ledger_get_close_run", "One close run with its findings and checks.",
         ledger_get_close_run,
         {"type": "object", "properties": {**lid, "run_id": {"type": "string"}},
          "required": ["ledger_id", "run_id"]},
         READ)

    # The AR/AP surface lives in its own module (size); it registers on the SAME
    # registry and stays inside the same `ledger_*` family.
    from .document_tools import register_document_tools  # noqa: PLC0415

    register_document_tools(registry)

    # Groups, consolidation and exports — the third and last module.
    from .group_tools import register_group_tools  # noqa: PLC0415

    register_group_tools(registry)


# The AR/AP and group tool names are merged here so `LEDGER_TOOLS` remains the ONE
# list the agent allowlist and the `ledger_write` authorizer read. Imported at the
# BOTTOM because those modules import the helpers defined above.
from .document_tools import DOCUMENT_READ_TOOLS, DOCUMENT_WRITE_TOOLS  # noqa: E402
from .group_tools import GROUP_READ_TOOLS, GROUP_WRITE_TOOLS  # noqa: E402

LEDGER_READ_TOOLS = [*LEDGER_READ_TOOLS, *DOCUMENT_READ_TOOLS, *GROUP_READ_TOOLS]
LEDGER_WRITE_TOOLS = [*LEDGER_WRITE_TOOLS, *DOCUMENT_WRITE_TOOLS, *GROUP_WRITE_TOOLS]
LEDGER_TOOLS = LEDGER_READ_TOOLS + LEDGER_WRITE_TOOLS

__all__ = [
    "LEDGER_READ_TOOLS",
    "LEDGER_TOOLS",
    "LEDGER_WRITE_TOOLS",
    "register_ledger_tools",
]
