"""Alpaca adapter (US equities/ETFs/crypto): reads AND order execution.

Alpaca has no position-id on its execution stream. Filled orders are therefore
normalized with FIFO into round-trip deals (`fifo.normalize_fifo`), so the journal
gets correct entry/exit and P&L instead of a guessed pairing. That P&L is
Eclipse-computed FIFO, not a broker-reported figure.

Execution: market/limit/stop orders, bracket (SL+TP) and OTO (one leg) orders,
cancel, replace, and close-by-symbol. A paper host is the demo account; every
other host is a real account.

Config (all non-secret):
  baseUrl       default https://paper-api.alpaca.markets
  dataUrl       default https://data.alpaca.markets
  accountId     optional label/display id
  credentialRef NAME of the secret (env var or secret://…) containing
                `KEY:SECRET` or {"key": "...", "secret": "..."}
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
from ..model import KIND_ALPACA
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .fifo import Execution, normalize_fifo
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
_MAX_ORDERS = 5000


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number and number not in (float("inf"), float("-inf")) else fallback


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _ms(iso: Any) -> int:
    text = str(iso or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return 0


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaAdapter(TradingAdapter):
    kind = KIND_ALPACA

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="ALPACA_CREDENTIALS",
            label="Alpaca API key",
        )
        if not raw:
            raise AdapterUnavailable(
                "ALPACA_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://paper-api.alpaca.markets"),
            code="ALPACA_CONFIG",
            label="Alpaca baseUrl",
        )

    def _data_base(self) -> str:
        return validate_http_url(
            str(self.config.get("dataUrl") or "https://data.alpaca.markets"),
            code="ALPACA_CONFIG",
            label="Alpaca dataUrl",
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        *,
        base: Optional[str] = None,
        allow_missing: bool = False,
    ) -> Any:
        key, secret = self._credentials()
        url = (base or self._base()) + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=data, method=method.upper())
        raw: bytes | None = None
        # Only a read is retried. A retried send can double-submit an order after
        # an ambiguous 5xx; the caller must see that ambiguity, not a duplicate.
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                    raw = response.read(4_000_000)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    retry = (exc.headers.get("Retry-After") if exc.headers else None) or ""
                    try:
                        delay = float(retry)
                    except ValueError:
                        delay = 2**attempt
                    time.sleep(min(max(delay, 0.5), 5.0))
                    continue
                if exc.code in (401, 403):
                    raise AdapterUnavailable(
                        "ALPACA_UNAUTHORIZED",
                        "Alpaca rejected the API key/secret",
                    ) from exc
                if exc.code == 404 and allow_missing:
                    return None
                detail = ""
                try:
                    detail = str((json.loads(exc.read() or b"{}") or {}).get("message") or "")
                except Exception:  # noqa: BLE001 - the body is advisory
                    detail = ""
                raise AdapterUnavailable(
                    "ALPACA_HTTP",
                    f"Alpaca returned HTTP {exc.code}" + (f": {detail}" if detail else ""),
                ) from exc
            except urllib.error.URLError as exc:
                raise AdapterUnavailable(
                    "ALPACA_UNREACHABLE",
                    f"Alpaca at {base or self._base()} is unreachable",
                ) from exc
            except TimeoutError as exc:
                raise AdapterUnavailable("ALPACA_TIMEOUT", "Alpaca timed out") from exc
        if raw is None:
            raise AdapterUnavailable("ALPACA_RETRY_EXHAUSTED", "Alpaca did not answer after retries")
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("ALPACA_BAD_JSON", "Alpaca returned invalid JSON") from exc

    def _get(
        self, path: str, params: Optional[dict[str, Any]] = None, *,
        base: Optional[str] = None, allow_missing: bool = False,
    ) -> Any:
        return self._request("GET", path, params, base=base, allow_missing=allow_missing)

    # ── BrokerAdapter ────────────────────────────────────────────────────────
    def probe(self) -> dict[str, Any]:
        try:
            account = self.account()
        except AdapterUnavailable as exc:
            return {
                "available": True,
                "connected": False,
                "code": exc.code,
                "reason": exc.reason,
                "terminal": {"api": self._base()},
                "account": None,
                "warnings": [],
            }
        return {
            "available": True,
            "connected": True,
            "code": None,
            "reason": None,
            "terminal": {"api": self._base()},
            "account": account,
            "warnings": [],
        }

    def account(self) -> dict[str, Any]:
        row = self._get("/v2/account")
        if not isinstance(row, dict):
            raise AdapterUnavailable("ALPACA_BAD_RESPONSE", "Alpaca returned no account")
        last_equity = _num(row.get("last_equity"), _num(row.get("equity")))
        return {
            "login": _int(row.get("account_number"), 0),
            "armRef": str(row.get("account_number") or "") or None,
            "mode": account_mode(self._base()),
            "server": "alpaca",
            "company": "Alpaca",
            "currency": str(row.get("currency") or "USD"),
            "balance": _num(row.get("cash")),
            "equity": _num(row.get("equity")),
            "margin": _num(row.get("initial_margin")),
            "marginFree": _num(row.get("buying_power")),
            "marginLevel": 0.0,
            "profit": _num(row.get("equity")) - last_equity,
            "leverage": _num(row.get("multiplier")),
            "marginMode": "unknown",
            "tradeAllowed": not bool(row.get("trading_blocked")),
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._get("/v2/positions") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            qty = _num(row.get("qty"))
            side = "buy" if qty >= 0 else "sell"
            out.append(
                {
                    "ticket": str(row.get("symbol") or "").upper(),
                    "symbol": str(row.get("symbol") or "").upper(),
                    "side": side,
                    "volume": abs(qty),
                    "openPrice": _num(row.get("avg_entry_price")),
                    "currentPrice": _num(row.get("current_price")),
                    "stopLoss": None,
                    "takeProfit": None,
                    "profit": _num(row.get("unrealized_pl")),
                    "swap": 0.0,
                    "magic": 0,
                    "comment": "alpaca (openedAt is first seen)",
                    # Alpaca's position payload has no open time. The opening
                    # order is in the deal window, so the journal normally uses
                    # that; when it is absent, "first seen" is honest and never
                    # silently dated 1970.
                    "openedAt": int(time.time() * 1000),
                }
            )
        return out

    def orders(self) -> list[dict[str, Any]]:
        rows = self._get("/v2/orders", {"status": "open", "limit": 500, "direction": "asc"}) or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            out.append(
                {
                    "ticket": str(row.get("id") or ""),
                    "symbol": str(row.get("symbol") or "").upper(),
                    "type": str(row.get("type") or ""),
                    "volumeCurrent": _num(row.get("qty")) - _num(row.get("filled_qty")),
                    "openPrice": _num(row.get("limit_price")) or _num(row.get("stop_price")),
                    "currentPrice": 0.0,
                    "stopLoss": _num(row.get("stop_loss")) or None,
                    "takeProfit": _num(row.get("take_profit")) or None,
                    "state": str(row.get("status") or ""),
                    "magic": 0,
                    "comment": "alpaca",
                    "placedAt": _ms(row.get("submitted_at")),
                }
            )
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        requested = str(symbol or "").strip()
        row = self._get(
            f"/v2/stocks/{urllib.parse.quote(requested.upper())}/quotes/latest",
            {"feed": "iex"},
            base=self._data_base(),
        )
        if isinstance(row, dict) and isinstance(row.get("quote"), dict):
            q = row["quote"]
        else:
            # Crypto pairs (BTC/USD) use the beta crypto endpoint.
            row = self._get(
                "/v1beta3/crypto/us/latest/quotes",
                {"symbols": requested.upper()},
                base=self._data_base(),
            )
            quotes = row.get("quotes") if isinstance(row, dict) else None
            q = quotes.get(requested.upper()) if isinstance(quotes, dict) else None
        if not isinstance(q, dict):
            raise AdapterUnavailable("ALPACA_NO_QUOTE", f"Alpaca has no quote for {requested}")
        bid = _num(q.get("bp"))
        ask = _num(q.get("ap"))
        return {
            "symbol": requested.upper(),
            "requestedSymbol": requested,
            "bid": bid,
            "ask": ask,
            "last": (bid + ask) / 2 if bid and ask else bid or ask,
            "spreadPoints": None,
            "digits": 4,
            "point": 0.0001,
            "stopsLevelPoints": 0,
            "serverTimeMs": _ms(q.get("t")) or int(time.time() * 1000),
            "provider": "alpaca",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        row = self._get(f"/v2/assets/{urllib.parse.quote(str(symbol or '').upper())}",
                        allow_missing=True)
        if not isinstance(row, dict):
            raise AdapterUnavailable("ALPACA_NO_SYMBOL", f"Alpaca has no asset {symbol!r}")
        tradable = bool(row.get("tradable")) and str(row.get("status") or "") == "active"
        fractional = bool(row.get("fractionable"))
        return {
            "symbol": str(row.get("symbol") or symbol).upper(),
            "requestedSymbol": symbol,
            "description": str(row.get("name") or ""),
            "path": str(row.get("class") or ""),
            "tradeMode": "full" if tradable else "disabled",
            "digits": 2,
            "point": 0.01,
            "tickSize": 0.01,
            "tickValue": 0.01,
            "currencyBase": "USD",
            "currencyProfit": "USD",
            "currencyMargin": "USD",
            "contractSize": 1.0,
            "volumeMin": 0.000001 if fractional else 1.0,
            "volumeMax": 0.0,
            "volumeStep": 0.000001 if fractional else 1.0,
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": False,
            "sessionDeals": [],
            "sessionNote": "US market hours are not reported by the assets endpoint",
            "retrievedAt": time.time(),
            "provider": "alpaca",
        }

    # ── execution ────────────────────────────────────────────────────────────
    def _fractionable(self, symbol: str) -> bool:
        try:
            return float(self.symbol_info(symbol).get("volumeStep") or 1) < 1
        except AdapterUnavailable:
            return False

    def _bracket_reasons(self, intent: Any, price: float) -> list[str]:
        reasons: list[str] = []
        if intent.stop_loss is None and intent.take_profit is None:
            return reasons
        if str(intent.order_type) == "stop":
            reasons.append("Alpaca cannot attach a bracket to a stop entry order")
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
        account = self.account()
        quote = self.quote(intent.symbol)
        positions = self.positions()
        fractional = self._fractionable(intent.symbol)
        step = 0.000001 if fractional else 1.0
        volume, reasons = step_volume(intent.volume, step, step)
        bid = _fnum(quote.get("bid"))
        ask = _fnum(quote.get("ask"))
        price = float(intent.price) if intent.order_type in ("limit", "stop") and intent.price else (
            ask if intent.side == "buy" else bid
        )
        reasons += self._bracket_reasons(intent, price)
        return build_preview(
            intent,
            account=account,
            quote=quote,
            volume=volume,
            price=price,
            contract_size=1.0,
            margin=max(0.0, volume * price),
            open_positions=len(positions),
            reasons=reasons,
            digits=2,
            volume_step=step,
            volume_min=step,
            extra={"venue": "alpaca", "fractionable": fractional},
        )

    def _payload(self, intent: Any, volume: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": str(intent.symbol).upper(),
            "qty": f"{volume:.10f}".rstrip("0").rstrip("."),
            "side": str(intent.side).lower(),
            "type": str(intent.order_type),
            "time_in_force": "day" if intent.time_in_force == "day" else "gtc",
            "client_order_id": client_order_id(intent.magic, intent.digest),
        }
        if intent.order_type == "limit":
            payload["limit_price"] = f"{float(intent.price):.6f}"
        elif intent.order_type == "stop":
            payload["stop_price"] = f"{float(intent.price):.6f}"
        if intent.stop_loss is not None or intent.take_profit is not None:
            payload["order_class"] = (
                "bracket" if intent.stop_loss is not None and intent.take_profit is not None
                else "oto"
            )
            if intent.stop_loss is not None:
                payload["stop_loss"] = {"stop_price": f"{float(intent.stop_loss):.6f}"}
            if intent.take_profit is not None:
                payload["take_profit"] = {"limit_price": f"{float(intent.take_profit):.6f}"}
        return payload

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0, retcode=None,
                retcode_name="PREVIEW_REJECTED", reasons=reasons,
                account=preview.get("account") or {},
            )
        payload = self._payload(intent, float(preview["volume"]))
        row = self._request("POST", "/v2/orders", body=payload)
        if not isinstance(row, dict) or not row.get("id"):
            raise AdapterUnavailable("ALPACA_BAD_RESPONSE", "Alpaca returned no order")
        status = str(row.get("status") or "").lower()
        accepted = status in ("new", "accepted", "pending_new", "partially_filled", "filled")
        return build_result(
            ok=accepted,
            status="FILLED" if status == "filled" else ("placed" if accepted else "REJECTED"),
            symbol=row.get("symbol") or intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=preview["volume"],
            fill_volume=_num(row.get("filled_qty")) or None,
            price=_num(row.get("filled_avg_price")) or preview["price"],
            ticket=str(row.get("id")),
            retcode=status or None,
            retcode_name=status.upper() or "ACCEPTED",
            comment=str(row.get("client_order_id") or ""),
            reasons=[] if accepted else [f"Alpaca reported status {status or 'unknown'}"],
            account=preview.get("account") or {},
            clientOrderId=row.get("client_order_id"),
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            self._request("DELETE", f"/v2/orders/{urllib.parse.quote(order_id)}")
        except AdapterUnavailable as exc:
            if exc.code == "ALPACA_HTTP" and "404" in exc.reason:
                return {"ok": False, "ticket": order_id, "retcode": 404,
                        "retcodeName": "ORDER_NOT_FOUND",
                        "reasons": [f"Alpaca has no order {order_id}"]}
            raise
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self._get("/v2/orders", {"status": "open", "limit": 500}) or []
        results: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not owns_order(row.get("client_order_id"), magic):
                continue
            order_id = str(row.get("id") or "")
            try:
                results.append(self.cancel_order(order_id))
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
        symbol = str(ticket or "").strip().upper()
        if not symbol:
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_SYMBOL",
                    "reasons": ["Alpaca closes by symbol; pass the position symbol"]}
        params = {"qty": f"{float(volume):.10f}".rstrip("0").rstrip(".")} if volume else None
        row = self._request(
            "DELETE", f"/v2/positions/{urllib.parse.quote(symbol)}", params,
            allow_missing=True,
        )
        if row is None:
            return {"ok": False, "ticket": symbol, "retcode": 404,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Alpaca has no open {symbol} position"]}
        return {
            "ok": True,
            "ticket": symbol,
            "symbol": symbol,
            "side": str(row.get("side") or "").lower() or ("buy" if _num(row.get("qty")) > 0 else "sell"),
            "volume": abs(_num(row.get("qty"))),
            "price": _num(row.get("avg_entry_price")),
            "retcode": 200,
            "retcodeName": "CLOSED",
            "partial": bool(volume),
            "verified": True,
            "reasons": [],
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "ticket": ticket,
            "retcode": None,
            "retcodeName": "POSITION_BRACKET_UNSUPPORTED",
            "reasons": [
                "Alpaca attaches stops to the order, not to an open position; "
                "submit the exit with a bracket order instead of editing the position"
            ],
        }

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        if stop_loss is not None or take_profit is not None:
            return {
                "ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "ORDER_BRACKET_UNSUPPORTED",
                "reasons": [
                    "Alpaca replaces a price but does not edit a bracket leg; "
                    "cancel the order and submit a new bracket"
                ],
            }
        if price is None:
            return {"ok": False, "ticket": ticket, "retcode": None,
                    "retcodeName": "INVALID_REQUEST",
                    "reasons": ["pass a new limit price"]}
        order_id = str(ticket)
        row = self._request(
            "PATCH", f"/v2/orders/{urllib.parse.quote(order_id)}",
            body={"limit_price": f"{float(price):.6f}"},
        )
        ok = isinstance(row, dict) and bool(row.get("id"))
        return {
            "ok": ok,
            "ticket": order_id,
            "symbol": str((row or {}).get("symbol") or "").upper(),
            "price": _num((row or {}).get("limit_price")) or float(price),
            "retcode": 200 if ok else None,
            "retcodeName": "REPLACED" if ok else "REPLACE_FAILED",
            "reasons": [] if ok else ["Alpaca did not return the replaced order"],
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        row = self._get(f"/v2/orders/{urllib.parse.quote(str(ticket))}")
        if not isinstance(row, dict) or not row.get("id"):
            return {"found": False, "ticket": ticket}
        return {
            "found": True,
            "ticket": str(row.get("id")),
            "state": row.get("status"),
            "stateName": str(row.get("status") or "").upper(),
            "filledVolume": _num(row.get("filled_qty")),
            "avgPrice": _num(row.get("filled_avg_price")),
        }

    # ── reconciliation ───────────────────────────────────────────────────────
    def _executions(self, after_ms: int, until_ms: int) -> list[Execution]:
        executions: list[Execution] = []
        after = _iso(after_ms)
        until = _iso(until_ms)
        seen = 0
        for _ in range(10):
            rows = self._get(
                "/v2/orders",
                {
                    "status": "closed",
                    "after": after,
                    "until": until,
                    "limit": 500,
                    "direction": "asc",
                    "nested": "false",
                },
            )
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                filled = _num(row.get("filled_qty"))
                price = _num(row.get("filled_avg_price"))
                when = _ms(row.get("filled_at") or row.get("submitted_at"))
                if filled <= 0 or price <= 0 or when <= 0:
                    continue
                executions.append(
                    Execution(
                        id=str(row.get("id") or f"{when}-{len(executions)}"),
                        symbol=str(row.get("symbol") or ""),
                        side="buy" if str(row.get("side") or "").lower() == "buy" else "sell",
                        qty=filled,
                        price=price,
                        time_ms=when,
                        fee=0.0,
                    )
                )
                seen += 1
            if len(rows) < 500 or seen >= _MAX_ORDERS:
                break
            after = _iso(_ms(rows[-1].get("filled_at") or rows[-1].get("submitted_at")) + 1)
        return executions

    def deals_since(self, since_ms: int) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        from_ms = int(since_ms or 0) or (now - 7 * 86_400_000)
        return normalize_fifo(self._executions(from_ms, now))

    def deals_window(self, days: int) -> list[dict[str, Any]]:
        safe = max(1, min(int(days or 7), 3650))
        now = int(time.time() * 1000)
        return normalize_fifo(self._executions(now - safe * 86_400_000, now))


__all__ = ["AlpacaAdapter"]
