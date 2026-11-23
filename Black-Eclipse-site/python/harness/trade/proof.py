"""The strategy proof gate: out-of-sample evidence AND demo trades before live.

A bot's tier says what it is ALLOWED to do; this module says whether it has
EARNED it. Live exposure needs both halves, and neither is inferred:

  * **out-of-sample** evidence - the strategy was scored on data it was not
    fitted on, with costs included, and produced a positive expectancy. That
    record is supplied by the backtest flow (`config.proof.outOfSample`); this
    module never manufactures one.
  * **demo** evidence - real demo fills from the broker's own deal history: at
    least `minDemoTrades`, spanning at least `minDemoDays`, with positive
    expectancy after commission and swap.

Absent or unreadable evidence is a REFUSAL (`PROOF_INSUFFICIENT`), never a pass.
The gate runs when a LIVE mandate is created; a demo mandate is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .model import ticket_ref


@dataclass(frozen=True)
class ProofRequirements:
    min_demo_trades: int = 30
    min_demo_days: float = 14.0
    min_expectancy: float = 0.0
    min_oos_trades: int = 30
    max_drawdown_pct: float = 20.0
    require_costs_included: bool = True


def requirements_from(definition: Any) -> ProofRequirements:
    config = json_safe(getattr(definition, "config", None))
    raw = dict((config.get("proof") or {}).get("requirements") or {})

    def _f(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    return ProofRequirements(
        min_demo_trades=int(_f("minDemoTrades", 30)),
        min_demo_days=_f("minDemoDays", 14.0),
        min_expectancy=_f("minExpectancy", 0.0),
        min_oos_trades=int(_f("minOosTrades", 30)),
        max_drawdown_pct=_f("maxDrawdownPct", 20.0),
        require_costs_included=bool(raw.get("requireCostsIncluded", True)),
    )


def json_safe(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def evaluate_proof(
    evidence: Mapping[str, Any],
    requirements: Optional[ProofRequirements] = None,
) -> dict[str, Any]:
    """Pass only on BOTH halves. Every missing number is a named gap."""
    req = requirements or ProofRequirements()
    oos = json_safe(evidence.get("outOfSample"))
    demo = json_safe(evidence.get("demo"))
    gaps: list[str] = []

    if not oos:
        gaps.append(
            "no out-of-sample record: run the strategy on data it was not fitted "
            "on and store the result before asking for live exposure"
        )
    else:
        if _num(oos.get("trades")) < req.min_oos_trades:
            gaps.append(
                f"out-of-sample trades {_num(oos.get('trades')):g} below "
                f"{req.min_oos_trades}"
            )
        if _num(oos.get("expectancy")) <= req.min_expectancy:
            gaps.append(
                f"out-of-sample expectancy {_num(oos.get('expectancy')):g} is not "
                "positive"
            )
        if req.require_costs_included and not bool(oos.get("costsIncluded")):
            gaps.append("the out-of-sample result does not include costs")
        drawdown = _num(oos.get("maxDrawdownPct"))
        if drawdown > req.max_drawdown_pct:
            gaps.append(
                f"out-of-sample drawdown {drawdown:g}% above "
                f"{req.max_drawdown_pct:g}%"
            )

    trades = int(_num(demo.get("closedTrades")))
    days = _num(demo.get("days"))
    if trades < req.min_demo_trades:
        gaps.append(f"demo trades {trades} below {req.min_demo_trades}")
    if days < req.min_demo_days:
        gaps.append(f"demo history {days:g} days below {req.min_demo_days:g}")
    if _num(demo.get("expectancy")) <= req.min_expectancy:
        gaps.append(
            f"demo expectancy {_num(demo.get('expectancy')):g} is not positive"
        )
    demo_drawdown = _num(demo.get("maxDrawdownPct"))
    if demo_drawdown > req.max_drawdown_pct:
        gaps.append(
            f"demo drawdown {demo_drawdown:g}% above {req.max_drawdown_pct:g}%"
        )

    passed = not gaps
    return {
        "verdict": "pass" if passed else "insufficient",
        "passed": passed,
        "gaps": gaps,
        "requirements": {
            "minDemoTrades": req.min_demo_trades,
            "minDemoDays": req.min_demo_days,
            "minExpectancy": req.min_expectancy,
            "minOosTrades": req.min_oos_trades,
            "maxDrawdownPct": req.max_drawdown_pct,
        },
        "outOfSample": oos or None,
        "demo": demo or None,
    }


def demo_evidence(service: Any, automation_id: str) -> dict[str, Any]:
    """Real demo fills for this bot, from the ledger + the broker's deal history."""
    return fills_evidence(service, automation_id, mode="demo")


def fills_evidence(service: Any, automation_id: str, *, mode: str = "demo") -> dict[str, Any]:
    """Real fills for this bot on connections of ONE mode (demo or live)."""
    if not automation_id:
        return {"available": False, "closedTrades": 0, "days": 0.0,
                "expectancy": 0.0, "reason": "no automation id"}
    try:
        orders = service.ledger_orders(None, limit=1000, automation_id=automation_id)
    except Exception as exc:  # noqa: BLE001 - unreadable is a refusal, not a pass
        return {"available": False, "closedTrades": 0, "days": 0.0,
                "expectancy": 0.0, "reason": f"ledger unreadable: {exc}"}
    fills: list[dict[str, Any]] = []
    connections = sorted({str(o.get("connectionId") or "") for o in orders})
    mode_connections: list[str] = []
    for connection_id in connections:
        if not connection_id:
            continue
        try:
            connection = service._connection(connection_id)
            connection_mode = getattr(connection.mode, "value", connection.mode)
            if str(connection_mode) == mode:
                mode_connections.append(connection_id)
        except Exception:  # noqa: BLE001 - unknown mode is not evidence
            continue
    for connection_id in mode_connections:
        try:
            rows = service.connections.list_fills(connection_id, limit=1000)
        except Exception:  # noqa: BLE001
            continue
        tickets = {
            ticket_ref(o.get("ticket") or 0) for o in orders
            if str(o.get("connectionId") or "") == connection_id
        }
        fills.extend(
            row for row in rows if ticket_ref(row.get("orderTicket") or 0) in tickets
        )
    if not fills:
        return {"available": True, "closedTrades": 0, "days": 0.0,
                "expectancy": 0.0, "realizedPnl": 0.0, "maxDrawdownPct": 0.0,
                "connections": mode_connections}
    fills.sort(key=lambda row: _num(row.get("dealtAt")))
    pnl = [
        _num(f.get("profit")) - _num(f.get("commission")) - _num(f.get("swap"))
        for f in fills
    ]
    total = sum(pnl)
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross = sum(abs(v) for v in pnl) or 1.0
    span_seconds = _num(fills[-1].get("dealtAt")) - _num(fills[0].get("dealtAt"))
    return {
        "available": True,
        "closedTrades": len(fills),
        "days": round(max(0.0, span_seconds) / 86_400.0, 2),
        "expectancy": round(total / len(fills), 6),
        "realizedPnl": round(total, 2),
        "maxDrawdownPct": round(drawdown / gross * 100.0, 2),
        "connections": mode_connections,
    }


def live_proof_gap(service: Any, definition: Any) -> Optional[dict[str, Any]]:
    """None when the bot has earned live exposure; otherwise the refusal body."""
    config = json_safe(getattr(definition, "config", None))
    proof = json_safe(config.get("proof"))
    oos = json_safe(proof.get("outOfSample"))
    bot_id = str(getattr(definition, "id", "") or "")
    demo = demo_evidence(service, bot_id)
    report = evaluate_proof(
        {"outOfSample": oos, "demo": demo}, requirements_from(definition),
    )
    if report["passed"]:
        return None
    return {
        "reason": "PROOF_INSUFFICIENT",
        "message": (
            "This bot has not earned live exposure yet: "
            + "; ".join(report["gaps"][:3])
            + ". Run it on a demo account until the demo evidence clears the gate."
        ),
        "proof": report,
    }


__all__ = [
    "ProofRequirements",
    "requirements_from",
    "evaluate_proof",
    "demo_evidence",
    "live_proof_gap",
]
