"""The gated live ramp: small money first, more only after live results.

Proof gets a bot INTO live trading; the ramp decides how MUCH money it may put
there. A live mandate's margin cap is clamped by the bot's current stage, and the
stage is earned from REAL live fills - not from optimism:

  probe  - no live track record (or a losing one): 25% of the bot's ceiling
  build  - >= 20 live fills, positive expectancy: 50%
  scale  - >= 50 live fills, positive expectancy: 100%

A stage is capped by the bot's own `max_margin_per_order`, so the ramp can only
make exposure smaller. A losing streak or a drawdown above the stage cap steps
the bot DOWN one stage; it never steps up on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RampStage:
    index: int
    label: str
    margin_fraction: float
    min_live_trades: int
    max_drawdown_pct: float


STAGES: tuple[RampStage, ...] = (
    RampStage(0, "probe", 0.25, 0, 10.0),
    RampStage(1, "build", 0.50, 20, 15.0),
    RampStage(2, "scale", 1.00, 50, 20.0),
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _config(definition: Any) -> dict[str, Any]:
    raw = getattr(definition, "config", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def caps_from(definition: Any) -> dict[str, float]:
    """The bot's own ceiling; the ramp never exceeds it."""
    authority = getattr(definition, "execution_authority", None)
    return {
        "maxMargin": _num(getattr(authority, "max_margin_per_order", 0)),
        "maxOrders": _num(getattr(authority, "max_orders", 0)) or 1.0,
    }


def stage_for(evidence: Mapping[str, Any], definition: Any = None) -> RampStage:
    """The highest stage the LIVE evidence earns; the lowest on bad evidence."""
    config = _config(definition)
    ramp = config.get("ramp") if isinstance(config.get("ramp"), Mapping) else {}
    trades = int(_num(evidence.get("closedTrades")))
    expectancy = _num(evidence.get("expectancy"))
    drawdown = _num(evidence.get("maxDrawdownPct"))

    stage = STAGES[0]
    for candidate in STAGES:
        override = ramp.get(candidate.label) if isinstance(ramp, Mapping) else None
        need = int(_num((override or {}).get("minLiveTrades"), candidate.min_live_trades))
        if trades >= need:
            stage = candidate
    # Bad results step DOWN one stage; they never step up.
    if trades and (expectancy <= 0 or drawdown > stage.max_drawdown_pct):
        stage = STAGES[max(0, stage.index - 1)]
    return stage


def ramp_limits(
    definition: Any, evidence: Mapping[str, Any], *, requested_orders: Optional[int] = None,
) -> dict[str, Any]:
    """The live caps this stage allows, plus what raising them needs."""
    caps = caps_from(definition)
    stage = stage_for(evidence, definition)
    max_margin = round(caps["maxMargin"] * stage.margin_fraction, 2)
    max_orders = max(1, int(caps["maxOrders"] * stage.margin_fraction))
    next_stage = STAGES[stage.index + 1] if stage.index + 1 < len(STAGES) else None
    return {
        "stage": stage.label,
        "stageIndex": stage.index,
        "marginFraction": stage.margin_fraction,
        "maxMargin": max_margin,
        "maxOrders": max_orders,
        "botMaxMargin": round(caps["maxMargin"], 2),
        "botMaxOrders": int(caps["maxOrders"]),
        "nextStage": next_stage.label if next_stage else None,
        "nextStageNeeds": (
            {
                "minLiveTrades": next_stage.min_live_trades,
                "positiveExpectancy": True,
                "maxDrawdownPct": next_stage.max_drawdown_pct,
            }
            if next_stage else None
        ),
    }


def live_evidence(service: Any, automation_id: str) -> dict[str, Any]:
    """Real LIVE fills for this bot, from the ledger + the broker's deal history."""
    from .proof import fills_evidence  # noqa: PLC0415 - shared attribution

    return fills_evidence(service, automation_id, mode="live")


def ramp_gap(
    service: Any, definition: Any, *, requested_margin: float, requested_orders: int,
) -> Optional[dict[str, Any]]:
    """None when the requested live caps fit the stage; otherwise the refusal."""
    evidence = live_evidence(service, str(getattr(definition, "id", "") or ""))
    limits = ramp_limits(definition, evidence, requested_orders=requested_orders)
    problems: list[str] = []
    if requested_margin > limits["maxMargin"] > 0:
        problems.append(
            f"margin cap {requested_margin:g} above the {limits['stage']} stage "
            f"cap {limits['maxMargin']:g}"
        )
    if requested_orders > limits["maxOrders"]:
        problems.append(
            f"{requested_orders} orders above the {limits['stage']} stage cap "
            f"{limits['maxOrders']}"
        )
    if not problems:
        return None
    need = limits.get("nextStageNeeds")
    if need:
        problems.append(
            f"the next stage ({limits['nextStage']}) needs "
            f"{need['minLiveTrades']} live fills with positive expectancy"
        )
    return {
        "reason": "RAMP_CAP_EXCEEDED",
        "message": "Live exposure is ramped: " + "; ".join(problems) + ".",
        "ramp": limits,
        "evidence": evidence,
    }


__all__ = [
    "RampStage",
    "STAGES",
    "caps_from",
    "stage_for",
    "ramp_limits",
    "live_evidence",
    "ramp_gap",
]
