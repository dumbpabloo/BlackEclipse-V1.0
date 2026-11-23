"""Tradier adapter (US equities and options): reads AND order execution.

Tradier authenticates with a single Bearer token and returns JSON in a nested
envelope (`{"balances": {...}}`, `{"positions": {"position": [...]}}`) whose list
fields collapse to a single object when there is exactly one row. The adapter
flattens that consistently so a caller never sees "sometimes a dict".

Execution: equity/option MARKET, LIMIT and STOP orders (form-encoded), cancel,
change-price, and close-by-symbol. There is **no client-order-id field**;
placement accepts a free-text `tag`, so Eclipse writes `eclipse-<magic>-…` there
and `cancel_all` only touches orders whose tag matches. When the venue returns
open orders with no tag at all, ownership cannot be proven and cancel_all
refuses rather than cancelling a manual order.

Declared limits, so nothing is invented:

  * **Quotes are snapshots**, not a stream; balances are the broker's own
    figures (this adapter never marks positions to market itself).
  * **No attached stop/target legs.** Orders are single-leg, so a `stop_loss` /
    `take_profit` on the intent, or `modify_position`, is refused by name.
  * **No instrument-metadata endpoint**, so `symbol_info` is a named refusal.

Config (all non-secret):
  baseUrl       default https://api.tradier.com (sandbox: https://sandbox.tradier.com)
  accountId     the brokerage account id (required for account/positions/orders)
  credentialRef NAME of an env var / secret holding the access token
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_TRADIER
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
_CLOSED = ("filled", "canceled", "cancelled", "expired", "rejected", "pending_cancel")


def _rows(value: Any) -> list[dict[str, Any]]:
    """Tradier's list fields: `None`, one object, or a list of objects."""
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return [value] if isinstance(value, dict) else []


def _nested(payload: Any, outer: str, inner: str) -> Any:
    """`payload[outer][inner]` without assuming either level exists or is a dict.

    Tradier returns `{"quotes": null}` as readily as `{"quotes": {"quote": …}}`,
    and `.get(outer, {})` still yields `None` when the key is present with a null
    value — the exact bug this helper exists to prevent.
    """
    if not isinstance(payload, dict):
        return None
    block = payload.get(outer)
    return block.get(inner) if isinstance(block, dict) else None


def _ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return 0


def option_parts(symbol: Any) -> tuple[bool, str]:
    """`AAPL230120C00150000` -> `(True, "AAPL")`; anything else is equity."""
    text = str(symbol or "").strip().upper()
    if len(text) >= 16 and text[-9] in ("C", "P") and text[-15:-9].isdigit() and text[-8:].isdigit():
        return True, text[:-15]
    return False, ""


class TradierAdapter(TradingAdapter):
    kind = KIND_TRADIER

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- http ------------------------------------------------------------

    def _token(self) -> str:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="TRADIER_CREDENTIALS", label="Tradier access token",
        )
        if not raw:
            raise AdapterUnavailable(
                "TRADIER_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding the token",
            )
        return str(raw).strip()

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.tradier.com"),
            code="TRADIER_CONFIG", label="Tradier baseUrl",
        )

    def _account_id(self) -> str:
        value = str(self.config.get("accountId") or "").strip()
        if not value:
            raise AdapterUnavailable(
                "TRADIER_ACCOUNT", "accountId is required for account, positions and orders"
            )
        return value

    def _request(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None, *, allow_missing: bool = False,
    ) -> Any:
        url = self._base() + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token()}"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, headers=headers, data=body, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            detail = {"status": exc.code}
            if exc.code in (401, 403):
                raise AdapterUnavailable("TRADIER_UNAUTHORIZED", "Tradier rejected the access token",
                                         detail=detail) from exc
            if exc.code == 404 and allow_missing:
                return None
            if exc.code == 429:
                raise AdapterUnavailable("TRADIER_RATE_LIMITED", "Tradier rate limit reached",
                                         detail=detail) from exc
            message = ""
            if exc.code != 404:
                try:
                    errors = (json.loads(exc.read() or b"{}") or {}).get("errors")
                    message = str(errors.get("error") if isinstance(errors, dict) else errors or "")
                except Exception:  # noqa: BLE001 - the body is advisory
                    pass
            code = "TRADIER_NOT_FOUND" if exc.code == 404 else "TRADIER_HTTP"
            text = "Tradier has no such order" if exc.code == 404 else f"Tradier returned HTTP {exc.code}"
            raise AdapterUnavailable(code, text + (f": {message}" if message else ""),
                                     detail=detail) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("TRADIER_UNREACHABLE",
                                     f"Tradier at {self._base()} is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("TRADIER_TIMEOUT", "Tradier timed out") from exc
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("TRADIER_BAD_JSON", "Tradier returned invalid JSON") from exc

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
        payload = self._get(f"/v1/accounts/{self._account_id()}/balances")
        balances = (payload or {}).get("balances") if isinstance(payload, dict) else None
        if not isinstance(balances, dict):
            raise AdapterUnavailable("TRADIER_BAD_RESPONSE", "Tradier returned no balances")
        margin = balances.get("margin")
        return {
            "login": 0, "armRef": self._account_id(),
            "mode": account_mode(self._base()), "server": "tradier",
            "company": "Tradier", "currency": "USD",
            "balance": _num(balances.get("total_cash")),
            "equity": _num(balances.get("total_equity")),
            "margin": _num(margin.get("stock_buying_power") if isinstance(margin, dict) else 0),
            "marginFree": _num(balances.get("total_cash")), "marginLevel": 0.0,
            "profit": _num(balances.get("close_pl")), "leverage": 1.0, "marginMode": "cash",
            # The balances payload has no trading switch; absence of a block is
            # the only truthful reading, and the venue still refuses at send time.
            "tradeAllowed": not bool(balances.get("trading_blocked")),
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        payload = self._get(f"/v1/accounts/{self._account_id()}/positions")
        out: list[dict[str, Any]] = []
        for row in _rows(_nested(payload, "positions", "position")):
            quantity = _num(row.get("quantity"))
            if quantity == 0:
                continue
            symbol = str(row.get("symbol") or "").upper()
            out.append({
                "ticket": symbol, "symbol": symbol,
                "side": "buy" if quantity >= 0 else "sell", "volume": abs(quantity),
                "openPrice": _num(row.get("cost_basis")),
                "currentPrice": 0.0,  # the adapter does not mark to market itself
                "stopLoss": None, "takeProfit": None, "profit": 0.0, "swap": 0.0,
                "magic": 0, "comment": "tradier", "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        payload = self._get(f"/v1/accounts/{self._account_id()}/orders", {"status": "open"})
        out: list[dict[str, Any]] = []
        for row in _rows(_nested(payload, "orders", "order")):
            out.append({
                "ticket": str(row.get("id") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "type": str(row.get("type") or ""),
                "volumeCurrent": _num(row.get("remaining_quantity")),
                "openPrice": _num(row.get("price")),
                "currentPrice": _num(row.get("avg_fill_price")),
                "stopLoss": None, "takeProfit": None, "state": str(row.get("status") or ""),
                "magic": 0, "comment": f"tradier {row.get('side') or ''}".strip(),
                "clientOrderId": row.get("tag"),
                "placedAt": _ms(row.get("create_date") or row.get("transaction_date")),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        requested = str(symbol or "").strip().upper()
        if not requested:
            raise AdapterUnavailable("TRADIER_SYMBOL", "a symbol is required")
        payload = self._get("/v1/markets/quotes", {"symbols": requested})
        rows = _rows(_nested(payload, "quotes", "quote"))
        if not rows:
            raise AdapterUnavailable("TRADIER_NO_QUOTE", f"Tradier has no quote for {requested}")
        row = rows[0]
        bid, ask = _num(row.get("bid")), _num(row.get("ask"))
        return {
            "symbol": requested, "requestedSymbol": symbol, "bid": bid, "ask": ask,
            "last": _num(row.get("last")) or (bid + ask) / 2 if (bid and ask) else _num(row.get("last")),
            "spreadPoints": None, "digits": 2, "point": 0.01, "stopsLevelPoints": 0,
            "serverTimeMs": int(time.time() * 1000), "provider": "tradier",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        raise AdapterUnavailable(
            "SYMBOL_INFO_NOT_SUPPORTED",
            "Tradier's brokerage API exposes quotes but no contract/instrument metadata "
            "(tick size, lot step, contract multiplier) for a symbol",
        )

    # -- execution -------------------------------------------------------

    def _form(self, symbol: str, side: str, order_type: str, volume: float,
              price: Optional[float], time_in_force: str) -> dict[str, Any]:
        text = str(symbol or "").strip().upper()
        is_option, underlying = option_parts(text)
        form: dict[str, Any] = {
            "class": "option" if is_option else "equity", "symbol": underlying or text,
            "side": str(side).lower(), "quantity": str(int(volume)),
            "type": str(order_type).lower(),
            "duration": "day" if str(time_in_force).lower() == "day" else "gtc",
        }
        if is_option:
            form["option_symbol"] = text
        if form["type"] == "limit" and price:
            form["price"] = f"{float(price):.2f}"
        elif form["type"] == "stop" and price:
            form["stop"] = f"{float(price):.2f}"
        return form

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, positions = self.account(), self.positions()
        quote = self.quote(intent.symbol)
        volume, reasons = step_volume(intent.volume, 1.0, 1.0)
        reasons = list(reasons)
        symbol = str(intent.symbol or "").strip().upper()
        is_option, _underlying = option_parts(symbol)
        side = str(intent.side or "").lower()
        bid, ask, last = _num(quote.get("bid")), _num(quote.get("ask")), _num(quote.get("last"))
        if str(intent.order_type) in ("limit", "stop") and intent.price:
            price = float(intent.price)
        else:
            price = (ask or last) if side == "buy" else (bid or last)
        if price <= 0:
            reasons.append(f"TRADIER_MARKET_CLOSED: Tradier returned no bid, ask or last price "
                           f"for {symbol} (the market may be closed or the symbol halted)")
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append("TRADIER_BRACKET_UNSUPPORTED: Tradier orders are single-leg; "
                           "submit the entry and the exit as two orders")
        contract = 100.0 if is_option else 1.0
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=contract, margin=max(0.0, volume * contract * price),
            open_positions=len(positions), reasons=reasons, digits=2,
            volume_step=1.0, volume_min=1.0,
            extra={"venue": "tradier", "assetClass": "option" if is_option else "equity",
                   "orderClass": "option" if is_option else "equity",
                   "duration": "day" if str(intent.time_in_force).lower() == "day" else "gtc"},
        )

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
        form = self._form(intent.symbol, intent.side, intent.order_type,
                          float(preview["volume"]), float(price), intent.time_in_force)
        form["tag"] = client_order_id(intent.magic, intent.digest)
        try:
            row = self._request("POST", f"/v1/accounts/{self._account_id()}/orders", data=form)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return build_result(ok=False, status="REJECTED", price=price, retcode=status,
                                    retcode_name=exc.code, reasons=[exc.reason], **base)
            raise
        order = (row or {}).get("order") if isinstance(row, dict) else None
        if not isinstance(order, dict) or order.get("id") is None:
            return build_result(
                ok=False, status="UNKNOWN", price=price, retcode_name="AMBIGUOUS_RESULT",
                reasons=["Tradier answered without an order id; the order state is unknown — "
                         "reconcile before sending anything else"], **base)
        accepted = str(order.get("status") or "ok").lower() in ("ok", "accepted", "open", "pending")
        return build_result(
            ok=accepted, status="placed" if accepted else "REJECTED", price=price,
            ticket=str(order["id"]), retcode=str(order.get("status") or ""),
            retcode_name="ACCEPTED" if accepted else "ORDER_REJECTED",
            comment="Tradier accepted the order; the fill is not confirmed by this response",
            reasons=[] if accepted else [f"Tradier reported status {order.get('status')!r}"],
            clientOrderId=form.get("tag"), **base)

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            row = self._request("DELETE", f"/v1/accounts/{self._account_id()}/orders/"
                                          f"{urllib.parse.quote(order_id)}")
        except AdapterUnavailable as exc:
            if exc.code == "TRADIER_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Tradier has no order {order_id}"]}
            if exc.code == "TRADIER_HTTP" and (exc.detail or {}).get("status") == 400:
                return {"ok": False, "ticket": order_id, "retcode": 400,
                        "retcodeName": "CANCEL_REJECTED", "reasons": [exc.reason]}
            raise
        order = (row or {}).get("order") if isinstance(row, dict) else None
        if isinstance(order, dict) and str(order.get("status") or "ok").lower() not in (
                "ok", "canceled", "cancelled"):
            return {"ok": False, "ticket": order_id, "retcode": order.get("status"),
                    "retcodeName": "CANCEL_REJECTED",
                    "reasons": [f"Tradier reported {order.get('status')!r} for order {order_id}"]}
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def _open_order_rows(self) -> list[dict[str, Any]]:
        payload = self._get(f"/v1/accounts/{self._account_id()}/orders", {"status": "open"})
        return _rows(_nested(payload, "orders", "order"))

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = [row for row in self._open_order_rows()
                if str(row.get("status") or "").lower() not in _CLOSED]
        if not rows:
            return {"ok": True, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "NO_ORDERS", "reasons": []}
        tags = [str(row.get("tag") or "").strip() for row in rows]
        if not any(tags):
            return {"ok": False, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "OWNERSHIP_UNAVAILABLE",
                    "reasons": ["Tradier returned open orders without any `tag`; Eclipse "
                                "cannot prove which orders it placed, so it will not cancel "
                                "any of them"]}
        results: list[dict[str, Any]] = []
        for row, tag in zip(rows, tags):
            if not owns_order(tag, magic):
                continue
            order_id = str(row.get("id") or "")
            try:
                results.append(self.cancel_order(order_id))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": order_id,
                                "retcodeName": exc.code, "reasons": [exc.reason]})
        cancelled = sum(1 for item in results if item.get("ok"))
        return {"ok": not any(not item.get("ok") for item in results), "cancelled": cancelled,
                "failed": len(results) - cancelled, "results": results,
                "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": []}

    def _order_row(self, ticket: Any) -> dict[str, Any]:
        payload = self._request(
            "GET", f"/v1/accounts/{self._account_id()}/orders/{urllib.parse.quote(str(ticket))}",
            allow_missing=True)
        order = (payload or {}).get("order") if isinstance(payload, dict) else None
        return order if isinstance(order, dict) else {}

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        symbol = str(ticket or "").strip().upper()
        rows = [row for row in self.positions()
                if row.get("symbol") == symbol and _num(row.get("volume")) > 0]
        if not rows:
            return {"ok": False, "ticket": symbol, "retcode": 404,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Tradier has no open {symbol} position"]}
        position = rows[0]
        held = _num(position.get("volume"))
        quantity, reasons = step_volume(volume if volume is not None else held, 1.0, 1.0)
        if reasons or quantity > held:
            return {"ok": False, "ticket": symbol, "volume": quantity,
                    "retcodeName": "INVALID_VOLUME",
                    "reasons": reasons or [f"cannot close {quantity:g} of a {held:g} unit "
                                           f"{symbol} position"]}
        side = "sell" if position.get("side") == "buy" else "buy"
        form = self._form(symbol, side, "market", quantity, None, "day")
        try:
            row = self._request("POST", f"/v1/accounts/{self._account_id()}/orders", data=form)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return {"ok": False, "ticket": symbol, "volume": quantity, "price": 0.0,
                        "retcode": status, "retcodeName": exc.code, "verified": False,
                        "reasons": [exc.reason]}
            raise
        order = (row or {}).get("order") if isinstance(row, dict) else None
        order_id = str((order or {}).get("id") or "")
        if not order_id:
            return {"ok": False, "ticket": symbol, "volume": quantity, "price": 0.0,
                    "retcodeName": "AMBIGUOUS_RESULT", "verified": False,
                    "reasons": ["Tradier did not return a closing order id; reconcile "
                                "the position"]}
        # One read to learn whether the venue already filled it. A read is not a
        # second send, and `verified` stays false when the venue has not filled.
        try:
            checked = self._order_row(order_id)
        except AdapterUnavailable:
            checked = {}
        filled = _num(checked.get("filled_quantity") or checked.get("exec_quantity"))
        price = _num(checked.get("avg_fill_price"))
        state = str(checked.get("status") or "").lower()
        verified = state == "filled" and filled > 0 and price > 0
        return {
            "ok": True, "ticket": order_id, "positionTicket": symbol, "symbol": symbol,
            "side": side, "volume": filled or quantity, "price": price, "retcode": 200,
            "retcodeName": "CLOSED" if verified else "CLOSE_SUBMITTED",
            "partial": bool(volume) and quantity < held, "verified": verified,
            "reasons": [] if verified else
                       ["Tradier accepted the closing order but has not confirmed the fill"],
        }

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["Tradier has no position-level stop/target; place a separate "
                            "stop or limit order instead of editing the position"]}

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is not None or take_profit is not None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "ORDER_BRACKET_UNSUPPORTED",
                    "reasons": ["Tradier changes one leg's price; cancel the order and "
                                "submit a new one to change its stop/target"]}
        if price is None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST", "reasons": ["pass a new limit price"]}
        order_id = str(ticket)
        current = self._order_row(order_id)
        if not current:
            return {"ok": False, "ticket": order_id, "retcode": 404,
                    "retcodeName": "ORDER_NOT_FOUND",
                    "reasons": [f"Tradier has no order {order_id}"]}
        if str(current.get("type") or "").lower() in ("stop", "stop_limit"):
            return {"ok": False, "ticket": order_id, "retcode": None,
                    "retcodeName": "ORDER_MODIFY_UNSUPPORTED",
                    "reasons": ["changing a stop price is not supported by this adapter; "
                                "cancel and re-submit the stop order"]}
        row = self._request(
            "PUT", f"/v1/accounts/{self._account_id()}/orders/{urllib.parse.quote(order_id)}",
            data={"type": "limit", "price": f"{float(price):.2f}", "duration": "day"},
            allow_missing=True)
        if row is None:
            return {"ok": False, "ticket": order_id, "retcode": 404,
                    "retcodeName": "ORDER_NOT_FOUND",
                    "reasons": [f"Tradier has no order {order_id}"]}
        order = (row or {}).get("order") if isinstance(row, dict) else None
        ok = isinstance(order, dict) and str(order.get("status") or "ok").lower() == "ok"
        return {"ok": ok, "ticket": order_id,
                "symbol": str((order or {}).get("symbol") or ""),
                "price": _num((order or {}).get("price")) or float(price),
                "retcode": 200 if ok else None,
                "retcodeName": "REPLACED" if ok else "REPLACE_FAILED", "verified": ok,
                "reasons": [] if ok else ["Tradier did not confirm the change"]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order = self._order_row(ticket)
        if not order:
            return {"found": False, "ticket": ticket}
        return {"found": True, "ticket": str(order.get("id") or ticket),
                "state": order.get("status"),
                "stateName": str(order.get("status") or "").upper(),
                "filledVolume": _num(order.get("exec_quantity") or order.get("filled_quantity")),
                "avgPrice": _num(order.get("avg_fill_price"))}


__all__ = ["TradierAdapter", "option_parts"]
