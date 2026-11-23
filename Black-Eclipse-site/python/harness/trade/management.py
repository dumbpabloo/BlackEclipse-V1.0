"""Deterministic stop management: break-even, ATR trailing, giveback and time stops.

This is the policy layer the broker cannot provide because it is the same across
every venue: a stop that only ever tightens, a break-even move that protects
capital, a floor that keeps a share of open profit, and a time stop for trades
that go nowhere.

Two hard rules, both enforced here rather than trusted to a caller:

* **A stop may only move in the position's favour.** A candidate that is not at
  least ``min_step`` better than the stored stop is refused, never applied. A
  flat or adverse tick can therefore never widen risk.
* **No number is invented.** R comes from the position's own entry and stop; the
  ATR comes from the broker's bars. If either is missing the action is refused
  with a named reason instead of guessed.

`evaluate_management` is pure (no adapter, no I/O) so the arithmetic is testable
without a broker. `PositionManager` is the thin applier used by a background
sweep; every action it performs is risk-reducing (modify stop / close), which the
service already allows without an approval or a live arm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .analysis import atr as _atr

#: Modes the engine understands. `off` is explicit so a stored policy cannot be
#: mistaken for the default.
MODES = ("off", "break_even", "atr_trail", "percent_trail")


@dataclass(frozen=True)
class PartialTake:
    """Close `fraction` of the position once it reaches `at_r`."""

    at_r: float
    fraction: float


@dataclass(frozen=True)
class ManagementPolicy:
    mode: str = "off"
    #: Move the stop to entry once price is this many R in profit.
    break_even_at_r: float = 1.0
    #: Leave this fraction of R as a buffer beyond entry (0 = exact entry).
    break_even_buffer_r: float = 0.0
    #: ATR period used by `atr_trail`.
    atr_period: int = 14
    #: Trail distance in ATR units.
    atr_multiple: float = 2.5
    #: Percent of price used by `percent_trail` (0.02 = 2%).
    percent: float = 0.0
    #: A candidate must beat the stored stop by this price distance to be applied.
    min_step: float = 0.0
    #: Once favourable by this many R, keep at least this many R of profit locked.
    giveback_cap_r: Optional[float] = None
    #: Close a trade that has not reached `time_stop_min_r` after this many minutes.
    time_stop_minutes: Optional[float] = None
    time_stop_min_r: float = 0.0
    #: Broker's minimum distance between the current price and a stop, in price units.
    min_stop_distance: float = 0.0
    #: Scale-out schedule; each entry fires once (state kept by the caller).
    partials: tuple[PartialTake, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, raw: Any) -> "ManagementPolicy":
        """Read `config.management`; malformed fields keep the safe default."""
        if not isinstance(raw, Mapping):
            return cls()
        mode = str(raw.get("mode") or "off").strip().lower()
        if mode not in MODES:
            mode = "off"

        def _f(key: str, default: float, *, positive: bool = True) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            if not math.isfinite(value):
                return default
            if positive and value <= 0:
                return default
            return value

        def _f0(key: str, default: float) -> float:
            """Like `_f` but 0 is meaningful (0 disables the rule)."""
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            if not math.isfinite(value) or value < 0:
                return default
            return value

        cap = raw.get("givebackCapR")
        giveback = None
        if cap is not None:
            try:
                parsed = float(cap)
                giveback = parsed if math.isfinite(parsed) and parsed > 0 else None
            except (TypeError, ValueError):
                giveback = None

        minutes = raw.get("timeStopMinutes")
        time_stop = None
        if minutes is not None:
            try:
                parsed = float(minutes)
                time_stop = parsed if math.isfinite(parsed) and parsed > 0 else None
            except (TypeError, ValueError):
                time_stop = None

        partials: list[PartialTake] = []
        raw_partials = raw.get("partials")
        if isinstance(raw_partials, list):
            for item in raw_partials:
                if not isinstance(item, Mapping):
                    continue
                try:
                    at_r = float(item.get("atR"))
                    fraction = float(item.get("fraction"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(at_r) and math.isfinite(fraction) \
                        and at_r > 0 and 0 < fraction < 1:
                    partials.append(PartialTake(at_r=at_r, fraction=fraction))
        partials.sort(key=lambda part: part.at_r)

        return cls(
            mode=mode,
            break_even_at_r=_f0("breakEvenAtR", 1.0),
            break_even_buffer_r=_f("breakEvenBufferR", 0.0, positive=False),
            atr_period=max(2, int(_f("atrPeriod", 14))),
            atr_multiple=_f("atrMultiple", 2.5),
            percent=_f("percent", 0.0, positive=False),
            min_step=_f("minStep", 0.0, positive=False),
            giveback_cap_r=giveback,
            time_stop_minutes=time_stop,
            time_stop_min_r=_f("timeStopMinR", 0.0, positive=False),
            min_stop_distance=_f("minStopDistance", 0.0, positive=False),
            partials=tuple(partials),
        )


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bars_high_low(bars: Iterable[Mapping[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    highs = [value for bar in bars if (value := _num(bar.get("high"))) is not None]
    lows = [value for bar in bars if (value := _num(bar.get("low"))) is not None]
    return (max(highs) if highs else None, min(lows) if lows else None)


def _better(side: str, candidate: float, current: Optional[float], min_step: float) -> bool:
    if current is None:
        return True
    if side == "buy":
        return candidate >= current + min_step
    return candidate <= current - min_step


def _respects_distance(side: str, price: float, stop: float, distance: float) -> bool:
    if distance <= 0:
        return True
    return abs(price - stop) >= distance


def _refused(reason: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "actions": [],
        "stopLoss": extra.pop("stopLoss", None),
        "risk": extra.pop("risk", None),
        "favorableR": None,
        "atr": None,
        "code": code,
        "reason": reason,
        **extra,
    }


def evaluate_management(
    position: Mapping[str, Any],
    policy: ManagementPolicy,
    *,
    quote: Optional[Mapping[str, Any]] = None,
    bars: Optional[Iterable[Mapping[str, Any]]] = None,
    price: Optional[float] = None,
    now: Optional[float] = None,
    opened_at: Optional[float] = None,
    partials_fired: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Return the risk-reducing actions a policy wants for one open position.

    ``position`` uses the adapter vocabulary (``side``, ``openPrice``,
    ``stopLoss``, ``volume``). ``price`` wins over the quote/position mark so a
    caller can feed the broker's bid/ask deliberately.
    """
    if policy.mode == "off":
        return _refused("management is off for this position", "MANAGEMENT_OFF")

    side = str(position.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return _refused("the position has no usable side", "SIDE_INVALID")

    entry = _num(position.get("openPrice"))
    stop = _num(position.get("stopLoss"))
    if entry is None or entry <= 0:
        return _refused("the position has no usable entry price", "ENTRY_UNAVAILABLE")
    if stop is None or stop <= 0:
        return _refused(
            "the position has no stop, so there is no R to manage; set a stop first",
            "NO_STOP",
            stopLoss=None,
        )

    mark = _num(price)
    if mark is None:
        mark = _num(position.get("currentPrice"))
    if mark is None and quote is not None:
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        mark = ask if side == "buy" else bid
        if mark is None:
            mark = bid if bid is not None else ask
    if mark is None or mark <= 0:
        return _refused("no usable current price to measure the trade", "PRICE_UNAVAILABLE")

    risk = abs(entry - stop)
    if risk <= 0:
        return _refused("the stop equals the entry, so R is zero", "RISK_ZERO")

    direction = 1.0 if side == "buy" else -1.0
    favorable = (mark - entry) * direction
    favorable_r = favorable / risk
    # Normalise once: a generator must not be consumed twice (ATR, then highs).
    bar_rows = list(bars) if bars is not None else None

    current_stop: Optional[float] = stop
    candidates: list[tuple[str, str, float]] = []  # (kind, code, stop price)

    # 1. Break-even (+ optional buffer), once and only once.
    if policy.break_even_at_r > 0 and favorable_r >= policy.break_even_at_r:
        be_target = entry + direction * policy.break_even_buffer_r * risk
        if _better(side, be_target, current_stop, policy.min_step):
            candidates.append(("break_even", "BREAK_EVEN_TRIGGERED", be_target))

    # 2. Profit floor: keep `giveback_cap_r` of the best favourable move.
    if policy.giveback_cap_r and favorable_r > policy.giveback_cap_r:
        floor_r = favorable_r - policy.giveback_cap_r
        floor = entry + direction * floor_r * risk
        if _better(side, floor, current_stop, policy.min_step):
            candidates.append(("giveback", "GIVEBACK_FLOOR", floor))

    # 3. Trailing stop.
    if policy.mode in ("atr_trail", "percent_trail"):
        if policy.mode == "atr_trail":
            atr_value = None
            if bar_rows is not None:
                atr_value = _atr(bar_rows, policy.atr_period)
            if atr_value is None or atr_value <= 0:
                return _refused(
                    "ATR trailing needs the broker's bars; none were readable",
                    "ATR_UNAVAILABLE",
                    stopLoss=current_stop,
                    risk=round(risk, 8),
                    favorableR=round(favorable_r, 4),
                )
            lookback_high, lookback_low = _bars_high_low(bar_rows or [])
            reference = lookback_high if side == "buy" else lookback_low
            if reference is None:
                reference = mark
            distance = atr_value * policy.atr_multiple
            trail = reference - direction * distance
            detail = {"atr": round(atr_value, 8), "atrDistance": round(distance, 8)}
        else:
            if policy.percent <= 0:
                return _refused(
                    "percent trailing needs a positive percent setting",
                    "PERCENT_UNAVAILABLE",
                    stopLoss=current_stop,
                    risk=round(risk, 8),
                    favorableR=round(favorable_r, 4),
                )
            distance = mark * policy.percent
            trail = mark - direction * distance
            detail = {"percentDistance": round(distance, 8)}
        if _better(side, trail, current_stop, policy.min_step):
            candidates.append(("trail", "TRAIL_TIGHTENED", trail))
        # Keep the detail for the result below.
    else:
        detail = {}

    # Pick the tightest safe candidate. For a buy the highest stop is tightest;
    # for a sell the lowest. Re-check the broker's minimum stop distance.
    actions: list[dict[str, Any]] = []
    if candidates:
        tightest = max(candidates, key=lambda row: row[2]) if side == "buy" \
            else min(candidates, key=lambda row: row[2])
        kind, code, candidate = tightest
        if not _respects_distance(side, mark, candidate, policy.min_stop_distance):
            actions_reason = (
                f"the {kind} stop {candidate:g} is inside the broker's minimum "
                f"stop distance {policy.min_stop_distance:g}"
            )
            actions.append({
                "kind": "modify_stop", "price": None, "code": "STOP_TOO_CLOSE",
                "reason": actions_reason, "applied": False,
            })
        else:
            actions.append({
                "kind": "modify_stop", "price": round(candidate, 8), "code": code,
                "reason": f"{kind} stop -> {candidate:g}", "applied": False,
            })

    # 4. Time stop: a trade that has gone nowhere for too long is capital tied up.
    if policy.time_stop_minutes and now is not None and opened_at is not None:
        elapsed_min = (now - opened_at) / 60.0
        if elapsed_min >= policy.time_stop_minutes and favorable_r <= policy.time_stop_min_r:
            actions.append({
                "kind": "close", "price": None, "code": "TIME_STOP",
                "reason": (
                    f"held {elapsed_min:.0f} min without reaching "
                    f"{policy.time_stop_min_r:g}R (time stop "
                    f"{policy.time_stop_minutes:g} min)"
                ),
                "applied": False,
            })

    # 5. Partial take-profits (stateful: the caller names the ones already fired).
    fired = partials_fired or set()
    for part in getattr(policy, "partials", []) or []:
        key = f"{part.at_r:g}"
        if key in fired:
            continue
        if favorable_r >= part.at_r and 0 < part.fraction < 1:
            actions.append({
                "kind": "partial_close", "fraction": part.fraction,
                "atR": part.at_r, "code": "PARTIAL_TAKE",
                "reason": f"take {part.fraction:g} at {part.at_r:g}R",
                "applied": False,
            })

    if not actions:
        return {
            "ok": True, "actions": [], "stopLoss": round(current_stop, 8) if current_stop else None,
            "risk": round(risk, 8), "favorableR": round(favorable_r, 4),
            "atr": detail.get("atr"), "code": "", "reason": "no management action due",
        }

    return {
        "ok": True,
        "actions": actions,
        "stopLoss": round(current_stop, 8) if current_stop else None,
        "risk": round(risk, 8),
        "favorableR": round(favorable_r, 4),
        "atr": detail.get("atr"),
        "code": actions[0]["code"],
        "reason": actions[0]["reason"],
        **detail,
    }


class PositionManager:
    """Apply a `ManagementPolicy` to a broker service's open positions.

    It never opens exposure. Every action is a stop move or a close, both of which
    the service already treats as risk-reducing (no approval, mandate or arm).
    A position whose policy is `off`, or whose numbers are unreadable, is left
    alone with a reason.
    """

    def __init__(self, service: Any, policy: ManagementPolicy) -> None:
        self.service = service
        self.policy = policy
        self._partials_fired: dict[str, set[str]] = {}

    def _policy_for(self, position: Mapping[str, Any]) -> ManagementPolicy:
        override = position.get("management")
        if isinstance(override, Mapping):
            return ManagementPolicy.from_config(override)
        return self.policy

    def manage(self, connection_id: str, positions: Optional[Iterable[Mapping[str, Any]]] = None,
               *, quote: Optional[Mapping[str, Any]] = None,
               bars: Optional[Iterable[Mapping[str, Any]]] = None,
               now: Optional[float] = None) -> dict[str, Any]:
        rows = list(positions) if positions is not None else []
        if positions is None:
            result = self.service.positions(connection_id)
            rows = list(result.get("data") or []) if result.get("available") else []

        reports: list[dict[str, Any]] = []
        for position in rows:
            if not isinstance(position, Mapping):
                continue
            ticket = position.get("ticket")
            policy = self._policy_for(position)
            if policy.mode == "off":
                reports.append({"ticket": ticket, "ok": True, "actions": [],
                                "reason": "management is off"})
                continue
            key = str(ticket)
            fired = self._partials_fired.setdefault(key, set())
            evaluation = evaluate_management(
                position, policy, quote=quote, bars=bars, now=now,
                partials_fired=fired,
            )
            applied: list[dict[str, Any]] = []
            for action in evaluation.get("actions", []):
                if action["kind"] == "modify_stop" and action.get("price") is not None:
                    outcome = self.service.modify_position(
                        connection_id, ticket, stop_loss=action["price"],
                    )
                    detail = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
                    moved = bool(outcome.get("modified"))
                    action["applied"] = moved
                    action["verified"] = detail.get("verified")
                    action["brokerReason"] = detail.get("retcodeName") or detail.get("error")
                elif action["kind"] == "partial_close":
                    held = _num(position.get("volume")) or 0.0
                    volume = round(held * float(action["fraction"]), 8)
                    if volume > 0:
                        outcome = self.service.close_position(
                            connection_id, ticket, volume=volume,
                        )
                        action["applied"] = bool(outcome.get("closed"))
                        action["volume"] = volume
                        if action["applied"]:
                            fired.add(f"{action.get('atR', 0):g}")
                elif action["kind"] == "close":
                    outcome = self.service.close_position(connection_id, ticket)
                    action["applied"] = bool(outcome.get("closed"))
                applied.append(action)
            reports.append({
                "ticket": ticket,
                "ok": evaluation.get("ok"),
                "favorableR": evaluation.get("favorableR"),
                "atr": evaluation.get("atr"),
                "actions": applied,
                "reason": evaluation.get("reason"),
                "code": evaluation.get("code"),
            })
        return {
            "connectionId": connection_id,
            "positions": len(rows),
            "managed": sum(1 for row in reports if any(a.get("applied") for a in row["actions"])),
            "reports": reports,
        }


__all__ = [
    "MODES",
    "ManagementPolicy",
    "PartialTake",
    "PositionManager",
    "evaluate_management",
]
