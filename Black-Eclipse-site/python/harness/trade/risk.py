"""Pre-trade risk: does this order fit the ACCOUNT, not just the budget?

`size_position` answers "how big so the stop costs 1%?". This module answers the
next question a risk desk asks: "and what does it do to the book?" - open
positions, same-symbol concentration, margin headroom and the spread's share of
the risk. It is advisory AND blocking: a plan that breaches a configured limit is
reported with `blocking=True` before the operator ever sees an approval card.

The limits live on the connection's `config.risk` (so they are per account, not
global) with conservative defaults. Nothing here invents a number: if the broker
did not report margin or spread, that check is `unmeasured`, not passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class RiskLimits:
    max_open_positions: int = 10
    max_symbol_positions: int = 3
    max_risk_pct: float = 2.0
    max_margin_pct: float = 50.0
    min_margin_level_pct: float = 150.0
    max_spread_pct_of_risk: float = 25.0
    #: Portfolio heat / concentration / drawdown / volatility (0 = off).
    max_heat_pct: float = 6.0
    max_cluster_risk_pct: float = 3.0
    max_cluster_positions: int = 3
    max_drawdown_pct: float = 10.0
    min_risk_scale: float = 0.25
    target_stop_vol_pct: float = 0.0


def limits_from_connection(config: Optional[Mapping[str, Any]]) -> RiskLimits:
    """Read `config.risk` with defaults; bad values fall back, never widen."""
    raw = dict((config or {}).get("risk") or {})

    def _int(key: str, default: int) -> int:
        try:
            return max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _float0(key: str, default: float) -> float:
        """Like `_float` but 0 is meaningful (0 disables the rule)."""
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    return RiskLimits(
        max_open_positions=_int("maxOpenPositions", 10),
        max_symbol_positions=_int("maxSymbolPositions", 3),
        max_risk_pct=_float("maxRiskPct", 2.0),
        max_margin_pct=_float("maxMarginPct", 50.0),
        min_margin_level_pct=_float("minMarginLevelPct", 150.0),
        max_spread_pct_of_risk=_float("maxSpreadPctOfRisk", 25.0),
        max_heat_pct=_float0("maxHeatPct", 6.0),
        max_cluster_risk_pct=_float0("maxClusterRiskPct", 3.0),
        max_cluster_positions=_int("maxClusterPositions", 3),
        max_drawdown_pct=_float0("maxDrawdownPct", 10.0),
        min_risk_scale=_float("minRiskScale", 0.25),
        target_stop_vol_pct=_float0("targetStopVolPct", 0.0),
    )


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _bare(symbol: str) -> str:
    text = str(symbol or "").upper()
    for suffix in (".US", ".L", ".HK", ".T", ".DE", ".PA", ".AS"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def assess_trade_risk(
    *,
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    symbol: str,
    planned_risk: float,
    risk_pct: float,
    estimated_margin: Optional[float] = None,
    spread_points: Optional[float] = None,
    point: float = 0.0,
    tick_value: float = 0.0,
    tick_size: float = 0.0,
    distance: float = 0.0,
    limits: Optional[RiskLimits] = None,
) -> dict[str, Any]:
    """Assess an order against the account's own book and limits."""
    limits = limits or RiskLimits()
    equity = _num(account.get("equity")) or _num(account.get("balance"))
    margin_used = _num(account.get("margin"))
    margin_free = _num(account.get("marginFree", account.get("freeMargin")))
    margin_level = _num(account.get("marginLevel")) or None

    rows = [row for row in positions if isinstance(row, Mapping)]
    open_count = len(rows)
    want = _bare(symbol)
    same_symbol = [row for row in rows if _bare(row.get("symbol")) == want]
    same_side = [
        row for row in same_symbol
        if str(row.get("side") or "").strip().lower() in ("buy", "long")
    ]
    opposite = [row for row in same_symbol if row not in same_side]

    reasons: list[str] = []
    warnings: list[str] = []
    unmeasured: list[str] = []
    blocking = False

    if limits.max_open_positions and open_count >= limits.max_open_positions:
        blocking = True
        reasons.append(
            f"The account already holds {open_count} positions (limit "
            f"{limits.max_open_positions})."
        )
    if limits.max_symbol_positions and len(same_symbol) >= limits.max_symbol_positions:
        blocking = True
        reasons.append(
            f"{symbol} already has {len(same_symbol)} open position(s) (limit "
            f"{limits.max_symbol_positions}); this would concentrate the book."
        )
    if risk_pct > limits.max_risk_pct:
        blocking = True
        reasons.append(
            f"The plan risks {risk_pct:g}% of equity; the account limit is "
            f"{limits.max_risk_pct:g}%."
        )

    margin_after = None
    if estimated_margin is not None and equity > 0:
        margin_after = margin_used + estimated_margin
        pct = margin_after / equity * 100.0
        if pct > limits.max_margin_pct:
            blocking = True
            reasons.append(
                f"Margin after this order would be {pct:.0f}% of equity (limit "
                f"{limits.max_margin_pct:g}%)."
            )
    elif equity <= 0:
        unmeasured.append("margin: the broker did not report equity")
    if margin_level is not None and margin_level < limits.min_margin_level_pct:
        blocking = True
        reasons.append(
            f"Margin level is {margin_level:.0f}% (floor "
            f"{limits.min_margin_level_pct:g}%); add funds before adding risk."
        )

    spread_cost = None
    spread_pct = None
    if (
        spread_points is not None
        and point > 0
        and tick_value > 0
        and tick_size > 0
    ):
        spread_cost = spread_points * point / tick_size * tick_value
        if planned_risk > 0:
            spread_pct = spread_cost / planned_risk * 100.0
            if spread_pct > limits.max_spread_pct_of_risk:
                blocking = True
                reasons.append(
                    f"The spread alone is {spread_pct:.0f}% of the planned risk "
                    f"(limit {limits.max_spread_pct_of_risk:g}%)."
                )
    elif spread_points is not None:
        unmeasured.append("spread cost: the broker did not report a tick value")

    if same_symbol and not opposite:
        warnings.append(
            f"You already hold {len(same_symbol)} {symbol} position(s) in the same "
            "direction; this adds to that exposure."
        )
    if opposite:
        warnings.append(
            f"There are {len(opposite)} {symbol} position(s) in the opposite "
            "direction; check whether this is a hedge or an accidental reversal."
        )
    if 0 < risk_pct <= limits.max_risk_pct and planned_risk > 0 and equity > 0:
        warnings.append(
            f"Max loss on this order is {planned_risk:,.2f}, "
            f"{risk_pct:.2f}% of equity."
        )
    if margin_free > 0 and estimated_margin is not None and estimated_margin > margin_free:
        blocking = True
        reasons.append(
            f"Estimated margin {estimated_margin:,.2f} exceeds free margin "
            f"{margin_free:,.2f}."
        )

    verdict = "block" if blocking else ("warn" if warnings else "ok")
    return {
        "verdict": verdict,
        "blocking": blocking,
        "reasons": reasons,
        "warnings": warnings,
        "unmeasured": unmeasured,
        "openPositions": open_count,
        "symbolPositions": len(same_symbol),
        "sameSide": len(same_side),
        "oppositeSide": len(opposite),
        "equity": round(equity, 2) if equity else None,
        "marginUsed": round(margin_used, 2) if margin_used else None,
        "marginFree": round(margin_free, 2) if margin_free else None,
        "marginLevelPct": round(margin_level, 1) if margin_level else None,
        "estimatedMargin": round(estimated_margin, 2) if estimated_margin is not None else None,
        "spreadCost": round(spread_cost, 2) if spread_cost is not None else None,
        "spreadPctOfRisk": round(spread_pct, 1) if spread_pct is not None else None,
        "limits": {
            "maxOpenPositions": limits.max_open_positions,
            "maxSymbolPositions": limits.max_symbol_positions,
            "maxRiskPct": limits.max_risk_pct,
            "maxMarginPct": limits.max_margin_pct,
            "minMarginLevelPct": limits.min_margin_level_pct,
            "maxSpreadPctOfRisk": limits.max_spread_pct_of_risk,
        },
    }


__all__ = ["RiskLimits", "limits_from_connection", "assess_trade_risk"]
