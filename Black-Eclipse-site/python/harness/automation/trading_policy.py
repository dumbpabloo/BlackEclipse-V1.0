"""Pure trading-bot policy (stdlib only).

These are the checks the strategy runner performs before it asks the broker for
anything. They live in their own module, free of pydantic/FastAPI/broker imports,
so they can be unit-tested with a plain interpreter and cannot accidentally hold
a market connection open.

Mirrored, code-for-code, by `src/lib/bots/trading/{consent,symbols,validate}.ts`.
When a rule changes, both sides change together — the TS side exists to show the
same message beside the same field, never to be the authority.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

MODES = ("shadow", "demo", "live")
TIERS = ("l0", "l1", "l2")
#: Default universe size for a bot form that does not choose one.
MAX_SYMBOLS = 8
#: Hard ceiling for an operator-chosen universe (`config.maxSymbols`). The UI
#: offers up to this; anything above it is clamped, never silently accepted.
MAX_SYMBOLS_HARD = 32
DEFAULT_THRESHOLD = 3


# ── Symbols ──────────────────────────────────────────────────────────────────

def normalize_symbols(items: Iterable[Any], limit: Optional[int] = None) -> list[str]:
    """Upper-case, de-duplicate and cap a symbol list. Never invents one.

    `limit` is the bot's configured universe size (`config.maxSymbols`). It is
    clamped to `MAX_SYMBOLS_HARD`, so the operator can widen the scan but not
    past the process ceiling.
    """
    try:
        cap = MAX_SYMBOLS if limit is None else int(limit)
    except (TypeError, ValueError):
        cap = MAX_SYMBOLS
    if cap <= 0:  # a malformed setting falls back to the default, never to 1
        cap = MAX_SYMBOLS
    cap = min(cap, MAX_SYMBOLS_HARD)
    seen: set[str] = set()
    out: list[str] = []
    for raw in items or []:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        if len(symbol) > 24:
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= cap:
            break
    return out


def _bare(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())


def is_symbol_tradable(symbol: str, venue_symbols: Iterable[str]) -> bool:
    """Exact match, or a broker suffix (`EURUSD.m`, `BTCUSD-ECN`).

    A suffix match requires a non-numeric tail, so EURUSD never matches EURUSDT.
    """
    wanted = _bare(symbol)
    if not wanted:
        return False
    upper = str(symbol or "").upper()
    for offered in venue_symbols or []:
        venue = str(offered or "").upper()
        if not venue:
            continue
        if venue == upper or _bare(venue) == wanted:
            return True
        if _bare(venue).startswith(wanted):
            remainder = venue[len(symbol):]
            if len(remainder) >= 2 and remainder[0] in ".-_":
                tail = remainder[1:]
                if tail.isalpha() and 1 <= len(tail) <= 6:
                    return True
    return False


def missing_symbols(symbols: Iterable[str], venue_symbols: Iterable[str]) -> list[str]:
    return [s for s in normalize_symbols(symbols)
            if not is_symbol_tradable(s, venue_symbols)]


# ── Session window ───────────────────────────────────────────────────────────

def parse_hhmm(value: Any, default: time) -> time:
    try:
        hour, minute = str(value or "").split(":")[:2]
        return time(max(0, min(23, int(hour))), max(0, min(59, int(minute))))
    except (TypeError, ValueError):
        return default


def session_state(now: datetime, session: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """`{"open": bool, "reason": str|None}` for one session window.

    Weekdays use the repo's 0=Sunday convention. An unknown timezone is closed
    with an explicit reason: a bot never trades on a guess about the clock.
    """
    if not session:
        return {"open": True, "reason": None}
    zone_name = str(session.get("timezone") or "UTC")
    try:
        zone = ZoneInfo(zone_name)
    except Exception:  # noqa: BLE001 - an unreadable zone is a refusal, not a crash
        return {"open": False, "reason": f"unknown timezone {zone_name!r}"}

    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    local = moment.astimezone(zone)
    weekdays = session.get("weekdays") or []
    try:
        wanted = {int(day) for day in weekdays}
    except (TypeError, ValueError):
        wanted = set()
    # python: Monday=0 … Sunday=6; repo: Sunday=0 … Saturday=6
    dow = (local.weekday() + 1) % 7
    if wanted and dow not in wanted:
        return {"open": False, "reason": "weekday outside the session window"}

    start = parse_hhmm(session.get("start"), time(0, 0))
    end = parse_hhmm(session.get("end"), time(23, 59))
    current = local.time()
    inside = (start <= current <= end) if start <= end else (current >= start or current <= end)
    return {
        "open": inside,
        "reason": None if inside else (
            f"outside the session window ({local.strftime('%H:%M')} {zone_name})"),
    }


# ── Freshness ────────────────────────────────────────────────────────────────

def parse_moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_fresh(last_bar: Any, now: datetime, timeframe_seconds: float,
             factor: float = 2.5) -> bool:
    """A bar is usable only when it is within `factor` timeframes of now.

    A broker stamps bars in SERVER time (MT5 demo here is UTC+2), so the newest
    bar can be a few hours AHEAD of the process clock. That is clock skew, not
    staleness - but no more than `_MAX_CLOCK_SKEW` of it, so a genuinely
    future-dated feed is still refused.
    """
    moment = parse_moment(last_bar)
    if moment is None or timeframe_seconds <= 0:
        return False
    reference = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = (reference - moment).total_seconds()
    return -_MAX_CLOCK_SKEW <= age <= timeframe_seconds * factor


TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}

#: How far ahead of local UTC a broker's server clock may legitimately be.
_MAX_CLOCK_SKEW = 3 * 60 * 60


def timeframe_seconds(name: Any) -> int:
    return TIMEFRAME_SECONDS.get(str(name or "").upper(), 0)


# ── Consent (the standing "auto mode") ──────────────────────────────────────

def _iso(value: Any) -> Optional[datetime]:
    return parse_moment(value)


def consent_status(consent: Optional[Mapping[str, Any]],
                   now: Optional[datetime] = None) -> str:
    if not consent:
        return "missing"
    if consent.get("revokedAt"):
        return "revoked"
    expires = _iso(consent.get("expiresAt"))
    moment = now or datetime.now(timezone.utc)
    if expires is None:
        modes = consent.get("modes") or []
        return "expired" if "live" in modes else "active"
    return "active" if moment < expires else "expired"


def consent_covers(consent: Optional[Mapping[str, Any]], request: Mapping[str, Any],
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Mirror of `consentCovers` in TS. The first failing dimension is named."""
    status = consent_status(consent, now)
    if status != "active":
        reason = {
            "missing": "this bot has no standing consent yet",
            "revoked": "the operator revoked this bot's trading consent",
            "expired": "the trading consent expired",
        }.get(status, "the trading consent is unavailable")
        return {"covered": False, "status": status, "reason": reason, "field": "consent"}

    mode = str(request.get("mode") or "")
    if mode != "shadow" and mode not in (consent.get("modes") or []):
        return {"covered": False, "status": status,
                "reason": f"the consent does not cover {mode} execution",
                "field": "consent"}

    limits = consent.get("limits") or {}
    # Compare the full granted/requested lists (hard ceiling), not the default
    # 8-symbol form cap: a bot configured with 12 symbols must not be told its
    # consent covers only the first eight in a particular order.
    allowed = normalize_symbols(limits.get("symbols") or [], limit=MAX_SYMBOLS_HARD)
    wanted = normalize_symbols(request.get("symbols") or [], limit=MAX_SYMBOLS_HARD)
    wildcard = "*" in allowed
    outside = [] if wildcard else [s for s in wanted if s not in allowed]
    if outside:
        return {"covered": False, "status": status,
                "reason": "the consent does not cover " + ", ".join(outside),
                "field": "symbols"}

    def number(key: str, default: float = 0.0) -> float:
        try:
            return float(limits.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    if float(request.get("maxOrdersPerDay") or 0) > number("maxOrdersPerDay", 0):
        return {"covered": False, "status": status,
                "reason": "the request asks for more orders/day than the consent allows",
                "field": "maxOrdersPerDay"}
    margin_cap = number("maxMarginPerOrder", 0)
    if margin_cap > 0 and float(request.get("maxMarginPerOrder") or 0) > margin_cap:
        return {"covered": False, "status": status,
                "reason": "the request asks for more margin than the consent allows",
                "field": "maxMarginPerOrder"}
    if float(request.get("maxOpenPositions") or 0) > number("maxOpenPositions", 0):
        return {"covered": False, "status": status,
                "reason": "the request asks for more open positions than the consent allows",
                "field": "maxOpenPositions"}
    return {"covered": True, "status": status, "reason": None, "field": None}


def needs_phrase(consent: Optional[Mapping[str, Any]], request: Mapping[str, Any],
                 now: Optional[datetime] = None) -> bool:
    if str(request.get("mode") or "") == "shadow":
        return False
    return not consent_covers(consent, request, now)["covered"]


def consent_request_for(config: Mapping[str, Any],
                        authority: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": config.get("mode"),
        "symbols": normalize_symbols(
            config.get("symbols") or [],
            limit=config.get("maxSymbols"),
        ),
        "maxOrdersPerDay": config.get("maxOrdersPerDay") or 0,
        "maxMarginPerOrder": authority.get("maxMarginPerOrder") or 0,
        "maxOpenPositions": authority.get("maxOpenPositions") or 0,
    }


# ── Readable decisions ──────────────────────────────────────────────────────

_ACTION_LABEL = {"buy": "BUY", "sell": "SELL", "close": "CLOSE", "none": "—"}
_OUTCOME_LABEL = {
    "filled": "filled", "placed": "working", "preview": "preview",
    "refused": "refused", "skipped": "skipped", "failed": "failed", "none": "no trade",
}


def format_decision_line(decision: Mapping[str, Any]) -> str:
    action = str(decision.get("action") or "none")
    risk = decision.get("risk") or {}
    size = ""
    if action != "none" and risk.get("quantity"):
        size = f" {risk['quantity']}"
    outcome = str(decision.get("outcome") or "none")
    bits = [
        str(decision.get("symbol") or "—"),
        f"{_ACTION_LABEL.get(action, action)}{size}",
        _OUTCOME_LABEL.get(outcome, outcome),
    ]
    if decision.get("code"):
        bits.append(f"({decision['code']})")
    return " · ".join(bits)


def decision(
    *,
    bot_id: str,
    bot_version: int,
    strategy_id: str,
    strategy_version: str,
    mode: str,
    tier: str,
    symbol: str = "",
    signal: str = "none",
    action: str = "none",
    reason: str = "",
    indicators: Optional[Mapping[str, Any]] = None,
    risk: Optional[Mapping[str, Any]] = None,
    outcome: str = "none",
    code: Optional[str] = None,
    order_id: Optional[str] = None,
    order_status: Optional[str] = None,
    bar_ts: Optional[str] = None,
    warnings: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """The ONE decision shape. Every runner branch returns this, including no-ops."""
    return {
        "botId": bot_id,
        "botVersion": int(bot_version or 0),
        "strategyId": strategy_id,
        "strategyVersion": strategy_version,
        "mode": mode,
        "tier": tier,
        "barTs": bar_ts,
        "symbol": symbol,
        "signal": signal,
        "action": action,
        "reason": reason,
        "indicators": dict(indicators or {}),
        "risk": dict(risk) if risk is not None else None,
        "outcome": outcome,
        "code": code,
        "orderId": order_id,
        "orderStatus": order_status,
        "warnings": list(warnings or []),
    }


def visual_teaching_prompt(visuals: Any) -> str:
    """The text block for reference images attached to a bot.

    Mirrors `visualTeachingPrompt` in TS. The images are GUIDANCE: a model must
    never quote a price from them or claim the current market matches one.
    """
    if not isinstance(visuals, (list, tuple)) or not visuals:
        return ""
    lines: list[str] = []
    for index, visual in enumerate(list(visuals)[:4], start=1):
        caption = ""
        if isinstance(visual, Mapping):
            caption = str(visual.get("caption") or "").strip()
        lines.append(f"  {index}. {caption or 'No caption.'}")
    return "\n".join([
        "The operator attached reference images that teach this strategy:",
        *lines,
        "Treat them as PATTERN GUIDANCE only. They are not live market data, they may be",
        "out of date, and you must never quote a price from an image or claim the current",
        "market matches it. When an image conflicts with the broker/JDdS data, the data wins",
        "and you say so.",
    ])


def rank_evaluations(evaluations, *, max_entries: int = 1,
                     open_symbols=None) -> list[dict]:
    """Rank a multi-symbol scan: exits first, then the best entries.

    Mirrors `rankEvaluations` in TS. A symbol already held is never re-entered, so
    a scan cannot stack a second position. `score` comes from the strategy, not
    from this function.
    """
    opened = {str(s).strip().upper() for s in (open_symbols or [])}
    ranked = sorted(
        [e for e in evaluations if isinstance(e, dict)],
        key=lambda item: (
            0 if str(item.get("signal") or "").endswith("_exit") else 1,
            -float(item.get("score") or 0),
        ),
    )
    picked: list[dict] = []
    entries = 0
    for item in ranked:
        signal = str(item.get("signal") or "")
        symbol = str(item.get("symbol") or "").upper()
        if signal.endswith("_exit"):
            picked.append(item)
            continue
        if not signal.endswith("_entry"):
            continue
        if symbol in opened or entries >= max_entries:
            continue
        entries += 1
        picked.append(item)
    return picked


__all__ = [
    "DEFAULT_THRESHOLD",
    "MAX_SYMBOLS",
    "MAX_SYMBOLS_HARD",
    "MODES",
    "TIERS",
    "TIMEFRAME_SECONDS",
    "consent_covers",
    "consent_request_for",
    "consent_status",
    "decision",
    "format_decision_line",
    "is_fresh",
    "is_symbol_tradable",
    "missing_symbols",
    "needs_phrase",
    "normalize_symbols",
    "parse_hhmm",
    "parse_moment",
    "rank_evaluations",
    "session_state",
    "timeframe_seconds",
    "visual_teaching_prompt",
]
