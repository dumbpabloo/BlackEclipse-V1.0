"""Bybit v5 adapter (unified account): reads AND order execution.

Bybit signs a **prefix**, not the payload alone:

    signature = hex(HMAC-SHA256(secret, timestamp + api_key + recv_window + payload))

`payload` is the query string for a GET and the exact JSON body text for a POST.
The timestamp is milliseconds. `sign_request` is pure so the exact input can be
checked against an independent computation — the usual mistakes are
signature-comma prefixes copied from v3 and signing the query on a POST.

Execution: `/v5/order/create` (Market/Limit, `takeProfit`/`stopLoss` on
derivatives), `/v5/order/cancel`, `/v5/order/amend`, `/v5/position/trading-stop`
and the `/v5/order/realtime` read. Ownership travels in `orderLinkId` =
`eclipse-<magic>-<digest>`.

Declared limits:

  * **Spot has no attached stop.** A spot order carrying SL/TP is refused with
    `STOP_UNSUPPORTED`.
  * **Stop entries are refused** (`STOP_ENTRY_UNSUPPORTED`): Bybit treats them
    as a conditional order this adapter does not submit.
  * **No websocket stream.**
  * A close reads the venue's own position (or spot balance) first and refuses
    when it is absent — the size is never guessed.
  * `retCode != 0` is a refusal with Bybit's own message, never empty data.

Config: `baseUrl` (default https://api.bybit.com), `category` (default `linear`),
`credentialRef` naming an env var/secret holding `KEY:SECRET`.
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
from ..model import KIND_BYBIT
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    client_order_id,
    number as _fnum,
    owns_order,
    step_volume,
)

_TIMEOUT = 15
_RECV_WINDOW = "5000"
_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH")


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: Any) -> str:
    return f"{_fnum(value):.10f}".rstrip("0").rstrip(".")


def _decimals(tick: Any) -> int:
    text = str(tick or "").strip()
    return len(text.split(".")[1]) if "." in text else 8


def sign_request(secret: str, timestamp: str, api_key: str, query: str,
                 recv_window: str = _RECV_WINDOW) -> str:
    """Bybit v5 `X-BAPI-SIGN`: hex HMAC-SHA256 over ts+key+window+query."""
    message = f"{timestamp}{api_key}{recv_window}{query}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _refusal(ticket: Any, code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "ticket": ticket, "retcode": None, "retcodeName": code,
            "reasons": [reason], **extra}


class BybitAdapter(TradingAdapter):
    kind = KIND_BYBIT

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="BYBIT_CREDENTIALS",
            label="Bybit API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "BYBIT_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.bybit.com"),
            code="BYBIT_CONFIG", label="Bybit baseUrl",
        )

    def _category(self) -> str:
        return str(self.config.get("category") or "linear")

    def _derivative(self) -> bool:
        return self._category() != "spot"

    def _request(self, path: str, params: Optional[dict[str, Any]] = None, *,
                 signed: bool = True, method: str = "GET",
                 body: Optional[dict[str, Any]] = None, check: bool = True) -> Any:
        """A signed call. `check=False` returns the envelope even on `retCode != 0`."""
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        ) if method.upper() == "GET" else ""
        text = "" if body is None else json.dumps(body, separators=(",", ":"))
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json"}
        if text:
            headers["Content-Type"] = "application/json"
        if signed:
            key, secret = self._credentials()
            timestamp = str(int(time.time() * 1000))
            # A POST signs its body; a GET signs its query string.
            headers.update({
                "X-BAPI-API-KEY": key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
                "X-BAPI-SIGN": sign_request(secret, timestamp, key, text or query),
            })
        request = urllib.request.Request(
            url, headers=headers, data=text.encode("utf-8") if text else None,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("BYBIT_UNAUTHORIZED", "Bybit rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("BYBIT_RATE_LIMITED", "Bybit rate limit reached") from exc
            if exc.code == 451:
                raise AdapterUnavailable("BYBIT_RESTRICTED", "Bybit refused this jurisdiction (451)") from exc
            raise AdapterUnavailable("BYBIT_HTTP", f"Bybit returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("BYBIT_UNREACHABLE", "Bybit is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BYBIT_TIMEOUT", "Bybit timed out") from exc
        try:
            payload = json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("BYBIT_BAD_JSON", "Bybit returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BYBIT_BAD_SHAPE", "Bybit returned no envelope")
        if int(payload.get("retCode") or 0) != 0:
            if not check:
                return payload
            raise AdapterUnavailable(
                "BYBIT_API_ERROR",
                f"Bybit error {payload.get('retCode')}: {payload.get('retMsg') or 'unknown'}",
            )
        if not check:
            return payload
        return payload.get("result") or {}

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
        result = self._request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        rows = result.get("list") if isinstance(result, dict) else None
        wallet = rows[0] if isinstance(rows, list) and rows else {}
        coins = wallet.get("coin") if isinstance(wallet, dict) else None
        equity = total = available = 0.0
        for coin in coins if isinstance(coins, list) else []:
            if not isinstance(coin, dict):
                continue
            equity += _num(coin.get("equity"))
            total += _num(coin.get("walletBalance"))
            available += _num(coin.get("availableToWithdraw"))
        return {
            "login": 0, "mode": account_mode(self._base()), "server": "bybit",
            "company": "Bybit", "currency": "USD", "balance": total, "equity": equity,
            "margin": equity - available, "marginFree": available, "marginLevel": 0.0,
            "profit": 0.0, "leverage": 1.0,
            "marginMode": str(wallet.get("accountType") or "UNIFIED").lower()
            if isinstance(wallet, dict) else "unified",
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        result = self._request("/v5/position/list", {"category": self._category()})
        rows = result.get("list") if isinstance(result, dict) else None
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            size = _num(row.get("size"))
            if size == 0:
                continue
            symbol = str(row.get("symbol") or "").upper()
            side = "buy" if str(row.get("side") or "").lower() == "buy" else "sell"
            idx = row.get("positionIdx")
            out.append({
                "ticket": f"{symbol}:{idx}" if idx is not None else f"{symbol}:{side}",
                "symbol": symbol, "side": side, "positionIdx": idx,
                "volume": size,
                "openPrice": _num(row.get("avgPrice")),
                "currentPrice": _num(row.get("markPrice")),
                "stopLoss": _num(row.get("stopLoss")) or None,
                "takeProfit": _num(row.get("takeProfit")) or None,
                "profit": _num(row.get("unrealisedPnl")),
                "swap": 0.0, "magic": 0,
                "comment": f"bybit {self._category()} position",
                "openedAt": int(_num(row.get("createdTime"))),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        result = self._request("/v5/order/realtime", {"category": self._category()})
        rows = result.get("list") if isinstance(result, dict) else None
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            out.append({
                "ticket": str(row.get("orderId") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "type": str(row.get("orderType") or ""),
                "volumeCurrent": _num(row.get("qty")) - _num(row.get("cumExecQty")),
                "openPrice": _num(row.get("price")), "currentPrice": 0.0,
                "stopLoss": None, "takeProfit": None,
                "state": str(row.get("orderStatus") or ""),
                "magic": 0, "clientOrderId": str(row.get("orderLinkId") or ""),
                "comment": f"bybit {str(row.get('side') or '').lower()}".strip(),
                "placedAt": int(_num(row.get("createdTime"))),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("BYBIT_SYMBOL", "a symbol is required")
        result = self._request(
            "/v5/market/tickers", {"category": "spot", "symbol": pair}, signed=False)
        rows = result.get("list") if isinstance(result, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict) or not row.get("lastPrice"):
            raise AdapterUnavailable("BYBIT_NO_QUOTE", f"Bybit has no ticker for {pair}")
        last = _num(row.get("lastPrice"))
        return {
            "symbol": pair, "requestedSymbol": symbol,
            "bid": _num(row.get("bid1Price")) or last,
            "ask": _num(row.get("ask1Price")) or last,
            "last": last, "spreadPoints": None, "digits": 8, "point": 0.00000001,
            "stopsLevelPoints": 0, "serverTimeMs": int(time.time() * 1000),
            "provider": "bybit",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("BYBIT_SYMBOL", "a symbol is required")
        result = self._request(
            "/v5/market/instruments-info",
            {"category": self._category(), "symbol": pair}, signed=False,
        )
        rows = result.get("list") if isinstance(result, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("BYBIT_NO_SYMBOL", f"Bybit has no instrument {pair}")
        lot = row.get("lotSizeFilter") if isinstance(row.get("lotSizeFilter"), dict) else {}
        price = row.get("priceFilter") if isinstance(row.get("priceFilter"), dict) else {}
        tick = _num(price.get("tickSize")) or 0.00000001
        derivative = self._derivative()
        return {
            "symbol": str(row.get("symbol") or pair), "requestedSymbol": symbol,
            "description": f"{row.get('contractType') or self._category()} "
                           f"{row.get('quoteCoin') or ''}".strip(),
            "path": self._category(),
            "tradeMode": "full" if str(row.get("status") or "Trading") == "Trading" else "disabled",
            "digits": _decimals(price.get("tickSize")), "point": tick, "tickSize": tick,
            "tickValue": tick, "currencyBase": str(row.get("baseCoin") or "").upper(),
            "currencyProfit": str(row.get("quoteCoin") or "").upper(),
            "currencyMargin": str(row.get("settleCoin") or row.get("quoteCoin") or "").upper(),
            # Bybit orders are sized in the base coin, so one unit is one contract.
            "contractSize": 1.0,
            "volumeMin": _num(lot.get("minOrderQty")),
            "volumeMax": _num(lot.get("maxOrderQty")),
            "volumeStep": _num(lot.get("qtyStep") or lot.get("basePrecision")) or 1.0,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "crypto instruments trade continuously; Bybit reports no session table",
            "retrievedAt": time.time(), "provider": "bybit", "category": self._category(),
            "derivative": derivative,
        }

    # -- execution -------------------------------------------------------

    def _magic(self) -> int:
        try:
            return int(self.config.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    def _payload(self, intent: Any, volume: float) -> dict[str, Any]:
        derivative = self._derivative()
        payload: dict[str, Any] = {
            "category": self._category(),
            "symbol": str(intent.symbol).upper(),
            "side": "Buy" if intent.side == "buy" else "Sell",
            "orderType": "Market" if intent.order_type == "market" else "Limit",
            "qty": _fmt(volume),
            "orderLinkId": client_order_id(intent.magic, intent.digest),
        }
        if intent.order_type == "limit":
            payload["price"] = _fmt(intent.price)
        if derivative and (intent.stop_loss is not None or intent.take_profit is not None):
            payload["tpslMode"] = "Full"
        if intent.stop_loss is not None:
            payload["stopLoss"] = _fmt(intent.stop_loss)
        if intent.take_profit is not None:
            payload["takeProfit"] = _fmt(intent.take_profit)
        return payload

    def order_preview(self, intent: Any) -> dict[str, Any]:
        derivative = self._derivative()
        info = self.symbol_info(intent.symbol)
        account = self.account()
        quote = self.quote(intent.symbol)
        step = _num(info.get("volumeStep")) or 0.0
        volume, reasons = step_volume(
            intent.volume, step, _num(info.get("volumeMin")), _num(info.get("volumeMax"))
        )
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        price = float(intent.price) if intent.order_type == "limit" and intent.price else (
            ask if intent.side == "buy" else bid
        )
        if intent.order_type == "stop":
            reasons.append(
                "Bybit entries are Market or Limit; a stop entry is a conditional order "
                "this adapter does not submit"
            )
        if not derivative and (intent.stop_loss is not None or intent.take_profit is not None):
            reasons.append(
                "Bybit spot has no attached stop/target; submit the spot order without a "
                "stop and place the exit separately"
            )
        if derivative and price > 0:
            above = intent.side == "buy"  # a buy's target is above, its stop below
            if intent.stop_loss is not None and (
                intent.stop_loss >= price if above else intent.stop_loss <= price
            ):
                reasons.append(
                    f"a {intent.side} stop loss must be "
                    f"{'below' if above else 'above'} the entry price"
                )
            if intent.take_profit is not None and (
                intent.take_profit <= price if above else intent.take_profit >= price
            ):
                reasons.append(
                    f"a {intent.side} take profit must be "
                    f"{'above' if above else 'below'} the entry price"
                )
        preview = build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, open_positions=len(self.positions()), reasons=reasons,
            digits=int(info.get("digits") or 8), volume_step=step,
            volume_min=_num(info.get("volumeMin")), volume_max=_num(info.get("volumeMax")),
            extra={
                "venue": "bybit", "category": self._category(), "derivative": derivative,
                "clientOrderId": client_order_id(intent.magic, intent.digest),
                "stopSupported": derivative,
            },
        )
        if not derivative and (intent.stop_loss is not None or intent.take_profit is not None):
            preview["retcodeName"] = "STOP_UNSUPPORTED"
        return preview

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0,
                retcode_name=str(preview.get("retcodeName") or "PREVIEW_REJECTED"),
                reasons=reasons, account=preview.get("account") or {},
            )
        raw = self._request("/v5/order/create", method="POST",
                            body=self._payload(intent, _fnum(preview.get("volume"))),
                            check=False)
        envelope = raw if isinstance(raw, dict) else {}
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        code = int(envelope.get("retCode") or 0)
        order_id = str(result.get("orderId") or "")
        ok = code == 0 and bool(order_id)
        return build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=intent.symbol,
            side=intent.side, order_type=intent.order_type,
            volume=preview.get("volume") or intent.volume,
            price=preview.get("price") or 0.0,
            ticket=order_id or None, retcode=envelope.get("retCode"),
            retcode_name="ACCEPTED" if ok else f"BYBIT_{envelope.get('retCode')}",
            comment=str(result.get("orderLinkId") or ""),
            reasons=[] if ok else [
                str(envelope.get("retMsg") or "Bybit did not return an order id")
            ],
            account=preview.get("account") or {},
            clientOrderId=str(result.get("orderLinkId") or "")
            or client_order_id(intent.magic, intent.digest),
        )

    def _find_order(self, ticket: Any) -> Optional[dict[str, Any]]:
        return next(
            (row for row in self.orders() if str(row.get("ticket")) == str(ticket)), None
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Bybit has no working order {order_id} on this account")
        raw = self._request("/v5/order/cancel", method="POST", check=False, body={
            "category": self._category(), "symbol": row["symbol"], "orderId": order_id,
        })
        envelope = raw if isinstance(raw, dict) else {}
        code = int(envelope.get("retCode") or 0)
        ok = code == 0
        return {
            "ok": ok, "ticket": order_id, "symbol": row["symbol"],
            "retcode": envelope.get("retCode"),
            "retcodeName": "CANCELLED" if ok else f"BYBIT_{envelope.get('retCode')}",
            "reasons": [] if ok else [
                str(envelope.get("retMsg") or "Bybit did not cancel the order")
            ],
            "clientOrderId": row.get("clientOrderId") or "",
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        if rows and all(not str(row.get("clientOrderId") or "") for row in rows):
            return _refusal(None, "OWNERSHIP_UNAVAILABLE",
                            "Bybit reported no orderLinkId, so Eclipse cannot tell its "
                            "own orders from manual ones")
        results = [
            self.cancel_order(row.get("ticket")) for row in rows
            if owns_order(row.get("clientOrderId"), magic)
        ]
        cancelled = sum(1 for item in results if item.get("ok"))
        return {
            "ok": not any(not item.get("ok") for item in results),
            "cancelled": cancelled, "failed": len(results) - cancelled,
            "results": results,
            "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS", "reasons": [],
        }

    def _position(self, ticket: Any) -> Optional[dict[str, Any]]:
        wanted = str(ticket)
        return next((
            row for row in self.positions()
            if str(row.get("ticket")) == wanted
            or f"{row.get('symbol')}:{row.get('side')}" == wanted
        ), None)

    def _spot_balance(self, symbol: str) -> float:
        base = str(symbol or "").upper()
        for quote in _QUOTES:
            if base.endswith(quote) and len(base) > len(quote):
                base = base[: -len(quote)]
                break
        result = self._request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        rows = result.get("list") if isinstance(result, dict) else None
        wallet = rows[0] if isinstance(rows, list) and rows else {}
        for coin in (wallet.get("coin") if isinstance(wallet, dict) else None) or []:
            if str((coin or {}).get("coin") or "").upper() == base:
                return _num(coin.get("availableToWithdraw") or coin.get("walletBalance"))
        return 0.0

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        row = self._position(ticket)
        derivative = self._derivative()
        symbol = str(row.get("symbol") if row else ticket or "").strip().upper()
        held = _num(row.get("volume")) if row else 0.0
        if row is None and derivative:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bybit has no open position {ticket!r} on this account")
        if row is None:
            held = self._spot_balance(symbol)
        size = float(volume) if volume else held
        if size <= 0:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bybit reports no size to close for {ticket!r}")
        side = "Sell" if row is None or str(row.get("side")) == "buy" else "Buy"
        body: dict[str, Any] = {
            "category": self._category(), "symbol": symbol, "side": side,
            "orderType": "Market", "qty": _fmt(size),
            "orderLinkId": client_order_id(self._magic(), f"close{int(time.time())}"),
        }
        if derivative:
            body["reduceOnly"] = True
            if row and row.get("positionIdx") is not None:
                body["positionIdx"] = int(row["positionIdx"])
        raw = self._request("/v5/order/create", method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        code = int(envelope.get("retCode") or 0)
        ok = code == 0 and bool(result.get("orderId"))
        outcome = build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=symbol,
            side=side.lower(), order_type="market", volume=size, price=0.0,
            ticket=str(result.get("orderId") or "") or None,
            retcode=envelope.get("retCode"),
            retcode_name="ACCEPTED" if ok else f"BYBIT_{envelope.get('retCode')}",
            comment=str(result.get("orderLinkId") or ""),
            reasons=[] if ok else [
                str(envelope.get("retMsg") or "Bybit did not return an order id")
            ],
            clientOrderId=str(result.get("orderLinkId") or body["orderLinkId"]),
        )
        outcome["partial"] = bool(volume and size < held)
        return outcome

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is None and take_profit is None:
            return _refusal(ticket, "INVALID_REQUEST", "pass stopLoss and/or takeProfit")
        row = self._position(ticket)
        if row is None:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bybit has no open position {ticket!r}")
        body: dict[str, Any] = {
            "category": self._category(), "symbol": row["symbol"], "tpslMode": "Full",
        }
        if row.get("positionIdx") is not None:
            body["positionIdx"] = int(row["positionIdx"])
        if stop_loss is not None:
            body["stopLoss"] = _fmt(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = _fmt(take_profit)
        raw = self._request("/v5/position/trading-stop", method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        code = int(envelope.get("retCode") or 0)
        ok = code == 0
        return {"ok": ok, "ticket": ticket, "symbol": row["symbol"],
                "stopLoss": stop_loss, "takeProfit": take_profit,
                "retcode": envelope.get("retCode"),
                "retcodeName": "PROTECTED" if ok else f"BYBIT_{envelope.get('retCode')}",
                "verified": False,
                "reasons": [] if ok else [
                    str(envelope.get("retMsg") or "Bybit did not store the bracket")
                ]}

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        if price is None and stop_loss is None and take_profit is None:
            return _refusal(ticket, "INVALID_REQUEST",
                            "pass price, stopLoss and/or takeProfit")
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Bybit has no working order {order_id}")
        body: dict[str, Any] = {
            "category": self._category(), "symbol": row["symbol"], "orderId": order_id,
        }
        if price is not None:
            body["price"] = _fmt(price)
        if stop_loss is not None:
            body["stopLoss"] = _fmt(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = _fmt(take_profit)
        raw = self._request("/v5/order/amend", method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        code = int(envelope.get("retCode") or 0)
        ok = code == 0
        return {"ok": ok, "ticket": order_id, "symbol": row["symbol"], "price": price,
                "retcode": envelope.get("retCode"),
                "retcodeName": "AMENDED" if ok else f"BYBIT_{envelope.get('retCode')}",
                "verified": False,
                "reasons": [] if ok else [
                    str(envelope.get("retMsg") or "Bybit did not amend the order")
                ]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        result = self._request(
            "/v5/order/realtime",
            {"category": self._category(), "orderId": order_id}, check=False,
        )
        envelope = result if isinstance(result, dict) else {}
        rows = (envelope.get("result") or {}).get("list") if isinstance(
            envelope.get("result"), dict) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            return {"found": False, "ticket": order_id}
        return {
            "found": True, "ticket": str(row.get("orderId") or order_id),
            "symbol": str(row.get("symbol") or "").upper(),
            "state": row.get("orderStatus"),
            "stateName": str(row.get("orderStatus") or "").upper(),
            "filledVolume": _num(row.get("cumExecQty")),
            "avgPrice": _num(row.get("avgPrice")),
            "clientOrderId": str(row.get("orderLinkId") or ""),
        }


__all__ = ["BybitAdapter", "sign_request"]
