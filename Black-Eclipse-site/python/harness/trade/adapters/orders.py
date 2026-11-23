"""Shared execution helpers for REST broker adapters.

Every REST venue differs in endpoints and auth, but the order *contract* the
trade layer consumes is the same one MetaTrader fills in:

  * `order_preview` returns the normalized intent plus the broker facts the
    policy engine checks (volume, price, notional, margin, spread, open
    positions, account);
  * `submit_order` returns `{ok, status, ticket, retcode, retcodeName, ...}`;
  * `cancel_order` / `close_position` / `modify_*` return `{ok, ticket, ...}`.

These helpers keep that shape in one place so a new venue is a mapping, not a
re-implementation of the contract. Nothing here performs I/O or invents a
number: a value the venue did not return stays `None` and is reported as such.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Iterable, Optional

_DEMO_HINTS = ("demo", "paper", "sandbox", "practice", "testnet", "sim")


def number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if result == result and result not in (float("inf"), float("-inf")) else fallback


def optional_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def account_mode(base_url: str) -> str:
    """`demo`/`real` from the endpoint the operator configured.

    The service requires the broker's account mode to agree with the connection
    (`demo`/`demo`, `real`/`live`). A paper/sandbox/practice host is a demo venue;
    every other host is a real account. This reads the URL the caller supplied —
    it does not guess from credentials.
    """
    text = str(base_url or "").lower()
    return "demo" if any(hint in text for hint in _DEMO_HINTS) else "real"


def step_volume(
    value: float,
    step: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 0.0,
) -> tuple[float, list[str]]:
    """Round DOWN to the venue's lot step and check its own min/max.

    Rounding down is the only safe direction: rounding up can exceed the
    operator's stated size or the account's margin. A missing step (0) is not
    applied; a venue that reports no step has already accepted the size.
    """
    reasons: list[str] = []
    volume = number(value)
    if volume <= 0:
        return volume, ["volume must be greater than zero"]
    if step and step > 0:
        try:
            units = Decimal(str(volume)) / Decimal(str(step))
            volume = float(units.quantize(Decimal("1"), rounding=ROUND_DOWN) * Decimal(str(step)))
        except (InvalidOperation, ZeroDivisionError):
            return volume, [f"volume {value:g} is not a multiple of {step:g}"]
    if minimum and volume < minimum:
        reasons.append(f"volume {volume:g} is below the venue minimum {minimum:g}")
    if maximum and volume > maximum:
        reasons.append(f"volume {volume:g} is above the venue maximum {maximum:g}")
    # A float artifact like 0.30000000000000004 is normalized, never widened.
    return round(volume, 10), reasons


def client_order_id(magic: int, digest: str = "") -> str:
    """The ownership tag REST venues get instead of MetaTrader's magic number.

    A venue order carries no magic field, so Eclipse prefixes the client order
    id with a fixed marker and the connection magic. `cancel_all` and the kill
    switch match that prefix; without it an "Eclipse order" is indistinguishable
    from a manual one.
    """
    tag = f"eclipse-{int(magic or 0)}"
    suffix = str(digest or "")[:12]
    return f"{tag}-{suffix}" if suffix else tag


def owns_order(client_id: Any, magic: int) -> bool:
    return str(client_id or "").startswith(f"eclipse-{int(magic or 0)}-")


def quote_preference(config: dict[str, Any], default: Iterable[str]) -> tuple[str, ...]:
    """First the operator's `quoteAsset`, then the venue's sensible defaults.

    A spot holding is closed by selling it into some quote currency. The adapter
    cannot know which one the operator means, so an explicit `quoteAsset` wins;
    otherwise the defaults are tried in order and the FIRST pair the venue lists
    is used. If none exists the holding keeps its asset ticket and a close is a
    named refusal rather than a guessed pair.
    """
    configured = str((config or {}).get("quoteAsset") or "").strip().upper()
    ordered = [configured] if configured else []
    for quote in default:
        text = str(quote or "").strip().upper()
        if text and text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def resolve_pair(
    asset: str,
    available: dict[str, str],
    quotes: Iterable[str],
    template: str,
) -> Optional[str]:
    """The venue's own spelling of `asset/quote`, or None.

    `available` maps UPPER-case pair ids to the venue's canonical spelling, so a
    case-sensitive venue (Bitstamp's `btcusd`) keeps its form. Nothing is
    invented: a pair the venue did not list is not returned.
    """
    base = str(asset or "").strip().upper()
    if not base:
        return None
    for quote in quotes:
        candidate = template.format(base=base, quote=str(quote).upper())
        found = available.get(candidate.upper())
        if found:
            return found
    return None


def resolve_close_pair(
    config: dict[str, Any],
    ticket: Any,
    available: dict[str, str],
    quotes: Iterable[str],
    template: str,
) -> Optional[str]:
    """Resolve the pair a spot close should sell into.

    Order of preference: an explicit `symbolMap` entry (`{"BTC": "BTCUSDT"}`),
    the ticket itself when it already names a listed pair, then the first quote
    in `quotes` the venue actually lists. A refusal (None) is better than
    selling a holding into a pair the operator did not choose.
    """
    raw = str(ticket or "").strip()
    if not raw:
        return None
    mapped = {
        str(key).strip().upper(): str(value).strip()
        for key, value in (config.get("symbolMap") or {}).items()
    } if isinstance(config.get("symbolMap"), dict) else {}
    candidate = mapped.get(raw.upper(), raw)
    found = available.get(candidate.upper())
    if found:
        return found
    return resolve_pair(candidate, available, quotes, template)


def build_preview(
    intent: Any,
    *,
    account: dict[str, Any],
    quote: dict[str, Any],
    volume: float,
    price: float,
    contract_size: float = 1.0,
    margin: Optional[float] = None,
    open_positions: int = 0,
    reasons: Iterable[str] = (),
    digits: int = 2,
    volume_step: float = 0.0,
    volume_min: float = 0.0,
    volume_max: float = 0.0,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The MT5-shaped preview every venue returns."""
    symbol = str(intent.symbol or "").upper()
    side = str(intent.side or "").lower()
    order_type = str(intent.order_type or "market")
    bid = number(quote.get("bid"))
    ask = number(quote.get("ask"))
    reference = price if price > 0 else (ask if side == "buy" else bid)
    contract = contract_size or 1.0
    notional = round(float(volume) * float(contract) * float(reference or 0), 6)
    # A cash/spot order is funded by its full notional; a margined venue can
    # pass its own figure. Never leave margin empty when notional is known: the
    # policy engine treats "unverified" as a refusal when a cap is configured.
    required = notional if margin is None else number(margin)
    collected = list(reasons)
    if reference <= 0:
        collected.append("the venue returned no usable price")
    if volume <= 0:
        collected.append("volume must be greater than zero")
    preview: dict[str, Any] = {
        "ok": not collected,
        "symbol": symbol,
        "requestedSymbol": intent.symbol,
        "side": side,
        "type": order_type,
        "volume": float(volume),
        "price": float(reference or 0),
        "notional": notional,
        "stopLoss": intent.stop_loss,
        "takeProfit": intent.take_profit,
        "contractSize": float(contract),
        "tickSize": number(quote.get("point")),
        "point": number(quote.get("point")),
        "digits": digits,
        "margin": required,
        "marginFree": account.get("marginFree"),
        "spreadPoints": quote.get("spreadPoints"),
        "openPositions": int(open_positions or 0),
        "account": account,
        "terminalTradeAllowed": bool(account.get("tradeAllowed")),
        "quote": quote,
        "request": {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "volume": float(volume),
            "price": intent.price,
            "stopLoss": intent.stop_loss,
            "takeProfit": intent.take_profit,
        },
        "retcode": None,
        "retcodeName": None,
        "volumeStep": volume_step,
        "volumeMin": volume_min,
        "volumeMax": volume_max,
        "reasons": collected,
    }
    if extra:
        preview.update(extra)
    return preview


def build_result(
    *,
    ok: bool,
    status: str,
    symbol: str,
    side: str,
    order_type: str,
    volume: float,
    price: float,
    ticket: Any = None,
    retcode: Any = None,
    retcode_name: str = "",
    comment: str = "",
    reasons: Iterable[str] = (),
    account: Optional[dict[str, Any]] = None,
    fill_volume: Optional[float] = None,
    **extra: Any,
) -> dict[str, Any]:
    """The execution result shape `BrokerService.submit_order` consumes."""
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "status": status,
        "symbol": str(symbol or "").upper(),
        "side": str(side or "").lower(),
        "type": str(order_type or "market").lower(),
        "volume": number(fill_volume, number(volume)),
        "requestedVolume": number(volume),
        "price": number(price),
        "ticket": ticket,
        "deal": None,
        "retcode": retcode,
        "retcodeName": str(retcode_name or ("ACCEPTED" if ok else "REJECTED")),
        "comment": str(comment or ""),
        "reasons": list(reasons or ()),
        "account": account or {},
    }
    payload.update(extra)
    return payload


__all__ = [
    "account_mode",
    "build_preview",
    "build_result",
    "client_order_id",
    "number",
    "optional_number",
    "owns_order",
    "quote_preference",
    "resolve_close_pair",
    "resolve_pair",
    "step_volume",
]
