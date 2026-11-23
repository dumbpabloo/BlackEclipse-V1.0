"""OKX v5 adapter (spot / perpetuals): reads AND order execution.

OKX authenticates every private call with four headers:

    OK-ACCESS-KEY        api key
    OK-ACCESS-TIMESTAMP  ISO-8601 UTC, millisecond precision
    OK-ACCESS-SIGN       base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body))
    OK-ACCESS-PASSPHRASE the key's passphrase

`request_path` includes the query string, and the body is `""` for a GET. For a
POST the adapter signs the exact JSON text it transmits. `sign_request` is pure
so the signing input can be checked against an independently computed digest.

Execution: `POST /api/v5/trade/order` (market/limit, `cash` on spot or
`cross`/`isolated` on derivatives, with `tpTriggerPx`/`slTriggerPx` attached),
`cancel-order`, `amend-order`, `order-algo` (position bracket), and the
`/trade/order` + `orders-history` reads. Ownership travels in `clOrdId` =
`eclipse<magic><digest>`: OKX allows alphanumerics only, so the shared
hyphenated tag is compacted rather than dropped.

Declared limits:

  * **Spot has no attached stop.** A spot order carrying SL/TP is refused with
    `STOP_UNSUPPORTED`; a stop is never silently dropped.
  * **Stop entries are refused** (`STOP_ENTRY_UNSUPPORTED`): OKX expresses them
    as a separate trigger order this adapter does not submit.
  * **Quotes are one ticker snapshot**, not a websocket stream.
  * A close reads the venue's own position (or spot balance) first and refuses
    when it is absent — the size is never guessed.

Config (all non-secret): `baseUrl` (default https://www.okx.com),
`credentialRef` naming an env var/secret holding `API_KEY:SECRET:PASSPHRASE`,
and optional `instType` (default `SWAP`) / `tdMode` (default `cross` on a
derivative, `cash` on spot).
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
from ..model import KIND_OKX
from .credentials import resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    client_order_id,
    number as _fnum,
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
    """An exchange number string: no exponent, no trailing zeros."""
    return f"{_fnum(value):.10f}".rstrip("0").rstrip(".")


def _decimals(tick: Any) -> int:
    text = str(tick or "").strip()
    return len(text.split(".")[1]) if "." in text else 8


def _timestamp() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"


def sign_request(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """OKX's documented signature: base64(HMAC-SHA256(secret, ts + METHOD + path + body))."""
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _ack(raw: Any) -> tuple[bool, dict[str, Any], str, str]:
    """`(ok, item, code, message)` from an OKX trading envelope."""
    envelope = raw if isinstance(raw, dict) else {}
    items = envelope.get("data") if isinstance(envelope.get("data"), list) else []
    item = items[0] if items and isinstance(items[0], dict) else {}
    code = str(envelope.get("code") or "")
    sub = str(item.get("sCode") if item.get("sCode") is not None else "0")
    message = str(item.get("sMsg") or envelope.get("msg") or "")
    return (code == "0" and sub == "0"), item, (sub if sub != "0" else code), message


def _acked(raw: Any, key: str) -> tuple[bool, dict[str, Any], str, str]:
    """`_ack` plus "the venue returned the object it claims to have created"."""
    ok, item, code, message = _ack(raw)
    return (ok and bool(item.get(key))), item, code, message


def _refusal(ticket: Any, code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "ticket": ticket, "retcode": None, "retcodeName": code,
            "reasons": [reason], **extra}


class OkxAdapter(TradingAdapter):
    kind = KIND_OKX

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="OKX_CREDENTIALS",
            label="OKX API key/secret/passphrase",
        )
        if not raw:
            raise AdapterUnavailable(
                "OKX_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing "
                "`API_KEY:SECRET:PASSPHRASE`",
            )
        parts = str(raw).split(":", 2)
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise AdapterUnavailable(
                "OKX_CREDENTIALS", "OKX needs `API_KEY:SECRET:PASSPHRASE` (three parts)"
            )
        return tuple(part.strip() for part in parts)  # type: ignore[return-value]

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://www.okx.com"),
            code="OKX_CONFIG", label="OKX baseUrl",
        )

    def _inst_type(self, symbol: str = "") -> str:
        upper = str(symbol or "").upper()
        for suffix, inst_type in (("-SWAP", "SWAP"), ("-FUTURES", "FUTURES"),
                                  ("-OPTION", "OPTION")):
            if upper.endswith(suffix):
                return inst_type
        return str(self.config.get("instType") or "SWAP").upper()

    @staticmethod
    def _derivative(symbol: Any) -> bool:
        return str(symbol or "").upper().endswith(("-SWAP", "-FUTURES"))

    def _td_mode(self, derivative: bool) -> str:
        return str(self.config.get("tdMode") or ("cross" if derivative else "cash"))

    def _request(self, path: str, params: Optional[dict[str, Any]] = None, *,
                 signed: bool = True, method: str = "GET",
                 body: Optional[dict[str, Any]] = None, check: bool = True) -> Any:
        """A signed call. `check=False` returns OKX's envelope even on an error.

        Order placement must see `code`/`sCode` itself: a refusal is a result the
        operator can read, not a transport failure raised away.
        """
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                path += "?" + urllib.parse.urlencode(clean)
        text = "" if body is None else json.dumps(body, separators=(",", ":"))
        headers = {"Accept": "application/json"}
        if text:
            headers["Content-Type"] = "application/json"
        if signed:
            key, secret, passphrase = self._credentials()
            stamp = _timestamp()
            headers.update({
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sign_request(secret, stamp, method, path, text),
                "OK-ACCESS-TIMESTAMP": stamp,
                "OK-ACCESS-PASSPHRASE": passphrase,
            })
        request = urllib.request.Request(
            self._base() + path, headers=headers,
            data=text.encode("utf-8") if text else None, method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("OKX_UNAUTHORIZED", "OKX rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("OKX_RATE_LIMITED", "OKX rate limit reached") from exc
            raise AdapterUnavailable("OKX_HTTP", f"OKX returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("OKX_UNREACHABLE", "OKX is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("OKX_TIMEOUT", "OKX timed out") from exc
        try:
            payload = json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("OKX_BAD_JSON", "OKX returned invalid JSON") from exc
        if isinstance(payload, dict) and str(payload.get("code") or "0") != "0":
            if not check:
                return payload
            raise AdapterUnavailable(
                "OKX_API_ERROR",
                f"OKX error {payload.get('code')}: {payload.get('msg') or 'unknown'}",
            )
        if not check:
            return payload
        return payload.get("data") if isinstance(payload, dict) else payload

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
        rows = self._request("/api/v5/account/balance") or []
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("OKX_BAD_RESPONSE", "OKX returned no account")
        return {
            "login": 0, "mode": account_mode(self._base()), "server": "okx", "company": "OKX",
            "currency": "USD",
            "balance": _num(row.get("availBal") or row.get("availEq")),
            "equity": _num(row.get("totalEq")),
            "margin": _num(row.get("imr")),
            "marginFree": _num(row.get("availEq")),
            "marginLevel": 0.0, "profit": _num(row.get("upl")), "leverage": 1.0,
            "marginMode": "unified", "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._request(
            "/api/v5/account/positions",
            {"instType": str(self.config.get("instType") or "SWAP")},
        ) or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            size = _num(row.get("pos"))
            if size == 0:
                continue
            symbol = str(row.get("instId") or "").upper()
            pos_side = str(row.get("posSide") or ("long" if size >= 0 else "short"))
            out.append({
                "ticket": str(row.get("posId") or "") or f"{symbol}:{pos_side}",
                "symbol": symbol, "side": pos_side, "posSide": pos_side,
                "volume": abs(size),
                "openPrice": _num(row.get("avgPx")),
                "currentPrice": _num(row.get("markPx")),
                "stopLoss": None, "takeProfit": None, "profit": _num(row.get("upl")),
                "swap": 0.0, "magic": 0, "comment": "okx",
                "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._request(
            "/api/v5/trade/orders-pending",
            {"instType": str(self.config.get("instType") or "SWAP")},
        ) or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            out.append({
                "ticket": str(row.get("ordId") or ""),
                "symbol": str(row.get("instId") or "").upper(),
                "type": str(row.get("ordType") or ""),
                "volumeCurrent": _num(row.get("sz")),
                "openPrice": _num(row.get("px")),
                "currentPrice": _num(row.get("avgPx")),
                "stopLoss": None, "takeProfit": None,
                "state": str(row.get("state") or ""), "magic": 0,
                "clientOrderId": str(row.get("clOrdId") or ""),
                "comment": f"okx {row.get('side') or ''}".strip(),
                "placedAt": int(_num(row.get("cTime"))),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        inst = str(symbol or "").strip().upper()
        if not inst:
            raise AdapterUnavailable("OKX_SYMBOL", "an instrument id is required")
        rows = self._request("/api/v5/market/ticker", {"instId": inst}) or []
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("OKX_NO_QUOTE", f"OKX has no ticker for {inst}")
        bid, ask = _num(row.get("bidPx")), _num(row.get("askPx"))
        last = _num(row.get("last")) or ((bid + ask) / 2 if bid and ask else 0.0)
        return {
            "symbol": inst, "requestedSymbol": symbol, "bid": bid, "ask": ask, "last": last,
            "spreadPoints": None, "digits": 8, "point": 0.00000001, "stopsLevelPoints": 0,
            "serverTimeMs": int(_num(row.get("ts"), time.time() * 1000)), "provider": "okx",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        inst = str(symbol or "").strip().upper()
        if not inst:
            raise AdapterUnavailable("OKX_SYMBOL", "an instrument id is required")
        # An id without a derivative suffix is a spot pair, whatever the account
        # default is; asking SWAP instruments for BTC-USDT returns nothing.
        inst_type = self._inst_type(inst) if self._derivative(inst) else "SPOT"
        rows = self._request(
            "/api/v5/public/instruments", {"instType": inst_type, "instId": inst}
        ) or []
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("OKX_NO_SYMBOL", f"OKX has no instrument {inst}")
        tick = _num(row.get("tickSz")) or 0.00000001
        contract = _num(row.get("ctVal"), 1.0) or 1.0
        settle = str(row.get("settleCcy") or row.get("quoteCcy") or "USDT").upper()
        return {
            "symbol": str(row.get("instId") or inst), "requestedSymbol": symbol,
            "description": f"{inst_type} {settle}".strip(),
            "path": inst_type.lower(),
            "tradeMode": "full" if str(row.get("state") or "") == "live" else "disabled",
            "digits": _decimals(row.get("tickSz")), "point": tick, "tickSize": tick,
            "tickValue": round(tick * contract, 12),
            "currencyBase": str(row.get("baseCcy") or row.get("ctValCcy") or "").upper(),
            "currencyProfit": settle, "currencyMargin": settle, "contractSize": contract,
            "volumeMin": _num(row.get("minSz")),
            "volumeMax": _num(row.get("maxMktSz") or row.get("maxLmtSz")),
            "volumeStep": _num(row.get("lotSz")),
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "crypto instruments trade continuously; OKX reports no session table",
            "retrievedAt": time.time(), "provider": "okx", "instType": inst_type,
        }

    # -- execution -------------------------------------------------------

    def _magic(self) -> int:
        try:
            return int(self.config.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _client_id(intent: Any) -> str:
        """OKX accepts alphanumerics only, so the hyphenated tag is compacted."""
        return client_order_id(intent.magic, intent.digest).replace("-", "")[:32]

    @staticmethod
    def _owns(client_id: Any, magic: int) -> bool:
        return str(client_id or "").startswith(f"eclipse{int(magic or 0)}")

    def _payload(self, intent: Any, volume: float) -> dict[str, Any]:
        derivative = self._derivative(intent.symbol)
        payload: dict[str, Any] = {
            "instId": str(intent.symbol).upper(),
            "tdMode": self._td_mode(derivative),
            "side": "buy" if intent.side == "buy" else "sell",
            "ordType": "market" if intent.order_type == "market" else "limit",
            "sz": _fmt(volume),
            "clOrdId": self._client_id(intent),
        }
        if intent.order_type == "limit":
            payload["px"] = _fmt(intent.price)
        elif not derivative:
            # Without this OKX reads a spot market BUY's sz in the quote currency.
            payload["tgtCcy"] = "base_ccy"
        if derivative:
            if intent.take_profit is not None:
                payload["tpTriggerPx"] = _fmt(intent.take_profit)
                payload["tpOrdPx"] = "-1"
            if intent.stop_loss is not None:
                payload["slTriggerPx"] = _fmt(intent.stop_loss)
                payload["slOrdPx"] = "-1"
        return payload

    def order_preview(self, intent: Any) -> dict[str, Any]:
        derivative = self._derivative(intent.symbol)
        info = self.symbol_info(intent.symbol)
        account = self.account()
        quote = self.quote(intent.symbol)
        step = _num(info.get("volumeStep"))
        volume, reasons = step_volume(
            intent.volume, step, _num(info.get("volumeMin")), _num(info.get("volumeMax"))
        )
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        price = float(intent.price) if intent.order_type == "limit" and intent.price else (
            ask if intent.side == "buy" else bid
        )
        if intent.order_type == "stop":
            reasons.append(
                "OKX entries are market or limit; a stop entry is a separate trigger "
                "order and is not submitted by this adapter"
            )
        if not derivative and (intent.stop_loss is not None or intent.take_profit is not None):
            reasons.append(
                "OKX spot has no attached stop/target; submit the spot order without a "
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
            contract_size=_num(info.get("contractSize"), 1.0) or 1.0,
            open_positions=len(self.positions()), reasons=reasons,
            digits=int(info.get("digits") or 8), volume_step=step,
            volume_min=_num(info.get("volumeMin")), volume_max=_num(info.get("volumeMax")),
            extra={
                "venue": "okx", "derivative": derivative,
                "instType": str(info.get("instType") or self._inst_type(intent.symbol)),
                "tdMode": self._td_mode(derivative),
                "clientOrderId": self._client_id(intent),
                "stopSupported": derivative,
            },
        )
        if not derivative and (intent.stop_loss is not None or intent.take_profit is not None):
            preview["retcodeName"] = "STOP_UNSUPPORTED"
        return preview

    def _place(self, symbol: str, side: str, size: float, td_mode: str,
               pos_side: str = "", reduce_only: bool = False) -> dict[str, Any]:
        payload = {
            "instId": symbol, "tdMode": td_mode, "side": side, "ordType": "market",
            "sz": _fmt(size),
            "clOrdId": client_order_id(self._magic(), f"close{int(time.time())}").replace("-", "")[:32],
        }
        if pos_side:
            payload["posSide"] = pos_side
        elif reduce_only:
            payload["reduceOnly"] = "true"
        raw = self._request("/api/v5/trade/order", method="POST", body=payload, check=False)
        ok, item, code, message = _acked(raw, "ordId")
        return build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=symbol, side=side,
            order_type="market", volume=size, price=0.0,
            ticket=item.get("ordId") or None, retcode=code or None,
            retcode_name="ACCEPTED" if ok else f"OKX_{code}",
            comment=str(item.get("clOrdId") or payload["clOrdId"]),
            reasons=[] if ok else [message or f"OKX rejected the order (code {code})"],
            clientOrderId=str(item.get("clOrdId") or payload["clOrdId"]),
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
        raw = self._request("/api/v5/trade/order", method="POST",
                            body=self._payload(intent, _fnum(preview.get("volume"))),
                            check=False)
        ok, item, code, message = _acked(raw, "ordId")
        return build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=intent.symbol,
            side=intent.side, order_type=intent.order_type,
            volume=preview.get("volume") or intent.volume,
            price=preview.get("price") or 0.0,
            ticket=item.get("ordId") or None, retcode=code or None,
            retcode_name="ACCEPTED" if ok else f"OKX_{code}",
            comment=str(item.get("clOrdId") or ""),
            reasons=[] if ok else [message or f"OKX rejected the order (code {code})"],
            account=preview.get("account") or {},
            clientOrderId=str(item.get("clOrdId") or self._client_id(intent)),
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
                            f"OKX has no working order {order_id} on this account")
        raw = self._request("/api/v5/trade/cancel-order", method="POST",
                            body={"instId": row["symbol"], "ordId": order_id}, check=False)
        ok, item, code, message = _acked(raw, "ordId")
        return {"ok": ok, "ticket": order_id, "symbol": row["symbol"],
                "retcode": code or None,
                "retcodeName": "CANCELLED" if ok else f"OKX_{code}",
                "reasons": [] if ok else [
                    message or "OKX did not cancel the order"
                ], "clientOrderId": str(item.get("clOrdId") or row.get("clientOrderId") or "")}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        if rows and all(not str(row.get("clientOrderId") or "") for row in rows):
            return _refusal(None, "OWNERSHIP_UNAVAILABLE",
                            "OKX reported no clOrdId, so Eclipse cannot tell its own "
                            "orders from manual ones")
        results = [
            self.cancel_order(row.get("ticket")) for row in rows
            if self._owns(row.get("clientOrderId"), magic)
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
        base = str(symbol or "").split("-")[0].upper()
        rows = self._request("/api/v5/account/balance", {"ccy": base}) or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        for detail in (row.get("details") if isinstance(row, dict) else None) or []:
            if str((detail or {}).get("ccy") or "").upper() == base:
                return _num(detail.get("availBal") or detail.get("cashBal"))
        return 0.0

    def close_position(self, ticket: Any, deviation: int = 20,
                       volume: float | None = None) -> dict[str, Any]:
        row = self._position(ticket)
        if row is not None:
            held = _num(row.get("volume"))
            size = float(volume) if volume else held
            if size <= 0:
                return _refusal(ticket, "POSITION_NOT_FOUND",
                                f"OKX reports no open size for position {ticket}")
            side = "sell" if str(row.get("side")) in ("long", "buy") else "buy"
            pos_side = str(row.get("posSide") or "")
            result = self._place(
                row["symbol"], side, size, self._td_mode(True),
                pos_side=pos_side if pos_side in ("long", "short") else "",
                reduce_only=pos_side not in ("long", "short"),
            )
            result["partial"] = bool(volume and size < held)
            return result
        symbol = str(ticket or "").strip().upper()
        if not symbol or self._derivative(symbol):
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"OKX has no open position {ticket!r} on this account")
        held = self._spot_balance(symbol)
        size = float(volume) if volume else held
        if size <= 0:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"OKX holds no {symbol.split('-')[0]} balance to sell")
        result = self._place(symbol, "sell", size, "cash")
        result["partial"] = bool(volume and size < held)
        return result

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is None and take_profit is None:
            return _refusal(ticket, "INVALID_REQUEST", "pass stopLoss and/or takeProfit")
        row = self._position(ticket)
        if row is None:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"OKX has no open position {ticket!r}")
        payload: dict[str, Any] = {
            "instId": row["symbol"], "tdMode": self._td_mode(True),
            "side": "sell" if str(row.get("side")) in ("long", "buy") else "buy",
            "ordType": "conditional", "sz": _fmt(row.get("volume")), "reduceOnly": "true",
        }
        if stop_loss is not None:
            payload["slTriggerPx"], payload["slOrdPx"] = _fmt(stop_loss), "-1"
        if take_profit is not None:
            payload["tpTriggerPx"], payload["tpOrdPx"] = _fmt(take_profit), "-1"
        raw = self._request("/api/v5/trade/order-algo", method="POST", body=payload, check=False)
        ok, item, code, message = _acked(raw, "algoId")
        return {"ok": ok, "ticket": ticket, "symbol": row["symbol"],
                "stopLoss": stop_loss, "takeProfit": take_profit,
                "algoId": item.get("algoId"), "retcode": code or None,
                "retcodeName": "PROTECTED" if ok else f"OKX_{code}",
                "verified": False,
                "reasons": [] if ok else [
                    message or "OKX did not store the bracket"
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
                            f"OKX has no working order {order_id}")
        body: dict[str, Any] = {"instId": row["symbol"], "ordId": order_id}
        if price is not None:
            body["newPx"] = _fmt(price)
        if stop_loss is not None:
            body["newSlTriggerPx"] = _fmt(stop_loss)
        if take_profit is not None:
            body["newTpTriggerPx"] = _fmt(take_profit)
        raw = self._request("/api/v5/trade/amend-order", method="POST", body=body, check=False)
        ok, item, code, message = _acked(raw, "ordId")
        return {"ok": ok, "ticket": order_id, "symbol": row["symbol"], "price": price,
                "retcode": code or None,
                "retcodeName": "AMENDED" if ok else f"OKX_{code}",
                "verified": False,
                "reasons": [] if ok else [
                    message or "OKX did not amend the order"
                ]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        pending = self._find_order(order_id)
        if pending is not None:
            rows = self._request(
                "/api/v5/trade/order",
                {"instId": pending["symbol"], "ordId": order_id},
            ) or []
        else:
            # A filled/cancelled order is no longer pending; history is keyed by ordId.
            rows = self._request(
                "/api/v5/trade/orders-history",
                {"instType": str(self.config.get("instType") or "SWAP"), "ordId": order_id},
            ) or []
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            return {"found": False, "ticket": order_id}
        return {
            "found": True, "ticket": order_id,
            "symbol": str(row.get("instId") or "").upper(),
            "state": row.get("state"), "stateName": str(row.get("state") or "").upper(),
            "filledVolume": _num(row.get("accFillSz")),
            "avgPrice": _num(row.get("avgPx")),
            "clientOrderId": str(row.get("clOrdId") or ""),
        }


__all__ = ["OkxAdapter", "sign_request"]
