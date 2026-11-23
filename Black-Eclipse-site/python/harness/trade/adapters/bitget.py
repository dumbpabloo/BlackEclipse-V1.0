"""Bitget v2 adapter (spot balances + USDT-margined positions): reads AND orders.

Bitget signs `timestamp + method + requestPath + body` with **HMAC-SHA256 and
then base64-encodes** the digest (not hex), and needs the API **passphrase** as a
fourth header. `requestPath` includes the query string, and for a POST the body
is the exact JSON text transmitted. `sign_request` is pure so the base64-vs-hex
choice and the exact preimage are checked against an independent computation.

Execution (TradingAdapter):

  * Product type picks the family: `SPOT` uses `/api/v2/spot/trade/*`, every
    derivative (`USDT-FUTURES`, `COIN-FUTURES`, …) uses `/api/v2/mix/*`.
  * `place-order`, `cancel-order`, `modify-order`, and `place-tpsl-order` for a
    position bracket; `GET orderInfo` / `GET detail` for state.
  * Ownership travels in `clientOid` = `eclipse-<magic>-<digest>`.
  * A derivative order may attach `presetStopSurplusPrice` /
    `presetStopLossPrice`; spot refuses an attached stop with
    `STOP_UNSUPPORTED`.
  * **Stop entries are refused** (`STOP_ENTRY_UNSUPPORTED`): Bitget expresses
    them as a plan (trigger) order this adapter does not submit.

Declared limits:

  * **No websocket stream.**
  * Bitget answers HTTP 200 with a non-`00000` `code`, so that code is the refusal
    check — an empty `data` list with a bad code is a failure, not "no positions".
  * Mix positions are reported per side exactly as Bitget returns them.
  * A close reads the venue's own position (or spot balance) first and refuses
    when it is absent — the size is never guessed.

Config: `baseUrl` (default https://api.bitget.com), `productType` (default
`USDT-FUTURES`), `marginMode` (default `crossed`), `credentialRef` naming an env
var/secret holding `KEY:SECRET:PASSPHRASE`.
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
from ..model import KIND_BITGET
from .credentials import resolve_secret_value, validate_http_url
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


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: Any) -> str:
    return f"{_fnum(value):.10f}".rstrip("0").rstrip(".")


def _place_step(place: Any) -> tuple[float, int]:
    """`(tick/step, digits)` from a venue precision field (a decimal count)."""
    try:
        digits = int(float(place))
    except (TypeError, ValueError):
        digits = 8
    digits = max(0, min(digits, 12))
    return round(10.0 ** -digits, 12), digits


def sign_request(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Bitget's `ACCESS-SIGN`: base64(HMAC-SHA256(secret, ts + method + path + body))."""
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _refusal(ticket: Any, code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "ticket": ticket, "retcode": None, "retcodeName": code,
            "reasons": [reason], **extra}


class BitgetAdapter(TradingAdapter):
    kind = KIND_BITGET

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="BITGET_CREDENTIALS",
            label="Bitget key/secret/passphrase",
        )
        if not raw:
            raise AdapterUnavailable(
                "BITGET_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding "
                "`KEY:SECRET:PASSPHRASE`",
            )
        parts = raw.split(":")
        if len(parts) != 3 or not all(parts):
            raise AdapterUnavailable(
                "BITGET_CREDENTIALS",
                "the credential must be `KEY:SECRET:PASSPHRASE` (three parts)",
            )
        return parts[0], parts[1], parts[2]

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.bitget.com"),
            code="BITGET_CONFIG", label="Bitget baseUrl",
        )

    def _product_type(self) -> str:
        return str(self.config.get("productType") or "USDT-FUTURES").upper()

    def _spot(self) -> bool:
        return self._product_type() == "SPOT"

    def _margin_coin(self) -> str:
        product = self._product_type()
        return product.split("-")[0] if "-" in product else "USDT"

    def _request(self, path: str, params: Optional[dict[str, Any]] = None, *,
                 signed: bool = True, method: str = "GET",
                 body: Optional[dict[str, Any]] = None, check: bool = True) -> Any:
        """A signed call. `check=False` returns the envelope even on a bad `code`."""
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        ) if method.upper() == "GET" else ""
        request_path = path + (f"?{query}" if query else "")
        text = "" if body is None else json.dumps(body, separators=(",", ":"))
        headers = {"Accept": "application/json", "locale": "en-US"}
        if text:
            headers["Content-Type"] = "application/json"
        if signed:
            key, secret, passphrase = self._credentials()
            timestamp = str(int(time.time() * 1000))
            headers.update({
                "ACCESS-KEY": key,
                "ACCESS-SIGN": sign_request(secret, timestamp, method, request_path, text),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": passphrase,
            })
        request = urllib.request.Request(
            f"{self._base()}{request_path}", headers=headers,
            data=text.encode("utf-8") if text else None, method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("BITGET_UNAUTHORIZED", "Bitget rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("BITGET_RATE_LIMITED", "Bitget rate limit reached") from exc
            raise AdapterUnavailable("BITGET_HTTP", f"Bitget returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("BITGET_UNREACHABLE", "Bitget is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BITGET_TIMEOUT", "Bitget timed out") from exc
        try:
            payload = json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("BITGET_BAD_JSON", "Bitget returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BITGET_BAD_SHAPE", "Bitget returned no envelope")
        if str(payload.get("code") or "") != "00000":
            if not check:
                return payload
            raise AdapterUnavailable(
                "BITGET_API_ERROR",
                f"Bitget error {payload.get('code')}: {payload.get('msg') or 'unknown'}",
            )
        if not check:
            return payload
        return payload.get("data")

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
        data = self._request("/api/v2/spot/account/assets")
        rows = data if isinstance(data, list) else []
        total = 0.0
        available = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            total += _num(row.get("balance"))
            available += _num(row.get("available"))
        return {
            "login": 0, "mode": account_mode(self._base()), "server": "bitget",
            "company": "Bitget", "currency": "USD", "balance": total, "equity": total,
            "margin": total - available, "marginFree": available, "marginLevel": 0.0,
            "profit": 0.0, "leverage": 1.0, "marginMode": "spot",
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        data = self._request("/api/v2/mix/position/all-position",
                             {"productType": self._product_type()})
        rows = data if isinstance(data, list) else []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            size = _num(row.get("total"))
            if size == 0:
                continue
            symbol = str(row.get("symbol") or "").upper()
            hold = str(row.get("holdSide") or "").lower()
            out.append({
                "ticket": f"{symbol}:{hold}",
                "symbol": symbol,
                "side": "buy" if hold == "long" else "sell",
                "holdSide": hold,
                "volume": size,
                "openPrice": _num(row.get("openPriceAvg")),
                "currentPrice": _num(row.get("markPrice")),
                "stopLoss": _num(row.get("stopLoss")) or None,
                "takeProfit": None,
                "profit": _num(row.get("unrealizedPL")),
                "swap": _num(row.get("totalFee")), "magic": 0,
                "comment": f"bitget {self._product_type()} "
                           f"{str(row.get('marginMode') or '').lower()}".strip(),
                "openedAt": int(_num(row.get("cTime"))),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        if self._spot():
            data = self._request("/api/v2/spot/trade/unfilled-orders")
        else:
            data = self._request("/api/v2/mix/order/orders-pending",
                                 {"productType": self._product_type()})
        rows = data if isinstance(data, list) else []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append({
                "ticket": str(row.get("orderId") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "type": str(row.get("orderType") or ""),
                "volumeCurrent": _num(row.get("size")) - _num(row.get("baseVolume")),
                "openPrice": _num(row.get("priceAvg") or row.get("price")),
                "currentPrice": 0.0, "stopLoss": None, "takeProfit": None,
                "state": str(row.get("status") or ""), "magic": 0,
                "clientOrderId": str(row.get("clientOid") or ""),
                "comment": f"bitget {str(row.get('side') or '').lower()}".strip(),
                "placedAt": int(_num(row.get("cTime"))),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("BITGET_SYMBOL", "a symbol is required")
        if self._spot():
            data = self._request("/api/v2/spot/market/tickers", {"symbol": pair}, signed=False)
        else:
            data = self._request("/api/v2/mix/market/ticker",
                                 {"productType": self._product_type(), "symbol": pair},
                                 signed=False)
        rows = data if isinstance(data, list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        if row is None or not row.get("lastPr"):
            raise AdapterUnavailable("BITGET_NO_QUOTE", f"Bitget has no ticker for {pair}")
        last = _num(row.get("lastPr"))
        return {
            "symbol": pair, "requestedSymbol": symbol,
            "bid": _num(row.get("bidPr")) or last,
            "ask": _num(row.get("askPr")) or last,
            "last": last, "spreadPoints": None, "digits": 8, "point": 0.00000001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(_num(row.get("ts")) or time.time() * 1000),
            "provider": "bitget",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("BITGET_SYMBOL", "a symbol is required")
        if self._spot():
            data = self._request("/api/v2/spot/public/symbols", {"symbol": pair}, signed=False)
            row = data[0] if isinstance(data, list) and data else data
        else:
            data = self._request(
                "/api/v2/mix/market/contracts",
                {"productType": self._product_type(), "symbol": pair}, signed=False,
            )
            row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            raise AdapterUnavailable("BITGET_NO_SYMBOL", f"Bitget has no instrument {pair}")
        if self._spot():
            tick, digits = _place_step(row.get("pricePrecision"))
            step, _ = _place_step(row.get("quantityPrecision"))
            status = str(row.get("status") or row.get("symbolStatus") or "online")
            minimum = _num(row.get("minTradeAmount"))
            maximum = _num(row.get("maxTradeAmount"))
        else:
            tick, digits = _place_step(row.get("pricePlace"))
            step, _ = _place_step(row.get("volumePlace"))
            status = str(row.get("symbolStatus") or "normal")
            minimum = _num(row.get("minTradeNum"))
            maximum = _num(row.get("maxTradeNum"))
        live = status.lower() in ("online", "normal", "listed", "1")
        return {
            "symbol": str(row.get("symbol") or pair), "requestedSymbol": symbol,
            "description": f"{self._product_type()} {row.get('quoteCoin') or ''}".strip(),
            "path": self._product_type().lower(),
            "tradeMode": "full" if live else "disabled",
            "digits": digits, "point": tick, "tickSize": tick, "tickValue": tick,
            "currencyBase": str(row.get("baseCoin") or "").upper(),
            "currencyProfit": str(row.get("quoteCoin") or "").upper(),
            "currencyMargin": self._margin_coin(),
            # Bitget sizes orders in the base coin; one unit is one contract.
            "contractSize": 1.0,
            "volumeMin": minimum, "volumeMax": maximum, "volumeStep": step,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "crypto instruments trade continuously; Bitget reports no session table",
            "retrievedAt": time.time(), "provider": "bitget",
            "productType": self._product_type(),
        }

    # -- execution -------------------------------------------------------

    def _magic(self) -> int:
        try:
            return int(self.config.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    def _payload(self, intent: Any, volume: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": str(intent.symbol).upper(),
            "side": "buy" if intent.side == "buy" else "sell",
            "orderType": "market" if intent.order_type == "market" else "limit",
            "force": "gtc",
            "size": _fmt(volume),
            "price": _fmt(intent.price) if intent.order_type == "limit" else "0",
            "clientOid": client_order_id(intent.magic, intent.digest),
        }
        if not self._spot():
            payload.update({
                "productType": self._product_type(),
                "marginMode": str(self.config.get("marginMode") or "crossed"),
                "marginCoin": self._margin_coin(),
            })
            if intent.take_profit is not None:
                payload["presetStopSurplusPrice"] = _fmt(intent.take_profit)
            if intent.stop_loss is not None:
                payload["presetStopLossPrice"] = _fmt(intent.stop_loss)
        return payload

    def order_preview(self, intent: Any) -> dict[str, Any]:
        spot = self._spot()
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
                "Bitget entries are market or limit; a stop entry is a plan (trigger) "
                "order this adapter does not submit"
            )
        if spot and (intent.stop_loss is not None or intent.take_profit is not None):
            reasons.append(
                "Bitget spot has no attached stop/target; submit the spot order without "
                "a stop and place the exit separately"
            )
        if not spot and price > 0:
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
                "venue": "bitget", "productType": self._product_type(), "spot": spot,
                "clientOrderId": client_order_id(intent.magic, intent.digest),
                "stopSupported": not spot,
            },
        )
        if spot and (intent.stop_loss is not None or intent.take_profit is not None):
            preview["retcodeName"] = "STOP_UNSUPPORTED"
        return preview

    def _place_path(self) -> str:
        return "/api/v2/spot/trade/place-order" if self._spot() else "/api/v2/mix/order/place-order"

    def _place(self, path: str, body: dict[str, Any], symbol: str, side: str,
               order_type: str, size: float, price: float) -> dict[str, Any]:
        raw = self._request(path, method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        result = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        code = str(envelope.get("code") or "")
        ok = code == "00000" and bool(result.get("orderId"))
        return build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=symbol, side=side,
            order_type=order_type, volume=size, price=price,
            ticket=str(result.get("orderId") or "") or None, retcode=code or None,
            retcode_name="ACCEPTED" if ok else f"BITGET_{code}",
            comment=str(result.get("clientOid") or ""),
            reasons=[] if ok else [
                str(envelope.get("msg") or "Bitget did not return an order id")
            ],
            clientOrderId=str(result.get("clientOid") or body.get("clientOid") or ""),
        )

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
        volume = _fnum(preview.get("volume"))
        body = self._payload(intent, volume)
        return self._place(self._place_path(), body, str(intent.symbol).upper(),
                           intent.side, intent.order_type, volume,
                           _fnum(preview.get("price")))

    def _find_order(self, ticket: Any) -> Optional[dict[str, Any]]:
        return next(
            (row for row in self.orders() if str(row.get("ticket")) == str(ticket)), None
        )

    def _cancel_path(self) -> str:
        return "/api/v2/spot/trade/cancel-order" if self._spot() else "/api/v2/mix/order/cancel-order"

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Bitget has no working order {order_id} on this account")
        body: dict[str, Any] = {"symbol": row["symbol"], "orderId": order_id}
        if not self._spot():
            body["productType"] = self._product_type()
        raw = self._request(self._cancel_path(), method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        code = str(envelope.get("code") or "")
        ok = code == "00000"
        return {
            "ok": ok, "ticket": order_id, "symbol": row["symbol"],
            "retcode": code or None,
            "retcodeName": "CANCELLED" if ok else f"BITGET_{code}",
            "reasons": [] if ok else [
                str(envelope.get("msg") or "Bitget did not cancel the order")
            ],
            "clientOrderId": row.get("clientOrderId") or "",
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        if rows and all(not str(row.get("clientOrderId") or "") for row in rows):
            return _refusal(None, "OWNERSHIP_UNAVAILABLE",
                            "Bitget reported no clientOid, so Eclipse cannot tell its "
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
        assets = self._request("/api/v2/spot/account/assets", {"coin": base})
        for row in assets if isinstance(assets, list) else []:
            if str((row or {}).get("coin") or "").upper() == base:
                return _num(row.get("available"))
        return 0.0

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        row = self._position(ticket)
        if row is not None:
            held = _num(row.get("volume"))
            size = float(volume) if volume else held
            if size <= 0:
                return _refusal(ticket, "POSITION_NOT_FOUND",
                                f"Bitget reports no open size for position {ticket}")
            body = {
                "symbol": row["symbol"],
                "side": "sell" if str(row.get("side")) == "buy" else "buy",
                "orderType": "market", "force": "gtc", "size": _fmt(size), "price": "0",
                "clientOid": client_order_id(self._magic(), f"close{int(time.time())}"),
                "productType": self._product_type(),
                "marginMode": str(self.config.get("marginMode") or "crossed"),
                "marginCoin": self._margin_coin(),
                "reduceOnly": "YES",
            }
            outcome = self._place(self._place_path(), body, row["symbol"], body["side"],
                                  "market", size, 0.0)
            outcome["partial"] = bool(volume and size < held)
            return outcome
        symbol = str(ticket or "").strip().upper()
        if not symbol or (not self._spot() and ":" in symbol):
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bitget has no open position {ticket!r} on this account")
        held = self._spot_balance(symbol)
        size = float(volume) if volume else held
        if size <= 0:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bitget holds no {symbol} balance to sell")
        body = {
            "symbol": symbol, "side": "sell", "orderType": "market", "force": "gtc",
            "size": _fmt(size), "price": "0", "clientOid": client_order_id(self._magic(), f"close{int(time.time())}"),
        }
        outcome = self._place("/api/v2/spot/trade/place-order", body, symbol,
                              "sell", "market", size, 0.0)
        outcome["partial"] = bool(volume and size < held)
        return outcome

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is None and take_profit is None:
            return _refusal(ticket, "INVALID_REQUEST", "pass stopLoss and/or takeProfit")
        if self._spot():
            return _refusal(
                ticket, "POSITION_BRACKET_UNSUPPORTED",
                "Bitget spot has no position bracket; place a separate plan order",
            )
        row = self._position(ticket)
        if row is None:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Bitget has no open position {ticket!r}")
        outcomes: list[dict[str, Any]] = []
        for plan, price in (("pos_loss", stop_loss), ("pos_profit", take_profit)):
            if price is None:
                continue
            body = {
                "marginCoin": self._margin_coin(), "productType": self._product_type(),
                "symbol": row["symbol"], "planType": plan, "triggerPrice": _fmt(price),
                "triggerType": "mark_price", "holdSide": str(row.get("holdSide") or ""),
            }
            raw = self._request("/api/v2/mix/order/place-tpsl-order", method="POST",
                                body=body, check=False)
            envelope = raw if isinstance(raw, dict) else {}
            code = str(envelope.get("code") or "")
            outcomes.append({
                "ok": code == "00000", "code": code, "planType": plan,
                "message": str(envelope.get("msg") or ""),
            })
        ok = bool(outcomes) and all(item["ok"] for item in outcomes)
        return {
            "ok": ok, "ticket": ticket, "symbol": row["symbol"],
            "stopLoss": stop_loss, "takeProfit": take_profit,
            "retcode": outcomes[0]["code"] if outcomes else None,
            "retcodeName": "PROTECTED" if ok else "BITGET_TPSL_FAILED",
            "verified": False,
            "reasons": [] if ok else [
                item["message"] or f"Bitget refused {item['planType']}"
                for item in outcomes if not item["ok"]
            ],
            "legs": outcomes,
        }

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        if price is None:
            if stop_loss is not None or take_profit is not None:
                return _refusal(
                    ticket, "ORDER_BRACKET_UNSUPPORTED",
                    "Bitget amends a working order's price only; cancel and re-submit "
                    "to change an attached stop",
                )
            return _refusal(ticket, "INVALID_REQUEST", "pass a new price")
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Bitget has no working order {order_id}")
        body: dict[str, Any] = {
            "symbol": row["symbol"], "orderId": order_id, "newPrice": _fmt(price),
        }
        path = "/api/v2/spot/trade/modify-order" if self._spot() else "/api/v2/mix/order/modify-order"
        if not self._spot():
            body["productType"] = self._product_type()
        raw = self._request(path, method="POST", body=body, check=False)
        envelope = raw if isinstance(raw, dict) else {}
        code = str(envelope.get("code") or "")
        ok = code == "00000"
        return {"ok": ok, "ticket": order_id, "symbol": row["symbol"], "price": price,
                "retcode": code or None,
                "retcodeName": "AMENDED" if ok else f"BITGET_{code}",
                "verified": False,
                "reasons": [] if ok else [
                    str(envelope.get("msg") or "Bitget did not amend the order")
                ]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        if self._spot():
            data = self._request("/api/v2/spot/trade/orderInfo", {"orderId": order_id})
        else:
            data = self._request(
                "/api/v2/mix/order/detail",
                {"productType": self._product_type(), "orderId": order_id},
            )
        rows = data if isinstance(data, list) else [data]
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        if not isinstance(row, dict):
            return {"found": False, "ticket": order_id}
        return {
            "found": True, "ticket": str(row.get("orderId") or order_id),
            "symbol": str(row.get("symbol") or "").upper(),
            "state": row.get("status"),
            "stateName": str(row.get("status") or "").upper(),
            "filledVolume": _num(row.get("baseVolume") or row.get("accBaseVolume")),
            "avgPrice": _num(row.get("priceAvg")),
            "clientOrderId": str(row.get("clientOid") or ""),
        }


__all__ = ["BitgetAdapter", "sign_request"]
