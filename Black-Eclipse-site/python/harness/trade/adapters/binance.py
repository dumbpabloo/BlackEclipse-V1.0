"""Binance spot adapter: reads AND real order execution.

Binance signs a query string, not a canonical request: the payload is
`<query>&signature=<hmac_sha256(secret, query)>`, sent with `X-MBX-APIKEY`. Public
endpoints (ticker price, exchangeInfo) take no signature, so `_request(signed=False)`
is a first-class path. `POST /api/v3/order` and `DELETE /api/v3/order` use the SAME
query-signed message as a read: the verb changes, the preimage does not, and there
is no body.

Declared limits, so nothing is invented:

  * **Spot only.** `positions()` reports non-zero **balances** (no margin/futures
    positions), with `currentPrice`/`profit` at 0 because pricing every asset is N
    calls and a silent failure mode. A spot close is an opposite MARKET order, so
    a position ticket is the asset and a close takes the PAIR (e.g. `BTCUSDT`).
  * **No attached stops.** An intent with `stop_loss`/`take_profit` is refused
    (`STOP_UNSUPPORTED`) instead of being silently dropped.
  * **Order ids are never truncated.** Cancelling resolves the symbol from the
    open-orders list (Binance requires `symbol` alongside the id) or refuses.
  * `order_preview` validates against `/api/v3/exchangeInfo` filters locally and
    never sends a test order. No websocket stream.

Config (all non-secret): `baseUrl` (default https://api.binance.com),
`credentialRef` naming an env var/secret holding `API_KEY:SECRET`, optional
`magic` used to tag Eclipse-placed close orders.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_BINANCE
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
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
    "Binance spot has no position-level stop/target to edit; a close is an "
    "opposite market order and a stop is its own resting order"
)
_NO_ORDER_MODIFY = (
    "Binance replaces an order by symbol + order id and keeps no bracket leg to "
    "edit; cancel the order and submit a new one"
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


def sign_query(secret: str, query: str) -> str:
    """Binance's signature: hex HMAC-SHA256 over the query string."""
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


class BinanceAdapter(TradingAdapter):
    kind = KIND_BINANCE

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="BINANCE_CREDENTIALS",
            label="Binance API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "BINANCE_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.binance.com"),
            code="BINANCE_CONFIG",
            label="Binance baseUrl",
        )

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        signed: bool = True,
        method: str = "GET",
    ) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        headers = {"Accept": "application/json"}
        if signed:
            key, secret = self._credentials()
            if query:
                query += "&"
            query += f"timestamp={int(time.time() * 1000)}"
            query += f"&signature={sign_query(secret, query)}"
            headers["X-MBX-APIKEY"] = key
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("BINANCE_UNAUTHORIZED", "Binance rejected the API key") from exc
            if exc.code == 418:
                raise AdapterUnavailable("BINANCE_IP_BANNED", "Binance banned this IP") from exc
            if exc.code == 429:
                raise AdapterUnavailable("BINANCE_RATE_LIMITED", "Binance rate limit reached") from exc
            if exc.code == 451:
                raise AdapterUnavailable(
                    "BINANCE_RESTRICTED",
                    "Binance refused the request from this jurisdiction (HTTP 451)",
                ) from exc
            detail = ""
            try:
                parsed = json.loads(exc.read() or b"{}")
                if isinstance(parsed, dict) and parsed.get("msg"):
                    detail = f": {parsed.get('msg')}"
            except Exception:  # noqa: BLE001 - the body is advisory
                detail = ""
            raise AdapterUnavailable(
                "BINANCE_HTTP", f"Binance returned HTTP {exc.code}{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("BINANCE_UNREACHABLE", "Binance is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BINANCE_TIMEOUT", "Binance timed out") from exc
        try:
            payload = json.loads(body or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("BINANCE_BAD_JSON", "Binance returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise AdapterUnavailable(
                "BINANCE_API_ERROR",
                f"Binance error {payload.get('code')}: {payload.get('msg') or 'unknown'}",
            )
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

    def _account_payload(self) -> dict[str, Any]:
        payload = self._request("/api/v3/account")
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BINANCE_BAD_RESPONSE", "Binance returned no account")
        return payload

    def _free_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in self._account_payload().get("balances") or []:
            if isinstance(row, dict) and row.get("asset"):
                out[str(row["asset"]).upper()] = _num(row.get("free"))
        return out

    def account(self) -> dict[str, Any]:
        payload = self._account_payload()
        balances = [row for row in payload.get("balances") or [] if isinstance(row, dict)]
        total = 0.0
        for row in balances:
            total += _num(row.get("free")) + _num(row.get("locked"))
        return {
            "login": 0,
            "mode": account_mode(self._base()),
            "server": "binance",
            "company": "Binance",
            "currency": "USD",
            "balance": total,
            "equity": total,
            "margin": 0.0,
            "marginFree": total,
            "marginLevel": 0.0,
            "profit": 0.0,
            "leverage": 1.0,
            "marginMode": "spot",
            # Only an explicit `canTrade: false` disables trading; a payload that
            # does not carry the flag is a live venue, not a disabled one.
            "tradeAllowed": payload.get("canTrade") is not False,
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        payload = self._account_payload()
        rows = payload.get("balances") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            quantity = _num(row.get("free")) + _num(row.get("locked"))
            if quantity == 0:
                continue
            asset = str(row.get("asset") or "").upper()
            out.append({
                # A spot holding is closed by an opposite market order, so the
                # asset itself is the ticket.
                "ticket": asset,
                "symbol": asset,
                "side": "buy",
                "volume": quantity,
                "openPrice": 0.0,     # not derived: the adapter does not price assets
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "profit": 0.0,
                "swap": 0.0,
                "magic": 0,
                "comment": "binance spot balance (not marked to market)",
                "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v3/openOrders")
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            out.append({
                "ticket": str(row.get("orderId") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "type": str(row.get("type") or ""),
                "volumeCurrent": _num(row.get("origQty")) - _num(row.get("executedQty")),
                "openPrice": _num(row.get("price")),
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "state": str(row.get("status") or ""),
                "magic": 0,
                "comment": (
                    f"binance {str(row.get('side') or '').lower()} "
                    f"{row.get('clientOrderId') or ''}"
                ).strip(),
                "placedAt": int(_num(row.get("time"))),
                "clientOrderId": str(row.get("clientOrderId") or ""),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("BINANCE_SYMBOL", "a symbol is required")
        row = self._request("/api/v3/ticker/price", {"symbol": pair}, signed=False)
        if not isinstance(row, dict) or not row.get("price"):
            raise AdapterUnavailable("BINANCE_NO_QUOTE", f"Binance has no price for {pair}")
        last = _num(row.get("price"))
        return {
            "symbol": pair,
            "requestedSymbol": symbol,
            "bid": last,
            "ask": last,
            "last": last,
            "spreadPoints": None,
            "digits": 8,
            "point": 0.00000001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(time.time() * 1000),
            "provider": "binance",
        }

    # -- instrument metadata -------------------------------------------------
    def _all_pairs(self) -> dict[str, str]:
        cached = getattr(self, "_pair_cache", None)
        if cached is not None:
            return cached
        payload = self._request("/api/v3/exchangeInfo", signed=False)
        rows = payload.get("symbols") if isinstance(payload, dict) else None
        pairs = {
            str(row.get("symbol")).upper(): str(row.get("symbol"))
            for row in rows or []
            if isinstance(row, dict) and row.get("symbol")
        }
        self._pair_cache = pairs
        return pairs

    def _exchange_info(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        row = self._request("/api/v3/exchangeInfo", {"symbol": pair}, signed=False)
        rows = row.get("symbols") if isinstance(row, dict) else None
        if not rows or not isinstance(rows[0], dict):
            raise AdapterUnavailable("BINANCE_NO_SYMBOL", f"Binance lists no symbol {pair!r}")
        return rows[0]

    @staticmethod
    def _filters(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in info.get("filters") or []:
            if isinstance(row, dict) and row.get("filterType"):
                out[str(row["filterType"])] = row
        return out

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        info = self._exchange_info(symbol)
        filters = self._filters(info)
        lot = filters.get("LOT_SIZE") or {}
        price_filter = filters.get("PRICE_FILTER") or {}
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        tick = _num(price_filter.get("tickSize"))
        digits = _digits_of(tick) or 8
        status = str(info.get("status") or "").upper()
        base = str(info.get("baseAsset") or "")
        quote_asset = str(info.get("quoteAsset") or "")
        return {
            "symbol": str(info.get("symbol") or symbol).upper(),
            "requestedSymbol": symbol,
            "description": f"{base}/{quote_asset}",
            "path": "spot",
            "tradeMode": "full" if status in ("TRADING", "ALLOW") else "disabled",
            "digits": digits,
            "point": tick or 10 ** -digits,
            "tickSize": tick,
            "tickValue": tick,
            "currencyBase": base,
            "currencyProfit": quote_asset,
            "currencyMargin": quote_asset,
            "contractSize": 1.0,
            "volumeMin": _num(lot.get("minQty")),
            "volumeMax": _num(lot.get("maxQty")),
            "volumeStep": _num(lot.get("stepSize")),
            "notionalMin": _num(notional.get("minNotional")),
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": True,
            "sessionNote": "Binance spot trades continuously",
            "retrievedAt": time.time(),
            "provider": "binance",
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
        extra: dict[str, Any] = {"venue": "binance", "stopSupported": False}
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(_STOP_REASON)
            extra["retcodeName"] = "STOP_UNSUPPORTED"
        if info.get("tradeMode") != "full":
            reasons.append(f"Binance reports {intent.symbol} as not tradable")
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

    def _order_params(self, intent: Any, volume: float) -> dict[str, Any]:
        order_type = str(intent.order_type)
        params: dict[str, Any] = {
            "symbol": str(intent.symbol).upper(),
            "side": str(intent.side).upper(),
            "type": {"market": "MARKET", "limit": "LIMIT", "stop": "STOP_LOSS_LIMIT"}[order_type],
            "quantity": _fmt(volume),
            "newClientOrderId": client_order_id(intent.magic, intent.digest),
        }
        if order_type == "limit":
            params["price"] = _fmt(float(intent.price))
            params["timeInForce"] = "GTC" if intent.time_in_force == "gtc" else "DAY"
        elif order_type == "stop":
            # A stop entry needs a trigger AND a limit price; Eclipse carries one
            # price, so it fills both rather than inventing a second number.
            params["price"] = _fmt(float(intent.price))
            params["stopPrice"] = _fmt(float(intent.price))
            params["timeInForce"] = "GTC" if intent.time_in_force == "gtc" else "DAY"
        return params

    @staticmethod
    def _avg_price(row: dict[str, Any]) -> float:
        filled = _num(row.get("executedQty"))
        quote_qty = _num(row.get("cummulativeQuoteQty") or row.get("cumulativeQuoteQty"))
        if filled > 0 and quote_qty > 0:
            return quote_qty / filled
        return _num(row.get("price"))

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
        row = self._request("/api/v3/order", self._order_params(intent, volume), method="POST")
        if not isinstance(row, dict) or row.get("orderId") in (None, ""):
            raise AdapterUnavailable("BINANCE_BAD_RESPONSE", "Binance returned no order id")
        status = str(row.get("status") or "NEW").upper()
        accepted = status in ("NEW", "PENDING_NEW", "PARTIALLY_FILLED", "FILLED")
        filled = _num(row.get("executedQty"))
        return build_result(
            ok=accepted,
            status="FILLED" if status == "FILLED" else ("placed" if accepted else "REJECTED"),
            symbol=row.get("symbol") or intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            fill_volume=filled or None,
            price=self._avg_price(row) or preview["price"],
            ticket=str(row.get("orderId")),
            retcode=status or None,
            retcode_name=status or "ACCEPTED",
            comment=str(row.get("clientOrderId") or ""),
            reasons=[] if accepted else [f"Binance reported status {status or 'unknown'}"],
            account=preview.get("account") or {},
            clientOrderId=row.get("clientOrderId"),
        )

    def _open_rows(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v3/openOrders")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _find_open(self, ticket: str) -> Optional[dict[str, Any]]:
        for row in self._open_rows():
            if ticket and ticket in (str(row.get("orderId") or ""),
                                     str(row.get("clientOrderId") or "")):
                return row
        return None

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        # Binance cancels by `symbol + orderId`; the id alone is not enough, so
        # the symbol is read from the open-orders list rather than guessed.
        row = self._find_open(order_id)
        if row is None:
            return {
                "ok": False, "ticket": order_id, "retcode": None,
                "retcodeName": "ORDER_NOT_FOUND",
                "reasons": [
                    f"Binance has no open order {order_id}; it was not cancelled "
                    "(cancelling needs the symbol of a working order)"
                ],
            }
        params: dict[str, Any] = {"symbol": str(row.get("symbol") or "")}
        if order_id == str(row.get("clientOrderId") or ""):
            params["origClientOrderId"] = order_id
        else:
            params["orderId"] = order_id
        try:
            self._request("/api/v3/order", params, method="DELETE")
        except AdapterUnavailable as exc:
            if "Unknown order" in exc.reason or "does not exist" in exc.reason:
                return {"ok": False, "ticket": order_id, "retcode": None,
                        "retcodeName": "ORDER_NOT_FOUND", "reasons": [exc.reason]}
            raise
        return {
            "ok": True, "ticket": order_id, "symbol": params["symbol"],
            "retcode": 200, "retcodeName": "CANCELLED", "reasons": [],
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for row in self._open_rows():
            order_id = str(row.get("orderId") or "")
            if not owns_order(row.get("clientOrderId"), magic):
                continue
            try:
                self._request(
                    "/api/v3/order",
                    {"symbol": str(row.get("symbol") or ""), "orderId": order_id},
                    method="DELETE",
                )
                results.append({"ok": True, "ticket": order_id,
                                "retcodeName": "CANCELLED", "reasons": []})
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
                    "reasons": ["pass the traded PAIR (e.g. BTCUSDT) to close a spot balance"]}
        try:
            available = self._all_pairs()
        except AdapterUnavailable:
            # The pair list is an optimization, not the authority; the venue
            # lookup below still refuses an unknown symbol.
            available = {}
        resolved = resolve_close_pair(
            self.config, pair, available,
            quote_preference(self.config, ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")),
            "{base}{quote}",
        )
        if not resolved:
            # No listed pair could be resolved (the venue's pair list may not be
            # readable). Fall through with the ticket as given: the venue lookup
            # below is the final authority and refuses an unknown symbol.
            resolved = pair
        pair = resolved
        try:
            info = self._exchange_info(pair)
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": pair, "retcodeName": "INVALID_SYMBOL",
                    "reasons": [f"{pair} is not a Binance spot pair: {exc.reason}"]}
        base = str(info.get("baseAsset") or "").upper()
        free = self._free_balances().get(base, 0.0)
        quantity = float(volume) if volume else free
        step = _num((self._filters(info).get("LOT_SIZE") or {}).get("stepSize"))
        quantity, reasons = step_volume(quantity, step, 0.0, 0.0)
        if quantity <= 0:
            return {"ok": False, "ticket": pair, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Binance reports no free {base} balance to close"]}
        params = {
            "symbol": pair,
            "side": "SELL",
            "type": "MARKET",
            "quantity": _fmt(quantity),
            "newClientOrderId": client_order_id(int(self.config.get("magic") or 0), "close"),
        }
        row = self._request("/api/v3/order", params, method="POST")
        if not isinstance(row, dict) or row.get("orderId") in (None, ""):
            raise AdapterUnavailable("BINANCE_BAD_RESPONSE", "Binance returned no close order")
        filled = _num(row.get("executedQty"))
        return {
            "ok": True,
            "ticket": pair,
            "symbol": pair,
            "side": "sell",
            "volume": filled or quantity,
            "requestedVolume": quantity,
            "price": self._avg_price(row),
            "orderTicket": str(row.get("orderId")),
            "retcode": str(row.get("status") or ""),
            "retcodeName": "CLOSED" if filled else "CLOSING",
            "partial": bool(volume),
            # The venue acknowledged the opposite order; a fill is reported only
            # when Binance's own payload carries an executed quantity.
            "verified": bool(filled),
            "reasons": [] if not reasons else reasons,
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._find_open(order_id)
        if row is None:
            return {
                "found": False,
                "ticket": order_id,
                "reasons": [
                    "Binance needs the symbol to query a finished order; only open "
                    "orders can be resolved from the id alone"
                ],
            }
        return {
            "found": True,
            "ticket": str(row.get("orderId") or order_id),
            "state": row.get("status"),
            "stateName": str(row.get("status") or "").upper(),
            "filledVolume": _num(row.get("executedQty")),
            "avgPrice": self._avg_price(row),
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


__all__ = ["BinanceAdapter", "sign_query"]
