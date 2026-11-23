"""Position management primitives: break-even, trailing stop, partial close.

All three are **risk-reducing or risk-scoping**, so they follow the same rule as
`trading.broker_close` / `broker_modify`: no mandate, no approval and no live arm
— an expired arm must never trap a position. They still need a pinned
`connectionId`, and the broker service enforces that the account mode matches the
connection.

Each node reads the position from the broker first, decides, then asks the
service to move or close it. `ok` means the operation was carried out (or the
request was already satisfied); `moved`/`closed` say whether anything changed;
`reason` explains a refusal in the broker's own words where possible.
"""

from __future__ import annotations

import math
from typing import Any

from ..trade.model import ticket_ref
from .model import ExecutionMode, PortDef, PortType, SideEffect
from .registry import ConfigField, NodeDef, NodeExecContext

_MODES = (ExecutionMode.PAPER, ExecutionMode.LIVE)


def _service() -> Any:
    from ..trade.bootstrap import get_service  # noqa: PLC0415 - avoids an import cycle

    service = get_service()
    if service is None:
        raise ValueError("broker connectivity is unavailable in this runtime")
    return service


def _connection(context: NodeExecContext) -> str:
    connection_id = str(context.config.get("connectionId") or "").strip()
    if not connection_id:
        raise ValueError("connectionId is required")
    return connection_id


def _number(context: NodeExecContext, handle: str, config_key: str | None = None) -> float | None:
    raw = context.inputs.get(handle)
    if raw is None or raw == "":
        raw = context.config.get(config_key or handle)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{handle} must be a number")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{handle} must be a number") from exc


def _ticket(context: NodeExecContext):
    raw = context.inputs.get("ticket")
    if raw in (None, ""):
        raw = context.config.get("ticket")
    if raw in (None, ""):
        raise ValueError("a position ticket is required")
    return ticket_ref(raw)


def _reason(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    nested = result.get("result")
    for source in (result, nested if isinstance(nested, dict) else {}):
        reasons = [str(item) for item in (source.get("reasons") or []) if str(item).strip()]
        if reasons:
            return "; ".join(reasons)
        name = str(source.get("retcodeName") or "").strip()
        if name and name != "DONE":
            return name
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("reason") or error.get("code") or "")
    return ""


def _quote(context: NodeExecContext) -> dict[str, Any]:
    raw = context.inputs.get("quote")
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    return data if isinstance(data, dict) else raw


def _position(service: Any, connection_id: str, ticket: Any) -> dict[str, Any] | None:
    result = service.positions(connection_id)
    if not result.get("available"):
        raise ValueError(f"the broker could not be read: {_reason(result) or 'unavailable'}")
    rows = result.get("data") if isinstance(result.get("data"), list) else []
    for row in rows:
        if isinstance(row, dict) and ticket_ref(row.get("ticket") or 0) == ticket:
            return row
    return None


def _missing(ticket: Any) -> dict[str, Any]:
    return {
        "moved": False,
        "closed": False,
        "stopLoss": None,
        "ok": False,
        "reason": f"no open position with ticket {ticket}",
    }


def exec_break_even(context: NodeExecContext) -> dict[str, Any]:
    """Move the stop to entry once the trade has advanced by `triggerR`."""
    service = _service()
    connection_id = _connection(context)
    ticket = _ticket(context)
    trigger = float(context.config.get("triggerR", 1.0) or 1.0)
    if trigger <= 0:
        raise ValueError("triggerR must be greater than zero")
    offset = float(context.config.get("offset") or 0.0)

    position = _position(service, connection_id, ticket)
    if position is None:
        return _missing(ticket)
    side = str(position.get("side") or "").lower()
    entry = float(position.get("openPrice") or 0)
    stop = position.get("stopLoss")
    price = _number(context, "price")
    if price is None:
        price = float(position.get("currentPrice") or 0)
    if entry <= 0 or price <= 0 or side not in ("buy", "sell"):
        raise ValueError("the position has no usable entry/current price")
    if stop is None or float(stop) <= 0:
        return {
            "moved": False, "closed": False, "stopLoss": None,
            "ok": False,
            "reason": "the position has no stop, so there is no R to measure",
        }
    stop = float(stop)
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("the position's stop equals its entry; R is zero")

    direction = 1.0 if side == "buy" else -1.0
    favorable = (price - entry) * direction
    target = entry + direction * offset
    already = (stop >= target) if side == "buy" else (stop <= target)
    if already:
        return {
            "moved": False, "closed": False, "stopLoss": stop, "ok": True,
            "reason": "the stop is already at or past break-even",
        }
    if favorable < trigger * risk:
        reached = favorable / risk
        return {
            "moved": False, "closed": False, "stopLoss": stop, "ok": True,
            "reason": (
                f"not triggered: {reached:.2f}R of the {trigger:g}R trigger"
            ),
        }

    result = service.modify_position(connection_id, ticket, stop_loss=round(target, 8))
    moved = bool(result.get("modified"))
    context.emit(
        f"break-even {ticket}: "
        + (f"stop -> {target:g}" if moved else f"refused ({_reason(result) or 'not modified'})")
    )
    return {
        "moved": moved,
        "closed": False,
        "stopLoss": round(target, 8) if moved else stop,
        "ok": moved,
        "reason": "" if moved else (_reason(result) or "the broker did not modify it"),
    }


def exec_trail_stop(context: NodeExecContext) -> dict[str, Any]:
    """Tighten the stop to a fixed distance behind the current price.

    The trail only ever moves the stop in the position's favour: a candidate that
    is not at least `minStep` better than the stored stop is left alone, so a
    flat or adverse tick never widens risk.
    """
    service = _service()
    connection_id = _connection(context)
    ticket = _ticket(context)
    distance = _number(context, "distance")
    if distance is None or distance <= 0:
        raise ValueError("a positive trail distance is required")
    min_step = float(context.config.get("minStep") or 0.0)
    if min_step < 0:
        raise ValueError("minStep cannot be negative")

    position = _position(service, connection_id, ticket)
    if position is None:
        return _missing(ticket)
    side = str(position.get("side") or "").lower()
    price = _number(context, "price")
    if price is None:
        price = float(position.get("currentPrice") or 0)
    if price <= 0 or side not in ("buy", "sell"):
        raise ValueError("the position has no usable current price")

    direction = 1.0 if side == "buy" else -1.0
    candidate = price - direction * distance
    current = position.get("stopLoss")
    current = float(current) if current not in (None, "") else None
    tighter = (
        current is None
        or (side == "buy" and candidate >= current + min_step)
        or (side == "sell" and candidate <= current - min_step)
    )
    if not tighter:
        return {
            "moved": False, "closed": False, "stopLoss": current, "ok": True,
            "reason": (
                f"the trail at {candidate:g} would not tighten the stop at {current:g}"
            ),
        }

    result = service.modify_position(
        connection_id, ticket, stop_loss=round(candidate, 8),
    )
    moved = bool(result.get("modified"))
    context.emit(
        f"trail {ticket}: "
        + (f"stop -> {candidate:g}" if moved else f"refused ({_reason(result) or 'not modified'})")
    )
    return {
        "moved": moved,
        "closed": False,
        "stopLoss": round(candidate, 8) if moved else current,
        "ok": moved,
        "reason": "" if moved else (_reason(result) or "the broker did not modify it"),
    }


def exec_partial_close(context: NodeExecContext) -> dict[str, Any]:
    """Close a fraction (or an explicit volume) of one position."""
    service = _service()
    connection_id = _connection(context)
    ticket = _ticket(context)
    deviation = int(context.config.get("deviation", 20) or 20)

    position = _position(service, connection_id, ticket)
    if position is None:
        return _missing(ticket)
    held = float(position.get("volume") or 0)
    if held <= 0:
        raise ValueError("the position reports no volume to close")

    volume = _number(context, "volume")
    if volume is None:
        fraction = _number(context, "fraction")
        if fraction is None or fraction <= 0 or fraction > 1:
            raise ValueError(
                "partial close needs a volume or a fraction in (0, 1]"
            )
        volume = held * fraction

    quote = _quote(context)
    step = float(quote.get("volumeStep") or 0.0)
    minimum = float(quote.get("volumeMin") or 0.0)
    if step <= 0:
        # No quote wired: ask the broker for the symbol's own volume step. A
        # symbol-facts read that fails falls back to the broker's judgement — the
        # adapter refuses an off-step volume by name rather than Eclipse guessing.
        symbol = str(position.get("symbol") or "").strip()
        if symbol:
            try:
                facts = service.symbol_info(connection_id, symbol)
            except Exception:  # noqa: BLE001 - a failed read must not block the close
                facts = {}
            data = facts.get("data") if isinstance(facts, dict) else None
            if isinstance(data, dict):
                step = float(data.get("volumeStep") or 0.0)
                minimum = float(data.get("volumeMin") or 0.0)
    if step > 0:
        # Floor to the broker's own step; rounding up could exceed what is held.
        volume = math.floor(volume / step + 1e-9) * step
    volume = round(volume, 8)
    if volume <= 0:
        return {
            "moved": False, "closed": False, "stopLoss": position.get("stopLoss"),
            "volume": 0.0, "ok": False,
            "reason": "the requested fraction is smaller than one volume step",
        }
    if minimum > 0 and volume < minimum:
        return {
            "moved": False, "closed": False, "stopLoss": position.get("stopLoss"),
            "volume": 0.0, "ok": False,
            "reason": f"the close volume {volume:g} is below the symbol minimum {minimum:g}",
        }
    if volume >= held - 1e-8:
        volume = held  # a full fraction is a full close, not an overshoot

    result = service.close_position(
        connection_id, ticket, volume=volume, deviation=deviation,
    )
    closed = bool(result.get("closed"))
    detail = result.get("result") if isinstance(result.get("result"), dict) else {}
    context.emit(
        f"partial close {ticket}: "
        + (f"{volume:g} of {held:g}" if closed else f"refused ({_reason(result) or 'not closed'})")
    )
    return {
        "moved": False,
        "closed": closed,
        "stopLoss": detail.get("stopLoss", position.get("stopLoss")),
        "volume": volume,
        "remainingVolume": detail.get("remainingVolume"),
        "verified": detail.get("verified"),
        "ok": closed,
        "reason": "" if closed else (_reason(result) or "the broker did not close it"),
    }


def build_ops_nodes() -> list[NodeDef]:
    connection = ConfigField("connectionId", "string", "", max_length=80)
    ticket_port = PortDef(id="ticket", name="Ticket", type=PortType.TICKET)
    ok_ports = (
        PortDef(id="ok", name="OK", type=PortType.BOOLEAN),
        PortDef(id="reason", name="Reason", type=PortType.STRING),
    )
    return [
        NodeDef(
            type="trading.break_even", version="1.0.0", label="Break even",
            category="trading",
            inputs=(
                ticket_port,
                PortDef(id="price", name="Current price", type=PortType.NUMBER, optional=True),
            ),
            outputs=(
                PortDef(id="moved", name="Stop moved", type=PortType.BOOLEAN),
                PortDef(id="stopLoss", name="Stop loss", type=PortType.NUMBER),
                PortDef(id="closed", name="Closed", type=PortType.BOOLEAN),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("triggerR", "number", 1.0, minimum=0.01, maximum=100.0),
                ConfigField("offset", "number", 0.0, minimum=-1_000_000.0, maximum=1_000_000.0),
            ),
            capabilities=("trading.broker",),
            side_effects=SideEffect.TRADING,
            allowed_modes=_MODES,
            executor=exec_break_even,
            metadata={
                "docs": (
                    "Reads the position, measures its own R (|entry − stop|), and moves "
                    "the stop to entry (+offset) once price is at least `triggerR` in "
                    "profit. `moved` says whether anything changed; a not-yet-triggered "
                    "trade is ok:true with a reason. Needs an existing stop. "
                    "Risk-reducing: no arm, approval or mandate."
                ),
            },
        ),
        NodeDef(
            type="trading.trail_stop", version="1.0.0", label="Trail stop",
            category="trading",
            inputs=(
                ticket_port,
                PortDef(id="price", name="Current price", type=PortType.NUMBER, optional=True),
                PortDef(id="distance", name="Distance", type=PortType.NUMBER, optional=True),
            ),
            outputs=(
                PortDef(id="moved", name="Stop moved", type=PortType.BOOLEAN),
                PortDef(id="stopLoss", name="Stop loss", type=PortType.NUMBER),
                PortDef(id="closed", name="Closed", type=PortType.BOOLEAN),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("distance", "number", 0.0, minimum=0.0, maximum=1_000_000_000.0),
                ConfigField("minStep", "number", 0.0, minimum=0.0, maximum=1_000_000_000.0),
            ),
            capabilities=("trading.broker",),
            side_effects=SideEffect.TRADING,
            allowed_modes=_MODES,
            executor=exec_trail_stop,
            metadata={
                "docs": (
                    "Keeps the stop `distance` behind the current price (price units), "
                    "moving it only when that is at least `minStep` better than the "
                    "stored stop. It never widens risk. Combine with trading.bracket "
                    "and broker_bars' ATR for volatility trailing. Risk-reducing: no "
                    "arm, approval or mandate."
                ),
            },
        ),
        NodeDef(
            type="trading.partial_close", version="1.0.0", label="Partial close",
            category="trading",
            inputs=(
                ticket_port,
                PortDef(id="volume", name="Volume", type=PortType.NUMBER, optional=True),
                PortDef(id="fraction", name="Fraction (0-1)", type=PortType.NUMBER, optional=True),
                PortDef(id="quote", name="Broker quote", type=PortType.OBJECT, optional=True),
            ),
            outputs=(
                PortDef(id="closed", name="Closed", type=PortType.BOOLEAN),
                PortDef(id="volume", name="Closed volume", type=PortType.NUMBER),
                PortDef(id="remainingVolume", name="Remaining volume", type=PortType.NUMBER),
                PortDef(id="verified", name="Read back", type=PortType.BOOLEAN),
                PortDef(id="moved", name="Stop moved", type=PortType.BOOLEAN),
                PortDef(id="stopLoss", name="Stop loss", type=PortType.NUMBER),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("fraction", "number", 0.5, minimum=0.0, maximum=1.0),
                ConfigField("deviation", "integer", 20, minimum=0, maximum=1_000),
            ),
            capabilities=("trading.broker",),
            side_effects=SideEffect.TRADING,
            allowed_modes=_MODES,
            executor=exec_partial_close,
            metadata={
                "docs": (
                    "Closes a fraction of the position (default 0.5) or an explicit "
                    "volume. Wire broker_quote to floor the volume to the symbol step; "
                    "without it the broker refuses an off-step volume by name rather "
                    "than Eclipse guessing. Risk-reducing: no arm, approval or mandate."
                ),
            },
        ),
    ]


__all__ = ["build_ops_nodes", "exec_break_even", "exec_trail_stop", "exec_partial_close"]
