"""Strategy-bot trading routes.

`prepare` is the ONE place a bot gains market authority: the strategy gates run,
a bounded trial/mandate is created, and the operator's typed phrase becomes a
standing consent stored on the bot. From then on the bot renews its own mandate
("auto mode") until the consent expires or is revoked.

The routes never place an order. Execution is the automation runner's job, and
every order still goes through `BrokerService.submit_order`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import require_operator, service
from ..automation import trading_policy
from ..quant.live import LiveStrategyService
from .automation import _svc

router = APIRouter(tags=["automations"])

#: How long a standing consent lasts. The mandate itself is shorter (24 h live);
#: auto-renew keeps the mandate alive while this grant is valid.
CONSENT_TTL_SECONDS = 30 * 24 * 60 * 60


class PrepareBody(BaseModel):
    strategyId: str = ""  # noqa: N815 - wire name
    strategyVersion: str = ""  # noqa: N815
    mode: str = "shadow"  # noqa: N815 - wire name
    connectionId: str = ""  # noqa: N815
    symbols: list[str] = Field(default_factory=list)
    timeframe: str = "H1"
    volume: float = 0.01
    stopLossPoints: float = 0.0  # noqa: N815
    takeProfitPoints: float = 0.0  # noqa: N815
    maxOrders: int = 2  # noqa: N815
    maxMargin: float = 0.0  # noqa: N815
    ttlSeconds: int = 86_400  # noqa: N815
    confirmation: str = ""
    note: str = ""
    live: bool = False


def _required_phrase(body: PrepareBody, live: LiveStrategyService) -> str:
    """The exact phrase the operator must type, matching the server's check.

    A demo trial's phrase is built from the connection LABEL (that is what
    `LiveStrategyService.prepare` compares), not its internal id: showing the id
    would hand the operator a phrase the server always rejects.
    """
    if body.mode == "live":
        return f"LIVE TRIAL {body.strategyId} {body.strategyVersion}"
    label = body.connectionId
    try:
        label = live.broker.get_connection(body.connectionId).get("label") or label
    except Exception:  # noqa: BLE001 - the exact phrase is returned by prepare anyway
        pass
    return f"MANDATE DEMO {label}"


def _universe(items: Any) -> list[str]:
    """Normalise a bot's symbol list against the process hard ceiling."""
    return trading_policy.normalize_symbols(
        items or [], limit=trading_policy.MAX_SYMBOLS_HARD,
    )


def _definition(automation_id: str):
    definition = _svc().get(automation_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    if getattr(definition.action, "type", "") not in ("run_strategy", "run_agent"):
        raise HTTPException(
            status_code=400,
            detail="this automation is not a trading bot",
        )
    return definition


def _trading_bot(definition) -> bool:
    return getattr(definition.action, "type", "") == "run_strategy"


def _grant_live_tier(definition):
    """The operator's explicit LIVE authorization grants L2.

    Live is a choice, not a track-record prison. The typed phrase and the armed
    connection remain the real gates; the tier is what the mode requires, so it is
    raised in the same operator action instead of dead-ending the form.
    """
    authority = getattr(definition, "execution_authority", None)
    tier = str(getattr(authority, "tier", "l0") or "l0")
    if tier == "l2":
        return definition
    payload = (
        authority.model_dump(by_alias=False)
        if hasattr(authority, "model_dump")
        else {key: getattr(authority, key) for key in (
            "tier", "appetite", "sizing_mode", "require_stop",
            "max_open_positions", "max_margin_per_order", "cooldown_s",
            "max_daily_loss_pct", "auto_mode", "auto_renew",
            "learning_enabled", "improve_after_failures",
        )}
    )
    payload["tier"] = "l2"
    return _svc().update(definition.id, {"execution_authority": payload})


def _prepare_agent(
    definition, body: PrepareBody, *, stored: Any, auto: bool,
) -> dict[str, Any]:
    """A trading AGENT needs a mandate, not a strategy trial.

    The agent decides at run time with its tools; the mandate is the bounded,
    bot-pinned authorization that lets it submit. `order_submit` still refuses a
    live order without the L2 fence and the mandate's own caps.
    """
    if body.mode == "shadow":
        raise HTTPException(
            status_code=400,
            detail={"code": "SHADOW_NOT_PREPARED", "message": "an agent in shadow mode needs no mandate"},
        )
    brokers = getattr(service, "brokers", None)
    if brokers is None:
        raise HTTPException(status_code=503, detail="the broker layer is not available")
    if not body.connectionId:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONNECTION_REQUIRED", "message": "choose a broker connection"},
        )
    connection = brokers.get_connection(body.connectionId)
    if str(connection.get("mode") or "") != body.mode:
        raise HTTPException(
            status_code=400,
            detail={"code": "MODE_MISMATCH", "message": "the bot mode and the connection mode disagree"},
        )
    expected = f"MANDATE {body.mode.upper()} {connection.get('label', '')}"
    if not auto and str(body.confirmation or "").strip() != expected:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRMATION_REQUIRED",
                "message": f"type {expected!r} exactly to authorize this bot",
                "requiredPhrase": expected,
            },
        )
    try:
        mandate = brokers.create_mandate(body.connectionId, {
            "mode": body.mode,
            "symbols": _universe(body.symbols) or ["*"],
            "maxOrders": max(1, int(body.maxOrders or 1)),
            "maxMargin": float(body.maxMargin or 0),
            "ttlSeconds": int(body.ttlSeconds or 86_400),
            "botId": definition.id,
            "botVersion": definition.version,
            "confirmation": expected,
            "note": "agent trading bot mandate",
        })
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", "") or "MANDATE_REFUSED")
        raise HTTPException(status_code=400, detail={"code": code, "message": str(exc)}) from exc
    consent = _consent(definition, body, stored, auto)
    updated = _svc().attach_trading(definition.id, trial_id="", consent=consent)
    return {
        "success": True,
        "mandate": mandate,
        "consent": consent,
        "automation": updated.public(),
        "requiredPhrase": expected,
    }


def _live() -> LiveStrategyService:
    store = getattr(service, "quant_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="the strategy store is not available")
    return LiveStrategyService(store)


def _consent(
    definition: Any, body: PrepareBody, stored: Any = None, auto: bool = False,
) -> Optional[dict[str, Any]]:
    """Build (or renew) the standing grant from what the operator approved.

    It is stored on the bot (not in a client cookie) so the runner can verify it
    without trusting the caller, and so revoking it is one PATCH. When a valid
    stored consent already covers this request, it is RENEWED rather than
    re-typed — that is the whole point of auto mode.
    """
    if body.mode == "shadow":
        return None
    authority = definition.execution_authority
    now = datetime.now(timezone.utc)
    if auto and isinstance(stored, dict):
        renewed = dict(stored)
        renewed["expiresAt"] = (now + timedelta(seconds=CONSENT_TTL_SECONDS)) \
            .isoformat().replace("+00:00", "Z")
        renewed["autoRenew"] = bool(getattr(authority, "auto_renew", True))
        renewed["revokedAt"] = None
        return renewed
    symbols = _universe(body.symbols) or ["*"]
    return {
        "version": 1,
        "kind": "standing",
        "modes": [body.mode],
        "grantedAt": now.isoformat().replace("+00:00", "Z"),
        "grantedBy": "operator",
        "expiresAt": (now + timedelta(seconds=CONSENT_TTL_SECONDS))
        .isoformat()
        .replace("+00:00", "Z"),
        "revokedAt": None,
        "autoRenew": bool(getattr(authority, "auto_renew", True)),
        "limits": {
            "symbols": symbols,
            "maxOrdersPerDay": max(1, int(body.maxOrders or 1)),
            "maxMarginPerOrder": float(body.maxMargin or 0),
            "maxOpenPositions": max(1, int(getattr(authority, "max_open_positions", 1) or 1)),
        },
        "phrase": str(body.confirmation or "").strip(),
    }


@router.post("/automations/{automation_id}/trading/prepare")
def prepare_trading(
    automation_id: str, body: PrepareBody, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    definition = _definition(automation_id)
    current_config = (
        definition.action.config if isinstance(definition.action.config, dict) else {}
    )
    stored_consent = current_config.get("consent")
    authority = definition.execution_authority
    # Auto mode: a valid stored consent that still covers this request means the
    # operator already answered the phrase question. It is renewed, not re-typed.
    auto = bool(stored_consent) and not str(body.confirmation or "").strip() and bool(
        trading_policy.consent_covers(
            stored_consent,
            trading_policy.consent_request_for(
                {
                    "mode": body.mode,
                    "symbols": _universe(body.symbols),
                    "maxSymbols": trading_policy.MAX_SYMBOLS_HARD,
                    "maxOrdersPerDay": body.maxOrders,
                },
                {
                    "maxMarginPerOrder": getattr(authority, "max_margin_per_order", 0),
                    "maxOpenPositions": getattr(authority, "max_open_positions", 1),
                },
            ),
        )["covered"]
    )

    # Re-preparing supersedes the old trial: stop it first so two mandates can
    # never be alive for one bot. An agent bot has a mandate but no trial.
    if body.mode == "live":
        definition = _grant_live_tier(definition)
    if not _trading_bot(definition):
        return _prepare_agent(definition, body, stored=stored_consent, auto=auto)
    live = _live()
    try:
        live.stop_by_bot(automation_id)
    except Exception:  # noqa: BLE001 - best effort; the new trial is still safe
        pass

    try:
        trial = live.prepare(
            strategy_id=body.strategyId,
            version=body.strategyVersion,
            connection_id=body.connectionId,
            symbols=_universe(body.symbols),
            mode=body.mode,
            timeframe=body.timeframe,
            volume=body.volume,
            stop_loss_points=body.stopLossPoints,
            take_profit_points=body.takeProfitPoints,
            max_orders=body.maxOrders,
            max_margin=body.maxMargin,
            ttl_seconds=body.ttlSeconds,
            confirmation=body.confirmation,
            note=body.note,
            bot_id=automation_id,
            bot_version=definition.version,
            automation_id=automation_id,
            standing_consent=auto,
        )
    except Exception as exc:  # noqa: BLE001 - every gate failure is a 4xx with a code
        code = str(getattr(exc, "code", "") or "")
        detail = getattr(exc, "detail", None)
        payload = {"code": code or "TRADING_PREPARE_FAILED", "message": str(exc)}
        if detail:
            payload["detail"] = detail
        raise HTTPException(status_code=400, detail=payload) from exc

    consent = _consent(definition, body, stored_consent, auto)
    try:
        updated = _svc().attach_trading(
            automation_id,
            trial_id=str(trial["id"]),
            consent=consent,
            config_patch={
                "mode": body.mode,
                "connectionId": body.connectionId,
                "symbols": _universe(body.symbols),
                "timeframe": body.timeframe,
                "volume": body.volume,
                "stopLossPoints": body.stopLossPoints,
                "takeProfitPoints": body.takeProfitPoints,
                "maxOrdersPerDay": body.maxOrders,
                "dryRun": False,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "success": True,
        "trial": trial,
        "consent": consent,
        "automation": updated.public(),
        "requiredPhrase": _required_phrase(body, live),
    }


@router.get("/automations/{automation_id}/trading")
def trading_status(
    automation_id: str, limit: int = 20, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    definition = _definition(automation_id)
    config = definition.action.config if isinstance(definition.action.config, dict) else {}
    authority = definition.execution_authority
    trial: Optional[dict[str, Any]] = None
    trial_error: Optional[str] = None
    trial_id = str(config.get("trialId") or "")
    live: Optional[LiveStrategyService] = None
    try:
        live = _live()
    except HTTPException:
        live = None
    if trial_id and live is not None:
        try:
            trial = live.get(trial_id)
        except Exception as exc:  # noqa: BLE001
            trial_error = str(exc)

    brokers = getattr(service, "brokers", None)
    connection_id = str(config.get("connectionId") or (trial or {}).get("connectionId") or "")
    account: Any = None
    positions: Any = None
    orders: Any = None
    unavailable: list[dict[str, Any]] = []
    if connection_id and brokers is not None:
        for name, call in (
            ("account", lambda: brokers.account(connection_id)),
            ("positions", lambda: brokers.positions(connection_id)),
            ("orders", lambda: brokers.orders(connection_id)),
        ):
            try:
                value = call()
                if name == "account":
                    account = value
                elif name == "positions":
                    positions = value
                else:
                    orders = value
            except Exception as exc:  # noqa: BLE001 - an honest gap, never a zero
                unavailable.append({"field": name, "reason": str(exc)})

    decisions: list[dict[str, Any]] = []
    try:
        for execution in _svc().history(automation_id, limit=max(1, min(200, limit))):
            if execution.get("decision"):
                decisions.append(execution["decision"])
    except Exception:  # noqa: BLE001
        pass

    # The arm + mandate countdown the bot panel shows. Read-only; it never
    # renews. A trial without a readable authority is reported honestly.
    authority_state: Optional[dict[str, Any]] = None
    if trial is not None and brokers is not None and live is not None:
        try:
            from ..trade.renewal import authority_status  # noqa: PLC0415

            authority_state = authority_status(brokers, live, trial)
        except Exception as exc:  # noqa: BLE001
            authority_state = {"available": False, "reason": str(exc)}

    return {
        "success": True,
        "botId": automation_id,
        "version": definition.version,
        "mode": config.get("mode") or "shadow",
        "tier": str(getattr(authority, "tier", "l0")),
        "dryRun": bool(config.get("dryRun")),
        "consent": config.get("consent"),
        "trial": trial,
        "trialError": trial_error,
        "authority": authority_state,
        "account": account,
        "positions": positions,
        "orders": orders,
        "decisions": decisions[-max(1, min(200, limit)):],
        "unavailable": unavailable,
    }


class ScanBody(BaseModel):
    symbols: list[str] = Field(default_factory=list)


@router.post("/automations/{automation_id}/trading/scan")
async def scan_trading(
    automation_id: str, body: ScanBody, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Find trades: evaluate the strategy across its universe on the last bar.

    This is the bot's own candidate source — the same signal the runner acts on.
    It never places an order; the operator adopts the returned symbols.
    """
    definition = _definition(automation_id)
    config = definition.action.config if isinstance(definition.action.config, dict) else {}
    trial_id = str(config.get("trialId") or "")
    if not trial_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "TRADING_NOT_PREPARED",
                    "message": "prepare trading before scanning for candidates"},
        )
    symbols = trading_policy.normalize_symbols(
        body.symbols or config.get("symbols") or [],
        limit=config.get("maxSymbols"),
    )
    if not symbols:
        raise HTTPException(
            status_code=400,
            detail={"code": "SYMBOLS_REQUIRED", "message": "no symbols to scan"},
        )
    live = _live()
    try:
        gateway = service.server_market_gateway()
    except Exception:  # noqa: BLE001 - a missing gateway is reported per symbol
        gateway = None
    results: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            reading = await live.signal(trial_id, gateway=gateway, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the scan
            results.append({
                "symbol": symbol, "signal": "none", "score": 0.0,
                "reason": str(exc), "actionable": False,
            })
            continue
        signal = str(reading.get("signal") or "none")
        results.append({
            "symbol": symbol,
            "signal": signal,
            "score": 1.0 if signal.endswith("_entry") else (0.5 if signal.endswith("_exit") else 0.0),
            "reason": str(reading.get("reason") or ""),
            "bar": reading.get("bar"),
            "actionable": signal != "none",
        })
    actionable = [row for row in results if row["actionable"]]
    ranked = trading_policy.rank_evaluations(actionable, max_entries=len(actionable) or 1)
    return {
        "success": True,
        "scanned": len(results),
        "actionable": [row["symbol"] for row in ranked],
        "results": results,
        "note": "A scan is a condition on the last closed bar, not an order.",
    }


@router.post("/automations/{automation_id}/trading/renew")
def renew_trading_authority(
    automation_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Extend the arm/mandate now instead of waiting for the next entry.

    This is the manual equivalent of what auto mode does at order time: it only
    extends an existing grant, clamped to the bot's current stage. Arming still
    requires the operator's typed phrase; an expired arm is not resurrected.
    """
    definition = _definition(automation_id)
    config = definition.action.config if isinstance(definition.action.config, dict) else {}
    trial_id = str(config.get("trialId") or "")
    if not trial_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "TRADING_NOT_PREPARED",
                    "message": "prepare trading before renewing its authority"},
        )
    brokers = getattr(service, "brokers", None)
    if brokers is None:
        raise HTTPException(status_code=503, detail="the broker layer is not available")
    from ..trade.renewal import ensure_authority  # noqa: PLC0415

    try:
        outcome = ensure_authority(
            brokers,
            _live(),
            trial_id,
            automation_id=automation_id,
            bot_version=definition.version,
            force=True,
        )
    except Exception as exc:  # noqa: BLE001 - every refusal is a 4xx with a code
        code = str(getattr(exc, "code", "") or "AUTHORITY_RENEWAL_FAILED")
        raise HTTPException(
            status_code=400, detail={"code": code, "message": str(exc)}
        ) from exc
    return {
        "success": True,
        "renewed": bool(outcome.get("renewed")),
        "arm": outcome.get("arm"),
        "mandate": outcome.get("mandate"),
        "authority": outcome.get("status"),
    }


@router.post("/automations/{automation_id}/trading/panic")
def panic_trading(
    automation_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Stop this bot's ability to trade. Positions stay for the operator."""
    definition = _definition(automation_id)
    result = _svc()._stop_trading(automation_id, "operator panic")  # noqa: SLF001 - same domain
    paused = None
    if definition.state == "active":
        try:
            paused = _svc().pause(automation_id)
        except Exception:  # noqa: BLE001
            paused = None
    return {
        "success": True,
        "stopped": result,
        "paused": bool(paused),
        "positionsClosed": 0,
        "note": "Positions were not closed; close them explicitly if you want out.",
    }
