"""General Ledger HTTP routes.

Thin adapters over the SAME `LedgerService` the agent tools use (reached through
`harness.deps.service`), so the UI and the agent can never disagree and a second
posting path is never built.

Every route is operator-gated (loopback or the runtime token), matching every
other runtime surface. Tenant scope: an optional actor identity comes from the
`X-Harness-User` header (or `actorId` in a body/query); when it does not match a
ledger's owner the request is refused (403). Absent, the app is in its documented
single-user local mode.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..deps import require_operator, service
from ..ledger.errors import LedgerError

router = APIRouter(tags=["ledger"])

_STATUS = {
    "LEDGER_NOT_FOUND": 404,
    "ACCOUNT_NOT_FOUND": 404,
    "ENTRY_NOT_FOUND": 404,
    "PERIOD_NOT_FOUND": 404,
    "RULE_NOT_FOUND": 404,
    "RECONCILIATION_NOT_FOUND": 404,
    "IMPORT_BATCH_NOT_FOUND": 404,
    "SCHEDULE_NOT_FOUND": 404,
    "CLOSE_RUN_NOT_FOUND": 404,
    "PARTY_NOT_FOUND": 404,
    "TAX_CODE_NOT_FOUND": 404,
    "DOCUMENT_NOT_FOUND": 404,
    "ARTIFACT_NOT_FOUND": 404,
    "ATTACHMENT_NOT_FOUND": 404,
    "ATTACHMENT_EXISTS": 409,
    "ARTIFACT_UNHASHED": 422,
    "ARTIFACT_STORE_UNAVAILABLE": 503,
    "LEDGER_FORBIDDEN": 403,
    "LEDGER_UNAVAILABLE": 503,
    "PERIOD_CLOSED": 409,
    "PERIOD_LOCKED": 409,
    "PERIOD_NOT_READY": 409,
    "PERIOD_NOT_CLOSED": 409,
    "ALREADY_REVERSED": 409,
    "ACCOUNT_EXISTS": 409,
    "DUPLICATE_ENTRY": 409,
    "UNBALANCED_ENTRY": 422,
    "INVALID_STATEMENT_METHOD": 422,
    "YEAR_END_NOT_READY": 409,
    "RETAINED_EARNINGS_MISSING": 422,
    "BUDGET_NOT_FOUND": 404,
    "BUDGET_INVALID": 422,
    "DIMENSION_REQUIRED": 422,
    "INVALID_MAPPING": 422,
    "INVALID_STATEMENT": 422,
    "IMPORT_ROW_INVALID": 422,
    "RULE_INVALID": 422,
    "RECONCILIATION_INVALID": 422,
    "IMPORT_ACCOUNT_INVALID": 400,
    "UNSUPPORTED_STATEMENT_FORMAT": 400,
    "SCHEDULE_INVALID": 422,
    "REVALUATION_INVALID": 422,
    "RATE_REQUIRED": 422,
    "SCHEDULE_EXHAUSTED": 409,
    "PARTY_INVALID": 422,
    "TAX_CODE_INVALID": 422,
    "UNSUPPORTED_TAX_JURISDICTION": 422,
    "DOCUMENT_INVALID": 422,
    "DOCUMENT_OVERPAID": 422,
    "DOCUMENT_HAS_PAYMENTS": 409,
    "DOCUMENT_VOID": 409,
    "DOCUMENT_NUMBER_EXISTS": 409,
    "CONTROL_ACCOUNT_MISSING": 422,
    "GROUP_NOT_FOUND": 404,
    "MEMBER_NOT_FOUND": 404,
    "MEMBER_LEDGER_MISSING": 404,
    "GROUP_INVALID": 422,
    "MEMBER_EXISTS": 409,
    "ACCOUNT_TYPE_CONFLICT": 422,
    "UNSUPPORTED_EXPORT": 422,
}


def _engine() -> Any:
    engine = getattr(service, "ledger", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="the general ledger is unavailable")
    return engine


def _raise(exc: LedgerError) -> None:
    detail = exc.to_dict()
    raise HTTPException(
        status_code=_STATUS.get(exc.code, 400),
        detail=detail.get("message") or exc.code,
    ) from exc


def _actor(request: Request, body: Optional[dict[str, Any]] = None,
           query_actor: Optional[str] = None) -> Optional[str]:
    return (
        request.headers.get("x-harness-user")
        or (body or {}).get("actorId")
        or query_actor
    )


@router.get("/ledger/list")
async def ledger_list(request: Request, owner_id: Optional[str] = None,
                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"available": True, "ledgers": _engine().list_ledgers(owner_id or _actor(request))}


@router.post("/ledger/create")
async def ledger_create(request: Request, body: dict[str, Any] = Body(...),
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="`name` is required")
    actor = _actor(request, body)
    fields = {k: v for k, v in body.items() if k not in ("name", "actorId", "ownerId")}
    try:
        return _engine().create_ledger(name, owner_id=actor or body.get("ownerId"), actor=actor,
                                       **fields)
    except LedgerError as exc:
        _raise(exc)


@router.delete("/ledger/{ledger_id}")
async def ledger_delete(ledger_id: str, request: Request,
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"deleted": _engine().delete_ledger(ledger_id, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/groups")
async def ledger_groups(request: Request, owner_id: Optional[str] = None,
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"groups": _engine().consolidation.list_groups(
        owner_id or _actor(request))}


@router.post("/ledger/groups")
async def ledger_create_group(request: Request, body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().consolidation.create_group(
            str(body.get("name") or ""),
            base_currency=str(body.get("base_currency") or body.get("baseCurrency") or "USD"),
            owner_id=_actor(request, body) or body.get("ownerId"),
            notes=body.get("notes"),
            actor=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/groups/{group_id}/members")
async def ledger_add_group_member(group_id: str, request: Request,
                                  body: dict[str, Any] = Body(...),
                                  _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().consolidation.add_member(
            group_id,
            str(body.get("ledger_id") or body.get("ledgerId") or ""),
            ownership_pct=body.get("ownership_pct") or body.get("ownershipPct") or "100",
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/groups/{group_id}/consolidate")
async def ledger_consolidate(group_id: str, request: Request,
                             body: dict[str, Any] = Body(default={}),
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().consolidation.consolidate(
            group_id,
            as_of=body.get("as_of") or body.get("asOf"),
            start=body.get("start"),
            rates=body.get("rates") or {},
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}")
async def ledger_get(ledger_id: str, request: Request,
                     _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().get_ledger(ledger_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Accounts ─────────────────────────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/accounts")
async def ledger_accounts(ledger_id: str, request: Request,
                          include_inactive: bool = True,
                          _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        rows = _engine().list_accounts(ledger_id, include_inactive=include_inactive,
                                       actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)
    return {"accounts": rows}


@router.post("/ledger/{ledger_id}/accounts")
async def ledger_create_account(ledger_id: str, request: Request,
                                body: dict[str, Any] = Body(...),
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_account(
            ledger_id,
            code=str(body.get("code") or ""),
            name=str(body.get("name") or ""),
            account_type=str(body.get("type") or body.get("account_type") or ""),
            subtype=body.get("subtype") or "",
            parent_code=body.get("parent_code"),
            currency=body.get("currency") or "",
            cash_flow_category=body.get("cash_flow_category") or "",
            is_cash=bool(body.get("is_cash", False)),
            is_contra=bool(body.get("is_contra", False)),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/accounts/{code}/active")
async def ledger_set_account_active(ledger_id: str, code: str, request: Request,
                                    body: dict[str, Any] = Body(default={}),
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().set_account_active(
            ledger_id, code, bool(body.get("active", True)), actor_id=_actor(request, body)
        )
    except LedgerError as exc:
        _raise(exc)


# ── Journal ──────────────────────────────────────────────────────────────────


@router.post("/ledger/{ledger_id}/entries")
async def ledger_post(ledger_id: str, request: Request,
                      body: dict[str, Any] = Body(...),
                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().post(
            ledger_id,
            entry_date=body.get("entry_date") or body.get("entryDate"),
            lines=body.get("lines") or [],
            memo=body.get("memo") or "",
            source=body.get("source") or "manual",
            external_id=body.get("external_id") or body.get("externalId"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/entries")
async def ledger_entries(ledger_id: str, request: Request,
                         start: Optional[str] = None, end: Optional[str] = None,
                         account_code: Optional[str] = None, limit: int = 100,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        rows = _engine().list_entries(
            ledger_id, start=start, end=end, account_code=account_code, limit=limit,
            actor_id=_actor(request),
        )
    except LedgerError as exc:
        _raise(exc)
    return {"entries": rows}


@router.get("/ledger/{ledger_id}/entries/{entry_id}")
async def ledger_entry(ledger_id: str, entry_id: str, request: Request,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().get_entry(ledger_id, entry_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/entries/{entry_id}/reverse")
async def ledger_reverse(ledger_id: str, entry_id: str, request: Request,
                         body: dict[str, Any] = Body(default={}),
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().reverse(
            ledger_id, entry_id,
            entry_date=body.get("entry_date"), memo=body.get("memo") or "",
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


# ── Statements ───────────────────────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/trial-balance")
async def ledger_trial_balance(ledger_id: str, request: Request,
                               as_of: Optional[str] = None, start: Optional[str] = None,
                               include_zero: bool = False,
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().trial_balance(ledger_id, as_of=as_of, start=start,
                                       include_zero=include_zero,
                                       actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/balance-sheet")
async def ledger_balance_sheet(ledger_id: str, request: Request,
                               as_of: Optional[str] = None,
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().balance_sheet(ledger_id, as_of=as_of, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/income-statement")
async def ledger_income_statement(ledger_id: str, request: Request,
                                  start: Optional[str] = None, end: Optional[str] = None,
                                  _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().income_statement(ledger_id, start=start, end=end,
                                          actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/cash-flow")
async def ledger_cash_flow(ledger_id: str, request: Request,
                           start: Optional[str] = None, end: Optional[str] = None,
                           method: str = "direct",
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        if method == "indirect":
            return _engine().cash_flow_indirect(ledger_id, start=start, end=end,
                                                 actor_id=_actor(request))
        if method != "direct":
            raise HTTPException(status_code=422,
                                detail="`method` must be `direct` or `indirect`")
        return _engine().cash_flow(ledger_id, start=start, end=end, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Budgets, dimensions, runway ──────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/budgets")
async def ledger_budgets(ledger_id: str, request: Request, limit: int = 100,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"budgets": _engine().list_budgets(ledger_id, limit=limit,
                                                  actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/budgets")
async def ledger_budget_create_route(ledger_id: str, request: Request,
                                     body: dict[str, Any] = Body(...),
                                     _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_budget(
            ledger_id,
            name=body.get("name") or "Budget",
            start=body.get("start"),
            end=body.get("end"),
            lines=body.get("lines") or [],
            note=body.get("note"),
            currency=body.get("currency"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/budgets/{budget_id}/variance")
async def ledger_budget_variance_route(ledger_id: str, budget_id: str, request: Request,
                                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().budget_vs_actual(ledger_id, budget_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/dimensions/{dimension}")
async def ledger_dimension_summary_route(ledger_id: str, dimension: str, request: Request,
                                         start: Optional[str] = None,
                                         end: Optional[str] = None,
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().dimension_summary(ledger_id, dimension, start=start, end=end,
                                           actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/runway")
async def ledger_runway_route(ledger_id: str, request: Request,
                              as_of: Optional[str] = None,
                              history_days: int = 90, horizon_days: int = 90,
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().cash_runway(ledger_id, as_of=as_of, history_days=history_days,
                                     horizon_days=horizon_days, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Year-end close ───────────────────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/year-end/preview")
async def ledger_year_end_preview_route(ledger_id: str, request: Request,
                                        fiscal_year: Optional[int] = None,
                                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().preview_fiscal_year_close(
            ledger_id, fiscal_year=fiscal_year, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/year-end/close")
async def ledger_year_end_close_route(ledger_id: str, request: Request,
                                      body: dict[str, Any] = Body(default={}),
                                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().close_fiscal_year(
            ledger_id,
            fiscal_year=body.get("fiscal_year") or body.get("fiscalYear"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


# ── Periods + audit ──────────────────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/periods")
async def ledger_periods(ledger_id: str, request: Request,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"periods": _engine().list_periods(ledger_id, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/periods/{period_id}/close")
async def ledger_close_period(ledger_id: str, period_id: str, request: Request,
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().close_period(ledger_id, period_id=period_id,
                                      actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/periods/{period_id}/reopen")
async def ledger_reopen_period(ledger_id: str, period_id: str, request: Request,
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().reopen_period(ledger_id, period_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/periods/{period_id}/lock")
async def ledger_lock_period(ledger_id: str, period_id: str, request: Request,
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().lock_period(ledger_id, period_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/audit")
async def ledger_audit(ledger_id: str, request: Request, limit: int = 100,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"events": _engine().audit_log(ledger_id, limit=limit,
                                              actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/audit/verify")
async def ledger_audit_verify_route(ledger_id: str, request: Request,
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().verify_audit(ledger_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Source-document attachments ───────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/attachments")
async def ledger_attachments(ledger_id: str, request: Request,
                             entry_id: Optional[str] = None, limit: int = 200,
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"attachments": _engine().list_attachments(
            ledger_id, entry_id=entry_id, limit=limit, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/attachments")
async def ledger_attach(ledger_id: str, request: Request,
                        body: dict[str, Any] = Body(...),
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().attach_document(
            ledger_id,
            str(body.get("entry_id") or body.get("entryId") or ""),
            str(body.get("artifact_id") or body.get("artifactId") or ""),
            artifact_service=getattr(service, "artifacts", None),
            note=body.get("note"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.delete("/ledger/{ledger_id}/attachments/{attachment_id}")
async def ledger_detach(ledger_id: str, attachment_id: str, request: Request,
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().detach_document(ledger_id, attachment_id,
                                         actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/attachments/{attachment_id}/verify")
async def ledger_verify_attachment_route(ledger_id: str, attachment_id: str,
                                         request: Request,
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().verify_attachment(
            ledger_id, attachment_id,
            artifact_service=getattr(service, "artifacts", None),
            actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Import / rules / reconciliation ───────────────────────────────────────────


def _statement_body(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_code": str(body.get("account_code") or body.get("accountCode") or ""),
        "content": str(body.get("content") or ""),
        "statement_format": str(body.get("format") or "csv"),
        "mapping": body.get("mapping"),
        "fx_rate": body.get("fx_rate") or body.get("fxRate"),
    }


@router.post("/ledger/{ledger_id}/import/preview")
async def ledger_import_preview(ledger_id: str, request: Request,
                                body: dict[str, Any] = Body(...),
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().preview_import(ledger_id, actor_id=_actor(request, body),
                                        **_statement_body(body))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/import/commit")
async def ledger_import_commit(ledger_id: str, request: Request,
                               body: dict[str, Any] = Body(...),
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().commit_import(
            ledger_id, actor_id=_actor(request, body),
            batch_ref=body.get("batch_ref") or body.get("batchRef"),
            **_statement_body(body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/imports")
async def ledger_imports(ledger_id: str, request: Request, limit: int = 50,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"batches": _engine().list_import_batches(ledger_id, limit=limit,
                                                         actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/rules")
async def ledger_rules(ledger_id: str, request: Request, active_only: bool = True,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"rules": _engine().list_rules(ledger_id, active_only=active_only,
                                              actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/rules")
async def ledger_create_rule(ledger_id: str, request: Request,
                             body: dict[str, Any] = Body(...),
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_rule(
            ledger_id,
            name=str(body.get("name") or ""),
            target_account=str(body.get("target_account") or body.get("targetAccount") or ""),
            priority=int(body.get("priority") or 100),
            payee_contains=body.get("payee_contains") or body.get("payeeContains") or (),
            payee_regex=str(body.get("payee_regex") or body.get("payeeRegex") or ""),
            amount_min=body.get("amount_min") or body.get("amountMin"),
            amount_max=body.get("amount_max") or body.get("amountMax"),
            direction=str(body.get("direction") or "ANY"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/rules/{rule_id}/deactivate")
async def ledger_deactivate_rule(ledger_id: str, rule_id: str, request: Request,
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().deactivate_rule(ledger_id, rule_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/reconcile")
async def ledger_reconcile(ledger_id: str, request: Request,
                           body: dict[str, Any] = Body(...),
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().reconcile_statement(
            ledger_id, actor_id=_actor(request, body),
            start=body.get("start"), end=body.get("end"),
            tolerance=body.get("tolerance") or "0.01",
            window_days=int(body.get("window_days") or body.get("windowDays") or 5),
            statement_balance=body.get("statement_balance") or body.get("statementBalance"),
            **_statement_body(body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/reconciliations")
async def ledger_reconciliations(ledger_id: str, request: Request, limit: int = 50,
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"reconciliations": _engine().list_reconciliations(
            ledger_id, limit=limit, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/reconciliations/{run_id}")
async def ledger_reconciliation(ledger_id: str, run_id: str, request: Request,
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().get_reconciliation(ledger_id, run_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── Schedules, accruals, revaluation, close ──────────────────────────────────


@router.get("/ledger/{ledger_id}/schedules")
async def ledger_schedules(ledger_id: str, request: Request, active_only: bool = True,
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"schedules": _engine().list_schedules(
            ledger_id, active_only=active_only, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/schedules")
async def ledger_create_schedule(ledger_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_schedule(
            ledger_id,
            name=str(body.get("name") or ""),
            kind=str(body.get("kind") or "DEPRECIATION"),
            method=str(body.get("method") or "STRAIGHT_LINE"),
            cost=body.get("cost"),
            salvage=body.get("salvage"),
            periods=int(body.get("periods") or 0),
            start_date=body.get("start_date") or body.get("startDate"),
            expense_account=str(body.get("expense_account") or body.get("expenseAccount") or ""),
            contra_account=str(body.get("contra_account") or body.get("contraAccount") or ""),
            factor=body.get("factor"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/schedules/{schedule_id}/post")
async def ledger_post_schedule(ledger_id: str, schedule_id: str, request: Request,
                               body: dict[str, Any] = Body(default={}),
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().post_schedule(
            ledger_id, schedule_id,
            through=body.get("through"), actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/accrue")
async def ledger_accrue(ledger_id: str, request: Request,
                        body: dict[str, Any] = Body(...),
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().post_accrual(
            ledger_id,
            entry_date=body.get("entry_date") or body.get("entryDate"),
            amount=body.get("amount"),
            debit_account=str(body.get("debit_account") or body.get("debitAccount") or ""),
            credit_account=str(body.get("credit_account") or body.get("creditAccount") or ""),
            memo=str(body.get("memo") or ""),
            reversal_date=body.get("reversal_date") or body.get("reversalDate"),
            reference=body.get("reference"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/revalue/preview")
async def ledger_revalue_preview(ledger_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().preview_revaluation(
            ledger_id,
            as_of=body.get("as_of") or body.get("asOf"),
            rates=body.get("rates") or {},
            gain_account=str(body.get("gain_account") or body.get("gainAccount") or ""),
            loss_account=str(body.get("loss_account") or body.get("lossAccount") or ""),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/revalue")
async def ledger_revalue_post(ledger_id: str, request: Request,
                              body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().post_revaluation(
            ledger_id,
            as_of=body.get("as_of") or body.get("asOf"),
            rates=body.get("rates") or {},
            gain_account=str(body.get("gain_account") or body.get("gainAccount") or ""),
            loss_account=str(body.get("loss_account") or body.get("lossAccount") or ""),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/close/preview")
async def ledger_close_preview(ledger_id: str, request: Request,
                               body: dict[str, Any] = Body(default={}),
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().close_report(
            ledger_id,
            period_id=body.get("period_id") or body.get("periodId"),
            fiscal_year=body.get("fiscal_year") or body.get("fiscalYear"),
            period_no=body.get("period_no") or body.get("periodNo"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/close-runs")
async def ledger_close_runs(ledger_id: str, request: Request, limit: int = 50,
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"runs": _engine().list_close_runs(ledger_id, limit=limit,
                                                  actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/close-runs/{run_id}")
async def ledger_close_run(ledger_id: str, run_id: str, request: Request,
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().get_close_run(ledger_id, run_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


# ── AR/AP subledger ──────────────────────────────────────────────────────────


@router.get("/ledger/{ledger_id}/parties")
async def ledger_parties(ledger_id: str, request: Request,
                         kind: Optional[str] = None, active_only: bool = True,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"parties": _engine().list_parties(
            ledger_id, kind=kind, active_only=active_only, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/parties")
async def ledger_create_party(ledger_id: str, request: Request,
                              body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_party(
            ledger_id,
            name=str(body.get("name") or ""),
            kind=str(body.get("kind") or ""),
            email=body.get("email"),
            tax_id=body.get("tax_id") or body.get("taxId"),
            currency=body.get("currency") or "",
            payment_terms_days=int(body.get("payment_terms_days")
                                   or body.get("paymentTermsDays") or 30),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/tax-codes")
async def ledger_tax_codes(ledger_id: str, request: Request, active_only: bool = True,
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"taxCodes": _engine().list_tax_codes(
            ledger_id, active_only=active_only, actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/tax-codes")
async def ledger_create_tax_code(ledger_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_tax_code(
            ledger_id,
            code=str(body.get("code") or ""),
            name=str(body.get("name") or body.get("code") or ""),
            jurisdiction=str(body.get("jurisdiction") or ""),
            rate=body.get("rate"),
            account_code=str(body.get("account_code") or body.get("accountCode") or ""),
            kind=str(body.get("kind") or "VAT"),
            price_includes_tax=bool(body.get("price_includes_tax")
                                    or body.get("priceIncludesTax") or False),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/documents")
async def ledger_documents(ledger_id: str, request: Request,
                           kind: Optional[str] = None, party_id: Optional[str] = None,
                           status: Optional[str] = None, limit: int = 100,
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"documents": _engine().list_documents(
            ledger_id, kind=kind, party_id=party_id, status=status, limit=limit,
            actor_id=_actor(request))}
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/documents")
async def ledger_create_document(ledger_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_document(
            ledger_id,
            kind=str(body.get("kind") or ""),
            party_id=str(body.get("party_id") or body.get("partyId") or ""),
            issue_date=body.get("issue_date") or body.get("issueDate"),
            due_date=body.get("due_date") or body.get("dueDate"),
            number=body.get("number"),
            memo=str(body.get("memo") or ""),
            reference=body.get("reference"),
            lines=body.get("lines") or [],
            receivable_account=body.get("receivable_account")
            or body.get("receivableAccount"),
            payable_account=body.get("payable_account") or body.get("payableAccount"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/documents/{document_id}")
async def ledger_document(ledger_id: str, document_id: str, request: Request,
                          _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().get_document(ledger_id, document_id, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/documents/{document_id}/void")
async def ledger_void_document(ledger_id: str, document_id: str, request: Request,
                               body: dict[str, Any] = Body(default={}),
                               _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().void_document(
            ledger_id, document_id, reason=str(body.get("reason") or ""),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.post("/ledger/{ledger_id}/documents/{document_id}/pay")
async def ledger_pay_document(ledger_id: str, document_id: str, request: Request,
                              body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().pay_document(
            ledger_id, document_id,
            date_paid=body.get("date") or body.get("date_paid") or body.get("datePaid"),
            amount=body.get("amount"),
            bank_account=str(body.get("bank_account") or body.get("bankAccount") or ""),
            reference=body.get("reference"),
            actor_id=_actor(request, body),
        )
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/aging")
async def ledger_aging(ledger_id: str, request: Request, kind: str = "AR",
                       as_of: Optional[str] = None,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().aging(ledger_id, kind=kind, as_of=as_of,
                               actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)


@router.get("/ledger/{ledger_id}/export")
async def ledger_export(ledger_id: str, request: Request,
                        format: str = "journal_csv",
                        start: Optional[str] = None, end: Optional[str] = None,
                        limit: int = 5000,
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..ledger.exports import export as _export  # noqa: PLC0415

    try:
        return _export(_engine(), ledger_id, fmt=format, start=start, end=end,
                       limit=limit, actor_id=_actor(request))
    except LedgerError as exc:
        _raise(exc)
