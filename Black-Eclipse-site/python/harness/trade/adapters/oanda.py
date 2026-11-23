"""OANDA v20 adapter (FX and CFD): reads AND order execution.

OANDA's auth is the simplest in the tree — a bearer token, no request signing —
but two things must stay honest:

  * **the account id is data, not a guess.** v20 addresses every call by
    `accountID`, so it is configured or discovered once from `GET /v3/accounts`;
    a missing id is a named refusal, never a default.
  * **positions are two-sided.** v20 reports `long` and `short` halves on the
    same instrument. A row is emitted per non-zero half with its own units and
    PnL, rather than netting them into a number the exchange never reported.

Execution: MARKET/LIMIT/STOP entries (signed units), `takeProfitOnFill` /
`stopLossOnFill`, replace/cancel a pending order, and close a whole or part of
one position side. `mode` comes from the endpoint (the practice host is the demo
account); `tradeAllowed` from the account summary's own block flags.

Declared limits:

  * **No streaming.** Prices normally arrive over HTTP streaming; `quote()` polls
    `/pricing` once.
  * **No day/session orders.** v20 offers GTC/GTD/FOK/IOC only, so `DAY` is
    refused by name instead of being silently widened to GTC.
  * **No position bracket edit.** Stops are attached to orders, so
    `modify_position` is a named refusal.

Config: `baseUrl` (default the v20 practice host), `accountId` (optional — it is
discovered when absent), `credentialRef` naming an env var/secret holding the token.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_OANDA
from .credentials import resolve_secret_value, validate_http_url
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
_PRACTICE = "https://api-fxpractice.oanda.com"
_ERRORS = {
    404: ("OANDA_NOT_FOUND", "OANDA has no such account, instrument or order"),
    429: ("OANDA_RATE_LIMITED", "OANDA rate limit reached"),
}


def instrument_symbol(name: Any) -> str:
    """`EUR_USD` -> `EURUSD` (OANDA spells pairs with an underscore)."""
    return str(name or "").replace("_", "").upper()


def _price(value: Any) -> str:
    return f"{float(value):.10f}".rstrip("0").rstrip(".")


class OandaAdapter(TradingAdapter):
    kind = KIND_OANDA

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _token(self) -> str:
        token = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="OANDA_CREDENTIALS", label="OANDA v20 access token",
        )
        if not token:
            raise AdapterUnavailable(
                "OANDA_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding the token",
            )
        return token

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or _PRACTICE),
            code="OANDA_CONFIG", label="OANDA baseUrl",
        )

    def _request(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None, *, allow_missing: bool = False,
    ) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=data, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            detail = {"status": exc.code}
            if exc.code in (401, 403):
                raise AdapterUnavailable("OANDA_UNAUTHORIZED", "OANDA rejected the token",
                                         detail=detail) from exc
            if exc.code == 404 and allow_missing:
                return None
            code, message = _ERRORS.get(exc.code, ("OANDA_HTTP", f"OANDA returned HTTP {exc.code}"))
            if exc.code not in (404, 429):
                try:
                    reported = (json.loads(exc.read() or b"{}") or {}).get("errorMessage")
                    message += f": {reported}" if reported else ""
                except Exception:  # noqa: BLE001 - the body is advisory
                    pass
            raise AdapterUnavailable(code, message, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("OANDA_UNREACHABLE", "OANDA is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("OANDA_TIMEOUT", "OANDA timed out") from exc
        try:
            payload = json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("OANDA_BAD_JSON", "OANDA returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("errorMessage"):
            raise AdapterUnavailable("OANDA_API_ERROR", str(payload["errorMessage"])[:300])
        return payload

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params)

    def _account_id(self) -> str:
        configured = str(self.config.get("accountId") or "").strip()
        if configured:
            return configured
        payload = self._get("/v3/accounts")
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        first = accounts[0] if isinstance(accounts, list) and accounts else None
        if not isinstance(first, dict) or not first.get("id"):
            raise AdapterUnavailable("OANDA_NO_ACCOUNT", "OANDA returned no account id")
        return str(first["id"])

    def _instrument_name(self, symbol: Any) -> str:
        """Accept `EURUSD`, `EUR_USD` and the ticket form `EUR_USD:LONG`."""
        raw = str(symbol or "").strip().upper().split(":", 1)[0].strip()
        if not raw:
            raise AdapterUnavailable("OANDA_SYMBOL", "an instrument is required")
        if "_" in raw:
            return raw
        return f"{raw[:3]}_{raw[3:]}" if len(raw) == 6 else raw

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
        payload = self._get(f"/v3/accounts/{self._account_id()}/summary")
        summary = payload.get("account") if isinstance(payload, dict) else None
        if not isinstance(summary, dict):
            raise AdapterUnavailable("OANDA_BAD_RESPONSE", "OANDA returned no account summary")
        nav, available = _num(summary.get("NAV")), _num(summary.get("marginAvailable"))
        rate = _num(summary.get("marginRate"))
        return {
            "login": 0, "armRef": self._account_id(),
            "mode": account_mode(self._base()), "server": "oanda", "company": "OANDA",
            "currency": str(summary.get("currency") or "USD"),
            "balance": _num(summary.get("balance")), "equity": nav,
            "margin": _num(summary.get("marginUsed")) or (nav - available),
            "marginFree": available,
            "marginLevel": _num(summary.get("marginCloseoutPercent")) * 100.0,
            "profit": _num(summary.get("unrealizedPL")),
            "leverage": round(1.0 / rate, 2) if rate > 0 else 0.0,
            "marginMode": "cfd",
            # v20 has no "trading enabled" switch; it reports block flags only.
            "tradeAllowed": not bool(summary.get("tradingDisabled") or summary.get("blocked")),
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        payload = self._get(f"/v3/accounts/{self._account_id()}/openPositions")
        rows = payload.get("positions") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for position in rows if isinstance(rows, list) else []:
            if not isinstance(position, dict):
                continue
            instrument = str(position.get("instrument") or "")
            # A v20 position has no id of its own; it is addressed by instrument
            # and side, so the ticket carries both.
            for side in ("long", "short"):
                half = position.get(side)
                units = abs(_num((half or {}).get("units"))) if isinstance(half, dict) else 0.0
                if units == 0:
                    continue
                out.append({
                    "ticket": f"{instrument}:{side.upper()}", "symbol": instrument_symbol(instrument),
                    "instrument": instrument, "side": "buy" if side == "long" else "sell",
                    "volume": units, "openPrice": _num(half.get("averagePrice")),
                    "currentPrice": 0.0,  # not marked: that needs a price per instrument
                    "stopLoss": None, "takeProfit": None,
                    "profit": _num(half.get("unrealizedPL")), "swap": _num(half.get("financing")),
                    "magic": 0, "comment": f"oanda {side} ({instrument})",
                    "openedAt": int(time.time() * 1000),
                })
        return out

    def orders(self) -> list[dict[str, Any]]:
        payload = self._get(f"/v3/accounts/{self._account_id()}/pendingOrders")
        rows = payload.get("orders") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for order in rows if isinstance(rows, list) else []:
            if not isinstance(order, dict):
                continue
            units = _num(order.get("units"))
            extensions = order.get("clientExtensions")
            out.append({
                "ticket": str(order.get("id") or ""),
                "symbol": instrument_symbol(order.get("instrument") or ""),
                "type": str(order.get("type") or ""), "volumeCurrent": abs(units),
                "openPrice": _num(order.get("price")), "currentPrice": 0.0,
                "stopLoss": None, "takeProfit": None, "state": str(order.get("state") or ""),
                "magic": 0, "comment": f"oanda {'buy' if units > 0 else 'sell'}".strip(),
                "clientOrderId": extensions.get("id") if isinstance(extensions, dict) else None,
                "placedAt": int(_num(order.get("createTime")) / 1e6) if order.get("createTime") else 0,
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        instrument = self._instrument_name(symbol)
        payload = self._get(f"/v3/accounts/{self._account_id()}/pricing", {"instruments": instrument})
        prices = payload.get("prices") if isinstance(payload, dict) else None
        row = prices[0] if isinstance(prices, list) and prices else None
        if not isinstance(row, dict) or not row.get("bids"):
            raise AdapterUnavailable("OANDA_NO_QUOTE", f"OANDA has no price for {instrument}")
        bid = _num((row.get("bids") or [{}])[0].get("price"))
        ask = _num((row.get("asks") or [{}])[0].get("price"))
        return {
            "symbol": instrument_symbol(instrument), "requestedSymbol": symbol,
            "instrument": instrument, "bid": bid, "ask": ask,
            "last": (bid + ask) / 2 if bid and ask else bid or ask,
            "spreadPoints": round((ask - bid) * 100_000, 1) if bid and ask else None,
            "digits": 5, "point": 0.00001, "stopsLevelPoints": 0,
            # v20 flags a closed/illiquid instrument on the price itself.
            "tradeable": bool(row.get("tradeable", True)),
            "serverTimeMs": int(time.time() * 1000), "provider": "oanda",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        instrument = self._instrument_name(symbol)
        payload = self._get(f"/v3/accounts/{self._account_id()}/instruments",
                            {"instruments": instrument})
        rows = payload.get("instruments") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("OANDA_NO_SYMBOL", f"OANDA has no instrument {instrument!r}")
        digits = int(_num(row.get("displayPrecision"), 5))
        step = 10 ** -int(_num(row.get("tradeUnitsPrecision"), 0))
        base, _, counter = instrument.partition("_")
        return {
            "symbol": instrument_symbol(instrument), "requestedSymbol": symbol,
            "description": str(row.get("displayName") or ""), "path": str(row.get("type") or ""),
            "tradeMode": "full", "digits": digits, "point": 10 ** -digits,
            "tickSize": 10 ** -digits, "tickValue": 10 ** -digits,
            "currencyBase": base, "currencyProfit": counter, "currencyMargin": counter,
            "contractSize": 1.0, "volumeMin": _num(row.get("minimumTradeSize")),
            "volumeMax": _num(row.get("maximumOrderUnits")), "volumeStep": step,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "OANDA reports tradeability per price, not a session table",
            "marginRate": _num(row.get("marginRate")), "retrievedAt": time.time(),
            "provider": "oanda",
        }

    # -- execution -------------------------------------------------------

    def _price_reasons(self, intent: Any, price: float, quote: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        side, kind = str(intent.side or "").lower(), str(intent.order_type or "market").lower()
        if quote.get("tradeable") is False:
            reasons.append(f"OANDA reports {quote.get('symbol')} as not tradeable right now "
                           "(the market may be closed)")
        if kind == "limit" and price > 0 and bid > 0 and ask > 0:
            if side == "buy" and price >= ask:
                reasons.append("a buy limit must be below the current ask")
            if side == "sell" and price <= bid:
                reasons.append("a sell limit must be above the current bid")
        if kind == "stop" and price > 0 and bid > 0 and ask > 0:
            if side == "buy" and price <= ask:
                reasons.append("a buy stop must be above the current ask")
            if side == "sell" and price >= bid:
                reasons.append("a sell stop must be below the current bid")
        if intent.stop_loss is not None:
            if side == "buy" and not intent.stop_loss < price:
                reasons.append("a buy stop loss must be below the entry price")
            if side == "sell" and not intent.stop_loss > price:
                reasons.append("a sell stop loss must be above the entry price")
        if intent.take_profit is not None:
            if side == "buy" and not intent.take_profit > price:
                reasons.append("a buy take profit must be above the entry price")
            if side == "sell" and not intent.take_profit < price:
                reasons.append("a sell take profit must be below the entry price")
        return reasons

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, positions = self.account(), self.positions()
        quote, info = self.quote(intent.symbol), self.symbol_info(intent.symbol)
        step = _num(info.get("volumeStep")) or 1.0
        volume, reasons = step_volume(intent.volume, step, _num(info.get("volumeMin")),
                                      _num(info.get("volumeMax")))
        side = str(intent.side or "").lower()
        if str(intent.order_type) in ("limit", "stop") and intent.price:
            price = float(intent.price)
        else:
            price = _num(quote.get("ask")) if side == "buy" else _num(quote.get("bid"))
        reasons += self._price_reasons(intent, price, quote)
        rate = _num(info.get("marginRate"))
        if rate <= 0 and _num(account.get("leverage")) > 0:
            rate = 1.0 / _num(account.get("leverage"))
        if str(intent.time_in_force or "gtc").lower() == "day":
            reasons.append("OANDA v20 has no day/session order; use gtc")
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price, contract_size=1.0,
            margin=volume * price * rate if rate > 0 else volume * price,
            open_positions=len(positions), reasons=reasons,
            digits=int(_num(info.get("digits")) or 5), volume_step=step,
            volume_min=_num(info.get("volumeMin")), volume_max=_num(info.get("volumeMax")),
            extra={"venue": "oanda", "instrument": info.get("symbol"), "marginRate": rate,
                   "timeInForce": "FOK" if str(intent.order_type) == "market" else "GTC"},
        )

    def _payload(self, intent: Any, preview: dict[str, Any]) -> dict[str, Any]:
        digits = int(_num(preview.get("digits")) or 5)
        units = f"{float(preview.get('volume') or 0):.10f}".rstrip("0").rstrip(".")
        order: dict[str, Any] = {
            "type": {"market": "MARKET", "limit": "LIMIT", "stop": "STOP"}[str(intent.order_type)],
            "instrument": self._instrument_name(intent.symbol),
            "units": units if str(intent.side) == "buy" else f"-{units}",
            "positionFill": "DEFAULT",
            "clientExtensions": {
                "id": client_order_id(intent.magic, intent.digest),
                "tag": client_order_id(intent.magic),
                "comment": str(intent.comment or "eclipse")[:128],
            },
        }
        if str(intent.order_type) == "market":
            # v20 market orders must say how to treat an unfilled remainder.
            order["timeInForce"] = "FOK"
        else:
            order["timeInForce"] = "GTC"
            order["price"] = f"{float(preview.get('price') or 0):.{digits}f}"
        if intent.take_profit is not None:
            order["takeProfitOnFill"] = {"price": f"{float(intent.take_profit):.{digits}f}"}
        if intent.stop_loss is not None:
            order["stopLossOnFill"] = {"price": f"{float(intent.stop_loss):.{digits}f}"}
        return order

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
        try:
            row = self._request("POST", f"/v3/accounts/{self._account_id()}/orders",
                                body={"order": self._payload(intent, preview)})
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return build_result(ok=False, status="REJECTED", price=price, retcode=status,
                                    retcode_name=exc.code, reasons=[exc.reason], **base)
            raise
        if not isinstance(row, dict):
            raise AdapterUnavailable("OANDA_BAD_RESPONSE", "OANDA returned no order response")
        fill, created = row.get("orderFillTransaction"), row.get("orderCreateTransaction")
        reject, cancelled = row.get("orderRejectTransaction"), row.get("orderCancelTransaction")
        if isinstance(fill, dict) and fill:
            client = (fill.get("clientExtensions") or {}).get("id")
            return build_result(
                ok=True, status="FILLED",
                price=_num(fill.get("price")) or price,
                fill_volume=abs(_num(fill.get("units"))) or None,
                ticket=str(fill.get("orderID") or fill.get("id") or ""),
                retcode="FILLED", retcode_name="FILLED", comment=str(client or ""),
                clientOrderId=client, **base)
        if isinstance(created, dict) and created:
            client = (created.get("clientExtensions") or {}).get("id")
            return build_result(ok=True, status="placed", price=price,
                                ticket=str(created.get("id") or ""),
                                retcode="PENDING", retcode_name="PENDING", comment=str(client or ""),
                                clientOrderId=client, **base)
        if isinstance(reject, dict) or isinstance(cancelled, dict):
            tx = reject if isinstance(reject, dict) else cancelled
            reason = str(tx.get("rejectReason") or tx.get("reason") or "OANDA refused the order")
            return build_result(ok=False, status="REJECTED", price=price, retcode=reason,
                                retcode_name="ORDER_REJECTED", reasons=[reason], **base)
        return build_result(
            ok=False, status="UNKNOWN", price=price, retcode=None,
            retcode_name="AMBIGUOUS_RESULT",
            reasons=["OANDA answered without a fill, an order id or a rejection; the order "
                     "state is unknown — reconcile before sending anything else"], **base)

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            row = self._request(
                "PUT", f"/v3/accounts/{self._account_id()}/orders/"
                       f"{urllib.parse.quote(order_id, safe='@')}/cancel")
        except AdapterUnavailable as exc:
            if exc.code == "OANDA_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"OANDA has no order {order_id}"]}
            raise
        tx = (row or {}).get("orderCancelTransaction") if isinstance(row, dict) else None
        tx = tx if isinstance(tx, dict) else {}
        if str(tx.get("reason") or "") == "CANCELLED":
            return {"ok": True, "ticket": order_id, "retcode": "CANCELLED",
                    "retcodeName": "CANCELLED", "reasons": []}
        reason = str(tx.get("rejectReason") or tx.get("reason") or "OANDA did not confirm the cancel")
        return {"ok": False, "ticket": order_id, "retcode": tx.get("reason"),
                "retcodeName": "CANCEL_REJECTED", "reasons": [reason]}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        payload = self._get(f"/v3/accounts/{self._account_id()}/pendingOrders")
        rows = payload.get("orders") if isinstance(payload, dict) else None
        results: list[dict[str, Any]] = []
        for order in rows if isinstance(rows, list) else []:
            extensions = order.get("clientExtensions") if isinstance(order, dict) else None
            client_id = extensions.get("id") if isinstance(extensions, dict) else None
            if not owns_order(client_id, magic):
                continue
            order_id = str(order.get("id") or "")
            try:
                results.append(self.cancel_order(order_id))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": order_id,
                                "retcodeName": exc.code, "reasons": [exc.reason]})
        cancelled = sum(1 for item in results if item.get("ok"))
        return {"ok": not any(not item.get("ok") for item in results), "cancelled": cancelled,
                "failed": len(results) - cancelled, "results": results,
                "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": []}

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        raw = str(ticket or "").strip().upper()
        instrument, _, side = raw.partition(":")
        if side not in ("LONG", "SHORT"):
            side = ""
        instrument = self._instrument_name(instrument)
        if not side:
            rows = [row for row in self.positions()
                    if row.get("instrument") == instrument and _num(row.get("volume")) > 0]
            if len(rows) != 1:
                return {"ok": False, "ticket": ticket, "retcodeName": "POSITION_AMBIGUOUS",
                        "reasons": [f"OANDA has {len(rows)} open {instrument} sides; pass the "
                                    f"ticket '{instrument}:LONG' or '{instrument}:SHORT'"]}
            side = "LONG" if rows[0].get("side") == "buy" else "SHORT"
        if volume is not None and _num(volume) <= 0:
            return {"ok": False, "ticket": ticket, "volume": 0.0, "retcodeName": "INVALID_VOLUME",
                    "reasons": ["volume to close must be greater than zero"]}
        field = "longUnits" if side == "LONG" else "shortUnits"
        body = {field: "ALL" if volume is None
                else f"{abs(_num(volume)):.10f}".rstrip("0").rstrip(".")}
        row = self._request(
            "PUT", f"/v3/accounts/{self._account_id()}/positions/"
                   f"{urllib.parse.quote(instrument)}/close",
            body=body, allow_missing=True)
        if row is None:
            return {"ok": False, "ticket": ticket, "retcode": 404,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"OANDA has no open {instrument} position"]}
        fill = row.get("longOrderFillTransaction") or row.get("shortOrderFillTransaction") or {}
        reject = row.get("longOrderRejectTransaction") or row.get("shortOrderRejectTransaction") or {}
        if not isinstance(fill, dict) or not fill:
            reason = str((reject or {}).get("rejectReason")
                         or "OANDA did not confirm the close; reconcile the position")
            return {"ok": False, "ticket": ticket, "volume": volume, "price": 0.0,
                    "retcodeName": "CLOSE_NOT_CONFIRMED", "verified": False, "reasons": [reason]}
        return {
            "ok": True, "ticket": str(fill.get("id") or fill.get("orderID") or ticket),
            "positionTicket": ticket, "instrument": instrument,
            "symbol": instrument_symbol(instrument),
            "side": "buy" if side == "LONG" else "sell",
            "volume": abs(_num(fill.get("units"))), "price": _num(fill.get("price")),
            "retcode": "FILLED", "retcodeName": "CLOSED", "partial": bool(volume),
            "verified": True, "reasons": [],
        }

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["OANDA attaches stops to the order (takeProfitOnFill/"
                            "stopLossOnFill), not to the open position; cancel and re-enter "
                            "with a bracket"]}

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        order: dict[str, Any] = {}
        if price is not None:
            order["price"] = _price(price)
        if stop_loss is not None:
            order["stopLossOnFill"] = {"price": _price(stop_loss)}
        if take_profit is not None:
            order["takeProfitOnFill"] = {"price": _price(take_profit)}
        if not order:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST",
                    "reasons": ["pass a new price, stop loss or take profit"]}
        order_id = str(ticket)
        row = self._request(
            "PUT", f"/v3/accounts/{self._account_id()}/orders/"
                   f"{urllib.parse.quote(order_id, safe='@')}",
            body={"order": order}, allow_missing=True)
        if row is None:
            return {"ok": False, "ticket": order_id, "retcode": 404,
                    "retcodeName": "ORDER_NOT_FOUND", "reasons": [f"OANDA has no order {order_id}"]}
        created = row.get("orderCreateTransaction") or row.get("orderFillTransaction") or {}
        ok = isinstance(created, dict) and bool(created)
        return {"ok": ok, "ticket": order_id, "price": price, "stopLoss": stop_loss,
                "takeProfit": take_profit, "retcode": "REPLACED" if ok else None,
                "retcodeName": "REPLACED" if ok else "REPLACE_FAILED", "verified": ok,
                "reasons": [] if ok else ["OANDA did not return the replaced order"]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._request(
            "GET", f"/v3/accounts/{self._account_id()}/orders/"
                   f"{urllib.parse.quote(order_id, safe='@')}", allow_missing=True)
        order = row.get("order") if isinstance(row, dict) else None
        if not isinstance(order, dict):
            return {"found": False, "ticket": ticket}
        extensions = order.get("clientExtensions")
        return {"found": True, "ticket": str(order.get("id") or order_id),
                "state": order.get("state"), "stateName": str(order.get("state") or "").upper(),
                "clientOrderId": extensions.get("id") if isinstance(extensions, dict) else None,
                "filledVolume": abs(_num(order.get("units")))}


__all__ = ["OandaAdapter", "instrument_symbol"]
