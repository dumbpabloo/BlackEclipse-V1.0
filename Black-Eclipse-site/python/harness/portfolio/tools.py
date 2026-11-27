"""Agent-facing portfolio tools.

Names use the runtime's flat `portfolio_*` convention (the ONE ToolRegistry
rejects dotted names). The mapping to the directive's conceptual names:

    portfolio.get             → portfolio_get
    portfolio.add_position    → portfolio_add_position
    portfolio.remove_position → portfolio_remove_position
    portfolio.update_position → portfolio_update_position
    portfolio.calculate_pnl   → portfolio_calculate_pnl
    portfolio.calculate_exposure → portfolio_calculate_exposure
    portfolio.calculate_allocation → portfolio_calculate_allocation
    portfolio.calculate_risk  → portfolio_calculate_risk
    portfolio.calculate_tax_lots → portfolio_calculate_tax_lots
    portfolio.rebalance       → portfolio_rebalance

Safety classification (§36):
  * read tools  — get / calculate_* / list. No effect.
  * mutating    — add/remove/update position, create portfolio, set metadata.
                  permission=write, effect=portfolio.write → requires approval.
  * consequential — rebalance. permission=write, effect=portfolio.rebalance_proposal;
                  it only PROPOSES (never executes).
"""

from __future__ import annotations

from typing import Any

from .errors import PortfolioError

READ = "read"
WRITE = "write"


def _service(ctx: Any) -> Any:
    service = getattr(ctx, "portfolio_service", None)
    if service is None:
        raise PortfolioError(
            "PORTFOLIO_UNAVAILABLE",
            "the portfolio engine is not available in this run",
        )
    return service


def _actor(ctx: Any) -> Any:
    return getattr(ctx, "user_id", None)


def _fail(error: Any) -> tuple[str, Any]:
    payload = error.to_dict() if isinstance(error, PortfolioError) else {
        "code": "PORTFOLIO_ERROR", "message": str(error), "details": {}, "retryable": False,
    }
    return f"Portfolio error ({payload['code']}): {payload['message']}", {
        "status": "FAILED", "error": payload,
    }


async def _run(fn: Any, *args: Any, **kwargs: Any) -> tuple[str, Any]:
    try:
        return await fn(*args, **kwargs)
    except PortfolioError as exc:
        return _fail(exc)


# ── Read tools ────────────────────────────────────────────────────────────────

async def portfolio_list(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.list_portfolios(owner_id=args.get("owner_id") or getattr(ctx, "user_id", None))
    lines = [f"{len(rows)} portfolio(s):"]
    for r in rows:
        lines.append(
            f"  - {r['id']}  {r['name']}  ({r.get('baseCurrency')}, {r.get('costBasisMethod')}, "
            f"{r.get('kind') or 'portfolio'})"
        )
    return "\n".join(lines), {"portfolios": rows}


async def portfolio_create(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)

    async def _do() -> tuple[str, Any]:
        # There is one user-facing portfolio type: a fund. The transaction
        # ledger is accounting substrate, never a separate "ledger portfolio".
        metadata: dict[str, Any] = {"kind": "fund"}
        for key in ("goal", "icon", "accent"):
            if args.get(key):
                metadata[key] = args[key]
        record = service.create_portfolio(
            args.get("name") or "Fund",
            base_currency=(args.get("base_currency") or "USD"),
            cost_basis_method=args.get("cost_basis_method") or "FIFO",
            description=args.get("description"),
            owner_id=getattr(ctx, "user_id", None),
            metadata=metadata,
            actor=args.get("created_by"),
        )
        return f"Created fund {record['id']} ({record['name']}).", {"status": "SUCCESS", "portfolio": record}

    return await _run(_do)


async def portfolio_get(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    include = args.get("include") or ["positions", "cash", "pnl", "allocation", "exposure"]
    try:
        reading = await service.read(
            args["portfolio_id"], include=include, gateway=getattr(ctx, "market_gateway", None),
            actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    val = reading["valuation"]
    label = "Fund" if reading["portfolio"].get("kind") == "fund" else "Portfolio"
    text = (
        f"{label} {reading['portfolio']['name']}: total {val['totalValue']} "
        f"{val['baseCurrency']} (cash {val['cash']}), {len(reading['positions'])} open position(s); "
        f"data quality {val['dataQuality']['status']}."
    )
    return text, reading


async def portfolio_audit_log(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        rows = service.audit_log(args["portfolio_id"], limit=int(args.get("limit") or 200),
                                 actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    return f"{len(rows)} portfolio audit event(s).", {"events": rows}


async def portfolio_audit_verify(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        result = service.verify_audit(args["portfolio_id"], actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    if result.get("ok"):
        summary = f"Audit chain verified ({result['checked']} event(s), head {result['head'][:12]}…)."
    elif result.get("status") == "UNSEALED":
        summary = (f"Audit chain has {result.get('unsealed', 0)} legacy unsealed event(s); "
                   "the sealed portion cannot be verified as a whole.")
    else:
        summary = f"Audit chain BROKEN at {result.get('brokenAt')} ({result.get('reason')})."
    return summary, result


async def portfolio_calculate_pnl(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        reading = await service.read(args["portfolio_id"], include=["pnl"],
                                     gateway=getattr(ctx, "market_gateway", None), actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    pnl = reading["pnl"]
    text = (
        f"P&L: realized {pnl['realizedPnl']}, unrealized {pnl['unrealizedPnl']}, "
        f"total {pnl['totalPnl']} {reading['portfolio']['baseCurrency']}."
    )
    return text, {"portfolio": reading["portfolio"], "pnl": pnl, "dataQuality": reading["dataQuality"]}


async def portfolio_calculate_exposure(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    dimension = args.get("dimension") or "assetClass"
    try:
        reading = await service.read(args["portfolio_id"], include=["exposure"],
                                     gateway=getattr(ctx, "market_gateway", None), actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    exposure = reading["exposure"].get(dimension) or reading["exposure"]["asset"]
    text = (
        f"{dimension} exposure: gross {exposure['grossExposure']}, net {exposure['netExposure']} "
        f"{exposure['baseCurrency']}."
    )
    return text, {"portfolio": reading["portfolio"], "exposure": reading["exposure"], "dimension": dimension}


async def portfolio_calculate_allocation(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    dimension = args.get("dimension") or "asset"
    try:
        reading = await service.read(args["portfolio_id"], include=["allocation"],
                                     gateway=getattr(ctx, "market_gateway", None), actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    allocation = reading["allocation"].get(dimension) or reading["allocation"]["asset"]
    text = f"{dimension} allocation across {len(allocation['breakdown'])} bucket(s); reconciles={allocation['reconciles']}."
    return text, {"portfolio": reading["portfolio"], "allocation": reading["allocation"], "dimension": dimension}


async def portfolio_harvest_candidates(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .harvest import harvest_candidates
    service = _service(ctx)
    try:
        reading = await service.read(
            args["portfolio_id"], include=["positions", "cash"],
            gateway=getattr(ctx, "market_gateway", None), actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    result = harvest_candidates(
        reading["positions"], threshold=args.get("threshold") or 0.0,
        base_currency=reading["portfolio"]["baseCurrency"], limit=args.get("limit"),
    )
    text = (
        f"{result['count']} tax-loss harvest candidate(s) with an unrealized loss of at "
        f"least {result['threshold']} {result['baseCurrency']}. {result['note']}"
    )
    return text, result


async def portfolio_calculate_risk(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        existing = service.get_portfolio_record(args["portfolio_id"], actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    if existing is None:
        return _fail(PortfolioError("PORTFOLIO_NOT_FOUND", f"portfolio {args['portfolio_id']} not found"))
    try:
        reading = await service.read(
            args["portfolio_id"], include=["risk"], gateway=getattr(ctx, "market_gateway", None),
            confidence=float(args.get("confidence") or 0.95), actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    risk = reading["risk"]
    metrics = risk.get("metrics", {})
    text = (
        f"Risk status {risk['status']}; volatility={metrics.get('volatility')}, "
        f"sharpe={metrics.get('sharpe')}, maxDD={metrics.get('maxDrawdown')}."
    )
    return text, {"portfolio": reading["portfolio"], "risk": risk}


async def portfolio_calculate_tax_lots(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        lots = service.calculate_tax_lots(args["portfolio_id"], symbol=args.get("symbol"),
                                          account_id=args.get("account_id"),
                                          actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    total = sum((len(s["openLots"]) for s in lots["symbols"]), 0)
    return (
        f"Tax lots ({lots['method']}): {total} open lot(s) across {len(lots['symbols'])} symbol(s).",
        lots,
    )


async def portfolio_realized_gains(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        report = service.realized_gains(
            args["portfolio_id"],
            tax_year=args.get("tax_year") or args.get("taxYear"),
            long_term_days=int(args.get("long_term_days") or args.get("longTermDays") or 365),
            wash_sale_window_days=int(
                args.get("wash_sale_window_days") or args.get("washSaleWindowDays") or 30),
            include_wash_sale=bool(args.get("include_wash_sale", True)),
            actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    candidates = sum(1 for row in report["disposals"] if row["washSaleSuspected"])
    text = (
        f"Realized gains ({report['method']}): {len(report['disposals'])} disposal(s) "
        f"across {len(report['years'])} tax-year/currency group(s); "
        f"{candidates} wash-sale candidate(s) reported, not applied."
    )
    return text, report


# ── Accounts (custody / cash separation) ─────────────────────────────────────

async def portfolio_create_account(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        account = service.create_account(
            args["portfolio_id"], str(args.get("name") or ""),
            kind=str(args.get("kind") or "brokerage"),
            institution=args.get("institution"),
            currency=args.get("currency"),
            external_id=args.get("external_id") or args.get("externalId"),
            actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    return (
        f"Created account {account['name']!r} ({account['id']}, {account['currency']}) "
        f"in portfolio {args['portfolio_id']}."
    ), account


async def portfolio_list_accounts(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        rows = service.list_accounts(args["portfolio_id"], actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    return f"{len(rows)} account(s).", {"accounts": rows}


async def portfolio_update_account(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        account = service.update_account(
            args["portfolio_id"], str(args["account_id"] or args.get("accountId") or ""),
            args, actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    return f"Updated account {account['id']}.", account


async def portfolio_delete_account(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        outcome = service.delete_account(
            args["portfolio_id"], str(args["account_id"] or args.get("accountId") or ""),
            actor_id=_actor(ctx),
        )
    except PortfolioError as exc:
        return _fail(exc)
    return (
        "Deleted the empty account." if outcome.get("deleted")
        else "No account was deleted."
    ), outcome


# ── Mutating tools ────────────────────────────────────────────────────────────

async def portfolio_add_position(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = {
        "type": args.get("type") or "BUY",
        "symbol": args["symbol"],
        "quantity": args["quantity"],
        "price": args.get("price"),
        "fees": args.get("fees"),
        "taxes": args.get("taxes"),
        "currency": args.get("currency"),
        "trade_date": args.get("trade_date"),
        "asset_type": args.get("asset_type"),
        "account_id": args.get("account_id"),
        "notes": args.get("notes"),
        "external_id": args.get("external_id"),
        "source": args.get("source") or "agent",
        "created_by": getattr(ctx, "user_id", None),
    }
    outcome = service.add_position(args["portfolio_id"], payload, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded {payload['type']} {args['quantity']} {args['symbol']}.", outcome


async def portfolio_remove_position(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = await service.remove_position(args["portfolio_id"], {
        "symbol": args["symbol"],
        "account_id": args.get("account_id"),
        "price": args.get("price"),
        "use_market_price": args.get("use_market_price", True),
        "reason": args.get("reason"),
        "source": args.get("source") or "agent",
        "created_by": getattr(ctx, "user_id", None),
    }, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Closed position {args['symbol']}.", outcome


async def portfolio_update_position(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.update_position(args["portfolio_id"], {
        "symbol": args["symbol"],
        "account_id": args.get("account_id"),
        "target_quantity": args["target_quantity"],
        "price": args.get("price"),
        "reason": args.get("reason"),
        "created_by": getattr(ctx, "user_id", None),
    }, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Corrected {args['symbol']} to quantity {args['target_quantity']}.", outcome


async def portfolio_set_metadata(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.set_position_metadata(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Updated metadata for {args['symbol']}.", outcome


async def portfolio_rebalance(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = await service.rebalance(args["portfolio_id"], {
        "target": args["target"],
        "allowed_drift": args.get("allowed_drift", 0.05),
        "method": args.get("method", "threshold"),
        "fee_rate": args.get("fee_rate", 0.0),
        "tax_rules": args.get("tax_rules"),
        "no_trade": args.get("no_trade"),
        "created_by": getattr(ctx, "user_id", None),
    }, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    n = len(outcome.get("recommendations", []))
    return (
        f"Rebalance proposal ({outcome['method']}) with {n} recommendation(s); "
        "no trades are executed — user approval is required.",
        outcome,
    )


async def portfolio_apply_corporate_action(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.apply_corporate_action(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Applied corporate action {outcome.get('action')}.", outcome


async def portfolio_record_swap(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.apply_swap(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded swap {outcome.get('fromSymbol')} -> {outcome.get('toSymbol')}.", outcome


async def portfolio_record_bridge(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.apply_bridge(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded bridge {outcome.get('fromSymbol')} -> {outcome.get('toSymbol')}.", outcome


async def portfolio_record_reward(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.record_reward(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded {args.get('type', 'STAKING_REWARD')} of {args.get('quantity')} {args.get('symbol')}.", outcome


async def portfolio_apply_staking(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.apply_staking(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded {args.get('action', 'stake')} for {args.get('symbol')}.", outcome


async def portfolio_rebuild(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.rebuild_all(args["portfolio_id"], actor_id=_actor(ctx))
    return f"Rebuilt {outcome['symbols']} materialized position(s).", outcome


async def portfolio_apply_slashing(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.apply_slashing(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded slashing of {args.get('quantity')} {args.get('symbol')}.", outcome


async def portfolio_record_funding(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.record_funding(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome))
    return f"Recorded funding {args.get('amount')}.", outcome


async def portfolio_reconcile(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.reconcile(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("status") == "FAILED" and outcome.get("error"):
        return _fail(_error_from(outcome))
    counts = outcome.get("counts", {})
    return (f"Reconciliation: {outcome.get('status')} "
            f"(matched {counts.get('MATCHED', 0)}, mismatch {counts.get('MISMATCH', 0)}, "
            f"missing {counts.get('MISSING', 0)}).", outcome)


async def portfolio_list_reconciliations(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        rows = service.list_reconciliations(args["portfolio_id"], args.get("limit", 50), actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    return f"{len(rows)} reconciliation run(s).", {"reconciliations": rows}


async def portfolio_optimize(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = service.optimize(args["portfolio_id"], args, actor_id=_actor(ctx))
    if outcome.get("available") is False or outcome.get("status") == "FAILED":
        return _fail(_error_from(outcome) if outcome.get("error") else PortfolioError("OPTIMIZE_UNAVAILABLE", outcome.get("reason", "optimization unavailable")))
    return (
        f"Optimizer ({outcome['method']}) produced weights for {len(outcome['weights'])} assets; "
        "proposal only — no trades placed.",
        outcome,
    )


async def portfolio_attribution(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    if args.get("periods"):
        from .attribution import multi_period_attribution

        outcome = multi_period_attribution(args["periods"], method=args.get("method", "fachler"))
        if not outcome.get("available"):
            return _fail(PortfolioError("ATTRIBUTION_UNAVAILABLE", outcome.get("reason", "unavailable")))
        return (f"Multi-period attribution active return {outcome['activeReturn']}, "
                f"reconciles={outcome['reconciles']}.", outcome)
    service = _service(ctx)
    try:
        outcome = await service.attribution(args["portfolio_id"], args, actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    if outcome.get("available") is False:
        return _fail(PortfolioError("ATTRIBUTION_UNAVAILABLE", outcome.get("reason", "unavailable")))
    effects = outcome.get("effects", {})
    return (f"Brinson attribution: allocation {effects.get('allocation')}, "
            f"selection {effects.get('selection')}, interaction {effects.get('interaction')}.",
            outcome)


async def portfolio_factor_exposure(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        outcome = await service.factor_exposure(args["portfolio_id"], args, actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    if outcome.get("available") is False:
        return _fail(PortfolioError("FACTOR_UNAVAILABLE", outcome.get("reason", "unavailable")))
    return f"Factor exposures: {outcome['exposures']}.", outcome


async def portfolio_hedge(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    try:
        outcome = await service.hedge(args["portfolio_id"], args, actor_id=_actor(ctx))
    except PortfolioError as exc:
        return _fail(exc)
    return f"{len(outcome['proposals'])} currency hedge proposal(s); proposal only.", outcome


async def portfolio_aggregate(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    outcome = await service.aggregate({**args, "owner_id": args.get("owner_id") or _actor(ctx)},
                                      actor_id=_actor(ctx))
    return f"Aggregate: {outcome['totalValue']} {outcome['baseCurrency']} across {outcome['portfolioCount']} portfolio(s).", outcome


async def portfolio_estimate_inputs(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    """Deterministic expected-return and covariance estimation from returns."""
    from .estimation import (align_returns, estimate_covariance,
                             estimate_expected_returns, estimate_factor_covariance)

    returns = args.get("returns")
    if returns is None and args.get("series"):
        aligned = align_returns(args["series"])
        if not aligned.get("available"):
            return (f"Estimation unavailable: {aligned.get('reason')}",
                    {"status": "UNAVAILABLE", "reason": aligned.get("reason")})
        returns = aligned["returns"]
        returns_symbols = aligned["symbols"]
    else:
        returns_symbols = sorted(returns or {})
    if not returns:
        return ("Estimation unavailable: supply per-symbol returns or dated price series.",
                {"status": "UNAVAILABLE", "reason": "no returns supplied"})
    er = estimate_expected_returns(
        returns, method=args.get("expected_returns_method", "arithmetic"),
        half_life=float(args.get("half_life") or 60.0), shrinkage=args.get("shrinkage"),
        user=args.get("user_expected_returns"))
    cov = estimate_covariance(
        returns, method=args.get("covariance_method", "sample"),
        half_life=float(args.get("half_life") or 60.0), shrinkage=args.get("shrinkage"),
        annualize=bool(args.get("annualize", False)))
    payload = {
        "symbols": sorted(returns), "expectedReturns": er, "covariance": cov,
        "diagnostics": {"expectedReturns": er.get("meta"), "covariance": cov.get("diagnostics"),
                        "warnings": cov.get("warnings")},
    }
    if args.get("factor_returns"):
        payload["factorModel"] = estimate_factor_covariance(
            returns, args["factor_returns"], annualize=bool(args.get("annualize", False)))
    text = (f"Estimated inputs for {len(returns_symbols) or len(returns)} assets: "
            f"expected returns ({er.get('method') or 'unavailable'}), covariance "
            f"({cov.get('method') or 'unavailable'}; PSD={cov.get('diagnostics', {}).get('positiveSemidefinite')}).")
    return text, payload


async def portfolio_construct(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    payload = dict(args)
    if ctx is not None and getattr(ctx, "market_gateway", None) is not None:
        service.gateway = service.gateway or getattr(ctx, "market_gateway")
    result = await service.construct(payload, actor_id=_actor(ctx))
    if result.get("available") is False:
        return (f"Construction unavailable ({result.get('status')}): {result.get('reason')}", result)
    if result.get("status") == "INFEASIBLE":
        return (f"Requested constraints are INFEASIBLE: {result.get('reason')}. "
                "No allocation was produced.", result)
    n = len(result.get("weights", {}))
    status = result.get("status")
    anchor = f"Construction ({result.get('method')}) -> {status}; {n} asset(s) allocated."
    if status == "OPTIMAL_INACCURATE":
        anchor += " Result failed independent validation of some constraint(s)."
    if not result.get("validation", {}).get("satisfied", True):
        failed = [c["name"] for c in result.get("validation", {}).get("violations", [])]
        anchor += f" Constraint issues: {failed}."
    anchor += " Proposal only — no trades placed."
    return anchor, result


async def portfolio_risk_contributions(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .risk_engine import risk_contributions as _rc

    weights = args.get("weights")
    if weights is None and args.get("portfolio_id"):
        service = _service(ctx)
        reading = await service.read(args["portfolio_id"], include=["positions"],
                                     gateway=getattr(ctx, "market_gateway", None), actor_id=_actor(ctx))
        weights = service._weight_map([
            {"symbol": p["symbol"], "marketValueBase": p.get("marketValueBase")} for p in reading["positions"]
        ])
    if not weights:
        return ("Risk contributions unavailable: supply weights or a portfolio.",
                {"available": False, "reason": "no weights"})
    result = _rc(weights, args["covariance"], names=args.get("names"), groups=args.get("groups"))
    if not result.get("available"):
        return (f"Risk contributions unavailable: {result.get('reason')}", result)
    top = sorted(result["riskContributions"].items(), key=lambda kv: -abs(kv[1]["component"]))[:3]
    text = (f"Portfolio volatility {result['portfolioVolatility']}; largest risk contributors: "
            + ", ".join(f"{k} ({v['component']})" for k, v in top)
            + f"; reconciles={result['reconciliation']['reconciles']}.")
    return text, result


async def portfolio_stress_test(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .risk_engine import stress_test as _stress

    weights = args.get("weights") or {}
    if not weights:
        return ("Stress test unavailable: supply weights.", {"available": False, "reason": "no weights"})
    result = _stress(weights, scenarios=args.get("scenarios") or [], covariance=args.get("covariance"))
    lines = []
    for s in result["scenarios"]:
        if s.get("type") == "monte_carlo":
            lines.append(f"{s['name']}: VaR {s.get('var')}, CVaR {s.get('cvar')}")
        elif s.get("available"):
            lines.append(f"{s['name']}: portfolio return {s.get('portfolioReturn', s.get('cumulativeReturn'))}")
        else:
            lines.append(f"{s['name']}: unavailable ({s.get('reason')})")
    return "Stress test: " + "; ".join(lines) + ".", result


async def portfolio_transaction_costs(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .costs import transaction_costs, turnover

    current = args.get("current_weights") or {}
    target = args.get("target_weights") or {}
    to = turnover(current, target)
    trades = to["deltas"]
    costs = transaction_costs(
        trades, commission_rate=float(args.get("commission_rate") or 0.0),
        half_spread=(float(args["half_spread"]) if args.get("half_spread") is not None else None),
        impact_coefficient=float(args.get("impact_coefficient") or 0.0),
        min_commission=float(args.get("min_commission") or 0.0),
        fx_rate=float(args.get("fx_rate") or 1.0),
    )
    return (f"Turnover (one-way) {to['oneWayTurnover']}; estimated cost "
            f"{costs['totalCostBps']} bps.", {"turnover": to, "costs": costs})


async def portfolio_compare_allocations(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .construction import compare_allocations as _compare

    covariance = args.get("covariance")
    if not args.get("symbols") or covariance is None:
        return ("Comparison unavailable: `symbols` and `covariance` are required.",
                {"available": False, "reason": "symbols and covariance are required"})
    result = _compare(
        symbols=args["symbols"], covariance=covariance,
        expected_returns=args.get("expected_returns"),
        methods=args.get("methods"),
        constraints_payload=args.get("constraints") or {},
        benchmark_weights=args.get("benchmark_weights"),
        market_weights=args.get("market_weights"),
        risk_budgets=args.get("risk_budgets"),
        target_volatility=args.get("target_volatility"),
        risk_aversion=float(args.get("risk_aversion") or 1.0),
        views=args.get("views"), factor_loadings=args.get("factor_loadings"),
        factor_constraints=args.get("factor_constraints"),
    )
    rows = result.get("comparison", [])
    text = "Compared " + ", ".join(f"{r['method']} ({r.get('status')})" for r in rows) + "."
    return text, result


async def portfolio_estimate_factor_loadings(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .estimation import estimate_factor_loadings

    result = estimate_factor_loadings(args.get("asset_returns") or {},
                                      args.get("factor_returns") or {})
    if not result.get("available"):
        return (f"Factor loadings unavailable: {result.get('reason')}", result)
    return (f"Estimated loadings for {len(result['loadings'])} asset(s) on "
            f"{len(result['factors'])} factor(s).", result)


async def portfolio_simulate_rebalancing(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    if str(args.get("mode") or "").upper() == "BACKGROUND":
        from .fabric import submit_simulation

        try:
            handle = await submit_simulation({
                "price_series": args.get("price_series"), "schedule": args.get("schedule"),
                "method": args.get("method"), "constraints": args.get("constraints"),
                "lookback": args.get("lookback"), "initial_capital": args.get("initial_capital"),
                "cost_rate": args.get("cost_rate"),
            })
            job_id = handle.get("jobId") or handle.get("id")
            return (f"Rebalancing simulation submitted as background job {job_id}.",
                    {"status": "SUBMITTED", "jobId": job_id, "job": handle})
        except RuntimeError as exc:  # fabric unavailable -> run inline, honestly
            if not args.get("price_series"):
                return (f"Simulation unavailable: {exc}", {"available": False, "reason": str(exc)})

    from .simulation import rebalancing_simulation

    result = rebalancing_simulation(
        args.get("price_series") or {}, schedule=args.get("schedule") or "quarterly",
        method=args.get("method") or "risk_parity",
        constraints_payload=args.get("constraints") or {},
        lookback=int(args.get("lookback") or 252),
        initial_capital=float(args.get("initial_capital") or 1.0),
        cost_rate=float(args.get("cost_rate") or 0.0),
    )
    if not result.get("available"):
        return (f"Rebalancing simulation unavailable: {result.get('reason')}", result)
    return (f"{result['schedule']} rebalancing ({result['method']}): total return "
            f"{result['totalReturn']}, vol {result['annualizedVolatility']}, "
            f"{result['rebalances']} rebalance(s), cost {result['totalCostFraction']}.", result)


async def portfolio_factor_risk(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    from .risk_engine import factor_risk as _factor_risk

    result = _factor_risk(args.get("weights") or {}, args.get("loadings") or {},
                          args.get("factor_covariance") or [], args.get("idiosyncratic") or {})
    if not result.get("available"):
        return (f"Factor risk unavailable: {result.get('reason')}", result)
    return (f"Portfolio volatility {result['portfolioVolatility']}; systematic share "
            f"{result['systematicShare']}.", result)


async def portfolio_list_runs(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    rows = service.store.list_construction_runs(args["portfolio_id"], args.get("limit", 50))
    return f"{len(rows)} construction run(s).", {"runs": rows}


async def portfolio_get_run(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    run = service.store.get_construction_run(args["run_id"])
    if run is None:
        return _fail(PortfolioError("RUN_NOT_FOUND", f"construction run {args['run_id']} not found"))
    return (f"Construction run {run['id']} ({run['method']}, {run['status']}).", run)


def _error_from(outcome: dict[str, Any]) -> PortfolioError:
    err = outcome.get("error") or {}
    return PortfolioError(err.get("code", "PORTFOLIO_ERROR"), err.get("message", "operation failed"),
                          err.get("details", {}))


# ── Registration ──────────────────────────────────────────────────────────────

def register_portfolio_tools(registry: Any) -> None:
    from ..tools import ToolDefinition

    def _def(name: str, description: str, handler: Any, schema: dict[str, Any],
             permission: str, effect: Any = None, timeout: int = 20_000) -> None:
        registry.register(ToolDefinition(
            name=name, description=description, input_schema=schema, handler=handler,
            permission=permission, risk="low" if permission == READ else "medium",
            timeout_ms=timeout, effect=effect,
        ))

    pid = {"portfolio_id": {"type": "string", "description": "Portfolio id."}}
    include = {"include": {"type": "array", "items": {"type": "string"},
                           "description": "Sections: positions, cash, pnl, allocation, exposure, risk, performance."}}

    _def("portfolio_list", "List portfolios for the current user.", portfolio_list,
         {"type": "object", "properties": {"owner_id": {"type": "string"}}}, READ)
    _def("portfolio_create_account",
         "Create a custody/cash account inside a fund (brokerage, bank, wallet, …). "
         "Transactions may carry `account_id`, so cash and lots are separable.",
         portfolio_create_account,
         {"type": "object", "properties": {
             **pid, "name": {"type": "string"}, "kind": {"type": "string"},
             "institution": {"type": "string"}, "currency": {"type": "string"},
             "external_id": {"type": "string"}}, "required": ["portfolio_id", "name"]},
         WRITE, "portfolio.write")
    _def("portfolio_list_accounts", "List a fund's custody/cash accounts.",
         portfolio_list_accounts,
         {"type": "object", "properties": {**pid}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_update_account", "Update an account's name/kind/institution/currency.",
         portfolio_update_account,
         {"type": "object", "properties": {
             **pid, "account_id": {"type": "string"}, "name": {"type": "string"},
             "kind": {"type": "string"}, "institution": {"type": "string"},
             "currency": {"type": "string"}, "external_id": {"type": "string"}},
          "required": ["portfolio_id", "account_id"]},
         WRITE, "portfolio.write")
    _def("portfolio_delete_account",
         "Delete an EMPTY account (refused with ACCOUNT_IN_USE if transactions reference it).",
         portfolio_delete_account,
         {"type": "object", "properties": {**pid, "account_id": {"type": "string"}},
          "required": ["portfolio_id", "account_id"]},
         WRITE, "portfolio.write")
    _def("portfolio_create",
         "Create a fund portfolio with its accounting ledger. This is the only "
         "user-facing portfolio type. `cost_basis_method` is FIFO, LIFO or AVERAGE_COST.",
         portfolio_create,
         {"type": "object", "properties": {
             "name": {"type": "string"}, "base_currency": {"type": "string"},
             "cost_basis_method": {"type": "string",
                                   "enum": ["FIFO", "LIFO", "AVERAGE_COST", "HIFO", "SPECIFIC_ID"]},
             "description": {"type": "string"},
             "goal": {"type": "string", "description": "Optional fund goal shown by the Fund view."},
             "icon": {"type": "string"}, "accent": {"type": "string"}},
          "required": ["name"]},
         WRITE, "portfolio.write")
    _def("portfolio_get",
         "Read a portfolio: positions, cash, valuation, P&L, allocation and exposure, "
         "with per-price freshness/data-quality. Prices come from the Market Data Gateway.",
         portfolio_get, {"type": "object", "properties": {**pid, **include}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_audit_log",
         "The portfolio's append-only audit trail (newest first), including each event's hash.",
         portfolio_audit_log,
         {"type": "object", "properties": {**pid, "limit": {"type": "integer"}}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_audit_verify",
         "Verify the portfolio audit trail's hash chain: OK, UNSEALED (legacy rows) or "
         "BROKEN at the first edited/reordered event.",
         portfolio_audit_verify,
         {"type": "object", "properties": {**pid}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_calculate_pnl",
         "Realized/unrealized/total P&L derived from the transaction ledger.",
         portfolio_calculate_pnl, {"type": "object", "properties": {**pid}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_calculate_exposure",
         "Gross/net exposure by asset, asset class, sector, currency or country.",
         portfolio_calculate_exposure,
         {"type": "object", "properties": {**pid, "dimension": {"type": "string"}}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_calculate_allocation",
         "Allocation weights by asset, asset class, sector, currency or country (weights reconcile to 100%).",
         portfolio_calculate_allocation,
         {"type": "object", "properties": {**pid, "dimension": {"type": "string"}}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_calculate_risk",
         "Deterministic risk: volatility, Sharpe, Sortino, max drawdown, VaR/CVaR (historical, "
         "parametric and seeded Monte-Carlo), beta and correlation. Needs price history; reports "
         "UNAVAILABLE rather than inventing numbers.",
         portfolio_calculate_risk,
         {"type": "object", "properties": {**pid, "confidence": {"type": "number"}}}, READ, timeout=40_000)
    _def("portfolio_calculate_tax_lots",
         "Open tax lots and realized lot matches under the portfolio's cost-basis method.",
         portfolio_calculate_tax_lots,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "account_id": {"type": "string"}},
          "required": ["portfolio_id"]},
         READ)
    _def("portfolio_realized_gains",
         "Realized gains grouped by tax year, currency and short/long term, with wash-sale "
         "CANDIDATES (a same-symbol acquisition in the window). It never applies a "
         "jurisdiction's rule or adjusts basis.",
         portfolio_realized_gains,
         {"type": "object", "properties": {
             **pid, "tax_year": {"type": "integer"},
             "long_term_days": {"type": "integer"},
             "wash_sale_window_days": {"type": "integer"},
             "include_wash_sale": {"type": "boolean"}},
          "required": ["portfolio_id"]},
         READ)
    _def("portfolio_harvest_candidates",
         "Positions with an unrealized loss beyond a threshold — the raw material for a "
         "tax-loss harvest. Proposal only; wash-sale rules are not modelled.",
         portfolio_harvest_candidates,
         {"type": "object", "properties": {
             **pid, "threshold": {"type": "number"},
             "limit": {"type": "integer"}}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_add_position",
         "Add a position by recording a BUY (or TRANSFER_IN) transaction; tax lots and the derived "
         "position are updated. Never a blind quantity UPDATE.",
         portfolio_add_position,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "quantity": {"type": "string"},
             "price": {"type": "string"}, "fees": {"type": "string"}, "taxes": {"type": "string"},
             "currency": {"type": "string"}, "trade_date": {"type": "string"},
             "type": {"type": "string", "enum": ["BUY", "TRANSFER_IN"]},
             "asset_type": {"type": "string"}, "external_id": {"type": "string"},
             "account_id": {"type": "string", "description": "Custody/cash account; omitting uses the unassigned bucket."},
             "notes": {"type": "string"}}, "required": ["portfolio_id", "symbol", "quantity"]},
         WRITE, "portfolio.write")
    _def("portfolio_remove_position",
         "Close a position by recording a real SELL disposal (tax lots matched, realized P&L).",
         portfolio_remove_position,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "price": {"type": "string"},
             "account_id": {"type": "string"},
             "use_market_price": {"type": "boolean"}, "reason": {"type": "string"}},
          "required": ["portfolio_id", "symbol"]},
         WRITE, "portfolio.write")
    _def("portfolio_update_position",
         "Correct a position quantity by appending an auditable ADJUSTMENT (history is never "
         "rewritten; a reason is required).",
         portfolio_update_position,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "target_quantity": {"type": "string"},
             "account_id": {"type": "string"},
             "price": {"type": "string"}, "reason": {"type": "string"}},
          "required": ["portfolio_id", "symbol", "target_quantity", "reason"]},
         WRITE, "portfolio.write")
    _def("portfolio_set_metadata",
         "Set descriptive metadata (name, asset class, sector, country, strategy, currency) used "
         "for exposure and allocation grouping.",
         portfolio_set_metadata,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "name": {"type": "string"},
             "asset_class": {"type": "string"}, "sector": {"type": "string"},
             "country": {"type": "string"}, "strategy": {"type": "string"},
             "currency": {"type": "string"}}, "required": ["portfolio_id", "symbol"]},
         WRITE, "portfolio.write")
    _def("portfolio_rebalance",
         "Propose a rebalance (threshold/calendar/hybrid) against target weights. Returns buy/sell "
         "recommendations, allocation before/after, estimated fees and tax impact. NEVER executes "
         "trades; execution requires user approval in the Trading layer.",
         portfolio_rebalance,
         {"type": "object", "properties": {
             **pid, "target": {"type": "object",
                               "description": "Map of symbol → target weight (fraction or percent)."},
             "allowed_drift": {"type": "number"}, "method": {"type": "string"},
             "fee_rate": {"type": "number"}, "tax_rules": {"type": "object"},
             "no_trade": {"type": "array", "items": {"type": "string"}}},
          "required": ["portfolio_id", "target"]},
         WRITE, "portfolio.rebalance_proposal")
    _def("portfolio_apply_corporate_action",
         "Apply an auditable corporate action as real ledger entries: SPLIT, REVERSE_SPLIT, "
         "SPIN_OFF (reallocates parent basis to the child), MERGER (stock ratio + optional cash "
         "consideration) or SYMBOL_CHANGE. History is never rewritten.",
         portfolio_apply_corporate_action,
         {"type": "object", "properties": {
             **pid,
             "action": {"type": "string", "enum": ["SPLIT", "REVERSE_SPLIT", "SPIN_OFF", "MERGER", "SYMBOL_CHANGE"]},
             "symbol": {"type": "string"}, "ratio": {"type": "string"},
             "parent_symbol": {"type": "string"}, "child_symbol": {"type": "string"},
             "child_quantity": {"type": "string"}, "child_shares_per_parent": {"type": "string"},
             "child_price": {"type": "string"}, "parent_price": {"type": "string"},
             "acquired_symbol": {"type": "string"}, "acquirer_symbol": {"type": "string"},
             "exchange_ratio": {"type": "string"}, "acquirer_price": {"type": "string"},
             "cash_per_share": {"type": "string"}, "old_symbol": {"type": "string"},
             "new_symbol": {"type": "string"}, "trade_date": {"type": "string"},
             "reason": {"type": "string"}}, "required": ["portfolio_id", "action"]},
         WRITE, "portfolio.write")
    _def("portfolio_record_swap",
         "Record a crypto swap: a taxable disposal of from_symbol plus an acquisition of "
         "to_symbol at fair value (basis carried = net proceeds).",
         portfolio_record_swap,
         {"type": "object", "properties": {
             **pid, "from_symbol": {"type": "string"}, "to_symbol": {"type": "string"},
             "from_quantity": {"type": "string"}, "from_price": {"type": "string"},
             "to_quantity": {"type": "string"}, "to_price": {"type": "string"},
             "fees": {"type": "string"}, "currency": {"type": "string"}},
          "required": ["portfolio_id", "from_symbol", "to_symbol", "from_quantity", "from_price"]},
         WRITE, "portfolio.write")
    _def("portfolio_record_bridge",
         "Record a cross-chain bridge: an in-kind transfer where cost basis carries and no "
         "gain/loss is realized.",
         portfolio_record_bridge,
         {"type": "object", "properties": {
             **pid, "from_symbol": {"type": "string"}, "to_symbol": {"type": "string"},
             "quantity": {"type": "string"}, "to_quantity": {"type": "string"}},
          "required": ["portfolio_id", "from_symbol", "to_symbol", "quantity"]},
         WRITE, "portfolio.write")
    _def("portfolio_record_reward",
         "Record a staking reward or airdrop as an acquisition at fair market value.",
         portfolio_record_reward,
         {"type": "object", "properties": {
             **pid, "type": {"type": "string", "enum": ["STAKING_REWARD", "AIRDROP"]},
             "symbol": {"type": "string"}, "quantity": {"type": "string"},
             "price": {"type": "string"}, "currency": {"type": "string"},
             "trade_date": {"type": "string"}, "notes": {"type": "string"}},
          "required": ["portfolio_id", "symbol", "quantity"]},
         WRITE, "portfolio.write")
    _def("portfolio_apply_staking",
         "Record a staking lock/unlock. Custody-only: the position and its basis are unchanged.",
         portfolio_apply_staking,
         {"type": "object", "properties": {
             **pid, "action": {"type": "string", "enum": ["stake", "unstake"]},
             "symbol": {"type": "string"}, "quantity": {"type": "string"}, "notes": {"type": "string"}},
          "required": ["portfolio_id", "symbol", "quantity"]},
         WRITE, "portfolio.write")
    _def("portfolio_rebuild",
         "Recompute and persist all derived lots and materialized positions for a portfolio "
         "(self-heal after an out-of-band change).",
         portfolio_rebuild,
         {"type": "object", "properties": {**pid}, "required": ["portfolio_id"]},
         WRITE, "portfolio.write")
    _def("portfolio_apply_slashing",
         "Write off slashed/written-off tokens (a disposal at zero proceeds, realizing a loss).",
         portfolio_apply_slashing,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "quantity": {"type": "string"},
             "notes": {"type": "string"}, "trade_date": {"type": "string"}},
          "required": ["portfolio_id", "symbol", "quantity"]},
         WRITE, "portfolio.write")
    _def("portfolio_record_funding",
         "Record a perpetual-futures funding payment. Signed: positive = received, negative = paid.",
         portfolio_record_funding,
         {"type": "object", "properties": {
             **pid, "symbol": {"type": "string"}, "amount": {"type": "string"},
             "currency": {"type": "string"}, "trade_date": {"type": "string"}},
          "required": ["portfolio_id", "amount"]},
         WRITE, "portfolio.write")
    _def("portfolio_reconcile",
         "Reconcile the ledger against an external statement (broker/exchange/wallet/manual). "
         "Returns MATCHED/MISMATCH/MISSING/UNKNOWN per symbol; never mutates the ledger.",
         portfolio_reconcile,
         {"type": "object", "properties": {
             **pid, "external_positions": {"type": "array", "items": {"type": "object"}},
             "source": {"type": "string"}, "tolerance": {"type": "string"},
             "relative_tolerance": {"type": "number"}, "as_of": {"type": "string"}},
          "required": ["portfolio_id", "external_positions"]},
         READ)
    _def("portfolio_list_reconciliations",
         "List saved reconciliation runs for a portfolio.",
         portfolio_list_reconciliations,
         {"type": "object", "properties": {**pid, "limit": {"type": "integer"}}, "required": ["portfolio_id"]},
         READ)
    _def("portfolio_optimize",
         "Constrained portfolio optimizer (min_variance / mean_variance / inverse_volatility) with "
         "long-only, per-asset bounds and an optional turnover cap. Proposal only.",
         portfolio_optimize,
         {"type": "object", "properties": {
             **pid, "symbols": {"type": "array", "items": {"type": "string"}},
             "expected_returns": {"type": "array", "items": {"type": "number"}},
             "covariance": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
             "min_weights": {"type": "array", "items": {"type": "number"}},
             "max_weights": {"type": "array", "items": {"type": "number"}},
             "risk_aversion": {"type": "number"}, "turnover_cap": {"type": "number"},
             "method": {"type": "string", "enum": ["min_variance", "mean_variance", "inverse_volatility"]}},
          "required": ["portfolio_id"]},
         WRITE, "portfolio.rebalance_proposal")
    _def("portfolio_attribution",
         "Brinson single-period performance attribution (allocation/selection/interaction) by group.",
         portfolio_attribution,
         {"type": "object", "properties": {
             **pid, "dimension": {"type": "string"},
             "segments": {"type": "array", "items": {"type": "object"}},
             "periods": {"type": "array", "items": {"type": "object"},
                         "description": "Multi-period: [{name, segments:[...]}] (Carino-linked)."},
             "benchmark_weights": {"type": "object"}, "portfolio_returns": {"type": "object"},
             "benchmark_returns": {"type": "object"},
             "method": {"type": "string", "enum": ["fachler", "bhb"]}},
           "required": []},
         READ)
    _def("portfolio_factor_exposure",
         "Portfolio exposure to named factors given caller-supplied per-symbol factor loadings.",
         portfolio_factor_exposure,
         {"type": "object", "properties": {**pid, "loadings": {"type": "object"}},
          "required": ["portfolio_id", "loadings"]},
         READ)
    _def("portfolio_hedge",
         "Propose FX hedges to flatten non-base currency exposure. Proposal only.",
         portfolio_hedge,
         {"type": "object", "properties": {
             **pid, "target_flat": {"type": "boolean"},
             "target_currency_weights": {"type": "object"}},
          "required": ["portfolio_id"]},
         READ)
    _def("portfolio_aggregate",
         "Aggregate several portfolios (by id or owner) into one combined view.",
         portfolio_aggregate,
         {"type": "object", "properties": {
             "portfolio_ids": {"type": "array", "items": {"type": "string"}},
             "owner_id": {"type": "string"}}},
         READ)
    _def("portfolio_estimate_inputs",
         "Deterministic estimation layer: expected returns (arithmetic/geometric/ewma/shrinkage/"
         "user) and covariance (sample/ewma/shrinkage/diagonal) with PSD/symmetry/condition "
         "diagnostics. Accepts per-symbol `returns` or dated `series`.",
         portfolio_estimate_inputs,
         {"type": "object", "properties": {
             "returns": {"type": "object", "description": "symbol -> list of per-period returns"},
             "series": {"type": "object", "description": "symbol -> [{date,timestamp,close,value}]"},
             "covariance_method": {"type": "string", "enum": ["sample", "ewma", "shrinkage", "diagonal"]},
             "expected_returns_method": {"type": "string",
                                         "enum": ["arithmetic", "geometric", "ewma", "shrinkage", "user"]},
             "half_life": {"type": "number"}, "shrinkage": {"type": "number"},
             "annualize": {"type": "boolean"},
             "factor_returns": {"type": "object",
                                "description": "Factor return series → adds a factor covariance model."},
             "user_expected_returns": {"type": "object"}}},
         READ)
    _def("portfolio_construct",
         "Portfolio construction engine. Methods: equal_weight, benchmark_weight, min_variance, "
         "mean_variance, risk_parity, volatility_target, black_litterman, factor_constrained, "
         "efficient_frontier. Enforces hard constraints (budget, long-only, per-asset bounds, "
         "sector/group caps, factor caps, turnover) and INDEPENDENTLY validates the result. "
         "Supply covariance/expected_returns or returns/symbols (estimated via the gateway). "
         "Proposal only — never places trades.",
         portfolio_construct,
         {"type": "object", "properties": {
             "portfolio_id": {"type": "string"},
             "symbols": {"type": "array", "items": {"type": "string"}},
             "method": {"type": "string", "enum": [
                 "equal_weight", "benchmark_weight", "min_variance", "mean_variance",
                 "risk_parity", "volatility_target", "black_litterman", "factor_constrained",
                 "efficient_frontier"]},
             "covariance": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
             "expected_returns": {"type": "array", "items": {"type": "number"}},
             "returns": {"type": "object"}, "covariance_method": {"type": "string"},
             "expected_returns_method": {"type": "string"},
             "constraints": {"type": "object"},
             "min_weights": {"type": "object"}, "max_weights": {"type": "object"},
             "groups": {"type": "array", "items": {"type": "object"}},
             "group_caps": {"type": "object"}, "group_membership": {"type": "object"},
             "factor_loadings": {"type": "object"},
             "factor_constraints": {"type": "array", "items": {"type": "object"}},
             "benchmark_weights": {"type": "object"}, "market_weights": {"type": "array",
                                                                        "items": {"type": "number"}},
             "risk_budgets": {"type": "array", "items": {"type": "number"}},
             "risk_aversion": {"type": "number"}, "target_volatility": {"type": "number"},
             "tau": {"type": "number"}, "views": {"type": "array", "items": {"type": "object"}},
             "turnover_cap": {"type": "number"}, "current_weights": {"type": "object"},
             "annualize": {"type": "boolean"}},
          "required": ["method"]},
         WRITE, "portfolio.rebalance_proposal", timeout=40_000)
    _def("portfolio_risk_contributions",
         "Euler risk decomposition of a portfolio's volatility across holdings and optional "
         "groups; reconciles Σ components to portfolio volatility. Preserves short-position signs.",
         portfolio_risk_contributions,
         {"type": "object", "properties": {
             "weights": {"type": "object"}, "portfolio_id": {"type": "string"},
             "names": {"type": "array", "items": {"type": "string"}},
             "groups": {"type": "object"},
             "covariance": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}},
          "required": ["covariance"]},
         READ)
    _def("portfolio_factor_risk",
         "Decompose portfolio risk into systematic (factor) and idiosyncratic parts from a factor "
         "covariance model (Σ = B·F·Bᵀ + D), with factor risk contributions. Avoids double-counting.",
         portfolio_factor_risk,
         {"type": "object", "properties": {
             "weights": {"type": "object"}, "loadings": {"type": "object"},
             "factor_covariance": {"type": "array", "items": {"type": "array",
                                                              "items": {"type": "number"}}},
             "idiosyncratic": {"type": "object"}},
          "required": ["weights", "loadings", "factor_covariance", "idiosyncratic"]},
         READ)
    _def("portfolio_stress_test",
         "Scenario/stress testing: hypothetical symbol/factor shocks, historical window replay "
         "and seeded Monte-Carlo (VaR/CVaR). Names assumptions and limitations.",
         portfolio_stress_test,
         {"type": "object", "properties": {
             "weights": {"type": "object"}, "covariance": {"type": "array",
                                                           "items": {"type": "array",
                                                                     "items": {"type": "number"}}},
             "scenarios": {"type": "array", "items": {"type": "object"}}},
          "required": ["weights", "scenarios"]},
         READ, timeout=40_000)
    _def("portfolio_transaction_costs",
         "Turnover (one-way and gross) and a configurable transaction-cost estimate "
         "(commission, half-spread, impact, FX) for a rebalance.",
         portfolio_transaction_costs,
         {"type": "object", "properties": {
             "current_weights": {"type": "object"}, "target_weights": {"type": "object"},
             "commission_rate": {"type": "number"},
             "half_spread": {"type": "number"}, "impact_coefficient": {"type": "number"},
             "min_commission": {"type": "number"}, "fx_rate": {"type": "number"}}},
         READ)
    _def("portfolio_compare_allocations",
         "Run several construction methods over the SAME inputs and compare expected return, "
         "volatility, concentration, turnover and constraint satisfaction. Does not pick a winner.",
         portfolio_compare_allocations,
         {"type": "object", "properties": {
             "symbols": {"type": "array", "items": {"type": "string"}},
             "covariance": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
             "expected_returns": {"type": "array", "items": {"type": "number"}},
             "methods": {"type": "array", "items": {"type": "string"}},
             "constraints": {"type": "object"}, "benchmark_weights": {"type": "object"},
             "market_weights": {"type": "array", "items": {"type": "number"}},
             "risk_budgets": {"type": "array", "items": {"type": "number"}},
             "target_volatility": {"type": "number"}, "risk_aversion": {"type": "number"},
             "views": {"type": "array", "items": {"type": "object"}},
             "factor_loadings": {"type": "object"},
             "factor_constraints": {"type": "array", "items": {"type": "object"}}},
          "required": ["symbols", "covariance"]},
         READ, timeout=40_000)
    _def("portfolio_estimate_factor_loadings",
         "Estimate per-asset factor loadings by OLS from asset and factor return series "
         "(loadings, alpha, residual variance, R²).",
         portfolio_estimate_factor_loadings,
         {"type": "object", "properties": {
             "asset_returns": {"type": "object"}, "factor_returns": {"type": "object"}},
          "required": ["asset_returns", "factor_returns"]},
         READ)
    _def("portfolio_simulate_rebalancing",
         "Point-in-time rebalancing / walk-forward simulation: re-estimates inputs from the "
         "trailing window at each scheduled rebalance (monthly/quarterly/semiannual/annual), "
         "applies turnover and costs, drifts holdings, and reports net performance and risk. "
         "No look-ahead.",
         portfolio_simulate_rebalancing,
         {"type": "object", "properties": {
             "price_series": {"type": "object"}, "schedule": {"type": "string"},
             "method": {"type": "string"}, "constraints": {"type": "object"},
             "lookback": {"type": "integer"}, "initial_capital": {"type": "number"},
             "cost_rate": {"type": "number"},
             "mode": {"type": "string", "enum": ["inline", "BACKGROUND"]}},
          "required": ["price_series"]},
         READ, timeout=60_000)
    _def("portfolio_list_runs",
         "List immutable construction/optimization runs for a portfolio (method, status, "
         "target, input data version).",
         portfolio_list_runs,
         {"type": "object", "properties": {**pid, "limit": {"type": "integer"}},
          "required": ["portfolio_id"]},
         READ)
    _def("portfolio_get_run",
         "Retrieve one immutable construction run by id (exact inputs, constraints, validated "
         "target weights, reproducibility metadata).",
         portfolio_get_run,
         {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
         READ)


PORTFOLIO_READ_TOOLS = [
    "portfolio_list", "portfolio_get", "portfolio_list_accounts",
    "portfolio_audit_log", "portfolio_audit_verify",
    "portfolio_calculate_pnl", "portfolio_calculate_exposure",
    "portfolio_calculate_allocation", "portfolio_calculate_risk", "portfolio_calculate_tax_lots",
    "portfolio_realized_gains",
    "portfolio_reconcile", "portfolio_list_reconciliations", "portfolio_attribution",
    "portfolio_factor_exposure", "portfolio_hedge", "portfolio_aggregate",
    "portfolio_estimate_inputs", "portfolio_risk_contributions", "portfolio_stress_test",
    "portfolio_transaction_costs", "portfolio_compare_allocations",
    "portfolio_estimate_factor_loadings", "portfolio_simulate_rebalancing",
    "portfolio_list_runs", "portfolio_get_run", "portfolio_factor_risk",
    "portfolio_harvest_candidates",
]
PORTFOLIO_WRITE_TOOLS = [
    "portfolio_create", "portfolio_create_account", "portfolio_update_account",
    "portfolio_delete_account",
    "portfolio_add_position", "portfolio_remove_position",
    "portfolio_update_position", "portfolio_set_metadata",
    "portfolio_apply_corporate_action", "portfolio_record_swap", "portfolio_record_bridge",
    "portfolio_record_reward", "portfolio_apply_staking", "portfolio_rebuild",
    "portfolio_apply_slashing", "portfolio_record_funding",
]
PORTFOLIO_CONSEQUENTIAL_TOOLS = ["portfolio_rebalance", "portfolio_optimize", "portfolio_construct"]
PORTFOLIO_TOOLS = PORTFOLIO_READ_TOOLS + PORTFOLIO_WRITE_TOOLS + PORTFOLIO_CONSEQUENTIAL_TOOLS


__all__ = ["register_portfolio_tools", "PORTFOLIO_TOOLS", "PORTFOLIO_READ_TOOLS",
           "PORTFOLIO_WRITE_TOOLS", "PORTFOLIO_CONSEQUENTIAL_TOOLS"]
