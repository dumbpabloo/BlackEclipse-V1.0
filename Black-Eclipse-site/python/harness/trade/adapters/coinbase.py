"""Coinbase Exchange adapter: reads AND real order execution.

Coinbase's Exchange API signs `timestamp + method + requestPath + body` with
**HMAC-SHA256 over the base64-decoded secret** and sends the digest base64-encoded
alongside the key and passphrase:

    prehash   = <unix seconds> + "GET" + "/accounts" + ""
    signature = base64(HMAC-SHA256(base64decode(secret), prehash))

`sign_request` is pure so the exact prehash can be checked against an independent
computation — the two easy mistakes here are signing the *encoded* secret and
forgetting that the body is part of the message (even when empty). `_request`
therefore signs the real verb and the serialized JSON body, so `POST /orders`
and `DELETE /orders/{id}` are signed exactly as the venue expects.

Declared limits, so nothing is implied that is not true:

  * **Balances, not positions.** There are no margin positions in this surface;
    `positions()` reports non-zero balances with `currentPrice`/`profit` at 0
    because marking every asset would be one call each. A spot close is an
    opposite MARKET order, so a position ticket is the currency and a close takes
    the PRODUCT id (e.g. `BTC-USD`).
  * **No attached stops.** Coinbase can rest a stop, but an intent carrying
    `stop_loss`/`take_profit` is refused (`STOP_UNSUPPORTED`) so no venue drops a
    stop silently; place the stop as its own order.
  * **Ownership tag is `client_oid`** (`eclipse-<magic>-<digest>`). `cancel_all`
    matches that prefix and never issues a blind cancel-all.
  * **No websocket stream.**

Config: `baseUrl` (default https://api.exchange.coinbase.com), `credentialRef`
naming an env var/secret holding `KEY:SECRET:PASSPHRASE`, optional `magic` used to
tag close orders.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_COINBASE
from .credentials import resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    client_order_id,
    owns_order,
    quote_preference,
    resolve_close_pair,
    step_volume,
)

_TIMEOUT = 15

_STOP_REASON = (
    "a spot entry cannot carry an attached stop loss or take profit; "
    "submit the entry, then place a separate stop order on the filled position"
)
_NO_POSITION_MODIFY = (
    "Coinbase Exchange has no position-level stop/target object; a close is an "
    "opposite market order and a stop is its own resting order"
)
_NO_ORDER_MODIFY = (
    "Coinbase does not edit a live order in place; cancel the order and submit the "
    "replacement"
)


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: float) -> str:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _digits_of(tick: float) -> int:
    text = f"{float(tick or 0):.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def sign_request(secret_b64: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Coinbase's `CB-ACCESS-SIGN`: base64(HMAC-SHA256(secret, ts+method+path+body))."""
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    digest = hmac.new(
        base64.b64decode(secret_b64), prehash.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class CoinbaseAdapter(TradingAdapter):
    kind = KIND_COINBASE

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="COINBASE_CREDENTIALS",
            label="Coinbase key/secret/passphrase",
        )
        if not raw:
            raise AdapterUnavailable(
                "COINBASE_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding "
                "`KEY:SECRET:PASSPHRASE`",
            )
        parts = raw.split(":")
        if len(parts) != 3 or not all(parts):
            raise AdapterUnavailable(
                "COINBASE_CREDENTIALS",
                "the credential must be `KEY:SECRET:PASSPHRASE` (three parts)",
            )
        return parts[0], parts[1], parts[2]

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.exchange.coinbase.com"),
            code="COINBASE_CONFIG",
            label="Coinbase baseUrl",
        )

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        signed: bool = True,
        method: str = "GET",
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        request_path = path + (f"?{query}" if query else "")
        text = json.dumps(body) if body is not None else ""
        data = text.encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "BlackEclipse/coinbase"}
        if signed:
            key, secret, passphrase = self._credentials()
            timestamp = str(int(time.time()))
            headers.update({
                "CB-ACCESS-KEY": key,
                "CB-ACCESS-PASSPHRASE": passphrase,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "CB-ACCESS-SIGN": sign_request(
                    secret, timestamp, method.upper(), request_path, text
                ),
            })
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base()}{request_path}", headers=headers, data=data,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("COINBASE_UNAUTHORIZED", "Coinbase rejected the API key") from exc
            if exc.code == 404:
                raise AdapterUnavailable("COINBASE_NOT_FOUND", f"Coinbase returned HTTP 404 for {path}") from exc
            if exc.code == 429:
                raise AdapterUnavailable("COINBASE_RATE_LIMITED", "Coinbase rate limit reached") from exc
            detail = ""
            try:
                parsed = json.loads(exc.read() or b"{}")
                if isinstance(parsed, dict) and parsed.get("message"):
                    detail = f": {parsed['message']}"
            except Exception:  # noqa: BLE001 - the body is advisory
                detail = ""
            raise AdapterUnavailable(
                "COINBASE_HTTP", f"Coinbase returned HTTP {exc.code}{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("COINBASE_UNREACHABLE", "Coinbase is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("COINBASE_TIMEOUT", "Coinbase timed out") from exc
        try:
            payload = json.loads(payload or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("COINBASE_BAD_JSON", "Coinbase returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("message"):
            raise AdapterUnavailable("COINBASE_API_ERROR", str(payload["message"])[:300])
        return payload

    # -- BrokerAdapter ---------------------------------------------------

    def probe(self) -> dict[str, Any]:
        try:
            account = self.account()
        except AdapterUnavailable as exc:
            return {"available": True, "connected": False, "code": exc.code,
                    "reason": exc.reason, "terminal": {"api": self._base()},
                    "account": None, "warnings": []}
        return {"available": True, "connected": True, "code": None, "reason": None,
                "terminal": {"api": self._base()}, "account": account,
                "warnings": []}

    def _accounts(self) -> list[dict[str, Any]]:
        payload = self._request("/accounts")
        if not isinstance(payload, list):
            raise AdapterUnavailable("COINBASE_BAD_RESPONSE", "Coinbase returned no accounts")
        return [row for row in payload if isinstance(row, dict)]

    def account(self) -> dict[str, Any]:
        rows = self._accounts()
        total = 0.0
        for row in rows:
            total += _num(row.get("balance"))
        # Coinbase marks a profile/account as non-tradable with `trading_enabled`
        # false. Anything else (including an absent flag) is a live venue.
        return {
            "login": 0,
            "mode": account_mode(self._base()),
            "server": "coinbase",
            "company": "Coinbase Exchange",
            "currency": "USD",
            "balance": total,
            "equity": total,
            "margin": 0.0,
            "marginFree": total,
            "marginLevel": 0.0,
            "profit": 0.0,
            "leverage": 1.0,
            "marginMode": "spot",
            "tradeAllowed": all(row.get("trading_enabled") is not False for row in rows),
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for row in self._accounts():
            quantity = _num(row.get("balance"))
            if quantity == 0:
                continue
            currency = str(row.get("currency") or "").upper()
            out.append({
                # Closed by an opposite market order, so the currency is the ticket.
                "ticket": currency,
                "symbol": currency,
                "side": "buy",
                "volume": quantity,
                "openPrice": 0.0,     # not derived: the adapter does not price assets
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "profit": 0.0,
                "swap": 0.0,
                "magic": 0,
                "comment": "coinbase spot balance (not marked to market)",
                "openedAt": now_ms,
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        payload = self._request("/orders", {"status": "open"})
        out: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict):
                continue
            out.append({
                "ticket": str(row.get("id") or ""),
                "symbol": str(row.get("product_id") or "").upper(),
                "type": str(row.get("type") or ""),
                "volumeCurrent": _num(row.get("size")) - _num(row.get("filled_size")),
                "openPrice": _num(row.get("price")),
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "state": str(row.get("status") or ""),
                "magic": 0,
                "comment": f"coinbase {str(row.get('side') or '').lower()}".strip(),
                "placedAt": _epoch_ms(row.get("created_at")),
                "clientOrderId": str(row.get("client_oid") or ""),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("COINBASE_SYMBOL", "a product id is required")
        row = self._request(f"/products/{urllib.parse.quote(pair, safe='')}/ticker", signed=False)
        if not isinstance(row, dict) or row.get("price") in (None, ""):
            raise AdapterUnavailable("COINBASE_NO_QUOTE", f"Coinbase has no ticker for {pair}")
        last = _num(row.get("price"))
        bid = _num(row.get("bid")) or last
        ask = _num(row.get("ask")) or last
        return {
            "symbol": pair,
            "requestedSymbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "spreadPoints": None,
            "digits": 8,
            "point": 0.00000001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(time.time() * 1000),
            "provider": "coinbase",
        }

    # -- instrument metadata -------------------------------------------------
    def _all_pairs(self) -> dict[str, str]:
        cached = getattr(self, "_pair_cache", None)
        if cached is not None:
            return cached
        rows = self._request("/products", signed=False)
        pairs = {
            str(row.get("id")).upper(): str(row.get("id"))
            for row in rows or []
            if isinstance(row, dict) and row.get("id")
        }
        self._pair_cache = pairs
        return pairs

    def _product(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("COINBASE_SYMBOL", "a product id is required")
        row = self._request(f"/products/{urllib.parse.quote(pair, safe='')}", signed=False)
        if not isinstance(row, dict) or not row.get("id"):
            raise AdapterUnavailable("COINBASE_NO_SYMBOL", f"Coinbase lists no product {pair!r}")
        return row

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        row = self._product(symbol)
        quote_increment = _num(row.get("quote_increment"))
        base_increment = _num(row.get("base_increment"))
        digits = _digits_of(quote_increment) or 8
        status = str(row.get("status") or "").lower()
        return {
            "symbol": str(row.get("id") or symbol).upper(),
            "requestedSymbol": symbol,
            "description": str(row.get("display_name") or row.get("id") or ""),
            "path": "spot",
            "tradeMode": "full" if status == "online" else "disabled",
            "digits": digits,
            "point": quote_increment or 10 ** -digits,
            "tickSize": quote_increment,
            "tickValue": quote_increment,
            "currencyBase": str(row.get("base_currency") or ""),
            "currencyProfit": str(row.get("quote_currency") or ""),
            "currencyMargin": str(row.get("quote_currency") or ""),
            "contractSize": 1.0,
            "volumeMin": _num(row.get("base_min_size")),
            "volumeMax": _num(row.get("base_max_size")),
            "volumeStep": base_increment,
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": bool(row.get("trading_disabled") is not True),
            "sessionNote": "Coinbase Exchange trades continuously",
            "retrievedAt": time.time(),
            "provider": "coinbase",
        }

    # -- execution -----------------------------------------------------------
    def order_preview(self, intent: Any) -> dict[str, Any]:
        account = self.account()
        quote = self.quote(intent.symbol)
        info = self.symbol_info(intent.symbol)
        step = _num(info.get("volumeStep"))
        volume, reasons = step_volume(
            intent.volume, step, _num(info.get("volumeMin")), _num(info.get("volumeMax")),
        )
        bid = _num(quote.get("bid"))
        ask = _num(quote.get("ask"))
        price = (
            float(intent.price)
            if intent.order_type in ("limit", "stop") and intent.price
            else (ask if intent.side == "buy" else bid)
        )
        extra: dict[str, Any] = {"venue": "coinbase", "stopSupported": False}
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(_STOP_REASON)
            extra["retcodeName"] = "STOP_UNSUPPORTED"
        if info.get("tradeMode") != "full":
            reasons.append(f"Coinbase reports {intent.symbol} as not tradable")
        return build_preview(
            intent,
            account=account,
            quote=quote,
            volume=volume,
            price=price,
            contract_size=1.0,
            margin=max(0.0, volume * price),
            open_positions=len(self.positions()),
            reasons=reasons,
            digits=int(info.get("digits") or 8),
            volume_step=step,
            volume_min=_num(info.get("volumeMin")),
            volume_max=_num(info.get("volumeMax")),
            extra=extra,
        )

    def _order_body(self, intent: Any, volume: float) -> dict[str, Any]:
        order_type = str(intent.order_type)
        body: dict[str, Any] = {
            "size": _fmt(volume),
            "side": str(intent.side).lower(),
            "product_id": str(intent.symbol).strip().upper(),
            "client_oid": client_order_id(intent.magic, intent.digest),
        }
        if order_type == "limit":
            body.update({
                "type": "limit",
                "price": _fmt(float(intent.price)),
                "time_in_force": "GTC" if intent.time_in_force == "gtc" else "GDD",
            })
        elif order_type == "stop":
            # A stop entry triggers a market order; the single Eclipse price is
            # the trigger, never widened into an invented limit price.
            body.update({
                "type": "stop",
                "stop": "entry",
                "stop_price": _fmt(float(intent.price)),
            })
        else:
            body["type"] = "market"
        return body

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        if preview.get("reasons"):
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0,
                retcode_name=str(preview.get("retcodeName") or "PREVIEW_REJECTED"),
                reasons=preview.get("reasons") or [],
                account=preview.get("account") or {},
            )
        volume = float(preview["volume"])
        row = self._request(
            "/orders", method="POST", body=self._order_body(intent, volume),
        )
        if not isinstance(row, dict) or not row.get("id"):
            raise AdapterUnavailable("COINBASE_BAD_RESPONSE", "Coinbase returned no order id")
        status = str(row.get("status") or "").lower()
        accepted = status in ("pending", "open", "active", "received", "done", "filled")
        filled = _num(row.get("filled_size"))
        executed = _num(row.get("executed_value"))
        fill_price = executed / filled if filled > 0 and executed > 0 else 0.0
        return build_result(
            ok=accepted,
            status="FILLED" if status == "filled" or (filled > 0 and status == "done") else (
                "placed" if accepted else "REJECTED"
            ),
            symbol=row.get("product_id") or intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            fill_volume=filled or None,
            price=fill_price or preview["price"],
            ticket=str(row.get("id")),
            retcode=status or None,
            retcode_name=status.upper() or "ACCEPTED",
            comment=str(row.get("client_oid") or ""),
            reasons=[] if accepted else [f"Coinbase reported status {status or 'unknown'}"],
            account=preview.get("account") or {},
            clientOrderId=row.get("client_oid"),
        )

    def _open_rows(self) -> list[dict[str, Any]]:
        rows = self._request("/orders", {"status": "open"})
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            self._request(f"/orders/{urllib.parse.quote(order_id, safe='')}", method="DELETE")
        except AdapterUnavailable as exc:
            if exc.code == "COINBASE_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Coinbase has no order {order_id}"]}
            raise
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for row in self._open_rows():
            if not owns_order(row.get("client_oid"), magic):
                continue
            order_id = str(row.get("id") or "")
            try:
                results.append(self.cancel_order(order_id))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": order_id,
                                "retcodeName": exc.code, "reasons": [exc.reason]})
        cancelled = sum(1 for item in results if item.get("ok"))
        return {
            "ok": not any(not item.get("ok") for item in results),
            "cancelled": cancelled,
            "failed": len(results) - cancelled,
            "results": results,
            "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS",
            "reasons": [],
        }

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        pair = str(ticket or "").strip().upper()
        if not pair:
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_SYMBOL",
                    "reasons": ["pass the traded PRODUCT id (e.g. BTC-USD) to close a balance"]}
        try:
            available = self._all_pairs()
        except AdapterUnavailable:
            available = {}
        resolved = resolve_close_pair(
            self.config, pair, available,
            quote_preference(self.config, ("USD", "USDT", "USDC", "EUR", "GBP", "BTC")),
            "{base}-{quote}",
        )
        if not resolved:
            resolved = pair
        pair = resolved
        try:
            product = self._product(pair)
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": pair, "retcodeName": "INVALID_SYMBOL",
                    "reasons": [f"{pair} is not a Coinbase product: {exc.reason}"]}
        base = str(product.get("base_currency") or "").upper()
        free = 0.0
        for row in self._accounts():
            if str(row.get("currency") or "").upper() == base:
                free = _num(row.get("available"), _num(row.get("balance")))
                break
        quantity = float(volume) if volume else free
        quantity, reasons = step_volume(
            quantity, _num(product.get("base_increment")), 0.0, 0.0,
        )
        if quantity <= 0:
            return {"ok": False, "ticket": pair, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Coinbase reports no available {base} balance to close"]}
        body = {
            "size": _fmt(quantity),
            "side": "sell",
            "product_id": str(product.get("id") or pair),
            "type": "market",
            "client_oid": client_order_id(int(self.config.get("magic") or 0), "close"),
        }
        row = self._request("/orders", method="POST", body=body)
        if not isinstance(row, dict) or not row.get("id"):
            raise AdapterUnavailable("COINBASE_BAD_RESPONSE", "Coinbase returned no close order")
        filled = _num(row.get("filled_size"))
        return {
            "ok": True,
            "ticket": pair,
            "symbol": pair,
            "side": "sell",
            "volume": filled or quantity,
            "requestedVolume": quantity,
            "price": _num(row.get("executed_value")) / filled if filled > 0 else 0.0,
            "orderTicket": str(row.get("id")),
            "retcode": str(row.get("status") or ""),
            "retcodeName": "CLOSED" if filled >= quantity > 0 else "CLOSING",
            "partial": bool(volume),
            # A fill is claimed only when Coinbase reports an executed size.
            "verified": bool(filled),
            "reasons": reasons,
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            row = self._request(f"/orders/{urllib.parse.quote(order_id, safe='')}")
        except AdapterUnavailable as exc:
            if exc.code == "COINBASE_NOT_FOUND":
                return {"found": False, "ticket": order_id}
            raise
        if not isinstance(row, dict) or not row.get("id"):
            return {"found": False, "ticket": order_id}
        filled = _num(row.get("filled_size"))
        executed = _num(row.get("executed_value"))
        return {
            "found": True,
            "ticket": str(row.get("id")),
            "state": row.get("status"),
            "stateName": str(row.get("status") or "").upper(),
            "filledVolume": filled,
            "avgPrice": executed / filled if filled > 0 else 0.0,
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "MODIFY_NOT_SUPPORTED", "reasons": [_NO_POSITION_MODIFY]}

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "ORDER_MODIFY_NOT_SUPPORTED", "reasons": [_NO_ORDER_MODIFY]}


def _epoch_ms(raw: Any) -> int:
    """Coinbase timestamps are ISO-8601 (e.g. `2026-11-01T12:00:00.000000Z`)."""
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        from datetime import datetime, timezone

        return int(datetime.fromisoformat(text.replace("Z", "+00:00"))
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


__all__ = ["CoinbaseAdapter", "sign_request"]
