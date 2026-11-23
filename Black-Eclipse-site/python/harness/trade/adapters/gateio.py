"""Gate.io v4 adapter (spot balances + USDT-settled perpetuals): reads AND orders.

Gate.io v4 signs a five-part, newline-joined preimage with **HMAC-SHA512**, and
the body enters it as its own SHA-512 hex digest (the hex of the empty string for
a bodyless request):

    payload_hash = sha512_hex(body)
    signature    = hex HMAC-SHA512(secret,
                     method + "\\n" + path + "\\n" + query + "\\n" +
                     payload_hash + "\\n" + timestamp)

Both details are the ones that go wrong: the digest is SHA-**512** (not 256), and
the *hash* of the body is signed rather than the body. `sign_request` is pure so
the exact preimage is checked against an independent computation.

Execution (TradingAdapter). `market` selects the family (default `spot`; set it
to `futures` for the perpetual book):

  * spot: `POST /api/v4/spot/orders`, `DELETE /api/v4/spot/orders/{id}?currency_pair=`,
    `GET /api/v4/spot/orders/{id}`, `PATCH` for a re-price.
  * futures: `POST /api/v4/futures/{settle}/orders`, `DELETE/GET .../orders/{id}`,
    `PUT` for a re-price, and `POST .../price_orders` for a position stop/target.
  * Ownership travels in the order `text` = `t-eclipse-<magic>-<digest>` (`t-`
    is Gate's custom-text marker). Orders without it are never touched.

Declared limits:

  * **No attached stop on a spot or futures ENTRY.** Gate keeps stops in a
    separate price-trigger book, so an order carrying SL/TP is refused with
    `STOP_UNSUPPORTED` instead of dropping the stop. A position's bracket
    can be set afterwards with `modify_position` (futures only).
  * **Stop entries are refused** (`STOP_ENTRY_UNSUPPORTED`).
  * **A spot market BUY is sized in the quote currency by Gate**, so the adapter
    converts the requested base size with the quote price and reports
    `quoteSized: true` — it does not send a base amount the venue would misread.
  * **No websocket stream.**
  * The perpetual positions carry `leverage` as a string and a signed `size` whose
    sign encodes the side; the adapter reads both rather than guessing a default.
  * A close reads the venue's own position (or spot balance) first and refuses
    when it is absent — the size is never guessed.

Config: `baseUrl` (default https://api.gateio.ws), `settle` (default `usdt`),
`market` (default `spot`), `credentialRef` naming an env var/secret holding
`KEY:SECRET`.
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
from ..model import KIND_GATEIO
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


def _place_step(place: Any) -> tuple[float, int]:
    try:
        digits = int(float(place))
    except (TypeError, ValueError):
        digits = 8
    digits = max(0, min(digits, 12))
    return round(10.0 ** -digits, 12), digits


def payload_hash(body: str = "") -> str:
    """Gate's body digest: SHA-512 hex, of the empty string for a GET."""
    return hashlib.sha512(str(body or "").encode("utf-8")).hexdigest()


def sign_request(secret: str, timestamp: str, method: str, path: str,
                 query: str = "", body: str = "") -> str:
    """Gate.io v4 `SIGN`: hex HMAC-SHA512 over method/path/query/hash/ts."""
    preimage = "\n".join([
        method.upper(), path, str(query or ""), payload_hash(body), str(timestamp),
    ])
    return hmac.new(secret.encode("utf-8"), preimage.encode("utf-8"),
                    hashlib.sha512).hexdigest()


def _refusal(ticket: Any, code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "ticket": ticket, "retcode": None, "retcodeName": code,
            "reasons": [reason], **extra}


class GateioAdapter(TradingAdapter):
    kind = KIND_GATEIO

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="GATEIO_CREDENTIALS",
            label="Gate.io API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "GATEIO_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.gateio.ws"),
            code="GATEIO_CONFIG", label="Gate.io baseUrl",
        )

    def _settle(self) -> str:
        return str(self.config.get("settle") or "usdt").lower()

    def _market(self) -> str:
        return str(self.config.get("market") or "spot").lower()

    def _futures(self) -> bool:
        return self._market() != "spot"

    def _request(self, path: str, params: Optional[dict[str, Any]] = None, *,
                 signed: bool = True, method: str = "GET",
                 body: Optional[dict[str, Any]] = None, check: bool = True) -> Any:
        """A signed call. `check=False` returns Gate's error body instead of raising."""
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        )
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        text = "" if body is None else json.dumps(body, separators=(",", ":"))
        headers = {"Accept": "application/json"}
        if text:
            headers["Content-Type"] = "application/json"
        if signed:
            key, secret = self._credentials()
            timestamp = str(int(time.time()))
            headers.update({
                "KEY": key,
                "Timestamp": timestamp,
                "SIGN": sign_request(secret, timestamp, method, path, query, text),
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
                raise AdapterUnavailable("GATEIO_UNAUTHORIZED", "Gate.io rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("GATEIO_RATE_LIMITED", "Gate.io rate limit reached") from exc
            detail = ""
            try:
                detail = str((json.loads(exc.read() or b"{}") or {}).get("message") or "")
            except Exception:  # noqa: BLE001 - the body is advisory
                detail = ""
            raise AdapterUnavailable(
                "GATEIO_HTTP", f"Gate.io returned HTTP {exc.code}" + (f": {detail}" if detail else "")
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("GATEIO_UNREACHABLE", "Gate.io is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("GATEIO_TIMEOUT", "Gate.io timed out") from exc
        try:
            payload = json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("GATEIO_BAD_JSON", "Gate.io returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("label"):
            if not check:
                return payload
            raise AdapterUnavailable("GATEIO_API_ERROR",
                                     str(payload.get("message") or payload["label"])[:300])
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
                "terminal": {"api": self._base()}, "account": account, "warnings": []}

    def account(self) -> dict[str, Any]:
        rows = self._request("/api/v4/spot/accounts")
        total = available = 0.0
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            total += _num(row.get("total"))
            available += _num(row.get("available"))
        frozen = total - available
        return {
            "login": 0, "mode": account_mode(self._base()), "server": "gateio",
            "company": "Gate.io", "currency": "USD", "balance": total, "equity": total,
            "margin": frozen, "marginFree": available, "marginLevel": 0.0,
            "profit": 0.0, "leverage": 1.0, "marginMode": "spot",
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._request(f"/api/v4/futures/{self._settle()}/positions")
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            signed_size = _num(row.get("size"))
            if signed_size == 0:
                continue
            symbol = str(row.get("contract") or "").upper()
            side = "buy" if signed_size > 0 else "sell"
            out.append({
                "ticket": f"{symbol}:{side}",
                "symbol": symbol, "side": side,
                "volume": abs(signed_size),
                "openPrice": _num(row.get("entry_price")),
                "currentPrice": _num(row.get("mark_price")),
                "stopLoss": None,      # Gate keeps a separate price-triggered order list
                "takeProfit": None,
                "profit": _num(row.get("unrealised_pnl")),
                "swap": _num(row.get("funding_rate")), "magic": 0,
                "comment": f"gateio {self._settle()} futures "
                           f"(leverage {row.get('leverage') or '?'})",
                "openedAt": int(_num(row.get("open_time")) * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        if self._futures():
            rows = self._request(
                f"/api/v4/futures/{self._settle()}/orders", {"status": "open"}
            )
            buckets = [{"orders": rows}] if isinstance(rows, list) else []
        else:
            buckets = self._request("/api/v4/spot/open_orders")
        out: list[dict[str, Any]] = []
        for bucket in buckets if isinstance(buckets, list) else []:
            for row in (bucket or {}).get("orders") or [] if isinstance(bucket, dict) else []:
                if not isinstance(row, dict):
                    continue
                amount = _num(row.get("amount"))
                left = _num(row.get("left"))
                out.append({
                    "ticket": str(row.get("id") or ""),
                    "symbol": str(row.get("currency_pair") or row.get("contract") or "").upper(),
                    "type": str(row.get("type") or ""),
                    "volumeCurrent": left or amount,
                    "openPrice": _num(row.get("price")), "currentPrice": 0.0,
                    "stopLoss": None, "takeProfit": None,
                    "state": str(row.get("status") or "open"), "magic": 0,
                    "clientOrderId": str(row.get("text") or ""),
                    "comment": f"gateio {str(row.get('side') or '').lower()}".strip(),
                    "placedAt": int(_num(row.get("create_time_ms"))
                                    or _num(row.get("create_time")) * 1000),
                })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("GATEIO_SYMBOL", "a currency pair is required")
        if self._futures():
            rows = self._request(
                f"/api/v4/futures/{self._settle()}/tickers", {"contract": pair}, signed=False
            )
        else:
            rows = self._request("/api/v4/spot/tickers", {"currency_pair": pair}, signed=False)
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if row is None or not row.get("last"):
            raise AdapterUnavailable("GATEIO_NO_QUOTE", f"Gate.io has no ticker for {pair}")
        last = _num(row.get("last"))
        return {
            "symbol": pair, "requestedSymbol": symbol,
            "bid": _num(row.get("highest_bid")) or last,
            "ask": _num(row.get("lowest_ask")) or last,
            "last": last, "spreadPoints": None, "digits": 8, "point": 0.00000001,
            "stopsLevelPoints": 0, "serverTimeMs": int(time.time() * 1000),
            "provider": "gateio",
        }

    def _spot_symbol_info(self, pair: str, requested: str) -> dict[str, Any]:
        row = self._request(f"/api/v4/spot/currency_pairs/{urllib.parse.quote(pair)}",
                            signed=False)
        if not isinstance(row, dict) or not row.get("id"):
            raise AdapterUnavailable("GATEIO_NO_SYMBOL", f"Gate.io has no pair {pair}")
        tick, digits = _place_step(row.get("precision"))
        step, _ = _place_step(row.get("amount_precision"))
        return {
            "symbol": str(row.get("id") or pair), "requestedSymbol": requested,
            "description": f"spot {row.get('quote') or ''}".strip(),
            "path": "spot", "tradeMode": "full" if str(
                row.get("trade_status") or "tradable") == "tradable" else "disabled",
            "digits": digits, "point": tick, "tickSize": tick, "tickValue": tick,
            "currencyBase": str(row.get("base") or "").upper(),
            "currencyProfit": str(row.get("quote") or "").upper(),
            "currencyMargin": str(row.get("quote") or "").upper(),
            "contractSize": 1.0,
            "volumeMin": _num(row.get("min_base_amount")), "volumeMax": 0.0,
            "volumeStep": step, "stopsLevelPoints": 0, "freezeLevelPoints": 0,
            "spreadPoints": None, "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "crypto instruments trade continuously; Gate.io reports no sessions",
            "retrievedAt": time.time(), "provider": "gateio", "market": "spot",
        }

    def _futures_symbol_info(self, contract: str, requested: str) -> dict[str, Any]:
        row = self._request(
            f"/api/v4/futures/{self._settle()}/contracts/{urllib.parse.quote(contract)}",
            signed=False,
        )
        if not isinstance(row, dict) or not row.get("name"):
            raise AdapterUnavailable("GATEIO_NO_SYMBOL", f"Gate.io has no contract {contract}")
        tick = _num(row.get("order_price_round")) or 0.00000001
        contract_size = _num(row.get("quanto_multiplier"), 1.0) or 1.0
        return {
            "symbol": str(row.get("name") or contract), "requestedSymbol": requested,
            "description": f"futures {row.get('type') or ''}".strip(),
            "path": "futures",
            "tradeMode": "disabled" if row.get("in_delivery") else "full",
            "digits": _decimals(row.get("order_price_round")), "point": tick,
            "tickSize": tick, "tickValue": round(tick * contract_size, 12),
            "currencyBase": contract.split("_")[0],
            "currencyProfit": contract.split("_")[1] if "_" in contract else "",
            "currencyMargin": self._settle().upper(),
            "contractSize": contract_size,
            "volumeMin": _num(row.get("order_size_min")),
            "volumeMax": _num(row.get("order_size_max")), "volumeStep": 1.0,
            "stopsLevelPoints": 0, "freezeLevelPoints": 0, "spreadPoints": None,
            "sessionsAvailable": False, "sessionDeals": [],
            "sessionNote": "crypto contracts trade continuously; Gate.io reports no sessions",
            "retrievedAt": time.time(), "provider": "gateio", "market": "futures",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("GATEIO_SYMBOL", "a currency pair is required")
        first, second = (
            (self._futures_symbol_info, self._spot_symbol_info) if self._futures()
            else (self._spot_symbol_info, self._futures_symbol_info)
        )
        try:
            return first(pair, symbol)
        except AdapterUnavailable as exc:
            # A pair that exists only on the other book answers 404, not a label.
            if exc.code == "GATEIO_NO_SYMBOL" or "404" in str(exc.reason):
                return second(pair, symbol)
            raise

    # -- execution -------------------------------------------------------

    def _magic(self) -> int:
        try:
            return int(self.config.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    def _text(self, intent: Any) -> str:
        return "t-" + client_order_id(intent.magic, intent.digest)

    @staticmethod
    def _owns(text: Any, magic: int) -> bool:
        raw = str(text or "")
        return raw.startswith("t-") and owns_order(raw[2:], magic)

    def _payload(self, intent: Any, volume: float, price: float) -> dict[str, Any]:
        text = self._text(intent)
        if self._futures():
            return {
                "contract": str(intent.symbol).upper(),
                "size": f"{-volume:g}" if intent.side == "sell" else f"{volume:g}",
                "price": _fmt(intent.price) if intent.order_type == "limit" else "0",
                "tif": "gtc" if intent.order_type == "limit" else "ioc",
                "text": text,
            }
        payload: dict[str, Any] = {
            "currency_pair": str(intent.symbol).upper(),
            "type": "limit" if intent.order_type == "limit" else "market",
            "side": "buy" if intent.side == "buy" else "sell",
            "amount": _fmt(volume),
            "time_in_force": "gtc" if intent.order_type == "limit" else "ioc",
            "text": text,
        }
        if intent.order_type == "limit":
            payload["price"] = _fmt(intent.price)
        elif intent.side == "buy":
            # Gate reads a spot market-buy amount in the quote currency.
            payload["amount"] = _fmt(volume * price)
        return payload

    def order_preview(self, intent: Any) -> dict[str, Any]:
        futures = self._futures()
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
                "Gate.io entries are limit or market; a stop entry is a price-triggered "
                "order this adapter does not submit"
            )
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(
                "Gate.io keeps stops in a separate price-triggered book and cannot attach "
                "one to an entry order; submit the entry, then set the bracket with "
                "modify_position on a futures position"
            )
        if price <= 0:
            reasons.append("Gate.io returned no usable price for this pair")
        quote_sized = (
            not futures and intent.order_type == "market" and intent.side == "buy"
        )
        preview = build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=_num(info.get("contractSize"), 1.0) or 1.0,
            margin=None if not quote_sized else round(volume * price, 6),
            open_positions=len(self.positions()) if futures else 0, reasons=reasons,
            digits=int(info.get("digits") or 8), volume_step=step,
            volume_min=_num(info.get("volumeMin")), volume_max=_num(info.get("volumeMax")),
            extra={
                "venue": "gateio", "market": self._market(), "futures": futures,
                "clientOrderId": self._text(intent), "quoteSized": quote_sized,
                "stopSupported": False,
            },
        )
        if intent.stop_loss is not None or intent.take_profit is not None:
            preview["retcodeName"] = "STOP_UNSUPPORTED"
        return preview

    def _place_path(self) -> str:
        return (f"/api/v4/futures/{self._settle()}/orders" if self._futures()
                else "/api/v4/spot/orders")

    def _place(self, path: str, body: dict[str, Any], symbol: str, side: str,
               order_type: str, size: float, price: float) -> dict[str, Any]:
        raw = self._request(path, method="POST", body=body, check=False)
        row = raw if isinstance(raw, dict) else {}
        ok = bool(row.get("id")) and not row.get("label")
        return build_result(
            ok=ok, status="placed" if ok else "REJECTED", symbol=symbol, side=side,
            order_type=order_type, volume=size, price=price,
            ticket=str(row.get("id") or "") or None,
            retcode=row.get("label") or None,
            retcode_name="ACCEPTED" if ok else f"GATEIO_{row.get('label') or 'REJECTED'}",
            comment=str(row.get("text") or ""),
            reasons=[] if ok else [
                str(row.get("message") or row.get("label") or "Gate.io returned no order id")
            ],
            clientOrderId=str(row.get("text") or body.get("text") or ""),
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
        body = self._payload(intent, volume, _fnum(preview.get("price")))
        return self._place(self._place_path(), body, str(intent.symbol).upper(),
                           intent.side, intent.order_type, volume,
                           _fnum(preview.get("price")))

    def _find_order(self, ticket: Any) -> Optional[dict[str, Any]]:
        return next(
            (row for row in self.orders() if str(row.get("ticket")) == str(ticket)), None
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Gate.io has no working order {order_id} on this account")
        if self._futures():
            path = f"/api/v4/futures/{self._settle()}/orders/{urllib.parse.quote(order_id)}"
            params = None
        else:
            path = f"/api/v4/spot/orders/{urllib.parse.quote(order_id)}"
            params = {"currency_pair": row["symbol"]}
        raw = self._request(path, params, method="DELETE", check=False)
        payload = raw if isinstance(raw, dict) else {}
        ok = bool(payload.get("id")) and not payload.get("label")
        return {
            "ok": ok, "ticket": order_id, "symbol": row["symbol"],
            "retcode": payload.get("label") or None,
            "retcodeName": "CANCELLED" if ok else f"GATEIO_{payload.get('label') or 'REJECTED'}",
            "reasons": [] if ok else [
                str(payload.get("message") or "Gate.io did not cancel the order")
            ],
            "clientOrderId": row.get("clientOrderId") or "",
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        if rows and all(not str(row.get("clientOrderId") or "") for row in rows):
            return _refusal(None, "OWNERSHIP_UNAVAILABLE",
                            "Gate.io reported no order text, so Eclipse cannot tell its "
                            "own orders from manual ones")
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
        base = str(symbol or "").split("_")[0].upper()
        rows = self._request("/api/v4/spot/accounts", {"currency": base})
        for row in rows if isinstance(rows, list) else []:
            if str((row or {}).get("currency") or "").upper() == base:
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
                                f"Gate.io reports no open size for position {ticket}")
            sign = -1 if str(row.get("side")) == "buy" else 1
            body = {
                "contract": row["symbol"], "size": f"{sign * size:g}", "price": "0",
                "tif": "ioc", "text": "t-" + client_order_id(self._magic(), f"close{int(time.time())}"), "close": True,
            }
            outcome = self._place(f"/api/v4/futures/{self._settle()}/orders", body,
                                  row["symbol"], "sell" if sign < 0 else "buy",
                                  "market", size, 0.0)
            outcome["partial"] = bool(volume and size < held)
            return outcome
        symbol = str(ticket or "").strip().upper()
        if not symbol or self._futures():
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Gate.io has no open position {ticket!r} on this account")
        held = self._spot_balance(symbol)
        size = float(volume) if volume else held
        if size <= 0:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Gate.io holds no {symbol.split('_')[0]} balance to sell")
        body = {
            "currency_pair": symbol, "type": "market", "side": "sell",
            "amount": _fmt(size), "time_in_force": "ioc",
            "text": "t-" + client_order_id(self._magic(), f"close{int(time.time())}"),
        }
        outcome = self._place("/api/v4/spot/orders", body, symbol, "sell",
                              "market", size, 0.0)
        outcome["partial"] = bool(volume and size < held)
        return outcome

    def modify_position(self, ticket: Any, stop_loss: float | None = None,
                        take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is None and take_profit is None:
            return _refusal(ticket, "INVALID_REQUEST", "pass stopLoss and/or takeProfit")
        if not self._futures():
            return _refusal(
                ticket, "POSITION_BRACKET_UNSUPPORTED",
                "Gate.io spot has no position bracket; place a separate price order",
            )
        row = self._position(ticket)
        if row is None:
            return _refusal(ticket, "POSITION_NOT_FOUND",
                            f"Gate.io has no open position {ticket!r}")
        close_type = ("close-long-position" if str(row.get("side")) == "buy"
                      else "close-short-position")
        outcomes: list[dict[str, Any]] = []
        for rule, price in ((1, stop_loss), (2, take_profit)):
            if price is None:
                continue
            body = {
                "initial": {
                    "contract": row["symbol"], "size": "0", "price": "0", "tif": "ioc",
                    "text": "t-" + client_order_id(self._magic(), f"sl{rule}"),
                },
                "trigger": {"strategy_type": 0, "price_type": 0, "price": _fmt(price)},
                "order_type": close_type, "rule": rule,
            }
            raw = self._request(f"/api/v4/futures/{self._settle()}/price_orders",
                                method="POST", body=body, check=False)
            payload = raw if isinstance(raw, dict) else {}
            ok = bool(payload.get("id")) and not payload.get("label")
            outcomes.append({
                "ok": ok, "rule": rule, "id": payload.get("id"),
                "label": payload.get("label"), "message": payload.get("message") or "",
            })
        ok = bool(outcomes) and all(item["ok"] for item in outcomes)
        return {
            "ok": ok, "ticket": ticket, "symbol": row["symbol"],
            "stopLoss": stop_loss, "takeProfit": take_profit,
            "retcode": outcomes[0]["label"] if outcomes else None,
            "retcodeName": "PROTECTED" if ok else "GATEIO_PRICE_ORDER_FAILED",
            "verified": False,
            "reasons": [] if ok else [
                str(item["message"] or item["label"] or f"Gate.io refused rule {item['rule']}")
                for item in outcomes if not item["ok"]
            ],
            "legs": outcomes,
        }

    def modify_order(self, ticket: Any, price: float | None = None,
                     stop_loss: float | None = None,
                     take_profit: float | None = None) -> dict[str, Any]:
        if stop_loss is not None or take_profit is not None:
            return _refusal(
                ticket, "ORDER_BRACKET_UNSUPPORTED",
                "Gate.io amends a working order's price only; a stop is a separate "
                "price-triggered order",
            )
        if price is None:
            return _refusal(ticket, "INVALID_REQUEST", "pass a new price")
        order_id = str(ticket)
        row = self._find_order(order_id)
        if row is None:
            return _refusal(order_id, "ORDER_NOT_FOUND",
                            f"Gate.io has no working order {order_id}")
        if self._futures():
            path = f"/api/v4/futures/{self._settle()}/orders/{urllib.parse.quote(order_id)}"
            body: dict[str, Any] = {"price": _fmt(price)}
            method = "PUT"
        else:
            path = f"/api/v4/spot/orders/{urllib.parse.quote(order_id)}"
            body = {"currency_pair": row["symbol"], "price": _fmt(price)}
            method = "PATCH"
        raw = self._request(path, method=method, body=body, check=False)
        payload = raw if isinstance(raw, dict) else {}
        ok = bool(payload.get("id")) and not payload.get("label")
        return {"ok": ok, "ticket": order_id, "symbol": row["symbol"], "price": price,
                "retcode": payload.get("label") or None,
                "retcodeName": "AMENDED" if ok else f"GATEIO_{payload.get('label') or 'REJECTED'}",
                "verified": False,
                "reasons": [] if ok else [
                    str(payload.get("message") or "Gate.io did not amend the order")
                ]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        if self._futures():
            path = f"/api/v4/futures/{self._settle()}/orders/{urllib.parse.quote(order_id)}"
            params = None
        else:
            row = self._find_order(order_id)
            if row is None:
                return {"found": False, "ticket": order_id}
            path = f"/api/v4/spot/orders/{urllib.parse.quote(order_id)}"
            params = {"currency_pair": row["symbol"]}
        payload = self._request(path, params, check=False)
        if not isinstance(payload, dict) or not payload.get("id"):
            return {"found": False, "ticket": order_id}
        return {
            "found": True, "ticket": str(payload.get("id") or order_id),
            "symbol": str(payload.get("currency_pair") or payload.get("contract") or "").upper(),
            "state": payload.get("status"),
            "stateName": str(payload.get("status") or "").upper(),
            "filledVolume": _num(payload.get("filled_amount")
                                 or payload.get("amount")) - _num(payload.get("left")),
            "avgPrice": _num(payload.get("avg_deal_price") or payload.get("fill_price")),
            "clientOrderId": str(payload.get("text") or ""),
        }


__all__ = ["GateioAdapter", "payload_hash", "sign_request"]
