"""Trading212 adapter (equities / ETFs): reads AND order execution.

The public API is a clean REST surface: cash, portfolio, open orders and
market/limit placement. Execution is deliberately narrow because the venue is:

  * **market + limit only.** No stop-entry order and no attached stop-loss/take
    profit, so both are refused by name rather than dropped silently.
  * **quote-less.** There is no price endpoint. `quote()` raises
    `TRADING212_QUOTE_UNSUPPORTED`, and `order_preview` can only price an order
    from a reference the caller supplied (`intent.price`). A market order with no
    reference is refused — Eclipse never invents a price.
  * **history-less.** The API exposes open orders, not fills, so `deals_since`
    stays unsupported and `close_position` reports `verified: false` unless the
    venue's own response carries a fill.
  * **no ownership tag.** `cancel_all` refuses with `OWNERSHIP_UNAVAILABLE`
    instead of cancelling an order Eclipse cannot prove it placed.

Auth: Trading212 issues an API key used as the HTTP Basic *username* with an
empty password. `authStyle` may be set to `"key"` for a deployment whose gateway
expects the raw key instead (the two forms are base64(`KEY:`) vs base64(`KEY`)).

Config (all non-secret):
  baseUrl        default https://live.trading212.com/api/v0
                 demo:  https://demo.trading212.com/api/v0
  credentialRef  NAME of an env var / secret holding the API key
  currency       account currency label (default GBP); the cash payload does not
                 carry it, so it is declared, never guessed
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_TRADING212
from .credentials import resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    number as _num,
    step_volume,
)

_TIMEOUT = 15
_MAX_RETRIES = 2


class Trading212Adapter(TradingAdapter):
    kind = KIND_TRADING212

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        # stdlib only: nothing to install, nothing to probe over the network here.
        return True, None

    # -- http ------------------------------------------------------------

    def _credentials(self) -> str:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="TRADING212_CREDENTIALS", label="Trading212 API key",
        )
        if not raw:
            raise AdapterUnavailable(
                "TRADING212_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding the API key",
            )
        return raw.strip()

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://live.trading212.com/api/v0"),
            code="TRADING212_CONFIG", label="Trading212 baseUrl",
        )

    def _auth_header(self) -> str:
        key = self._credentials()
        style = str(self.config.get("authStyle") or "colon").strip().lower()
        token = base64.b64encode((key if style == "key" else key + ":").encode("utf-8"))
        return "Basic " + token.decode("ascii")

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
                 *, missing_ok: bool = True) -> Any:
        """One HTTP call. Reads may retry a 429/5xx; a send never does."""
        url = self._base() + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": self._auth_header()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=data, method=method.upper())
        retryable = method.upper() in ("GET", "HEAD")
        answer: bytes | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                    answer = response.read(4_000_000)
                break
            except urllib.error.HTTPError as exc:
                detail = {"status": exc.code}
                if retryable and exc.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
                    time.sleep(min(max(2**attempt, 0.5), 5.0))
                    continue
                if exc.code in (401, 403):
                    raise AdapterUnavailable("TRADING212_UNAUTHORIZED",
                                             "Trading212 rejected the API key",
                                             detail=detail) from exc
                if exc.code == 404 and missing_ok:
                    return None
                if exc.code == 404:
                    raise AdapterUnavailable("TRADING212_NOT_FOUND",
                                             "Trading212 has no such order",
                                             detail=detail) from exc
                raise AdapterUnavailable("TRADING212_HTTP",
                                         f"Trading212 returned HTTP {exc.code}",
                                         detail=detail) from exc
            except urllib.error.URLError as exc:
                raise AdapterUnavailable(
                    "TRADING212_UNREACHABLE", f"Trading212 at {self._base()} is unreachable"
                ) from exc
            except TimeoutError as exc:
                raise AdapterUnavailable("TRADING212_TIMEOUT", "Trading212 timed out") from exc
        if answer is None:
            raise AdapterUnavailable("TRADING212_RETRY_EXHAUSTED",
                                     "Trading212 did not answer after retries")
        try:
            return json.loads(answer or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("TRADING212_BAD_JSON",
                                     "Trading212 returned invalid JSON") from exc

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

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
        row = self._get("/equity/account/cash")
        if not isinstance(row, dict):
            raise AdapterUnavailable("TRADING212_BAD_RESPONSE", "Trading212 returned no account")
        blocked = row.get("blocked")
        if blocked is None:
            blocked = row.get("tradingBlocked")
        return {
            "login": 0, "mode": account_mode(self._base()), "server": "trading212",
            "company": "Trading212", "currency": str(self.config.get("currency") or "GBP"),
            "balance": _num(row.get("free")), "equity": _num(row.get("total")),
            "margin": _num(row.get("invested")), "marginFree": _num(row.get("free")),
            "marginLevel": 0.0, "profit": _num(row.get("ppl")), "leverage": 1.0,
            "marginMode": "cash",
            # The cash payload has no trading switch; only an explicit block says no.
            "tradeAllowed": not bool(blocked), "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._get("/equity/portfolio") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            quantity = _num(row.get("quantity"))
            if quantity == 0:
                continue
            ticker = str(row.get("ticker") or "").upper()
            out.append({
                "ticket": ticker, "symbol": ticker,
                "side": "buy" if quantity >= 0 else "sell", "volume": abs(quantity),
                "openPrice": _num(row.get("averagePrice")),
                "currentPrice": _num(row.get("currentPrice")),
                "stopLoss": None, "takeProfit": None, "profit": _num(row.get("ppl")),
                "swap": _num(row.get("fxPpl")), "magic": 0, "comment": "trading212",
                # The payload has no open timestamp; "first seen" is honest.
                "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._get("/equity/orders") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            quantity = _num(row.get("quantity"))
            out.append({
                "ticket": str(row.get("id") or ""),
                "symbol": str(row.get("ticker") or "").upper(),
                "type": str(row.get("type") or ""), "volumeCurrent": abs(quantity),
                "openPrice": _num(row.get("limitPrice")) or _num(row.get("stopPrice")),
                "currentPrice": 0.0,
                "stopLoss": _num(row.get("stopLoss")) or None,
                "takeProfit": _num(row.get("takeProfit")) or None,
                "state": str(row.get("status") or ""), "magic": 0,
                "comment": "trading212", "placedAt": 0,
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        raise AdapterUnavailable(
            "TRADING212_QUOTE_UNSUPPORTED",
            "Trading212's public API has no quote endpoint; use a market-data provider",
        )

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        ticker = str(symbol or "").strip().upper()
        rows = self._get("/equity/metadata/instruments") or []
        row = next((item for item in rows if isinstance(item, dict)
                    and str(item.get("ticker") or "").upper() == ticker), None) \
            if isinstance(rows, list) else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("TRADING212_NO_SYMBOL",
                                     f"Trading212 has no instrument {ticker!r}")
        currency = str(row.get("currencyCode") or "")
        return {
            "symbol": ticker, "requestedSymbol": symbol,
            "description": str(row.get("name") or ""), "path": str(row.get("type") or ""),
            "tradeMode": "full", "digits": 4, "point": 0.0001, "tickSize": 0.0001,
            "tickValue": 0.0001, "currencyBase": currency, "currencyProfit": currency,
            "currencyMargin": currency, "contractSize": 1.0,
            # Trading212 publishes no lot minimum/step over this endpoint.
            "volumeMin": 0.0, "volumeMax": _num(row.get("maxOpenQuantity")), "volumeStep": 0.0,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "Trading212's instrument metadata carries no session or lot step",
            "isin": str(row.get("isin") or ""), "retrievedAt": time.time(),
            "provider": "trading212",
        }

    # -- execution -------------------------------------------------------

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, positions = self.account(), self.positions()
        volume, reasons = step_volume(intent.volume)
        reasons = list(reasons)
        symbol = str(intent.symbol or "").strip().upper()
        kind = str(intent.order_type or "market").lower()
        if kind == "stop":
            reasons.append("TRADING212_STOP_ENTRY_UNSUPPORTED: Trading212's public API has "
                           "no stop entry order; use a market or limit order")
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append("TRADING212_BRACKET_UNSUPPORTED: Trading212 orders cannot carry "
                           "a stop loss or take profit; place a separate order to exit")
        price = float(intent.price) if intent.price else 0.0
        if price <= 0:
            reasons.append("TRADING212_QUOTE_UNSUPPORTED: Trading212's public API has no quote "
                           "endpoint, so a market order needs a price reference (intent.price); "
                           "Eclipse will not invent one")
        # No venue quote exists: the quote block stays empty rather than carrying
        # a price the venue never reported.
        quote = {"symbol": symbol, "requestedSymbol": intent.symbol, "bid": 0.0, "ask": 0.0,
                 "last": 0.0, "spreadPoints": None, "digits": 4, "point": 0.0001,
                 "stopsLevelPoints": 0, "provider": "trading212",
                 "priceSource": "intent" if price > 0 else "none"}
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, margin=max(0.0, volume * price),
            open_positions=len(positions), reasons=reasons, digits=4,
            extra={"venue": "trading212", "orderType": kind,
                   "priceSource": quote["priceSource"]},
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
        quantity = float(preview["volume"])
        signed = quantity if str(intent.side) == "buy" else -quantity
        body: dict[str, Any] = {"ticker": str(intent.symbol).strip().upper(), "quantity": signed}
        if str(intent.order_type) == "limit":
            body["limitPrice"] = float(price)
            path = "/equity/orders/limit"
        else:
            path = "/equity/orders/market"
        try:
            row = self._request("POST", path, body, missing_ok=False)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return build_result(ok=False, status="REJECTED", price=price, retcode=status,
                                    retcode_name=exc.code, reasons=[exc.reason], **base)
            raise
        if not isinstance(row, dict) or row.get("id") is None:
            return build_result(
                ok=False, status="UNKNOWN", price=price, retcode_name="AMBIGUOUS_RESULT",
                reasons=["Trading212 answered without an order id; the order state is "
                         "unknown — reconcile before sending anything else"], **base)
        state = str(row.get("status") or "").upper()
        accepted = state not in ("REJECTED", "CANCELLED", "CANCELED", "DECLINED")
        filled = state == "FILLED"
        return build_result(
            ok=accepted, status="FILLED" if filled else ("placed" if accepted else "REJECTED"),
            price=price,
            fill_volume=_num(row.get("filledQuantity")) or (quantity if filled else None),
            ticket=str(row["id"]), retcode=state or None,
            retcode_name=state or ("ACCEPTED" if accepted else "REJECTED"),
            comment=str(row.get("dateCreated") or ""),
            reasons=[] if accepted else [f"Trading212 reported status {state or 'unknown'}"],
            **base)

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            self._request("DELETE", f"/equity/orders/{urllib.parse.quote(order_id)}",
                          missing_ok=False)
        except AdapterUnavailable as exc:
            if exc.code == "TRADING212_NOT_FOUND":
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Trading212 has no order {order_id}"]}
            if exc.code == "TRADING212_HTTP" and (exc.detail or {}).get("status") == 400:
                return {"ok": False, "ticket": order_id, "retcode": 400,
                        "retcodeName": "CANCEL_REJECTED", "reasons": [exc.reason]}
            raise
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        return {
            "ok": False, "cancelled": 0, "failed": 0, "results": [],
            "retcodeName": "OWNERSHIP_UNAVAILABLE",
            "reasons": ["Trading212's public API carries no client-order id and no order tag; "
                        "Eclipse cannot prove which open orders it placed, so it will not "
                        "cancel any of them — cancel the specific order by id instead"],
        }

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        ticker = str(ticket or "").strip().upper()
        rows = [row for row in self.positions()
                if row.get("symbol") == ticker and _num(row.get("volume")) > 0]
        if not rows:
            return {"ok": False, "ticket": ticker, "retcode": 404,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Trading212 has no open {ticker} position"]}
        position = rows[0]
        held = _num(position.get("volume"))
        quantity, reasons = step_volume(volume if volume is not None else held)
        if reasons or quantity > held:
            return {"ok": False, "ticket": ticker, "volume": quantity,
                    "retcodeName": "INVALID_VOLUME",
                    "reasons": reasons or [f"cannot close {quantity:g} of a {held:g} unit "
                                           f"{ticker} position"]}
        signed = -quantity if position.get("side") == "buy" else quantity
        try:
            row = self._request("POST", "/equity/orders/market",
                                {"ticker": ticker, "quantity": signed}, missing_ok=False)
        except AdapterUnavailable as exc:
            status = (exc.detail or {}).get("status")
            if isinstance(status, int) and 400 <= status < 500:
                return {"ok": False, "ticket": ticker, "volume": quantity, "price": 0.0,
                        "retcode": status, "retcodeName": exc.code, "verified": False,
                        "reasons": [exc.reason]}
            raise
        if not isinstance(row, dict) or row.get("id") is None:
            return {"ok": False, "ticket": ticker, "volume": quantity, "price": 0.0,
                    "retcodeName": "AMBIGUOUS_RESULT", "verified": False,
                    "reasons": ["Trading212 did not return a closing order id; reconcile "
                                "the position"]}
        filled = _num(row.get("filledQuantity"))
        price = _num(row.get("fillPrice") or row.get("price"))
        state = str(row.get("status") or "").upper()
        verified = state == "FILLED" and filled > 0 and price > 0
        return {
            "ok": True, "ticket": str(row["id"]), "positionTicket": ticker, "symbol": ticker,
            "side": "sell" if signed < 0 else "buy", "volume": filled or quantity,
            "price": price, "retcode": state or None,
            "retcodeName": "CLOSED" if verified else "CLOSE_SUBMITTED",
            "partial": bool(volume) and quantity < held, "verified": verified,
            "reasons": [] if verified else ["Trading212 accepted the closing order but this "
                                            "response carries no confirmed fill"],
        }

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["Trading212's public API has no position-level stop/target; "
                            "close the position or place a separate order"]}

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "ORDER_MODIFY_UNSUPPORTED",
                "reasons": ["Trading212's public API has no order-replace endpoint; cancel "
                            "the order and submit a new one"]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._request("GET", f"/equity/orders/{urllib.parse.quote(order_id)}",
                            missing_ok=True)
        if not isinstance(row, dict) or row.get("id") is None:
            return {"found": False, "ticket": ticket}
        return {"found": True, "ticket": str(row.get("id")), "state": row.get("status"),
                "stateName": str(row.get("status") or "").upper(),
                "filledVolume": _num(row.get("filledQuantity")),
                "avgPrice": _num(row.get("fillPrice") or row.get("price"))}


__all__ = ["Trading212Adapter"]
