"""Portfolio heat, concentration clusters, drawdown and event rules.

The per-order check answers "is THIS order too big?". This module answers the
portfolio questions a risk desk asks next:

* **Heat** - the money at risk if every open stop were hit, as a share of equity,
  plus the same figure including the planned order.
* **Concentration** - open risk grouped into deterministic clusters (quote
  currency for FX/metals, stable quote for crypto) so five long-USD pairs count
  as one bet, not five.
* **Drawdown governor** - a risk multiplier that shrinks new size as the book
  bleeds, never grows it.
* **Volatility targeting** - a stop-distance scale that makes 1R comparable
  across instruments.
* **Event blackout** - a pure gate over a supplied calendar; it never invents an
  event and never widens a stop.

Nothing here reads a broker or a clock on its own. Every figure is arithmetic
from rows the caller passes; an unmeasurable position is reported as
`unmeasured`, never counted as zero risk.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

#: Fiat currencies used to recognise an FX pair.
_FIAT = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNH", "CNY",
    "SEK", "NOK", "MXN", "ZAR", "TRY", "INR", "SGD", "HKD", "PLN", "DKK",
})

#: Stablecoins used to recognise a crypto pair and group it as a USD bet.
_STABLE = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDD")

_SYMBOL_SUFFIXES = (".US", ".L", ".HK", ".T", ".DE", ".PA", ".AS", ".TO", ".AX")


@dataclass(frozen=True)
class HeatLimits:
    #: Total open risk at stops as % of equity (0 disables the cap).
    max_heat_pct: float = 6.0
    #: One cluster's open risk as % of equity (0 disables the cap).
    max_cluster_risk_pct: float = 3.0
    #: Positions allowed in one cluster (0 disables the cap).
    max_cluster_positions: int = 3
    #: Drawdown at which size falls to `min_risk_scale` (0 disables the governor).
    max_drawdown_pct: float = 10.0
    #: Floor for the drawdown multiplier; the governor never returns 0.
    min_risk_scale: float = 0.25
    #: Target stop distance as % of price (0 disables volatility targeting).
    target_stop_vol_pct: float = 0.0


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bare_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    for suffix in _SYMBOL_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def cluster_for(symbol: Any) -> str:
    """A deterministic concentration bucket for an instrument.

    The label groups instruments whose risk tends to move together, using only
    the symbol itself (no external taxonomy to go stale):

    * crypto stable pairs -> ``CRYPTO:<stable>`` (all BTC/ETH USDT pairs together)
    * FX pairs -> ``FX:<quote>`` (EURUSD/GBPUSD/AUDUSD are one USD bet)
    * metals -> ``METALS:<quote>``
    * anything else -> its bare symbol (its own cluster)
    """
    text = bare_symbol(symbol)
    # Broker-specific FX suffixes are commonly `.a`, `.m`, `.pro`. Strip a dotted
    # suffix only when the remaining text is a valid six-letter FX pair, so an
    # equity ticker such as BRK.B keeps its identity.
    if "." in text:
        base = text.rsplit(".", 1)[0]
        if len(base) == 6 and base[:3] in _FIAT and base[3:] in _FIAT:
            text = base
    for stable in _STABLE:
        if text.endswith(stable) and len(text) > len(stable):
            return f"CRYPTO:{stable}"
    if len(text) == 6 and text[:3] in _FIAT and text[3:] in _FIAT:
        return f"FX:{text[3:]}"
    if text.startswith(("XAU", "XAG", "XPT", "XPD")):
        return f"METALS:{text[3:] or 'USD'}"
    return text or "UNKNOWN"


def position_risk(position: Mapping[str, Any]) -> Optional[float]:
    """Money at risk if this position's stop is hit, or None when unmeasurable."""
    explicit = _num(position.get("riskAmount"))
    if explicit is not None and explicit > 0:
        return explicit
    entry = _num(position.get("openPrice"))
    stop = _num(position.get("stopLoss"))
    volume = _num(position.get("volume"))
    if entry is None or stop is None or volume is None:
        return None
    if entry <= 0 or stop <= 0 or volume <= 0:
        return None
    distance = abs(entry - stop)
    if distance <= 0:
        return None
    tick_size = _num(position.get("tickSize"))
    tick_value = _num(position.get("tickValue"))
    if tick_size and tick_value and tick_size > 0 and tick_value > 0:
        per_lot = distance / tick_size * tick_value
    else:
        contract = _num(position.get("contractSize"))
        if contract is None or contract <= 0:
            return None
        per_lot = distance * contract
    return volume * per_lot


def assess_heat(
    *,
    positions: Iterable[Mapping[str, Any]],
    equity: float,
    planned_risk: float,
    planned_symbol: str,
    limits: Optional[HeatLimits] = None,
) -> dict[str, Any]:
    """Measure the book's open risk and whether the planned order fits it."""
    limits = limits or HeatLimits()
    equity = _num(equity) or 0.0
    planned_risk = max(0.0, _num(planned_risk) or 0.0)

    rows = [row for row in positions if isinstance(row, Mapping)]
    cluster_risk: dict[str, float] = {}
    cluster_count: dict[str, int] = {}
    measured = 0.0
    measured_positions = 0
    unmeasured: list[dict[str, Any]] = []

    for row in rows:
        risk = position_risk(row)
        if risk is None or risk <= 0:
            unmeasured.append({
                "ticket": row.get("ticket"),
                "symbol": row.get("symbol"),
                "reason": "no stop or no contract/tick value, so its risk is unknown",
            })
            continue
        cluster = cluster_for(row.get("symbol"))
        measured += risk
        measured_positions += 1
        cluster_risk[cluster] = cluster_risk.get(cluster, 0.0) + risk
        cluster_count[cluster] = cluster_count.get(cluster, 0) + 1

    planned_cluster = cluster_for(planned_symbol)
    heat_pct = measured / equity * 100.0 if equity > 0 else None
    after_heat_pct = (measured + planned_risk) / equity * 100.0 if equity > 0 else None
    cluster_before = cluster_risk.get(planned_cluster, 0.0)
    cluster_after_pct = (
        (cluster_before + planned_risk) / equity * 100.0 if equity > 0 else None
    )

    reasons: list[str] = []
    warnings: list[str] = []
    blocking = False

    if equity <= 0:
        unmeasured.append({"ticket": None, "symbol": None,
                           "reason": "no positive equity to express heat against"})
    else:
        if limits.max_heat_pct > 0 and after_heat_pct is not None \
                and after_heat_pct > limits.max_heat_pct:
            blocking = True
            reasons.append(
                f"Open risk after this order would be {after_heat_pct:.2f}% of "
                f"equity (heat limit {limits.max_heat_pct:g}%)."
            )
        if limits.max_cluster_risk_pct > 0 and cluster_after_pct is not None \
                and cluster_after_pct > limits.max_cluster_risk_pct:
            blocking = True
            reasons.append(
                f"{planned_cluster} would carry {cluster_after_pct:.2f}% of equity "
                f"at risk (cluster limit {limits.max_cluster_risk_pct:g}%)."
            )
        if limits.max_cluster_positions > 0 \
                and cluster_count.get(planned_cluster, 0) >= limits.max_cluster_positions:
            blocking = True
            reasons.append(
                f"{planned_cluster} already has {cluster_count[planned_cluster]} "
                f"open position(s) (limit {limits.max_cluster_positions}); this "
                "would concentrate the book."
            )
        if after_heat_pct is not None and limits.max_heat_pct > 0 \
                and not blocking and after_heat_pct >= limits.max_heat_pct * 0.8:
            warnings.append(
                f"Open risk after this order is {after_heat_pct:.2f}% of equity, "
                f"near the {limits.max_heat_pct:g}% heat limit."
            )

    if unmeasured:
        warnings.append(
            f"{len(unmeasured)} open position(s) could not be measured for heat; "
            "the total is a lower bound, not the full book."
        )

    return {
        "verdict": "block" if blocking else ("warn" if warnings else "ok"),
        "blocking": blocking,
        "reasons": reasons,
        "warnings": warnings,
        "unmeasured": unmeasured,
        "equity": round(equity, 2) if equity else None,
        "measuredRisk": round(measured, 2),
        "measuredPositions": measured_positions,
        "plannedRisk": round(planned_risk, 2),
        "heatPct": round(heat_pct, 2) if heat_pct is not None else None,
        "afterHeatPct": round(after_heat_pct, 2) if after_heat_pct is not None else None,
        "plannedCluster": planned_cluster,
        "clusterRisk": {key: round(value, 2) for key, value in sorted(cluster_risk.items())},
        "clusterAfterPct": (
            round(cluster_after_pct, 2) if cluster_after_pct is not None else None
        ),
        "limits": {
            "maxHeatPct": limits.max_heat_pct,
            "maxClusterRiskPct": limits.max_cluster_risk_pct,
            "maxClusterPositions": limits.max_cluster_positions,
        },
    }


def drawdown_scale(drawdown_pct: float, limits: Optional[HeatLimits] = None) -> float:
    """Risk multiplier for a drawdown: 1 at zero, the floor at the limit, never >1."""
    limits = limits or HeatLimits()
    if limits.max_drawdown_pct <= 0:
        return 1.0
    drop = _num(drawdown_pct) or 0.0
    if drop <= 0:
        return 1.0
    scale = 1.0 - drop / limits.max_drawdown_pct
    return max(limits.min_risk_scale, min(1.0, scale))


def volatility_scale(
    stop_distance_pct: float,
    limits: Optional[HeatLimits] = None,
) -> float:
    """Scale risk so the stop distance approximates the configured target."""
    limits = limits or HeatLimits()
    target = limits.target_stop_vol_pct
    distance = _num(stop_distance_pct) or 0.0
    if target <= 0 or distance <= 0:
        return 1.0
    return max(0.25, min(2.0, target / distance))


def _parse_time(value: Any) -> Optional[_dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _impact(event: Mapping[str, Any]) -> str:
    for key in ("impact", "importance", "priority"):
        text = str(event.get(key) or "").strip().lower()
        if text:
            return text
    return ""


def event_blackout(
    events: Iterable[Mapping[str, Any]],
    now: Any,
    *,
    window_minutes: float = 30.0,
    min_impact: str = "high",
) -> dict[str, Any]:
    """Block entries inside the window around a high-impact event.

    The caller supplies the calendar; this function never fetches one. A missing
    or unparseable time is ignored rather than treated as an event.
    """
    moment = _parse_time(now)
    if moment is None:
        return {"blocking": False, "event": None, "reason": "", "checked": 0}
    wanted = min_impact.strip().lower()
    checked = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        when = _parse_time(
            event.get("time") or event.get("date") or event.get("timestamp")
            or event.get("datetime")
        )
        if when is None:
            continue
        checked += 1
        impact = _impact(event)
        if wanted and impact and impact != wanted:
            continue
        if wanted and not impact:
            continue
        delta_min = abs((when - moment).total_seconds()) / 60.0
        if delta_min <= window_minutes:
            return {
                "blocking": True,
                "event": event,
                "minutesToEvent": round((when - moment).total_seconds() / 60.0, 1),
                "reason": (
                    f"a {impact or 'high'}-impact event is within "
                    f"{window_minutes:g} min; new entries are paused"
                ),
                "checked": checked,
            }
    return {"blocking": False, "event": None, "reason": "", "checked": checked}


__all__ = [
    "HeatLimits",
    "assess_heat",
    "bare_symbol",
    "cluster_for",
    "drawdown_scale",
    "event_blackout",
    "position_risk",
    "volatility_scale",
]
