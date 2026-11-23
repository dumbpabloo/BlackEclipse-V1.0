"""Deribit adapter (crypto derivatives): reads AND order execution.

Deribit's private API is JSON-RPC over **GET**, and its signature is a
newline-joined preimage rather than a concatenation:

    sig = hex HMAC-SHA256(client_secret, ts + "\\n" + nonce + "\\n" + data + "\\n" + query)
    Authorization: deri-hmac-sha256 id=<client_id>,ts=<ts>,nonce=<nonce>,sig=<sig>

`data` is the empty string for GET, and the query string is signed **verbatim** —
`sign_request` is pure so both facts can be checked against an independent
computation instead of hoped for.

Execution uses the same signed GET: `/api/v2/private/buy` and `/private/sell`
take `instrument_name`, `amount` (contracts, always positive — the endpoint
picks the direction), `type` (market/limit/stop_market), `price`, `stop_loss`,
`take_profit` and the Eclipse `label`. Cancel/edit/state are
`/private/cancel`, `/private/edit` and `/private/get_order_state`.

Declared limits:

  * **No websocket stream.**
  * The account summary is per **currency** (Deribit keeps them separate), so
    `account()` marks the returned `currency` rather than inventing a total.
  * A JSON-RPC `error` object is a refusal, never empty data.
  * A position's stop/target is an **order** attribute, not a position field;
    `modify_position` refuses and `modify_order` is the edit path.
  * Closing submits the opposite market order with `reduce_only=true`; the
    returned state comes from Deribit, never from optimism.

Config: `baseUrl` (default https://www.deribit.com), `currency` (default `BTC`),
`credentialRef` naming an env var/secret holding `CLIENT_ID:CLIENT_SECRET`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_DERIBIT
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    client_order_id,
    owns_order,
    step_volume,
)

_TIMEOUT = 15


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: Any) -> str:
    """A decimal string the venue parses the same way; never scientific notation."""
    return f"{_num(value):.10f}".rstrip("0").rstrip(".") or "0"


def _digits(tick: float) -> int:
    text = _fmt(tick)
    return len(text.split(".", 1)[1]) if "." in text else 0


def sign_request(secret: str, timestamp: str, nonce: str, query: str,
                 data: str = "") -> str:
    """Deribit's `deri-hmac-sha256` signature over ts/nonce/data/query."""
    preimage = f"{timestamp}\n{nonce}\n{data}\n{query}"
    return hmac.new(secret.encode("utf-8"), preimage.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class DeribitAdapter(TradingAdapter):
    kind = KIND_DERIBIT

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="DERIBIT_CREDENTIALS",
            label="Deribit client id/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "DERIBIT_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing "
                "`CLIENT_ID:CLIENT_SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://www.deribit.com"),
            code="DERIBIT_CONFIG",
            label="Deribit baseUrl",
        )

    def _currency(self) -> str:
        return str(self.config.get("currency") or "BTC").upper()

    def _request(self, path: str, params: Optional[dict[str, Any]] = None, *,
                 signed: bool = True) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json"}
        if signed:
            client_id, secret = self._credentials()
            timestamp = str(int(time.time() * 1000))
            nonce = str(uuid.uuid4())
            signature = sign_request(secret, timestamp, nonce, query)
            headers["Authorization"] = (
                f"deri-hmac-sha256 id={client_id},ts={timestamp},"
                f"nonce={nonce},sig={signature}"
            )
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("DERIBIT_UNAUTHORIZED", "Deribit rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("DERIBIT_RATE_LIMITED", "Deribit rate limit reached") from exc
            raise AdapterUnavailable("DERIBIT_HTTP", f"Deribit returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("DERIBIT_UNREACHABLE", "Deribit is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("DERIBIT_TIMEOUT", "Deribit timed out") from exc
        try:
            payload = json.loads(body or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("DERIBIT_BAD_JSON", "Deribit returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterUnavailable("DERIBIT_BAD_SHAPE", "Deribit returned no envelope")
        error = payload.get("error")
        if isinstance(error, dict) and error:
            raise AdapterUnavailable(
                "DERIBIT_API_ERROR",
                f"Deribit error {error.get('code')}: {error.get('message') or 'unknown'}",
            )
        return payload.get("result")

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
        result = self._request("/api/v2/private/get_account_summary",
                              {"currency": self._currency()})
        if not isinstance(result, dict):
            raise AdapterUnavailable("DERIBIT_BAD_RESPONSE", "Deribit returned no account summary")
        equity = _num(result.get("equity"))
        available = _num(result.get("available_funds"))
        return {
            "login": 0, "mode": account_mode(self._base()),
            "server": "deribit", "company": "Deribit",
            "currency": str(result.get("currency") or self._currency()),
            "balance": _num(result.get("balance")), "equity": equity,
            "margin": equity - available, "marginFree": available,
            "marginLevel": 0.0, "profit": _num(result.get("session_upl")),
            "leverage": 1.0, "marginMode": "derivatives",
            # The summary carries no trading switch; a rejected order is the
            # venue's own answer, so the account is not pre-declared disabled.
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v2/private/get_positions", {"currency": self._currency()})
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            size = abs(_num(row.get("size")))
            if size == 0:
                continue
            direction = str(row.get("direction") or "")
            instrument = str(row.get("instrument_name") or "").upper()
            out.append({
                # A Deribit position has no id, but (instrument, direction) is
                # stable and is what close_position parses back.
                "ticket": f"{instrument}:{direction}",
                "symbol": instrument,
                "side": "buy" if direction == "buy" else "sell",
                "volume": size,
                "openPrice": _num(row.get("average_price")),
                "currentPrice": _num(row.get("mark_price")),
                "stopLoss": None,
                "takeProfit": None,
                "profit": _num(row.get("floating_profit_loss")),
                "swap": 0.0,
                "magic": 0,
                "comment": f"deribit {row.get('kind') or 'position'} ({self._currency()})",
                "openedAt": int(_num(row.get("timestamp"))),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._request("/api/v2/private/get_open_orders_by_currency",
                             {"currency": self._currency()})
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            amount = abs(_num(row.get("amount")))
            filled = abs(_num(row.get("filled_amount")))
            out.append({
                "ticket": str(row.get("order_id") or ""),
                "symbol": str(row.get("instrument_name") or "").upper(),
                "type": str(row.get("order_type") or ""),
                "volumeCurrent": max(0.0, amount - filled),
                "openPrice": _num(row.get("price")),
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "state": str(row.get("order_state") or ""),
                "magic": 0,
                "comment": f"deribit {str(row.get('direction') or '').lower()}".strip(),
                "clientOrderId": str(row.get("label") or ""),
                "placedAt": int(_num(row.get("creation_timestamp"))),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        instrument = str(symbol or "").strip().upper()
        if not instrument:
            raise AdapterUnavailable("DERIBIT_SYMBOL", "an instrument name is required")
        row = self._request("/api/v2/public/ticker", {"instrument_name": instrument},
                            signed=False)
        if not isinstance(row, dict) or row.get("last_price") in (None, ""):
            raise AdapterUnavailable("DERIBIT_NO_QUOTE", f"Deribit has no ticker for {instrument}")
        last = _num(row.get("last_price"))
        return {
            "symbol": instrument,
            "requestedSymbol": symbol,
            "bid": _num(row.get("best_bid_price")) or last,
            "ask": _num(row.get("best_ask_price")) or last,
            "last": last,
            "spreadPoints": None,
            "digits": 8,
            "point": 0.00000001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(_num(row.get("timestamp")) or time.time() * 1000),
            "provider": "deribit",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        instrument = str(symbol or "").strip().upper()
        if not instrument:
            raise AdapterUnavailable("DERIBIT_SYMBOL", "an instrument name is required")
        rows = self._request(
            "/api/v2/public/get_instruments",
            {"currency": self._currency(), "expired": "false"},
            signed=False,
        )
        match = None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("instrument_name") or "").upper() == instrument:
                match = row
                break
        if match is None:
            raise AdapterUnavailable(
                "DERIBIT_NO_SYMBOL", f"Deribit has no instrument {instrument}"
            )
        tick = _num(match.get("tick_size"))
        settle = str(match.get("settlement_currency") or "")
        return {
            "symbol": instrument, "requestedSymbol": symbol,
            "description": str(match.get("kind") or ""), "path": settle,
            "tradeMode": "disabled" if match.get("is_active") is False else "full",
            "digits": _digits(tick), "point": tick, "tickSize": tick, "tickValue": tick,
            "currencyBase": str(match.get("base_currency") or ""),
            "currencyProfit": settle, "currencyMargin": settle,
            "contractSize": _num(match.get("contract_size"), 1.0) or 1.0,
            "volumeMin": _num(match.get("min_trade_amount")),
            "volumeMax": _num(match.get("max_trade_amount")),
            # Deribit publishes no lot step for every kind; an absent step is
            # reported as 0, which step_volume treats as "not applied".
            "volumeStep": _num(match.get("amount_step")),
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False,
            "sessionNote": "Deribit trades continuously; no session table is returned",
            "retrievedAt": time.time(), "provider": "deribit",
        }

    # -- execution -------------------------------------------------------
    _TYPES = {"market": "market", "limit": "limit", "stop": "stop_market"}

    @staticmethod
    def _bracket_reasons(intent: Any, price: float) -> list[str]:
        reasons: list[str] = []
        if intent.stop_loss is not None:
            if intent.side == "buy" and not intent.stop_loss < price:
                reasons.append("a buy stop loss must be below the entry price")
            if intent.side == "sell" and not intent.stop_loss > price:
                reasons.append("a sell stop loss must be above the entry price")
        if intent.take_profit is not None:
            if intent.side == "buy" and not intent.take_profit > price:
                reasons.append("a buy take profit must be above the entry price")
            if intent.side == "sell" and not intent.take_profit < price:
                reasons.append("a sell take profit must be below the entry price")
        return reasons

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, quote, positions = self.account(), self.quote(intent.symbol), self.positions()
        try:
            info = self.symbol_info(intent.symbol)
        except AdapterUnavailable:
            info = {}
        step, minimum = _num(info.get("volumeStep")), _num(info.get("volumeMin"))
        volume, reasons = step_volume(intent.volume, step, minimum)
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        price = float(intent.price) if intent.order_type in ("limit", "stop") and intent.price else (
            ask if intent.side == "buy" else bid)
        reasons += self._bracket_reasons(intent, price)
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, margin=max(0.0, volume * price),
            open_positions=len(positions), reasons=reasons,
            digits=int(info.get("digits") or 8), volume_step=step, volume_min=minimum,
            volume_max=_num(info.get("volumeMax")),
            extra={"venue": "deribit", "instrument": info},
        )

    def _order_params(self, intent: Any, volume: float) -> dict[str, Any]:
        params: dict[str, Any] = {
            "instrument_name": str(intent.symbol).strip().upper(),
            "amount": _fmt(volume),
            "type": self._TYPES.get(str(intent.order_type), "market"),
            "label": client_order_id(intent.magic, intent.digest),
        }
        if intent.order_type == "limit":
            params["price"] = _fmt(intent.price)
        elif intent.order_type == "stop":
            # A Deribit stop triggers on a price; the same value is the
            # protective limit, so the order cannot run away after the trigger.
            params["trigger_price"] = _fmt(intent.price)
            params["price"] = _fmt(intent.price)
        if intent.stop_loss is not None:
            params["stop_loss"] = _fmt(intent.stop_loss)
        if intent.take_profit is not None:
            params["take_profit"] = _fmt(intent.take_profit)
        return params

    @staticmethod
    def _order_of(result: Any) -> Optional[dict[str, Any]]:
        order = result.get("order") if isinstance(result, dict) else None
        return order if isinstance(order, dict) else None

    @staticmethod
    def _accepted(state: str) -> bool:
        return state in ("open", "untriggered", "filled")

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0, retcode_name="PREVIEW_REJECTED",
                reasons=reasons, account=preview.get("account") or {})
        order = self._order_of(self._request(
            "/api/v2/private/buy" if intent.side == "buy" else "/api/v2/private/sell",
            self._order_params(intent, float(preview["volume"]))))
        if order is None:
            raise AdapterUnavailable("DERIBIT_BAD_RESPONSE", "Deribit returned no order")
        state = str(order.get("order_state") or "").lower()
        accepted = self._accepted(state)
        return build_result(
            ok=accepted,
            status="FILLED" if state == "filled" else ("placed" if accepted else "REJECTED"),
            symbol=order.get("instrument_name") or intent.symbol, side=intent.side,
            order_type=intent.order_type, volume=preview["volume"],
            fill_volume=_num(order.get("filled_amount")) or None,
            price=_num(order.get("average_price")) or preview["price"],
            ticket=str(order.get("order_id") or ""), retcode=state or None,
            retcode_name=state.upper() or "ACCEPTED", comment=str(order.get("label") or ""),
            reasons=[] if accepted else [f"Deribit reported order state {state or 'unknown'}"],
            account=preview.get("account") or {}, clientOrderId=order.get("label"),
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            order = self._order_of(
                self._request("/api/v2/private/cancel", {"order_id": order_id}))
        except AdapterUnavailable as exc:
            if exc.code == "DERIBIT_API_ERROR" and "not found" in exc.reason.lower():
                return {"ok": False, "ticket": order_id, "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Deribit has no order {order_id}"]}
            raise
        state = str((order or {}).get("order_state") or "").lower()
        if state == "cancelled":
            return {"ok": True, "ticket": order_id, "retcodeName": "CANCELLED", "reasons": []}
        if state == "filled":
            return {"ok": False, "ticket": order_id, "retcodeName": "TOO_LATE",
                    "reasons": ["the order filled before it could be cancelled"]}
        return {"ok": False, "ticket": order_id,
                "retcodeName": state.upper() or "CANCEL_REJECTED",
                "reasons": [f"Deribit reported state {state or 'unknown'} after the cancel"]}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        tagged = [row for row in rows if str(row.get("clientOrderId") or "")]
        if rows and not tagged:
            return {"ok": False, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "OWNERSHIP_UNAVAILABLE",
                    "reasons": ["Deribit returned open orders without their Eclipse label"]}
        results: list[dict[str, Any]] = []
        for row in tagged:
            if not owns_order(row.get("clientOrderId"), magic):
                continue
            order_id = str(row.get("ticket") or "")
            try:
                results.append(self.cancel_order(order_id))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": order_id,
                                "retcodeName": exc.code, "reasons": [exc.reason]})
        cancelled = sum(1 for item in results if item.get("ok"))
        return {"ok": not any(not item.get("ok") for item in results), "cancelled": cancelled,
                "failed": len(results) - cancelled, "results": results,
                "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": []}

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        instrument, _, direction = str(ticket or "").partition(":")
        if not instrument or direction not in ("buy", "sell"):
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_TICKET",
                    "reasons": ["pass the position ticket `INSTRUMENT:direction`"]}
        row = next((item for item in self.positions() if str(item.get("ticket")) == str(ticket)), None)
        if row is None:
            return {"ok": False, "ticket": ticket, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Deribit has no open {instrument} {direction} position"]}
        size = float(volume) if volume else _num(row.get("volume"))
        partial = bool(volume) and size < _num(row.get("volume"))
        order = self._order_of(self._request(
            "/api/v2/private/buy" if direction == "sell" else "/api/v2/private/sell",
            {"instrument_name": instrument, "amount": _fmt(size), "type": "market",
             # Deribit documents reduce_only for derivative orders; it stops a
             # race from flipping the position instead of flattening it.
             "reduce_only": "true"},
        ))
        if order is None:
            raise AdapterUnavailable("DERIBIT_BAD_RESPONSE", "Deribit returned no close order")
        state = str(order.get("order_state") or "").lower()
        accepted = self._accepted(state)
        return {
            "ok": accepted, "ticket": str(ticket), "symbol": instrument,
            "side": "buy" if direction == "sell" else "sell", "volume": size,
            "price": _num(order.get("average_price")) or _num(row.get("currentPrice")),
            "retcode": state or None,
            "retcodeName": "CLOSED" if state == "filled" else (state.upper() if accepted else "REJECTED"),
            "orderId": str(order.get("order_id") or ""), "partial": partial,
            "reduceOnly": True, "verified": state == "filled",
            "reasons": [] if accepted else [f"Deribit reported state {state or 'unknown'}"],
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
                "reasons": ["Deribit attaches stops to orders, not positions; "
                            "use modify_order on the working order instead"]}

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"order_id": str(ticket)}
        if price is not None:
            params["price"] = _fmt(price)
        if stop_loss is not None:
            params["stop_loss"] = _fmt(stop_loss)
        if take_profit is not None:
            params["take_profit"] = _fmt(take_profit)
        if len(params) == 1:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST",
                    "reasons": ["pass a new price, stopLoss and/or takeProfit"]}
        order = self._order_of(self._request("/api/v2/private/edit", params))
        if order is None:
            raise AdapterUnavailable("DERIBIT_BAD_RESPONSE", "Deribit returned no edited order")
        state = str(order.get("order_state") or "").lower()
        ok = self._accepted(state)
        return {
            "ok": ok, "ticket": str(ticket),
            "symbol": str(order.get("instrument_name") or "").upper(),
            "price": _num(order.get("price")) or price,
            "stopLoss": _num(order.get("stop_loss")) or stop_loss,
            "takeProfit": _num(order.get("take_profit")) or take_profit,
            "retcode": state or None, "retcodeName": "REPLACED" if ok else "REPLACE_FAILED",
            "verified": ok,
            "reasons": [] if ok else [f"Deribit reported state {state or 'unknown'}"],
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        try:
            order = self._request(
                "/api/v2/private/get_order_state", {"order_id": str(ticket)})
        except AdapterUnavailable as exc:
            if exc.code == "DERIBIT_API_ERROR" and "not found" in exc.reason.lower():
                return {"found": False, "ticket": str(ticket)}
            raise
        if not isinstance(order, dict):
            return {"found": False, "ticket": str(ticket)}
        state = str(order.get("order_state") or "").lower()
        return {"found": True, "ticket": str(order.get("order_id") or ticket),
                "state": state, "stateName": state.upper(),
                "filledVolume": _num(order.get("filled_amount")),
                "avgPrice": _num(order.get("average_price"))}


__all__ = ["DeribitAdapter", "sign_request"]
