"""Bot execution authority (P5) — the leash, checked before a live order.

A bot's tier lives on its definition (`execution_authority`), versioned with the
objective. This module is the ONE place that answers "may THIS bot place a LIVE
order right now?", so the answer cannot drift between the tool, the authorizer and
the mandate store:

  * `l0` proposes (`order_preview`) and nothing more;
  * `l1` may fill a DEMO account unattended;
  * `l2` may fill a LIVE account, but only with a mandate pinned to this bot AND
    the objective version the operator approved.

Everything here reads; the actual submit still re-checks arm, policy, guardrails,
daily-loss brake and the mandate's own caps in `trade/service.py`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

#: Default risk appetite bands. A bot may choose INSIDE its band, never outside.
APPETITE_BANDS: dict[str, dict[str, float]] = {
    "conservative": {"riskPerTradePct": 0.25, "maxPositions": 1, "maxOrdersPerDay": 2},
    "normal": {"riskPerTradePct": 0.50, "maxPositions": 2, "maxOrdersPerDay": 4},
    "aggressive": {"riskPerTradePct": 1.00, "maxPositions": 3, "maxOrdersPerDay": 8},
}


def bot_definition(automation_id: str) -> Any:
    """The bot's definition, or None. Lazy import to avoid a module cycle."""
    if not automation_id:
        return None
    try:
        from ..automation.tools import get_automation_service  # noqa: PLC0415

        service = get_automation_service()
        return service.get(automation_id) if service is not None else None
    except Exception:  # noqa: BLE001 - an unreadable definition is a refusal, not a crash
        return None


def bot_version(automation_id: str) -> Optional[int]:
    definition = bot_definition(automation_id)
    try:
        return int(definition.version) if definition is not None else None
    except (TypeError, ValueError):
        return None


def authority_of(automation_id: str) -> Any:
    definition = bot_definition(automation_id)
    return getattr(definition, "execution_authority", None) if definition else None


def _refusal(message: str, **detail: Any) -> tuple[str, dict[str, Any]]:
    return message, {"status": "REFUSED", **detail}


def _age_seconds(raw: Any) -> Optional[float]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _eclipse_open_positions(service: Any, connection_id: str) -> Optional[int]:
    """How many open positions Eclipse itself owns on this connection.

    Positions carry the connection's `magic`; counting only those keeps the cap
    from being consumed by the operator's manual trades. None when the broker read
    is unavailable.
    """
    try:
        connection = service._connection(connection_id)
        magic = int(service._magic(connection))
        payload = service.positions(connection_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or not payload.get("available", True):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        positions = data.get("positions")
    elif isinstance(data, list):
        # `BrokerService.positions` returns the adapter's list under `data`
        # (`{"available": true, "data": [...]}`), not a nested object. An EMPTY
        # list must count as "zero positions", not "unreadable".
        positions = data
    else:
        positions = payload.get("positions")
    if not isinstance(positions, list):
        return None
    if magic <= 0:
        return len(positions)
    return len([p for p in positions if int(p.get("magic") or 0) == magic])


def check_live_order(
    service: Any,
    *,
    automation_id: str,
    connection_id: str,
    mode: str,
    intent: dict[str, Any],
) -> Optional[tuple[str, dict[str, Any]]]:
    """None when a LIVE order may proceed; otherwise the refusal to return.

    Demo connections are not this function's business — they return None and the
    mandate path handles them as before.
    """
    if mode == "demo":
        return None
    if mode != "live":
        return _refusal(
            "This connection has no execution mode; set it to demo or live.",
            reason="EXECUTION_DISABLED",
        )
    if not automation_id:
        return _refusal(
            "Live orders are not available from this conversation. Use "
            "`order_preview` and approve the card — that is the one manual live "
            "path.",
            reason="LIVE_REQUIRES_BOT",
        )
    definition = bot_definition(automation_id)
    if definition is None:
        return _refusal(
            "This bot's definition could not be read, so its authority cannot be "
            "verified. No live order was sent.",
            reason="BOT_UNAVAILABLE", botId=automation_id,
        )
    authority = getattr(definition, "execution_authority", None)
    tier = str(getattr(authority, "tier", "l0") or "l0").lower()
    if tier != "l2":
        return _refusal(
            f"This bot is at tier {tier.upper()} and is not authorized to place "
            "live orders. It can propose them (order_preview) for your approval. "
            "L2 is granted by the operator, after a scored track record.",
            reason="BOT_TIER_INSUFFICIENT", tier=tier,
        )

    require_stop = bool(getattr(authority, "require_stop", True))
    if require_stop and intent.get("stopLoss") in (None, "", 0):
        return _refusal(
            "This bot's authority requires a stop loss on every order, and this "
            "intent has none. No live order was sent.",
            reason="STOP_REQUIRED",
        )

    appetite = str(getattr(authority, "appetite", "normal") or "normal").lower()
    band = APPETITE_BANDS.get(appetite, APPETITE_BANDS["normal"])
    max_positions = min(int(getattr(authority, "max_open_positions", 2) or 2),
                        int(band["maxPositions"]))
    open_positions = _eclipse_open_positions(service, connection_id)
    if open_positions is None:
        return _refusal(
            "Eclipse could not read open positions, so the position cap cannot be "
            "checked. No live order was sent.",
            reason="POSITIONS_UNAVAILABLE",
        )
    if open_positions >= max_positions:
        return _refusal(
            f"This bot is at its position cap ({open_positions}/{max_positions} "
            "open Eclipse positions). No live order was sent.",
            reason="POSITION_CAP_REACHED",
            openPositions=open_positions, maxPositions=max_positions,
        )

    cooldown = int(getattr(authority, "cooldown_s", 0) or 0)
    if cooldown > 0:
        try:
            recent = service.ledger_orders(None, limit=1, automation_id=automation_id)
        except Exception:  # noqa: BLE001
            recent = []
        if recent:
            age = _age_seconds(recent[0].get("createdAt"))
            if age is not None and age < cooldown:
                return _refusal(
                    f"This bot is in its cooldown window ({int(age)}s of "
                    f"{cooldown}s since its last order). No live order was sent.",
                    reason="COOLDOWN_ACTIVE", remainingSeconds=int(cooldown - age),
                )
    return None


def check_mandate_creation(
    service: Any,
    *,
    bot_id: str,
    bot_version: Optional[int],
    max_margin: float,
    mode: str = "live",
    max_orders: int = 0,
) -> None:
    """Validate a mandate pinned to a bot, at the moment it is created.

    The mandate is the standing grant, so this is where the bot's tier and its
    margin ceiling must hold. A LIVE grant needs L2; a DEMO grant needs L1 (the
    ladder in `docs/trading.md`: L1 fills a demo account unattended).
    """
    from .model import TradeRefused  # noqa: PLC0415

    definition = bot_definition(bot_id)
    if definition is None:
        raise TradeRefused(
            "BOT_UNAVAILABLE",
            "that bot does not exist, so it cannot be granted trading authority",
            status_code=404,
        )
    authority = getattr(definition, "execution_authority", None)
    tier = str(getattr(authority, "tier", "l0") or "l0").lower()
    order = {"l0": 0, "l1": 1, "l2": 2}
    required = "l2" if str(mode) == "live" else "l1"
    if order.get(tier, 0) < order[required]:
        raise TradeRefused(
            "BOT_TIER_INSUFFICIENT",
            f"that bot is at tier {tier.upper()}; a {mode} mandate needs "
            f"{required.upper()}",
            status_code=409,
        )
    if bot_version is None or int(bot_version) != int(definition.version):
        raise TradeRefused(
            "BOT_VERSION_STALE",
            "the mandate must pin the bot's CURRENT objective version",
            status_code=409,
        )
    cap = float(getattr(authority, "max_margin_per_order", 0) or 0)
    if cap > 0 and float(max_margin or 0) > cap:
        raise TradeRefused(
            "MANDATE_ABOVE_BOT_CAP",
            f"the mandate margin cap {float(max_margin):g} is above this bot's "
            f"ceiling {cap:g}",
            status_code=400,
        )
    if str(mode) == "live":
        # The tier is the PERMISSION; this is the EVIDENCE. A live grant needs
        # out-of-sample results AND real demo fills, both after costs. Absent
        # evidence is a refusal, never a benefit of the doubt.
        from . import proof as proof_mod  # noqa: PLC0415 - avoids a cycle

        gap = proof_mod.live_proof_gap(service, definition)
        if gap is not None:
            raise TradeRefused(
                str(gap["reason"]),
                str(gap["message"]),
                status_code=409,
                detail={"proof": gap.get("proof")},
            )
        # Proof earns live access; the ramp decides how MUCH. The requested caps
        # must fit the stage the bot's REAL live results have earned.
        from . import ramp as ramp_mod  # noqa: PLC0415 - avoids a cycle

        ramp_refusal = ramp_mod.ramp_gap(
            service, definition,
            requested_margin=float(max_margin or 0),
            requested_orders=int(max_orders or 0),
        )
        if ramp_refusal is not None:
            raise TradeRefused(
                str(ramp_refusal["reason"]),
                str(ramp_refusal["message"]),
                status_code=409,
                detail={"ramp": ramp_refusal.get("ramp")},
            )


def risk_amount(equity: float, appetite: str) -> float:
    """The money at risk for one trade, from the band. Never negative/NaN."""
    band = APPETITE_BANDS.get(str(appetite or "normal").lower(), APPETITE_BANDS["normal"])
    try:
        value = float(equity) * float(band["riskPerTradePct"]) / 100.0
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


__all__ = [
    "APPETITE_BANDS",
    "authority_of",
    "bot_definition",
    "bot_version",
    "check_live_order",
    "check_mandate_creation",
    "risk_amount",
]
