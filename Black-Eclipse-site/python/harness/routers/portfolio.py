"""Portfolio Accounting Engine HTTP routes.

Thin adapters over the SAME portfolio service the agent tools use (reached
through `harness.deps.service`), so the UI and the agent can never disagree and
a second engine is never built. Reads AND mutations are operator-gated
(loopback or runtime token) — matching every other runtime surface.

Tenant scope: an optional actor identity is taken from the `X-Harness-User`
header (or `actorId` in a body / query). When present and it does not match a
portfolio's owner, the request is refused (403). Absent, the app is in its
documented single-user local mode.

Prices for a read may be supplied by the front door as a `marks` mapping (the
Next.js side already fetches live quotes); without them the read reports an
honest data-quality status rather than inventing prices.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..deps import require_operator, service

router = APIRouter(tags=["portfolio"])


def _engine() -> Any:
    engine = getattr(service, "portfolio", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="the portfolio engine is unavailable")
    return engine


def _actor(request: Request, body: Optional[dict[str, Any]] = None,
           query_actor: Optional[str] = None) -> Optional[str]:
    return (
        request.headers.get("x-harness-user")
        or (body or {}).get("actorId")
        or query_actor
    )


@router.get("/portfolio/list")
async def portfolio_list(request: Request, owner_id: Optional[str] = None,
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    owner = owner_id or _actor(request)
    return {"available": True, "portfolios": _engine().list_portfolios(owner)}


@router.post("/portfolio/create")
async def portfolio_create(request: Request, body: dict[str, Any] = Body(...),
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="`name` is required")
    actor = _actor(request, body)
    fields = {k: v for k, v in body.items() if k not in ("name", "actorId")}
    if actor:
        fields["owner_id"] = actor
    return _engine().create_portfolio(name, **fields)


@router.post("/portfolio/aggregate")
async def portfolio_aggregate(request: Request, body: dict[str, Any] = Body(default={}),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    engine = _engine()
    return await engine.aggregate({**body, "owner_id": body.get("owner_id") or _actor(request, body)},
                                  actor_id=_actor(request, body))


@router.delete("/portfolio/{portfolio_id}")
async def portfolio_delete(portfolio_id: str, request: Request,
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"deleted": _engine().delete_portfolio(portfolio_id, actor_id=_actor(request))}


@router.post("/portfolio/{portfolio_id}/query")
async def portfolio_query(portfolio_id: str, request: Request,
                          body: dict[str, Any] = Body(default={}),
                          _: bool = Depends(require_operator)) -> dict[str, Any]:
    engine = _engine()
    try:
        return await engine.read(
            portfolio_id,
            include=body.get("include"),
            marks=body.get("marks"),
            histories=body.get("histories"),
            benchmark_closes=body.get("benchmarkCloses"),
            confidence=float(body.get("confidence") or 0.95),
            actor_id=_actor(request, body),
        )
    except Exception as exc:  # noqa: BLE001 - a missing portfolio is a 404, not a 500
        raise _http_error(exc) from exc


@router.get("/portfolio/{portfolio_id}")
async def portfolio_get(portfolio_id: str, request: Request,
                        _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return await _engine().read(portfolio_id, actor_id=_actor(request))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.get("/portfolio/{portfolio_id}/audit")
async def portfolio_audit(portfolio_id: str, request: Request, limit: int = 200,
                          _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"events": _engine().audit_log(portfolio_id, limit=limit,
                                              actor_id=_actor(request))}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.get("/portfolio/{portfolio_id}/audit/verify")
async def portfolio_audit_verify(portfolio_id: str, request: Request,
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().verify_audit(portfolio_id, actor_id=_actor(request))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.patch("/portfolio/{portfolio_id}")
async def portfolio_update(portfolio_id: str, request: Request,
                           body: dict[str, Any] = Body(...),
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().update_fund(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/transaction")
async def portfolio_transaction(portfolio_id: str, request: Request,
                                body: dict[str, Any] = Body(...),
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().add_transaction(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/position")
async def portfolio_add_position(portfolio_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().add_position(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/position/remove")
async def portfolio_remove_position(portfolio_id: str, request: Request,
                                    body: dict[str, Any] = Body(...),
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().remove_position(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/position/update")
async def portfolio_update_position(portfolio_id: str, request: Request,
                                    body: dict[str, Any] = Body(...),
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().update_position(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/metadata")
async def portfolio_set_metadata(portfolio_id: str, request: Request,
                                 body: dict[str, Any] = Body(...),
                                 _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().set_position_metadata(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/corporate-action")
async def portfolio_corporate_action(portfolio_id: str, request: Request,
                                     body: dict[str, Any] = Body(...),
                                     _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().apply_corporate_action(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/swap")
async def portfolio_swap(portfolio_id: str, request: Request,
                         body: dict[str, Any] = Body(...),
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().apply_swap(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/bridge")
async def portfolio_bridge(portfolio_id: str, request: Request,
                           body: dict[str, Any] = Body(...),
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().apply_bridge(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/reward")
async def portfolio_reward(portfolio_id: str, request: Request,
                           body: dict[str, Any] = Body(...),
                           _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().record_reward(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/staking")
async def portfolio_staking(portfolio_id: str, request: Request,
                            body: dict[str, Any] = Body(...),
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().apply_staking(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/rebuild")
async def portfolio_rebuild(portfolio_id: str, request: Request,
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().rebuild_all(portfolio_id, actor_id=_actor(request))


@router.post("/portfolio/{portfolio_id}/slashing")
async def portfolio_slashing(portfolio_id: str, request: Request, body: dict[str, Any] = Body(...),
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().apply_slashing(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/funding")
async def portfolio_funding(portfolio_id: str, request: Request, body: dict[str, Any] = Body(...),
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().record_funding(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/reconcile")
async def portfolio_reconcile(portfolio_id: str, request: Request, body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().reconcile(portfolio_id, body, actor_id=_actor(request, body))


@router.get("/portfolio/{portfolio_id}/reconciliations")
async def portfolio_reconciliations(portfolio_id: str, request: Request, limit: int = 50,
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"reconciliations": _engine().list_reconciliations(portfolio_id, limit, actor_id=_actor(request))}


@router.post("/portfolio/{portfolio_id}/optimize")
async def portfolio_optimize(portfolio_id: str, request: Request, body: dict[str, Any] = Body(default={}),
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().optimize(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/attribution")
async def portfolio_attribution(portfolio_id: str, request: Request, body: dict[str, Any] = Body(default={}),
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().attribution(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/factor-exposure")
async def portfolio_factor_exposure(portfolio_id: str, request: Request,
                                    body: dict[str, Any] = Body(...),
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().factor_exposure(portfolio_id, body, actor_id=_actor(request, body))


@router.post("/portfolio/{portfolio_id}/hedge")
async def portfolio_hedge(portfolio_id: str, request: Request, body: dict[str, Any] = Body(default={}),
                          _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().hedge(portfolio_id, body, actor_id=_actor(request, body))


@router.get("/portfolio/{portfolio_id}/accounts")
async def portfolio_accounts(portfolio_id: str, request: Request,
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"accounts": _engine().list_accounts(portfolio_id, actor_id=_actor(request))}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/portfolio/{portfolio_id}/accounts")
async def portfolio_create_account_route(portfolio_id: str, request: Request,
                                         body: dict[str, Any] = Body(...),
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().create_account(
            portfolio_id, str(body.get("name") or ""),
            kind=str(body.get("kind") or "brokerage"),
            institution=body.get("institution"),
            currency=body.get("currency"),
            external_id=body.get("external_id") or body.get("externalId"),
            actor_id=_actor(request, body),
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.patch("/portfolio/{portfolio_id}/accounts/{account_id}")
async def portfolio_update_account_route(portfolio_id: str, account_id: str, request: Request,
                                         body: dict[str, Any] = Body(...),
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().update_account(portfolio_id, account_id, body,
                                        actor_id=_actor(request, body))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.delete("/portfolio/{portfolio_id}/accounts/{account_id}")
async def portfolio_delete_account_route(portfolio_id: str, account_id: str, request: Request,
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _engine().delete_account(portfolio_id, account_id, actor_id=_actor(request))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.get("/portfolio/{portfolio_id}/tax-lots")
async def portfolio_tax_lots(portfolio_id: str, request: Request, symbol: Optional[str] = None,
                             account_id: Optional[str] = None,
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().calculate_tax_lots(portfolio_id, symbol=symbol,
                                        account_id=account_id,
                                        persist=not bool(account_id),
                                        actor_id=_actor(request))


@router.get("/portfolio/{portfolio_id}/realized-gains")
async def portfolio_realized_gains_route(
    portfolio_id: str, request: Request,
    tax_year: Optional[int] = None, long_term_days: int = 365,
    wash_sale_window_days: int = 30, include_wash_sale: bool = True,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    try:
        return _engine().realized_gains(
            portfolio_id, tax_year=tax_year, long_term_days=long_term_days,
            wash_sale_window_days=wash_sale_window_days,
            include_wash_sale=include_wash_sale, actor_id=_actor(request),
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/portfolio/{portfolio_id}/rebalance")
async def portfolio_rebalance(portfolio_id: str, request: Request,
                              body: dict[str, Any] = Body(...),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().rebalance(portfolio_id, body, actor_id=_actor(request, body))


@router.get("/portfolio/{portfolio_id}/rebalance/plans")
async def portfolio_rebalance_plans(portfolio_id: str, request: Request, limit: int = 50,
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"plans": _engine().list_rebalance_plans(portfolio_id, limit, actor_id=_actor(request))}


@router.post("/portfolio/{portfolio_id}/snapshot")
async def portfolio_snapshot(portfolio_id: str, request: Request,
                             body: dict[str, Any] = Body(default={}),
                             _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().snapshot(portfolio_id, marks=body.get("marks"),
                                    created_by=body.get("createdBy"), actor_id=_actor(request, body))


@router.get("/portfolio/{portfolio_id}/snapshots")
async def portfolio_snapshots(portfolio_id: str, request: Request, limit: int = 365,
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"snapshots": _engine().list_snapshots(portfolio_id, limit, actor_id=_actor(request))}


@router.get("/portfolio/{portfolio_id}/history")
async def portfolio_history(portfolio_id: str, request: Request, limit: int = 200,
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _engine().history(portfolio_id, limit, actor_id=_actor(request))


# ── Construction & risk engine ──────────────────────────────────────────────

@router.post("/portfolio/{portfolio_id}/construct")
async def portfolio_construct(portfolio_id: str, request: Request,
                              body: dict[str, Any] = Body(default={}),
                              _: bool = Depends(require_operator)) -> dict[str, Any]:
    return await _engine().construct({**body, "portfolio_id": portfolio_id},
                                     actor_id=_actor(request, body))


@router.get("/portfolio/{portfolio_id}/construction-runs")
async def portfolio_construction_runs(portfolio_id: str, request: Request, limit: int = 50,
                                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"runs": _engine().store.list_construction_runs(portfolio_id, limit)}


@router.get("/portfolio/runs/{run_id}")
async def portfolio_get_run(run_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    run = _engine().store.get_construction_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"construction run {run_id} not found")
    return run


@router.post("/portfolio/estimate-inputs")
async def portfolio_estimate_inputs(body: dict[str, Any] = Body(default={}),
                                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.estimation import (align_returns, estimate_covariance,
                                        estimate_expected_returns)

    returns = body.get("returns")
    if returns is None and body.get("series"):
        aligned = align_returns(body["series"])
        if not aligned.get("available"):
            return {"available": False, "reason": aligned.get("reason")}
        returns = aligned["returns"]
    if not returns:
        return {"available": False, "reason": "supply `returns` or `series`"}
    return {
        "symbols": sorted(returns),
        "expectedReturns": estimate_expected_returns(
            returns, method=body.get("expected_returns_method", "arithmetic")),
        "covariance": estimate_covariance(
            returns, method=body.get("covariance_method", "sample"),
            annualize=bool(body.get("annualize", False))),
    }


@router.post("/portfolio/{portfolio_id}/stress-test")
async def portfolio_stress_test(portfolio_id: str, body: dict[str, Any] = Body(...),
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.risk_engine import stress_test

    return stress_test(body.get("weights") or {}, scenarios=body.get("scenarios") or [],
                       covariance=body.get("covariance"))


@router.post("/portfolio/{portfolio_id}/transaction-costs")
async def portfolio_transaction_costs(portfolio_id: str, body: dict[str, Any] = Body(default={}),
                                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.costs import transaction_costs, turnover

    to = turnover(body.get("current_weights") or {}, body.get("target_weights") or {})
    costs = transaction_costs(
        to["deltas"], commission_rate=float(body.get("commission_rate") or 0.0),
        half_spread=body.get("half_spread"), impact_coefficient=float(body.get("impact_coefficient") or 0.0),
        min_commission=float(body.get("min_commission") or 0.0), fx_rate=float(body.get("fx_rate") or 1.0))
    return {"turnover": to, "costs": costs}


@router.post("/portfolio/{portfolio_id}/compare")
async def portfolio_compare(portfolio_id: str, body: dict[str, Any] = Body(...),
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.construction import compare_allocations

    return compare_allocations(
        symbols=body.get("symbols") or [], covariance=body.get("covariance") or [],
        expected_returns=body.get("expected_returns"), methods=body.get("methods"),
        constraints_payload=body.get("constraints") or {}, benchmark_weights=body.get("benchmark_weights"),
        market_weights=body.get("market_weights"), risk_budgets=body.get("risk_budgets"),
        target_volatility=body.get("target_volatility"), risk_aversion=float(body.get("risk_aversion") or 1.0),
        views=body.get("views"), factor_loadings=body.get("factor_loadings"),
        factor_constraints=body.get("factor_constraints"))


@router.post("/portfolio/{portfolio_id}/simulate-rebalancing")
async def portfolio_simulate_rebalancing(portfolio_id: str, body: dict[str, Any] = Body(...),
                                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    if str(body.get("mode") or "").upper() == "BACKGROUND":
        from ..portfolio.fabric import submit_simulation

        return await submit_simulation(body)
    from ..portfolio.simulation import rebalancing_simulation

    return rebalancing_simulation(
        body.get("price_series") or {}, schedule=body.get("schedule") or "quarterly",
        method=body.get("method") or "risk_parity", constraints_payload=body.get("constraints") or {},
        lookback=int(body.get("lookback") or 252), initial_capital=float(body.get("initial_capital") or 1.0),
        cost_rate=float(body.get("cost_rate") or 0.0))


@router.get("/portfolio/simulations/{job_id}")
async def portfolio_simulation_status(job_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.fabric import get_simulation_job

    job = get_simulation_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"simulation job {job_id} not found")
    return job


@router.delete("/portfolio/simulations/{job_id}")
async def portfolio_simulation_cancel(job_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    from ..portfolio.fabric import cancel_simulation

    return await cancel_simulation(job_id)


def _http_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", None)
    if code == "PORTFOLIO_NOT_FOUND":
        return HTTPException(status_code=404, detail=getattr(exc, "message", str(exc)))
    if code == "PORTFOLIO_FORBIDDEN":
        return HTTPException(status_code=403, detail=getattr(exc, "message", str(exc)))
    if code == "ACCOUNT_NOT_FOUND":
        return HTTPException(status_code=404, detail=getattr(exc, "message", str(exc)))
    if code in ("ACCOUNT_EXISTS", "ACCOUNT_IN_USE"):
        return HTTPException(status_code=409, detail=getattr(exc, "message", str(exc)))
    if code == "INVALID_ACCOUNT":
        return HTTPException(status_code=422, detail=getattr(exc, "message", str(exc)))
    return HTTPException(status_code=400, detail=str(exc))


__all__ = ["router"]
