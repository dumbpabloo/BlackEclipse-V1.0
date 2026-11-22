"""Build a trade PLAN: instrument facts + risk-based size + account fit.

This is the step between "I want to take a trade" and `order_preview`. It reads
only broker facts (account, symbol spec, quote, open positions) and computes:

  * the exact entry (the operator's price, or the broker's ask/bid),
  * the largest volume whose stop-out stays inside the risk budget,
  * the account-level fit (open positions, concentration, margin, spread),
  * the reward:risk when a target is given.

It sends nothing and creates no approval. The plan is a value the agent shows and
then passes to `order_preview`; a refusal here is a complete answer (the min-lot
risk or the too-tight stop), not a dead end.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from .risk import assess_trade_risk, limits_from_connection
from .sizing import size_position

_DEFAULT_RISK_PCT = 1.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _failed(service: Any, connection: Any, symbol: str, reason: str, code: str) -> dict[str, Any]:
    return {
        "available": True,
        "allowed": False,
        "status": "UNAVAILABLE",
        "connectionId": getattr(connection, "id", None),
        "mode": getattr(getattr(connection, "mode", None), "value", None),
        "symbol": symbol,
        "error": {"code": code, "message": reason},
        "reasons": [reason],
    }


def build_trade_plan(
    service: Any, connection_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    connection = service._connection(connection_id)
    symbol = str(payload.get("symbol") or "").strip()
    side = str(payload.get("side") or "").strip().lower()
    if not symbol:
        return _failed(service, connection, "", "Name the instrument.", "SYMBOL_REQUIRED")
    if side not in ("buy", "sell"):
        return _failed(service, connection, symbol, "A trade is a buy or a sell.", "SIDE_INVALID")

    risk_pct = _num(payload.get("riskPct"), _DEFAULT_RISK_PCT) or _DEFAULT_RISK_PCT
    stop = _num(payload.get("stop"))
    target = _num(payload.get("target")) or None

    facts_result = service.symbol_info(connection_id, symbol)
    if not facts_result.get("available"):
        return {**facts_result, "status": "UNAVAILABLE", "allowed": False}
    facts = dict(facts_result.get("data") or {})

    account_result = service.account(connection_id)
    if not account_result.get("available"):
        return {**account_result, "status": "UNAVAILABLE", "allowed": False}
    account = dict(account_result.get("data") or {})
    equity = _num(account.get("equity")) or _num(account.get("balance"))

    entry = _num(payload.get("entry"))
    entry_source = "operator"
    quote: Optional[dict[str, Any]] = None
    if entry <= 0:
        quote_result = service.quote(connection_id, symbol)
        if not quote_result.get("available"):
            return {**quote_result, "status": "UNAVAILABLE", "allowed": False}
        quote = dict(quote_result.get("data") or {})
        bid = _num(quote.get("bid"))
        ask = _num(quote.get("ask"))
        entry = ask if side == "buy" else bid
        entry_source = "broker quote"
        if entry <= 0:
            return _failed(
                service, connection, symbol,
                "The broker returned no usable price for this instrument.",
                "QUOTE_UNAVAILABLE",
            )

    sizing = size_position(
        equity=equity,
        risk_pct=risk_pct,
        entry=entry,
        stop=stop,
        side=side,
        contract_size=_num(facts.get("contractSize")),
        volume_min=_num(facts.get("volumeMin")),
        volume_max=_num(facts.get("volumeMax")),
        volume_step=_num(facts.get("volumeStep")),
        tick_size=_num(facts.get("tickSize")),
        tick_value=_num(facts.get("tickValue")),
        stops_level_points=_num(facts.get("stopsLevelPoints")),
        point=_num(facts.get("point")),
    )

    positions: list[dict[str, Any]] = []
    positions_result = service.positions(connection_id)
    if positions_result.get("available") and isinstance(positions_result.get("data"), list):
        positions = [row for row in positions_result["data"] if isinstance(row, dict)]

    plan: dict[str, Any] = {
        "available": True,
        "allowed": bool(sizing.get("allowed")),
        "status": sizing.get("status"),
        "connectionId": connection.id,
        "mode": getattr(connection.mode, "value", connection.mode),
        "symbol": facts.get("symbol") or symbol,
        "side": side,
        "entry": entry,
        "entrySource": entry_source,
        "stop": stop,
        "target": target,
        "riskPct": risk_pct,
        "equity": round(equity, 2) if equity else None,
        "currency": account.get("currency") or None,
        "volume": sizing.get("volume"),
        "riskAmount": sizing.get("riskAmount"),
        "riskPctActual": sizing.get("riskPctActual"),
        "notional": sizing.get("notional"),
        "basis": sizing.get("basis"),
        "warnings": list(sizing.get("warnings") or []),
        "reasons": list(sizing.get("reasons") or []),
        "error": sizing.get("error"),
        "symbolFacts": {
            key: facts.get(key)
            for key in (
                "digits", "contractSize", "volumeMin", "volumeMax",
                "volumeStep", "tickSize", "tickValue", "point",
                "stopsLevelPoints", "spreadPoints", "tradeMode",
                "sessionsAvailable", "sessionNote", "description",
            )
        },
        "quote": {
            "bid": (quote or {}).get("bid"),
            "ask": (quote or {}).get("ask"),
            "spreadPoints": (quote or {}).get("spreadPoints"),
            "retrievedAt": (quote or {}).get("retrievedAt"),
        } if quote else None,
    }

    if sizing.get("allowed"):
        distance = _num(sizing.get("distance"))
        volume = _num(sizing.get("volume"))
        risk_amount = _num(sizing.get("riskAmount"))
        limits = limits_from_connection(getattr(connection, "config", None))
        risk = assess_trade_risk(
            account=account,
            positions=positions,
            symbol=plan["symbol"],
            planned_risk=risk_amount,
            risk_pct=_num(sizing.get("riskPctActual")),
            spread_points=_num((quote or {}).get("spreadPoints"))
            or _num(facts.get("spreadPoints")) or None,
            point=_num(facts.get("point")),
            tick_value=_num(facts.get("tickValue")),
            tick_size=_num(facts.get("tickSize")),
            distance=distance,
            limits=limits,
        )
        plan["risk"] = risk
        if target and distance > 0:
            plan["riskReward"] = round(abs(target - entry) / distance, 2)
        if risk.get("blocking"):
            plan["allowed"] = False
            plan["status"] = "REFUSED"
            plan["reasons"] = list(risk.get("reasons") or [])
        plan["planId"] = _plan_id(connection.id, plan)
    else:
        plan["risk"] = None
        plan["planId"] = None

    plan["summary"] = _summary(plan)
    return plan


def _plan_id(connection_id: str, plan: Mapping[str, Any]) -> str:
    raw = "|".join(
        str(plan.get(key)) for key in
        ("symbol", "side", "entry", "stop", "target", "volume", "riskPct")
    )
    digest = hashlib.sha256(f"{connection_id}|{raw}".encode("utf-8")).hexdigest()
    return f"plan_{digest[:16]}"


def _summary(plan: Mapping[str, Any]) -> str:
    if not plan.get("allowed"):
        reasons = plan.get("reasons") or ["not sized"]
        return f"Not placed: {reasons[0]}"
    side = str(plan.get("side") or "").upper()
    volume = plan.get("volume")
    symbol = plan.get("symbol")
    entry = plan.get("entry")
    stop = plan.get("stop")
    risk = plan.get("riskAmount")
    pct = plan.get("riskPctActual")
    text = (
        f"{side} {volume:g} {symbol} at {entry:g}, stop {stop:g} - "
        f"max loss {risk:,.2f} ({pct:g}% of equity)"
    )
    rr = plan.get("riskReward")
    if rr:
        text += f", reward:risk {rr:g}"
    return text


__all__ = ["build_trade_plan"]
