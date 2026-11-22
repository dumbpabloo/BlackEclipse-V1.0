"""Risk-based position sizing for a discretionary trade.

The one rule: the stop decides the size, never the other way round. A caller
supplies the account equity, the risk budget and the stop distance; this module
returns the largest broker-legal volume whose loss at that stop stays inside the
budget - or an explicit refusal naming the smallest trade the instrument allows.

It is pure arithmetic (no adapter, no I/O) so the maths is testable without a
broker. The money basis is the broker's own: when MT5 reports `trade_tick_value`
(the value of one tick per lot in the ACCOUNT currency) that is used directly;
otherwise the notional is `distance x contractSize x priceToAccount`, and the
result says which basis it used so a currency mismatch cannot masquerade as an
exact number.
"""

from __future__ import annotations

import math
from typing import Any, Optional

_DECIMALS = 8


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, _DECIMALS)
    return round(math.floor(value / step + 1e-9) * step, _DECIMALS)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def size_position(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    side: str = "buy",
    contract_size: float = 0.0,
    volume_min: float = 0.0,
    volume_max: float = 0.0,
    volume_step: float = 0.0,
    tick_size: float = 0.0,
    tick_value: float = 0.0,
    price_to_account: float = 1.0,
    stops_level_points: float = 0.0,
    point: float = 0.0,
) -> dict[str, Any]:
    """Size `volume` so a stop-out loses at most `risk_pct` of `equity`.

    Returns `allowed=True` with the volume, or `allowed=False` with a named
    reason and the numbers a caller needs to correct course. It never rounds a
    volume UP (that would exceed the risk budget) and never returns a volume
    below the broker minimum pretending it is valid.
    """
    warnings: list[str] = []
    equity = _num(equity)
    risk_pct = _num(risk_pct)
    entry = _num(entry)
    stop = _num(stop)
    side = str(side or "").strip().lower()

    if side not in ("buy", "sell"):
        return _refused("SIDE_INVALID", "A trade is either a buy or a sell.")
    if equity <= 0:
        return _refused(
            "EQUITY_UNAVAILABLE",
            "The broker did not report a positive account equity, so a risk "
            "budget cannot be computed.",
        )
    if not (0 < risk_pct <= 100):
        return _refused("RISK_PCT_INVALID", "riskPct must be between 0 and 100.")
    if entry <= 0 or stop <= 0:
        return _refused("PRICE_INVALID", "Entry and stop must both be positive prices.")
    distance = abs(entry - stop)
    if distance <= 0:
        return _refused("STOP_EQUALS_ENTRY", "The stop cannot equal the entry.")

    if stops_level_points > 0 and point > 0:
        min_distance = stops_level_points * point
        if distance < min_distance:
            return _refused(
                "STOP_TOO_TIGHT",
                f"The broker needs at least {stops_level_points:g} points "
                f"({min_distance:g}) between entry and stop.",
                extra={"minStopDistance": min_distance},
            )

    if tick_size > 0 and tick_value > 0:
        risk_per_lot = (distance / tick_size) * tick_value
        basis = "tick-value"
    elif contract_size > 0:
        risk_per_lot = distance * contract_size * _num(price_to_account, 1.0)
        basis = "contract-size"
        if abs(_num(price_to_account, 1.0) - 1.0) > 1e-9:
            warnings.append(
                "The risk-per-lot assumes the quote currency equals the account "
                "currency; verify the broker's own margin on the preview."
            )
    else:
        return _refused(
            "SYMBOL_SPEC_UNAVAILABLE",
            "The broker did not report a contract size or tick value, so the "
            "loss per lot cannot be computed.",
        )
    if risk_per_lot <= 0:
        return _refused(
            "RISK_PER_LOT_UNAVAILABLE",
            "The broker's symbol facts imply a zero loss per lot at this stop.",
        )

    budget = equity * risk_pct / 100.0
    raw_volume = budget / risk_per_lot
    step = volume_step if volume_step > 0 else 0.01
    volume = _floor_to_step(raw_volume, step)

    minimum = volume_min if volume_min > 0 else step
    if volume < minimum:
        min_risk = minimum * risk_per_lot
        return _refused(
            "RISK_TOO_SMALL",
            f"The smallest trade ({minimum:g} volume) risks {min_risk:,.2f}, "
            f"which is above the {budget:,.2f} budget ({risk_pct:g}% of "
            f"{equity:,.2f}). Widen the risk budget or tighten the stop "
            "deliberately.",
            extra={
                "minVolume": minimum,
                "minRiskAmount": round(min_risk, 2),
                "budget": round(budget, 2),
                "riskPerLot": round(risk_per_lot, 6),
            },
        )

    maximum = volume_max if volume_max > 0 else volume
    if volume > maximum:
        volume = _floor_to_step(maximum, step)
        warnings.append(f"Volume capped at the broker maximum of {maximum:g}.")
        if volume < minimum:
            return _refused(
                "VOLUME_LIMITS_INCONSISTENT",
                "The broker's minimum volume is above its maximum volume.",
            )

    actual_risk = volume * risk_per_lot
    notional = volume * contract_size * entry if contract_size > 0 else None
    return {
        "available": True,
        "allowed": True,
        "status": "SIZED",
        "side": side,
        "entry": entry,
        "stop": stop,
        "distance": round(distance, _DECIMALS),
        "volume": volume,
        "volumeMin": minimum,
        "volumeMax": maximum if maximum > 0 else None,
        "volumeStep": step,
        "riskPct": risk_pct,
        "budget": round(budget, 2),
        "riskPerLot": round(risk_per_lot, 6),
        "riskAmount": round(actual_risk, 2),
        "riskPctActual": round(actual_risk / equity * 100.0, 4),
        "notional": round(notional, 2) if notional is not None else None,
        "equity": round(equity, 2),
        "basis": basis,
        "warnings": warnings,
    }


def _refused(code: str, reason: str, *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": True,
        "allowed": False,
        "status": "REFUSED",
        "error": {"code": code, "message": reason},
        "reasons": [reason],
    }
    if extra:
        payload.update(extra)
    return payload


__all__ = ["size_position"]
