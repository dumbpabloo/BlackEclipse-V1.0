"""Bitfinex v2 adapter: reads AND order execution.

Bitfinex v2 is the only connector here that signs with **HMAC-SHA384**, and its
preimage is the *signed* path (the public one with `auth` replaced by `r`), the
nonce and the body:

    signature = hex HMAC-SHA384(secret,
                  "/api/v2/r/wallets" + nonce + body)

Three consequences the tests pin:

  * a **96-hex** digest (SHA-384), not 64 — the mistake a copy-paste from the
    SHA-256 adapters produces;
  * the path is signed **as sent**, so the write path is signed in its `/r/`
    form (`/api/v2/r/order/submit`) while the request URL uses `/w/`
    (`/api/v2/w/order/submit`) — Bitfinex's documented routing;
  * v2 answers with **positional arrays**, not objects, so every field is read by
    index with the schema documented next to it.

Order ownership: Bitfinex has no string client id, only an integer `cid`
(int45). Eclipse packs the magic and a digest-derived 6-digit suffix into that
integer (`77001234`), so `cancel_all` can prove an order is ours. The `meta`
object accepts only platform options (affiliate code, self-match), so it is not
used as a tag.

Declared limits:

  * **No websocket stream.**
  * Wallet balances are not marked to market; a position's `currentPrice` is only
    filled when Bitfinex's own position payload carries a mark (it does not), so it
    stays 0 and the comment says so.
  * Stops/targets are OCO legs, not order fields; an order-level SL/TP is refused
    with `STOP_UNSUPPORTED`. A stop **entry** uses `price_aux_limit`.
  * There is no by-id status route without the symbol, so `order_status` searches
    active orders and then the documented order history.

Config: `baseUrl` (default https://api.bitfinex.com), `credentialRef` naming an
env var/secret holding `KEY:SECRET`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_BITFINEX
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    number as _fnum,
    step_volume,
)

_TIMEOUT = 15
_CID_SUFFIX = 6


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: Any) -> str:
    return f"{_num(value):.10f}".rstrip("0").rstrip(".") or "0"


def sign_request(secret: str, path: str, nonce: str, body: str = "") -> str:
    """Bitfinex v2: hex HMAC-SHA384 over signed path + nonce + body."""
    message = f"{path}{nonce}{body}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha384).hexdigest()


def symbol_for(raw: Any) -> str:
    """`tBTCUSD` / `fUSD` -> `BTCUSD` / `USD` (the `t`/`f` prefix is a class tag)."""
    text = str(raw or "").strip()
    return text[1:].upper() if text[:1] in ("t", "f") else text.upper()


def tagged_cid(magic: int, digest: str = "") -> int:
    """The integer `cid` that carries Eclipse's ownership tag.

    A 6-digit suffix keeps `magic + suffix` inside Bitfinex's int45 `cid`, and
    the suffix is derived from the intent digest so two different orders from the
    same connection do not share an id. The same intent maps to the same cid,
    which also makes an accidental double submit collide instead of duplicating.
    """
    suffix = int(hashlib.sha256(str(digest or "").encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
    return int(f"{int(magic or 0)}{suffix:0{_CID_SUFFIX}d}")


def owns_cid(cid: Any, magic: int) -> bool:
    """True only for the exact `magic + 6 digits` shape `tagged_cid` produces."""
    text = str(cid if cid is not None else "")
    prefix = str(int(magic or 0))
    return len(text) == len(prefix) + _CID_SUFFIX and text.startswith(prefix)


def _pair(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "").upper() if ch.isalnum())


def _order_rows(payload: Any) -> list[list[Any]]:
    """Every positional order row in a v2 response, at any nesting depth."""
    rows: list[list[Any]] = []

    def walk(item: Any) -> None:
        if not isinstance(item, list):
            return
        if len(item) >= 14 and isinstance(item[0], (int, float)) and not isinstance(item[0], bool):
            rows.append(item)
            return
        for child in item:
            walk(child)

    walk(payload)
    return rows


def _state_of(status: Any) -> str:
    text = str(status or "").upper()
    if text.startswith("ACTIVE"):
        return "open"
    if text.startswith("PARTIALLY FILLED"):
        return "partial"
    if text.startswith("EXECUTED"):
        return "filled"
    if text.startswith("CANCELED") or text.startswith("CANCELLED"):
        return "cancelled"
    return text.lower() or "unknown"


class BitfinexAdapter(TradingAdapter):
    kind = KIND_BITFINEX

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="BITFINEX_CREDENTIALS",
            label="Bitfinex API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "BITFINEX_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.bitfinex.com"),
            code="BITFINEX_CONFIG",
            label="Bitfinex baseUrl",
        )

    def _nonce(self) -> str:
        candidate = int(time.time() * 1000)
        last = int(getattr(self, "_last_nonce", 0) or 0)
        if candidate <= last:
            candidate = last + 1
        self._last_nonce = candidate
        return str(candidate)

    def _request(
        self, path: str, *, signed: bool = False, body: str = "",
        url_path: Optional[str] = None,
    ) -> Any:
        """Send one call. `path` is SIGNED; `url_path` is what the URL uses.

        Bitfinex documents that the write routes live under `/w/` while the
        signature covers the `/r/` form, so the two are separate arguments. A
        body (a JSON string) selects POST, exactly as before.
        """
        headers = {"Accept": "application/json"}
        url = f"{self._base()}{url_path or path}"
        data: Optional[bytes] = None
        if signed:
            key, secret = self._credentials()
            nonce = self._nonce()
            headers.update({
                "bfx-apikey": key,
                "bfx-nonce": nonce,
                "bfx-signature": sign_request(secret, path, nonce, body),
            })
            if body:
                data = body.encode("utf-8")
                headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, headers=headers, data=data, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("BITFINEX_UNAUTHORIZED", "Bitfinex rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("BITFINEX_RATE_LIMITED", "Bitfinex rate limit reached") from exc
            raise AdapterUnavailable("BITFINEX_HTTP", f"Bitfinex returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("BITFINEX_UNREACHABLE", "Bitfinex is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BITFINEX_TIMEOUT", "Bitfinex timed out") from exc
        try:
            parsed = json.loads(payload or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("BITFINEX_BAD_JSON", "Bitfinex returned invalid JSON") from exc
        if isinstance(parsed, dict) and parsed.get("error"):
            raise AdapterUnavailable("BITFINEX_API_ERROR", str(parsed["error"])[:300])
        if isinstance(parsed, list) and parsed and parsed[0] == "error":
            detail = parsed[2] if len(parsed) > 2 else parsed
            raise AdapterUnavailable("BITFINEX_API_ERROR", str(detail)[:300])
        return parsed

    # -- BrokerAdapter ---------------------------------------------------

    def probe(self) -> dict[str, Any]:
        try:
            account = self.account()
        except AdapterUnavailable as exc:
            return {
                "available": True, "connected": False, "code": exc.code,
                "reason": exc.reason, "terminal": {"api": self._base()},
                "account": None, "warnings": [],
            }
        return {
            "available": True, "connected": True, "code": None, "reason": None,
            "terminal": {"api": self._base()}, "account": account, "warnings": [],
        }

    def account(self) -> dict[str, Any]:
        rows = self._request("/api/v2/r/wallets", signed=True)
        # [TYPE, CURRENCY, BALANCE, UNSETTLED_INTEREST, AVAILABLE_BALANCE, ...]
        total = available = 0.0
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 5:
                continue
            total += _num(row[2])
            available += _num(row[4])
        return {
            "login": 0, "mode": account_mode(self._base()),
            "server": "bitfinex", "company": "Bitfinex", "currency": "USD",
            "balance": total, "equity": total, "margin": total - available,
            "marginFree": available, "marginLevel": 0.0, "profit": 0.0,
            "leverage": 1.0, "marginMode": "spot",
            # Wallets carry no trading switch; the venue's rejection is the answer.
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v2/r/positions", signed=True)
        # [SYMBOL, STATUS, AMOUNT, BASE_PRICE, FUNDING_COST, FUNDING_VALUE,
        #  COLLATERAL, PL, PL_PERC, PRICE_LIQ, LEVERAGE, _PLACEHOLDER, POSITION_ID,
        #  MTIME, _PLACEHOLDER, _PLACEHOLDER, _PLACEHOLDER, PRICE]
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 4:
                continue
            amount = _num(row[2])
            if amount == 0:
                continue
            out.append({
                # The position id is the number Bitfinex will net an opposite
                # order against; keep it a string so a >2^53 id cannot round.
                "ticket": str(row[12]) if len(row) > 12 and row[12] is not None else "",
                "symbol": symbol_for(row[0]),
                "side": "buy" if amount > 0 else "sell",
                "volume": abs(amount),
                "openPrice": _num(row[3]),
                "currentPrice": _num(row[17]) if len(row) > 17 else 0.0,
                "stopLoss": None, "takeProfit": None,
                "profit": _num(row[7]), "swap": _num(row[4]), "magic": 0,
                "comment": f"bitfinex {str(row[1] or '').lower()}".strip(),
                "openedAt": int(_num(row[13])) if len(row) > 13 else 0,
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v2/r/orders", signed=True)
        # [ID, GID, CID, SYMBOL, MTIME, AMOUNT, PRICE, _PLACEHOLDER, _PLACEHOLDER,
        #  TYPE, _PLACEHOLDER, _PLACEHOLDER, STATUS, ...]
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 7:
                continue
            amount = _num(row[5])
            out.append({
                # Exact id, never via float: Bitfinex order ids exceed 2^53.
                "ticket": str(row[0]),
                "symbol": symbol_for(row[3]),
                "type": str(row[9] or "") if len(row) > 9 else "",
                "volumeCurrent": abs(amount),
                "openPrice": _num(row[6]),
                "currentPrice": 0.0, "stopLoss": None, "takeProfit": None,
                "state": str(row[12] or "ACTIVE") if len(row) > 12 else "ACTIVE",
                "magic": 0,
                "comment": f"bitfinex {'buy' if amount > 0 else 'sell'}".strip(),
                "clientOrderId": row[2] if len(row) > 2 else None,
                "placedAt": int(_num(row[4])),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = _pair(symbol)
        if not pair:
            raise AdapterUnavailable("BITFINEX_SYMBOL", "a pair is required")
        row = self._request(f"/api/v2/ticker/t{pair}")
        # [BID, BID_SIZE, ASK, ASK_SIZE, DAILY_CHANGE, DAILY_CHANGE_REL,
        #  LAST_PRICE, VOLUME, HIGH, LOW]
        if not isinstance(row, list) or len(row) < 7:
            raise AdapterUnavailable("BITFINEX_NO_QUOTE", f"Bitfinex has no ticker for {pair}")
        last = _num(row[6])
        return {
            "symbol": pair, "requestedSymbol": symbol,
            "bid": _num(row[0]) or last, "ask": _num(row[2]) or last, "last": last,
            "spreadPoints": None, "digits": 8, "point": 0.00000001,
            "stopsLevelPoints": 0, "serverTimeMs": int(time.time() * 1000),
            "provider": "bitfinex",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        pair = _pair(symbol)
        if not pair:
            raise AdapterUnavailable("BITFINEX_SYMBOL", "a pair is required")
        listing = self._request("/api/v2/conf/pub:list:pair:exchange")
        pairs = listing[0] if isinstance(listing, list) and listing and isinstance(listing[0], list) else listing
        known = {str(item).upper() for item in pairs} if isinstance(pairs, list) else set()
        if pair not in known:
            raise AdapterUnavailable("BITFINEX_NO_SYMBOL", f"Bitfinex does not list {pair}")
        last = _num(self.quote(pair).get("last"))
        return {
            "symbol": pair, "requestedSymbol": symbol,
            "description": f"Bitfinex exchange pair {pair}", "path": "spot",
            "tradeMode": "full", "digits": 8, "point": 0.0,
            "tickSize": 0.0, "tickValue": 0.0,
            "currencyBase": pair[:3], "currencyProfit": pair[3:], "currencyMargin": pair[3:],
            "contractSize": 1.0, "last": last,
            # The public pair list and ticker publish no lot step or minimum, so
            # both stay 0 ("not reported") rather than being invented.
            "volumeMin": 0.0, "volumeMax": 0.0, "volumeStep": 0.0,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False,
            "sessionNote": "Bitfinex trades continuously and publishes no lot step",
            "retrievedAt": time.time(), "provider": "bitfinex",
        }

    # -- execution -------------------------------------------------------
    def _bracket_reasons(self, intent: Any) -> list[str]:
        if intent.stop_loss is None and intent.take_profit is None:
            return []
        return [
            "Bitfinex needs separate OCO stop/target orders; this adapter submits a "
            "single order and will not pretend a bracket was attached"
        ]

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, quote, positions = self.account(), self.quote(intent.symbol), self.positions()
        volume, reasons = step_volume(intent.volume)
        bid, ask = _fnum(quote.get("bid")), _fnum(quote.get("ask"))
        price = float(intent.price) if intent.order_type in ("limit", "stop") and intent.price else (
            ask if intent.side == "buy" else bid)
        reasons += self._bracket_reasons(intent)
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, margin=max(0.0, volume * price),
            open_positions=len(positions), reasons=reasons, digits=8,
            extra={"venue": "bitfinex"},
        )

    def _payload(self, intent: Any, volume: float) -> dict[str, Any]:
        amount = float(volume) if intent.side == "buy" else -float(volume)
        payload: dict[str, Any] = {
            "symbol": "t" + _pair(intent.symbol),
            "amount": _fmt(amount),
            "cid": tagged_cid(intent.magic, intent.digest),
            # 0 = no special flag. An OCO bracket is two orders with flag 4096,
            # which this adapter does not submit as one; see _bracket_reasons.
            "flags": 0,
        }
        if intent.order_type == "limit":
            payload.update(type="EXCHANGE LIMIT", price=_fmt(intent.price))
        elif intent.order_type == "stop":
            # Bitfinex's stop limit: `price` is the trigger, `price_aux_limit`
            # the limit left behind after it fires.
            payload.update(type="EXCHANGE STOP LIMIT", price=_fmt(intent.price),
                           price_aux_limit=_fmt(intent.price))
        else:
            payload.update(type="EXCHANGE MARKET", price="0")
        return payload

    def _state_payload(self, row: list[Any], fallback_volume: float, fallback_price: float) -> dict[str, Any]:
        status_text = str(row[13]) if len(row) > 13 else "ACTIVE"
        state = _state_of(status_text)
        filled = abs(_num(row[6])) or abs(_num(row[7]) if len(row) > 7 else 0.0)
        avg = _num(row[17]) if len(row) > 17 else 0.0
        return {
            "state": state, "statusText": status_text, "ticket": str(row[0]),
            "filled": filled if state in ("partial", "filled") else 0.0,
            "price": avg or fallback_price, "volume": fallback_volume,
            "cid": row[2] if len(row) > 2 else None,
        }

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            retcode = "STOP_UNSUPPORTED" if self._bracket_reasons(intent) else "PREVIEW_REJECTED"
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0, retcode_name=retcode,
                reasons=reasons, account=preview.get("account") or {})
        payload = self._payload(intent, float(preview["volume"]))
        result = self._request(
            "/api/v2/r/order/submit", signed=True, body=json.dumps(payload),
            url_path="/api/v2/w/order/submit",
        )
        rows = _order_rows(result)
        if not rows:
            raise AdapterUnavailable("BITFINEX_BAD_RESPONSE", "Bitfinex returned no order")
        info = self._state_payload(rows[0], float(preview["volume"]), float(preview["price"]))
        accepted = info["state"] in ("open", "partial", "filled")
        return build_result(
            ok=accepted,
            status="FILLED" if info["state"] == "filled" else ("placed" if accepted else "REJECTED"),
            symbol=intent.symbol, side=intent.side, order_type=intent.order_type,
            volume=preview["volume"], fill_volume=info["filled"] or None,
            price=info["price"], ticket=info["ticket"], retcode=info["statusText"],
            retcode_name=str(info["statusText"]).upper() or "ACCEPTED",
            reasons=[] if accepted else [f"Bitfinex reported {info['statusText'] or 'no status'}"],
            account=preview.get("account") or {}, clientOrderId=info["cid"],
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            result = self._request(
                "/api/v2/r/order/cancel", signed=True,
                body=json.dumps({"id": int(order_id) if order_id.isdigit() else order_id}),
                url_path="/api/v2/w/order/cancel",
            )
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": order_id, "retcodeName": exc.code,
                    "reasons": [exc.reason]}
        rows = _order_rows(result)
        if not rows:
            return {"ok": False, "ticket": order_id, "retcodeName": "CANCEL_UNCONFIRMED",
                    "reasons": ["Bitfinex did not return the cancelled order"]}
        info = self._state_payload(rows[0], 0.0, 0.0)
        ok = info["state"] == "cancelled"
        return {"ok": ok, "ticket": order_id, "state": info["state"],
                "retcodeName": "CANCELLED" if ok else "CANCEL_REJECTED",
                "reasons": [] if ok else [f"Bitfinex reported {info['statusText']}"]}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        tagged = [row for row in rows if row.get("clientOrderId") not in (None, "")]
        if rows and not tagged:
            return {"ok": False, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "OWNERSHIP_UNAVAILABLE",
                    "reasons": ["Bitfinex returned open orders without their cid"]}
        results: list[dict[str, Any]] = []
        for row in tagged:
            if not owns_cid(row.get("clientOrderId"), magic):
                continue
            results.append(self.cancel_order(str(row.get("ticket") or "")))
        cancelled = sum(1 for item in results if item.get("ok"))
        return {"ok": not any(not item.get("ok") for item in results), "cancelled": cancelled,
                "failed": len(results) - cancelled, "results": results,
                "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": []}

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        row = next((item for item in self.positions() if str(item.get("ticket")) == str(ticket)), None)
        if row is None:
            return {"ok": False, "ticket": ticket, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Bitfinex has no open position {ticket}"]}
        size = float(volume) if volume else _num(row.get("volume"))
        partial = bool(volume) and size < _num(row.get("volume"))
        close_side = "sell" if row.get("side") == "buy" else "buy"
        payload = {
            "type": "EXCHANGE MARKET", "symbol": "t" + _pair(row.get("symbol")),
            "amount": _fmt(size if close_side == "buy" else -size), "price": "0",
        }
        result = self._request(
            "/api/v2/r/order/submit", signed=True, body=json.dumps(payload),
            url_path="/api/v2/w/order/submit",
        )
        rows = _order_rows(result)
        if not rows:
            raise AdapterUnavailable("BITFINEX_BAD_RESPONSE", "Bitfinex returned no close order")
        info = self._state_payload(rows[0], size, _num(row.get("currentPrice")))
        accepted = info["state"] in ("open", "partial", "filled")
        return {
            "ok": accepted, "ticket": str(ticket), "symbol": row.get("symbol"),
            "side": close_side, "volume": size, "price": info["price"],
            "retcode": info["statusText"], "orderId": info["ticket"], "partial": partial,
            "retcodeName": "CLOSED" if info["state"] == "filled" else (
                "PLACED" if accepted else "REJECTED"),
            "verified": info["state"] == "filled",
            "reasons": [] if accepted else [f"Bitfinex reported {info['statusText']}"],
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["Bitfinex positions carry no stop field; submit an OCO "
                            "exit order instead of editing the position"]}

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        if stop_loss is not None or take_profit is not None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "ORDER_BRACKET_UNSUPPORTED",
                    "reasons": ["Bitfinex updates one order's price; an OCO bracket "
                                "is two orders, so cancel and resubmit instead"]}
        if price is None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST", "reasons": ["pass a new price"]}
        order_id = str(ticket)
        body = {"id": int(order_id) if order_id.isdigit() else order_id, "price": _fmt(price)}
        result = self._request(
            "/api/v2/r/order/update", signed=True, body=json.dumps(body),
            url_path="/api/v2/w/order/update",
        )
        rows = _order_rows(result)
        info = self._state_payload(rows[0], 0.0, float(price)) if rows else None
        ok = bool(info and info["state"] in ("open", "partial"))
        return {"ok": ok, "ticket": order_id, "price": (info or {}).get("price") or price,
                "retcode": (info or {}).get("statusText"), "retcodeName": "REPLACED" if ok else "REPLACE_FAILED",
                "verified": ok,
                "reasons": [] if ok else ["Bitfinex did not confirm the updated order"]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        wanted = str(ticket)
        for row in self.orders():
            if str(row.get("ticket")) == wanted:
                return {"found": True, "ticket": wanted, "state": str(row.get("state")),
                        "stateName": str(row.get("state")), "filledVolume": 0.0,
                        "avgPrice": _num(row.get("openPrice"))}
        try:
            history = self._request(
                "/api/v2/r/orders/hist", signed=True, body=json.dumps({"limit": 250}))
        except AdapterUnavailable:
            return {"found": False, "ticket": wanted}
        for row in _order_rows(history):
            if str(row[0]) != wanted:
                continue
            info = self._state_payload(row, 0.0, 0.0)
            return {"found": True, "ticket": wanted, "state": info["state"],
                    "stateName": info["statusText"], "filledVolume": info["filled"],
                    "avgPrice": info["price"]}
        return {"found": False, "ticket": wanted}


__all__ = ["BitfinexAdapter", "owns_cid", "sign_request", "symbol_for", "tagged_cid"]
