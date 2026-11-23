"""The strategy-bot runner.

One automation execution = one decision on the last closed bar. The runner owns
the parts the quant engine cannot know about (bot identity, consent, session,
freshness, multi-symbol selection) and delegates the ORDER to the existing
`LiveStrategyService.run_once` -> `BrokerService.submit_order` path. There is no
second order path here and no agent loop.

Every branch returns a `DecisionRecord`, including "nothing happened". A silent
run is a bug: the decision is persisted on the execution and is what the chat and
the trading panel explain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import trading_policy as policy

COMPLETED = "completed"
SKIPPED = "skipped"
BLOCKED = "blocked"
FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authority(definition: Any) -> Any:
    return getattr(definition, "execution_authority", None)


def _tier(definition: Any) -> str:
    return str(getattr(_authority(definition), "tier", "l0") or "l0")


def _config(definition: Any) -> dict[str, Any]:
    action = getattr(definition, "action", None)
    config = getattr(action, "config", None)
    return config if isinstance(config, dict) else {}


def _consent(definition: Any) -> Any:
    return _config(definition).get("consent")


def _store(service: Any) -> Any:
    return getattr(service, "quant_store", None)


def _live(service: Any) -> Any:
    # Injected for tests and for a runtime that already holds one service.
    existing = getattr(service, "live_strategy", None)
    if existing is not None:
        return existing
    from ..quant.live import LiveStrategyService  # noqa: PLC0415

    store = _store(service)
    if store is None:
        raise RuntimeError("the strategy store is not installed in this runtime")
    return LiveStrategyService(store)


def _broker(service: Any) -> Any:
    return getattr(service, "brokers", None)


def _decision(definition: Any, *, strategy_id: str, version: str, **kwargs: Any) -> dict[str, Any]:
    config = _config(definition)
    return policy.decision(
        bot_id=str(getattr(definition, "id", "")),
        bot_version=int(getattr(definition, "version", 1) or 1),
        strategy_id=strategy_id,
        strategy_version=version,
        mode=str(config.get("mode") or "shadow"),
        tier=_tier(definition),
        **kwargs,
    )


def _finish(state: str, decision: dict[str, Any], *, error: str | None = None,
            error_class: str | None = None) -> Any:
    from .dispatcher import DispatchResult  # noqa: PLC0415 - avoids an import cycle

    line = policy.format_decision_line(decision)
    reason = decision.get("reason") or ""
    output = f"{line}: {reason}" if reason else line
    return DispatchResult(
        state=state,
        output=output,
        error=error,
        error_class=error_class,
        detail={"decision": decision},
    )


async def run_strategy(service: Any, definition: Any, *, run_id: str,
                       execution_id: str) -> Any:
    """Entry point called by the dispatcher for `action.type == 'run_strategy'`."""
    config = _config(definition)
    strategy_id = str(config.get("strategyId") or "").strip()
    version = str(config.get("strategyVersion") or "").strip()
    mode = str(config.get("mode") or "shadow").strip().lower()
    symbols = policy.normalize_symbols(
        config.get("symbols") or [], limit=config.get("maxSymbols"),
    )
    authority = _authority(definition)

    # ── Preparation ───────────────────────────────────────────────────────────
    store = _store(service)
    if store is None:
        return _finish(FAILED, _decision(definition, strategy_id=strategy_id,
                                         version=version, reason="strategy store unavailable",
                                         outcome="failed", code="RUNTIME_UNAVAILABLE"),
                       error="the strategy store is not installed", error_class="RUNTIME_UNAVAILABLE")

    strategy = None
    try:
        strategy = store.get_strategy(strategy_id, version)
    except Exception:  # noqa: BLE001
        strategy = None
    if strategy is None:
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason="the pinned strategy version no longer exists",
            outcome="failed", code="STRATEGY_UNAVAILABLE"))

    if mode not in policy.MODES:
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason=f"unknown mode {mode!r}", outcome="refused",
            code="INVALID_TRADING_MODE"))

    if not symbols:
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason="no symbols to scan", outcome="refused", code="SYMBOLS_REQUIRED"))

    dry_run = bool(config.get("dryRun"))
    tier = _tier(definition)
    if mode == "live" and tier != "l2":
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason="live execution needs tier L2", outcome="refused",
            code="BOT_TIER_REQUIRED"))
    if mode == "demo" and not dry_run and tier == "l0":
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason="tier L0 only proposes; raise it to L1 to fill demo",
            outcome="refused", code="BOT_TIER_REQUIRED"))

    if mode != "shadow" and not dry_run:
        request = policy.consent_request_for(config, {
            "maxMarginPerOrder": getattr(authority, "max_margin_per_order", 0),
            "maxOpenPositions": getattr(authority, "max_open_positions", 1),
        })
        check = policy.consent_covers(_consent(definition), request)
        if not check["covered"]:
            code = {"revoked": "CONSENT_REVOKED", "expired": "CONSENT_EXPIRED"}.get(
                check["status"], "CONSENT_REQUIRED")
            return _finish(BLOCKED, _decision(
                definition, strategy_id=strategy_id, version=version,
                reason=check["reason"] or "standing consent is required",
                outcome="refused", code=code))

    session = config.get("session")
    state = policy.session_state(_now(), session if isinstance(session, dict) else None)
    if not state["open"]:
        return _finish(SKIPPED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason=state["reason"] or "outside the session window",
            outcome="skipped", code="SESSION_CLOSED"))

    trial_id = str(config.get("trialId") or "").strip()
    if not trial_id:
        return _finish(BLOCKED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason="trading was never prepared for this bot",
            outcome="refused", code="TRADING_NOT_PREPARED"))

    live = _live(service)
    try:
        trial = live.get(trial_id)
    except Exception as exc:  # noqa: BLE001
        return _finish(FAILED, _decision(
            definition, strategy_id=strategy_id, version=version,
            reason=str(exc), outcome="failed", code="TRADING_NOT_PREPARED"),
            error=str(exc), error_class="TRADING_NOT_PREPARED")

    # A mandate/strategy pin that went stale is a refusal, not a retry.
    pinned_bot = trial.get("botId")
    if pinned_bot and str(pinned_bot) == str(getattr(definition, "id", "")):
        pinned_version = trial.get("botVersion")
        if pinned_version is not None and int(pinned_version) != int(getattr(definition, "version", 0) or 0):
            return _finish(BLOCKED, _decision(
                definition, strategy_id=strategy_id, version=version,
                reason="this bot changed since its trading authority was granted",
                outcome="refused", code="BOT_VERSION_STALE"))

    gateway = None
    try:
        gateway = service.server_market_gateway()
    except Exception:  # noqa: BLE001 - a missing gateway is reported by the signal
        gateway = None

    # ── Multi-symbol scan ─────────────────────────────────────────────────────
    # `dynamicSymbols` means "scan the whole universe and take the best signal
    # each bar". Without it the bot is pinned to its first symbol, which is the
    # predictable choice for a single-instrument strategy.
    scan = symbols if bool(config.get("dynamicSymbols")) else symbols[:1]
    evaluations: list[dict[str, Any]] = []
    for symbol in scan:
        try:
            reading = await live.signal(trial_id, gateway=gateway, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the scan
            evaluations.append({"symbol": symbol, "signal": "none", "score": 0.0,
                                "reason": f"signal failed: {exc}", "bar": None})
            continue
        evaluations.append({
            "symbol": symbol,
            "signal": str(reading.get("signal") or "none"),
            "score": 1.0 if str(reading.get("signal") or "").endswith("_entry") else 0.5,
            "reason": str(reading.get("reason") or ""),
            "bar": reading.get("bar"),
            "indicators": reading.get("indicators") or {},
        })

    chosen = _choose(evaluations)
    if chosen is None:
        freshest = _freshest_bar(evaluations)
        if freshest and not policy.is_fresh(
            freshest, _now(), policy.timeframe_seconds(config.get("timeframe")),
        ):
            return _finish(SKIPPED, _decision(
                definition, strategy_id=strategy_id, version=version,
                symbol=symbols[0], bar_ts=freshest,
                reason="the last closed bar is too old to trade",
                outcome="skipped", code="STALE_MARKET_DATA"))
        failed = [
            f"{row.get('symbol')}: {row.get('reason')}"
            for row in evaluations
            if str(row.get("signal") or "") == "none" and row.get("reason")
        ]
        detail = f" ({failed[0]})" if failed else ""
        return _finish(COMPLETED, _decision(
            definition, strategy_id=strategy_id, version=version,
            symbol=symbols[0], bar_ts=freshest,
            reason=f"scanned {len(scan)} symbol(s): no actionable signal{detail}",
            indicators=evaluations[0].get("indicators") or {},
            outcome="none", code=None))

    chosen_bar = chosen.get("bar")
    if chosen_bar and not policy.is_fresh(
        chosen_bar, _now(), policy.timeframe_seconds(config.get("timeframe")),
    ):
        return _finish(SKIPPED, _decision(
            definition, strategy_id=strategy_id, version=version,
            symbol=chosen["symbol"], bar_ts=chosen_bar,
            reason="the last closed bar is too old to trade",
            outcome="skipped", code="STALE_MARKET_DATA"))

    # ── Execute through the EXISTING path ─────────────────────────────────────
    # Shadow (or a bot with no venue) never touches the broker: record the exact
    # decision it WOULD have made, with the size the band would have used.
    if mode == "shadow" or not str(config.get("connectionId") or "").strip():
        return _finish(COMPLETED, _decision(
            definition, strategy_id=strategy_id, version=version,
            symbol=chosen["symbol"], bar_ts=chosen_bar,
            signal=chosen["signal"], action=_action_for(chosen["signal"]),
            reason=f"{chosen.get('reason') or 'signal'} — shadow: nothing was sent",
            indicators=chosen.get("indicators") or {},
            risk={
                "allowed": True,
                "quantity": float(config.get("volume") or 0) or None,
                "reasons": [],
            },
            outcome="preview", code="SHADOW", order_status="SHADOW",
        ))

    try:
        result = await live.run_once(
            trial_id, gateway=gateway, symbol=chosen["symbol"],
            automation_id=str(getattr(definition, "id", "")),
            bot_version=int(getattr(definition, "version", 1) or 1),
            dry_run=dry_run or mode == "shadow",
        )
    except Exception as exc:  # noqa: BLE001 - a refusal is a decision, not a crash
        code = getattr(exc, "code", None) or "BROKER_UNAVAILABLE"
        state = BLOCKED if code not in ("RUNTIME_UNAVAILABLE",) else FAILED
        return _finish(state, _decision(
            definition, strategy_id=strategy_id, version=version,
            symbol=chosen["symbol"], bar_ts=chosen_bar,
            signal=chosen["signal"], action=_action_for(chosen["signal"]),
            reason=str(exc), indicators=chosen.get("indicators") or {},
            outcome="refused", code=str(code)),
            error=str(exc), error_class=str(code))

    return _from_result(definition, strategy_id, version, chosen, result, dry_run)


def _action_for(signal: str) -> str:
    return {
        "long_entry": "buy", "short_entry": "sell",
        "long_exit": "close", "short_exit": "close",
    }.get(str(signal), "none")


def _choose(evaluations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Exits first (risk-reducing), then the best entry — one decision per run."""
    ranked = policy.rank_evaluations(evaluations, max_entries=1)
    return ranked[0] if ranked else None


def _freshest_bar(evaluations: list[dict[str, Any]]) -> str | None:
    moments = [policy.parse_moment(item.get("bar")) for item in evaluations]
    usable = [moment for moment in moments if moment is not None]
    if not usable:
        return None
    newest = max(usable)
    return newest.isoformat().replace("+00:00", "Z")


def _from_result(definition: Any, strategy_id: str, version: str,
                 chosen: dict[str, Any], result: dict[str, Any],
                 dry_run: bool) -> Any:
    signal = result.get("signal") or {}
    order = result.get("order") or {}
    closed = result.get("closed") or []
    risk = result.get("risk") or {}
    action = _action_for(str(signal.get("signal") or chosen["signal"]))
    status = str(order.get("status") or "")
    reported = str(result.get("code") or "")
    reasons = list(order.get("reasons") or []) or list(risk.get("reasons") or [])
    rating = {
        "allowed": bool(risk.get("allowed", True)),
        "quantity": (order.get("volume") or signal.get("volume")),
        "reasons": reasons,
    }

    outcome = "none"
    code = None
    state = COMPLETED
    if closed:
        outcome = "filled"
        action = "close"
    elif order:
        if status in ("filled",):
            outcome = "filled"
        elif status in ("placed", "working", "PLACED"):
            outcome = "placed"
        elif status in ("REFUSED", "REJECTED", "REJECTED_BY_BROKER"):
            outcome = "refused"
            code = "ORDER_REFUSED"
            state = BLOCKED
        elif result.get("dryRun"):
            outcome = "preview"
        else:
            outcome = "placed"
    elif risk and risk.get("allowed") is False:
        outcome = "refused"
        code = "RISK_REFUSED"
        state = BLOCKED
    elif action == "none":
        outcome = "none"
    else:
        outcome = "skipped"
        code = "POSITION_ALREADY_OPEN"

    # The trial reports WHY it did not send; keep that code instead of guessing.
    if reported and not order and not closed:
        if reported in ("BAR_ALREADY_PROCESSED", "POSITION_ALREADY_OPEN"):
            outcome, code, state = "skipped", reported, SKIPPED
        else:
            outcome, code, state = "refused", reported, BLOCKED

    if result.get("dryRun") and order and outcome not in ("refused", "failed"):
        outcome = "preview"

    decision = _decision(
        definition,
        strategy_id=strategy_id,
        version=version,
        symbol=str(signal.get("symbol") or chosen["symbol"]),
        bar_ts=str(signal.get("bar") or chosen.get("bar") or "") or None,
        signal=str(signal.get("signal") or chosen["signal"]),
        action=action,
        reason=str(signal.get("reason") or result.get("message") or chosen.get("reason") or ""),
        indicators=signal.get("indicators") or chosen.get("indicators") or {},
        risk=rating,
        outcome=outcome,
        code=code,
        order_id=str(order.get("orderId") or order.get("ticket") or "") or None,
        order_status=status or None,
        warnings=list(result.get("warnings") or []),
    )
    return _finish(state, decision)


__all__ = ["run_strategy"]
