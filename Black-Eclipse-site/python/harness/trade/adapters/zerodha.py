"""Zerodha Kite adapter (NSE/BSE equities, F&O, currency): reads AND execution.

Kite authenticates with two static headers (`Authorization: token
<api_key>:<access_token>` and `X-Kite-Version: 3`) — no request signing — which
keeps the adapter small and the tests able to run on recorded shapes.

Execution: MARKET/LIMIT/SL-M orders plus modify and cancel. `product` follows
Kite's own day/delivery split (MIS for a day intent, CNC otherwise) and every
order carries `tag=eclipse-<magic>-…` so `cancel_all` can prove ownership. Kite
has no attached stop/target legs and no position bracket edit, so both are
refused by name.

What it does NOT do, stated so nothing is invented:

  * **Quotes are per instrument, not a stream.** `quote()` returns one snapshot
    from `/quote`; the WebSocket ticker is not implemented.
  * **Positions are the broker's net + day split.** `positions()` returns the
    `net` book only; merging the `day` rows would fabricate a position the broker
    does not report as net.
  * **No fill on the send response.** Kite returns an order id only, so
    `submit_order` reports `placed` and `close_position` reads the order back
    once to decide whether it is `verified`.

Config (all non-secret):
  baseUrl        default https://api.kite.trade
  credentialRef  NAME of an env var / secret holding `API_KEY:ACCESS_TOKEN`

Symbol convention: `EXCHANGE:TRADINGSYMBOL` (e.g. `NSE:RELIANCE`). A bare ticker
is refused rather than guessed at — the exchange changes the instrument.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_ZERODHA
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    client_order_id,
    number as _num,
    owns_order,
    step_volume,
)

_TIMEOUT = 15
_API_VERSION = "3"
_OPEN_STATES = ("OPEN", "TRIGGER PENDING")
_DEAD_STATES = ("COMPLETE", "REJECTED", "CANCELLED", "EXPIRED")


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _tag(magic: int, digest: str) -> str:
    """Kite caps `tag` at 20 characters, so the digest is trimmed to fit."""
    prefix = f"eclipse-{int(magic or 0)}-"
    room = max(0, 20 - len(prefix))
    return client_order_id(magic, str(digest or "")[:room]) if room else client_order_id(magic)


class ZerodhaAdapter(TradingAdapter):
    kind = KIND_ZERODHA

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- http ------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="ZERODHA_CREDENTIALS", label="Zerodha API key/access token",
        )
        if not raw:
            raise AdapterUnavailable(
                "ZERODHA_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing "
                "`API_KEY:ACCESS_TOKEN`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.kite.trade"),
            code="ZERODHA_CONFIG", label="Zerodha baseUrl",
        )

    def _headers(self) -> dict[str, str]:
        key, token = self._credentials()
        return {"Accept": "application/json", "X-Kite-Version": _API_VERSION,
                "Authorization": f"token {key}:{token}"}

    def _request(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None, *, missing_ok: bool = True,
        raw: bool = False,
    ) -> Any:
        url = self._base() + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, headers=headers, data=body, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = response.read(8_000_000)
        except urllib.error.HTTPError as exc:
            detail = {"status": exc.code}
            if exc.code in (401, 403):
                raise AdapterUnavailable("ZERODHA_UNAUTHORIZED",
                                         "Kite rejected the API key/access token",
                                         detail=detail) from exc
            if exc.code == 429:
                raise AdapterUnavailable("ZERODHA_RATE_LIMITED", "Kite rate limit reached",
                                         detail=detail) from exc
            if exc.code == 404 and missing_ok:
                return None
            message = ""
            if exc.code != 404:
                try:
                    reported = json.loads(exc.read() or b"{}")
                    if isinstance(reported, dict):
                        message = str(reported.get("message") or reported.get("error_type") or "")
                except Exception:  # noqa: BLE001 - the body is advisory
                    pass
            code = "ZERODHA_NOT_FOUND" if exc.code == 404 else "ZERODHA_HTTP"
            text = ("Kite has no such order or instrument" if exc.code == 404
                    else f"Kite returned HTTP {exc.code}")
            raise AdapterUnavailable(code, text + (f": {message}" if message else ""),
                                     detail=detail) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("ZERODHA_UNREACHABLE",
                                     f"Kite at {self._base()} is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("ZERODHA_TIMEOUT", "Kite timed out") from exc
        if raw:
            return payload
        try:
            parsed = json.loads(payload or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("ZERODHA_BAD_JSON", "Kite returned invalid JSON") from exc
        if isinstance(parsed, dict) and parsed.get("status") == "error":
            raise AdapterUnavailable("ZERODHA_API_ERROR",
                                     str(parsed.get("message") or "Kite returned an error"))
        return parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params)

    # -- BrokerAdapter ---------------------------------------------------

    def probe(self) -> dict[str, Any]:
        try:
            account = self.account()
        except AdapterUnavailable as exc:
            return {"available": True, "connected": False, "code": exc.code,
                    "reason": exc.reason, "terminal": {"api": self._base()},
                    "account": None, "warnings": []}
        return {"available": True, "connected": True, "code": None, "reason": None,
                "terminal": {"api": self._base()}, "account": account, "warnings": []}

    def account(self) -> dict[str, Any]:
        profile = self._get("/user/profile") or {}
        margins = self._get("/user/margins") or {}
        equity = margins.get("equity") if isinstance(margins, dict) else {}
        equity = equity if isinstance(equity, dict) else {}
        available = equity.get("available") if isinstance(equity.get("available"), dict) else {}
        utilised = equity.get("utilised") if isinstance(equity.get("utilised"), dict) else {}
        return {
            "login": _int(profile.get("user_id"), 0),
            "armRef": str(profile.get("user_id") or "") or None,
            "mode": account_mode(self._base()),
            "server": "zerodha", "company": str(profile.get("broker") or "Zerodha"),
            "currency": "INR", "balance": _num(available.get("cash")),
            "equity": _num(equity.get("net")), "margin": _num(utilised.get("debits")),
            "marginFree": _num(available.get("cash")), "marginLevel": 0.0, "profit": 0.0,
            "leverage": 1.0, "marginMode": "cash",
            # Kite exposes no account-level trading switch; a block is enforced at
            # order time and reported by the venue's rejection.
            "tradeAllowed": not bool(profile.get("blocked") or margins.get("blocked")),
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        payload = self._get("/portfolio/positions") or {}
        rows = payload.get("net") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            quantity = _num(row.get("quantity"))
            if quantity == 0:
                continue
            symbol = str(row.get("tradingsymbol") or "").upper()
            out.append({
                "ticket": symbol, "symbol": symbol,
                "exchange": str(row.get("exchange") or "").upper(),
                "product": str(row.get("product") or "").upper(),
                "side": "buy" if quantity >= 0 else "sell", "volume": abs(quantity),
                "openPrice": _num(row.get("average_price")),
                "currentPrice": _num(row.get("last_price")),
                "stopLoss": None, "takeProfit": None, "profit": _num(row.get("pnl")),
                "swap": 0.0, "magic": 0, "comment": "zerodha (net book)",
                "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._get("/orders") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            out.append({
                "ticket": str(row.get("order_id") or ""),
                "symbol": str(row.get("tradingsymbol") or "").upper(),
                "type": str(row.get("order_type") or ""),
                "volumeCurrent": _num(row.get("pending_quantity")),
                "openPrice": _num(row.get("price")),
                "currentPrice": _num(row.get("average_price")),
                "stopLoss": None, "takeProfit": None, "state": str(row.get("status") or ""),
                "magic": 0, "comment": f"zerodha {row.get('transaction_type') or ''}".strip(),
                "clientOrderId": row.get("tag"), "placedAt": 0,
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        requested = str(symbol or "").strip().upper()
        if ":" not in requested:
            raise AdapterUnavailable(
                "ZERODHA_SYMBOL_AMBIGUOUS",
                f"quote {requested!r} needs an exchange, e.g. NSE:{requested}",
            )
        row = self._get("/quote", {"i": requested})
        if not isinstance(row, dict) or not row:
            raise AdapterUnavailable("ZERODHA_NO_QUOTE", f"Kite has no quote for {requested}")
        data = next(iter(row.values()))
        if not isinstance(data, dict):
            raise AdapterUnavailable("ZERODHA_NO_QUOTE", f"Kite has no quote for {requested}")
        depth = data.get("depth") if isinstance(data.get("depth"), dict) else {}
        buys = depth.get("buy") if isinstance(depth.get("buy"), list) else []
        sells = depth.get("sell") if isinstance(depth.get("sell"), list) else []
        return {
            "symbol": requested, "requestedSymbol": symbol,
            "bid": _num(buys[0].get("price")) if buys else 0.0,
            "ask": _num(sells[0].get("price")) if sells else 0.0,
            "last": _num(data.get("last_price")), "spreadPoints": None, "digits": 2,
            "point": 0.01, "stopsLevelPoints": 0,
            "instrumentToken": data.get("instrument_token"),
            "serverTimeMs": int(time.time() * 1000), "provider": "zerodha",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        exchange, tradingsymbol = self._split(symbol)
        row = self._instrument_row(exchange, tradingsymbol)
        if not isinstance(row, dict):
            raise AdapterUnavailable("ZERODHA_NO_SYMBOL",
                                     f"Kite has no instrument {exchange}:{tradingsymbol}")
        tick = _num(row.get("tick_size")) or 0.05
        lot = _num(row.get("lot_size")) or 1.0
        digits = len(f"{tick:.10f}".rstrip("0").partition(".")[2])
        return {
            "symbol": f"{exchange}:{tradingsymbol}", "requestedSymbol": symbol,
            "description": str(row.get("name") or ""), "path": str(row.get("instrument_type") or ""),
            "tradeMode": "full", "digits": digits, "point": tick, "tickSize": tick,
            "tickValue": tick, "currencyBase": "INR", "currencyProfit": "INR",
            "currencyMargin": "INR", "contractSize": lot, "volumeMin": lot, "volumeMax": 0.0,
            "volumeStep": lot, "stopsLevelPoints": 0, "freezeLevelPoints": 0,
            "spreadPoints": None, "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "Kite's instruments dump carries no session table",
            "instrumentToken": row.get("instrument_token"),
            "segment": str(row.get("segment") or ""), "retrievedAt": time.time(),
            "provider": "zerodha",
        }

    # -- execution -------------------------------------------------------

    def _split(self, symbol: Any) -> tuple[str, str]:
        text = str(symbol or "").strip().upper()
        exchange, sep, tradingsymbol = text.partition(":")
        if not sep or not exchange or not tradingsymbol:
            raise AdapterUnavailable(
                "ZERODHA_SYMBOL_AMBIGUOUS",
                f"Kite needs EXCHANGE:TRADINGSYMBOL, not {text!r}",
            )
        return exchange, tradingsymbol

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account = self.account()
        try:
            exchange, tradingsymbol = self._split(intent.symbol)
            symbol_error = ""
        except AdapterUnavailable as exc:
            exchange, tradingsymbol, symbol_error = "", "", exc.reason
        quote: dict[str, Any] = {}
        if not symbol_error:
            try:
                quote = self.quote(intent.symbol)
            except AdapterUnavailable as exc:
                quote = {"symbol": f"{exchange}:{tradingsymbol}", "provider": "zerodha",
                         "unavailable": exc.code}
        positions = self.positions()
        volume, reasons = step_volume(intent.volume, 1.0, 1.0)
        reasons = list(reasons)
        kind, side = str(intent.order_type or "market").lower(), str(intent.side or "").lower()
        if symbol_error:
            reasons.append(f"ZERODHA_SYMBOL_AMBIGUOUS: {symbol_error}")
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append("ZERODHA_BRACKET_UNSUPPORTED: Kite has no attached stop/target "
                           "leg; send the entry, then a separate SL-M order for the stop")
        bid, ask, last = _num(quote.get("bid")), _num(quote.get("ask")), _num(quote.get("last"))
        if kind in ("limit", "stop") and intent.price:
            price = float(intent.price)
        else:
            price = (ask or last) if side == "buy" else (bid or last)
        if price <= 0:
            reasons.append(f"ZERODHA_NO_PRICE: Kite reported no usable price for "
                           f"{intent.symbol} (the market may be closed)")
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, margin=max(0.0, volume * price),
            open_positions=len(positions), reasons=reasons, digits=2,
            volume_step=1.0, volume_min=1.0,
            extra={"venue": "zerodha", "exchange": exchange, "tradingsymbol": tradingsymbol,
                   "product": "MIS" if str(intent.time_in_force).lower() == "day" else "CNC",
                   "kiteOrderType": {"market": "MARKET", "limit": "LIMIT",
                                     "stop": "SL-M"}[kind]},
        )

    def _payload(self, intent: Any, preview: dict[str, Any]) -> dict[str, Any]:
        exchange, tradingsymbol = self._split(intent.symbol)
        kind = str(preview.get("kiteOrderType") or "MARKET")
        payload: dict[str, Any] = {
            "tradingsymbol": str(preview.get("tradingsymbol") or tradingsymbol),
            "exchange": str(preview.get("exchange") or exchange),
            "transaction_type": str(intent.side).upper(), "order_type": kind,
            "quantity": str(int(float(preview.get("volume") or 0))),
            "product": str(preview.get("product") or "MIS"), "validity": "DAY",
            "tag": _tag(intent.magic, intent.digest),
        }
        price = float(preview.get("price") or 0)
        if kind == "LIMIT":
            payload["price"] = f"{price:.2f}"
        elif kind == "SL-M":
            payload["trigger_price"] = f"{price:.2f}"
        return payload

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        price = preview.get("price") or 0.0
        base = {"symbol": intent.symbol, "side": intent.side, "order_type": intent.order_type,
                "volume": preview.get("volume") or intent.volume,
                "account": preview.get("account") or {}}
        if reasons:
            return build_result(ok=False, status="REFUSED", price=price,
                                retcode_name="PREVIEW_REJECTED", reasons=reasons, **base)
        payload = self._payload(intent, preview)
        try:
            row = self._request("POST", "/orders/regular", data=payload, missing_ok=False)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if (isinstance(status, int) and 400 <= status < 500) or exc.code == "ZERODHA_API_ERROR":
                return build_result(ok=False, status="REJECTED", price=price, retcode=status,
                                    retcode_name=exc.code, reasons=[exc.reason], **base)
            raise
        order_id = str((row or {}).get("order_id") or "") if isinstance(row, dict) else ""
        if not order_id:
            return build_result(
                ok=False, status="UNKNOWN", price=price, retcode_name="AMBIGUOUS_RESULT",
                reasons=["Kite answered without an order id; the order state is unknown — "
                         "reconcile before sending anything else"], **base)
        return build_result(
            ok=True, status="placed", price=price, ticket=order_id, retcode=200,
            retcode_name="ACCEPTED",
            comment="Kite accepted the order; the fill is not confirmed by this response",
            clientOrderId=payload.get("tag"), **base)

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            self._request("DELETE", f"/orders/{urllib.parse.quote(order_id)}",
                          {"variety": "regular"}, missing_ok=False)
        except AdapterUnavailable as exc:
            if exc.code == "ZERODHA_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Kite has no order {order_id}"]}
            if exc.code == "ZERODHA_HTTP" and (exc.detail or {}).get("status") == 400:
                return {"ok": False, "ticket": order_id, "retcode": 400,
                        "retcodeName": "CANCEL_REJECTED", "reasons": [exc.reason]}
            raise
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self._get("/orders") or []
        rows = [row for row in rows if isinstance(row, dict)
                and str(row.get("status") or "").upper() in _OPEN_STATES] \
            if isinstance(rows, list) else []
        if not rows:
            return {"ok": True, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "NO_ORDERS", "reasons": []}
        tags = [str(row.get("tag") or "").strip() for row in rows]
        if not any(tags):
            return {"ok": False, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "OWNERSHIP_UNAVAILABLE",
                    "reasons": ["Kite returned open orders with no tag; Eclipse cannot "
                                "prove which orders it placed, so it will not cancel any"]}
        results: list[dict[str, Any]] = []
        for row, tag in zip(rows, tags):
            if not owns_order(tag, magic):
                continue
            order_id = str(row.get("order_id") or "")
            try:
                results.append(self.cancel_order(order_id))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": order_id,
                                "retcodeName": exc.code, "reasons": [exc.reason]})
        cancelled = sum(1 for item in results if item.get("ok"))
        return {"ok": not any(not item.get("ok") for item in results), "cancelled": cancelled,
                "failed": len(results) - cancelled, "results": results,
                "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": []}

    def _history(self, ticket: Any) -> list[dict[str, Any]]:
        rows = self._request("GET", f"/orders/{urllib.parse.quote(str(ticket))}",
                             missing_ok=True)
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        symbol = str(ticket or "").strip().upper()
        rows = [row for row in self.positions()
                if row.get("symbol") == symbol and _num(row.get("volume")) > 0]
        if not rows:
            return {"ok": False, "ticket": symbol, "retcode": 404,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Kite has no open {symbol} position"]}
        pairs = {(row.get("exchange"), row.get("product")) for row in rows}
        if len(pairs) != 1:
            return {"ok": False, "ticket": symbol, "retcodeName": "POSITION_AMBIGUOUS",
                    "reasons": [f"{symbol} is open on more than one exchange/product "
                                f"({sorted(':'.join(map(str, pair)) for pair in pairs)}); "
                                "close it in Kite"]}
        position = rows[0]
        exchange = str(position.get("exchange") or "")
        held = _num(position.get("volume"))
        quantity, reasons = step_volume(volume if volume is not None else held, 1.0, 1.0)
        if reasons or quantity > held:
            return {"ok": False, "ticket": symbol, "volume": quantity,
                    "retcodeName": "INVALID_VOLUME",
                    "reasons": reasons or [f"cannot close {quantity:g} of a {held:g} unit "
                                           f"{symbol} position"]}
        payload = {
            "tradingsymbol": symbol, "exchange": exchange,
            "transaction_type": "SELL" if position.get("side") == "buy" else "BUY",
            "order_type": "MARKET", "quantity": str(int(quantity)),
            "product": str(position.get("product") or ""), "validity": "DAY",
        }
        try:
            row = self._request("POST", "/orders/regular", data=payload, missing_ok=False)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return {"ok": False, "ticket": symbol, "volume": quantity, "price": 0.0,
                        "retcode": status, "retcodeName": exc.code, "verified": False,
                        "reasons": [exc.reason]}
            raise
        order_id = str((row or {}).get("order_id") or "") if isinstance(row, dict) else ""
        if not order_id:
            return {"ok": False, "ticket": symbol, "volume": quantity, "price": 0.0,
                    "retcodeName": "AMBIGUOUS_RESULT", "verified": False,
                    "reasons": ["Kite did not return a closing order id; reconcile "
                                "the position"]}
        try:
            history = self._history(order_id)
            checked = history[-1] if history else {}
        except AdapterUnavailable:
            checked = {}
        state = str(checked.get("status") or "").upper()
        filled = _num(checked.get("filled_quantity"))
        price = _num(checked.get("average_price"))
        verified = state == "COMPLETE" and filled > 0 and price > 0
        return {
            "ok": True, "ticket": order_id, "positionTicket": symbol, "symbol": symbol,
            "exchange": exchange, "side": "sell" if position.get("side") == "buy" else "buy",
            "volume": filled or quantity, "price": price, "retcode": state or 200,
            "retcodeName": "CLOSED" if verified else "CLOSE_SUBMITTED",
            "partial": bool(volume) and quantity < held, "verified": verified,
            "reasons": [] if verified else
                       [f"Kite accepted the closing order; its status is {state or 'unknown'}"],
        }

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["Kite has no position-level stop/target edit; place an SL-M "
                            "order or a GTT instead"]}

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is not None or take_profit is not None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "ORDER_BRACKET_UNSUPPORTED",
                    "reasons": ["Kite orders have no attached stop/target leg to change"]}
        if price is None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST", "reasons": ["pass a new limit price"]}
        order_id = str(ticket)
        history = self._history(order_id)
        if not history:
            return {"ok": False, "ticket": order_id, "retcode": 404,
                    "retcodeName": "ORDER_NOT_FOUND",
                    "reasons": [f"Kite has no order {order_id}"]}
        current = history[-1]
        if str(current.get("order_type") or "").upper() in ("SL", "SL-M"):
            return {"ok": False, "ticket": order_id, "retcode": None,
                    "retcodeName": "ORDER_MODIFY_UNSUPPORTED",
                    "reasons": ["this adapter changes prices on LIMIT orders only; cancel "
                                "and re-submit the stop order"]}
        if str(current.get("status") or "").upper() in _DEAD_STATES:
            return {"ok": False, "ticket": order_id, "retcode": current.get("status"),
                    "retcodeName": "ORDER_NOT_OPEN",
                    "reasons": [f"order {order_id} is {current.get('status')}"]}
        try:
            self._request("PUT", f"/orders/{urllib.parse.quote(order_id)}",
                          data={"order_type": "LIMIT", "price": f"{float(price):.2f}",
                                "validity": "DAY"}, missing_ok=False)
        except AdapterUnavailable as exc:
            if exc.code == "ZERODHA_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Kite has no order {order_id}"]}
            if exc.code == "ZERODHA_HTTP" and (exc.detail or {}).get("status") == 400:
                return {"ok": False, "ticket": order_id, "retcode": 400,
                        "retcodeName": "REPLACE_FAILED", "reasons": [exc.reason]}
            raise
        return {"ok": True, "ticket": order_id, "price": float(price), "retcode": 200,
                "retcodeName": "REPLACED", "verified": True, "reasons": []}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        history = self._history(ticket)
        if not history:
            return {"found": False, "ticket": ticket}
        row = history[-1]
        return {"found": True, "ticket": str(row.get("order_id") or ticket),
                "state": row.get("status"),
                "stateName": str(row.get("status") or "").upper(),
                "filledVolume": _num(row.get("filled_quantity")),
                "avgPrice": _num(row.get("average_price"))}

    # -- instrument metadata (the Kite instruments CSV) -------------------

    def _instrument_row(self, exchange: str, tradingsymbol: str) -> Optional[dict[str, Any]]:
        payload = self._request("GET", f"/instruments/{urllib.parse.quote(exchange)}",
                                missing_ok=True, raw=True)
        if payload is None:
            return None
        reader = csv.reader(payload.decode("utf-8", "replace").splitlines())
        header = next(reader, None)
        if not header:
            return None
        for cells in reader:
            row = dict(zip(header, cells))
            if str(row.get("tradingsymbol") or "").upper() == tradingsymbol:
                return row
        return None


__all__ = ["ZerodhaAdapter"]
