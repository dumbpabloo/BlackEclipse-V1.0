"""Coincheck adapter (JPY spot): reads AND order execution.

Coincheck signs `nonce + url` — the **full request URL including scheme, path
and query** — with HMAC-SHA256 (hex) and sends `ACCESS-KEY`, `ACCESS-NONCE`,
`ACCESS-SIGN` as headers:

    sign = hex HMAC-SHA256(secret, nonce + "https://coincheck.com/api/accounts/balance")

`sign_request` is pure so the exact string (including the base and the query) is
checked against an independent computation; the usual mistake is signing the path
alone, which validates locally and fails remotely. A POST body is **not** part of
the preimage, so `_request` signs the URL and then sends the form-encoded body.

Execution uses `/api/exchange/orders` (POST), `DELETE /api/exchange/orders/{id}`
and `GET /api/exchange/orders/{id}`. A market buy is a JPY-denominated
`market_buy_amount`; every other order is an asset `amount`.

Declared limits:

  * **No websocket stream.**
  * Balances are per currency; `positions()` reports non-zero holdings and does not
    mark them to market (Coincheck's private surface carries no mark price).
  * Coincheck publishes no lot step, so `symbol_info()` raises
    `SYMBOL_INFO_NOT_SUPPORTED` instead of inventing one.
  * A take-profit has no field; an order-level SL is `stop_loss_rate` on a limit
    order (a percentage) and is refused elsewhere with `STOP_UNSUPPORTED`.
  * Open orders expose no client id, so `cancel_all` refuses with
    `OWNERSHIP_UNAVAILABLE` rather than cancelling a human's orders.

Config: `baseUrl` (default https://coincheck.com), `credentialRef` naming an env
var/secret holding `KEY:SECRET`.
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
from ..model import KIND_COINCHECK
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
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
    return f"{_num(value):.10f}".rstrip("0").rstrip(".") or "0"


def _pair(raw: Any) -> str:
    """`BTC`, `btc/jpy` or `btc_jpy` -> Coincheck's `btc_jpy` pair."""
    text = str(raw or "").strip().lower()
    if "_" in text:
        return text
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if cleaned.endswith("jpy") and len(cleaned) > 3:
        return f"{cleaned[:-3]}_jpy"
    return f"{cleaned}_jpy" if cleaned else ""


def sign_request(secret: str, nonce: str, url: str) -> str:
    """Coincheck: hex HMAC-SHA256 over `nonce + full url`."""
    return hmac.new(secret.encode("utf-8"), f"{nonce}{url}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


class CoincheckAdapter(TradingAdapter):
    kind = KIND_COINCHECK

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="COINCHECK_CREDENTIALS",
            label="Coincheck API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "COINCHECK_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://coincheck.com"),
            code="COINCHECK_CONFIG",
            label="Coincheck baseUrl",
        )

    def _nonce(self) -> str:
        candidate = int(time.time() * 1000)
        last = int(getattr(self, "_last_nonce", 0) or 0)
        if candidate <= last:
            candidate = last + 1
        self._last_nonce = candidate
        return str(candidate)

    def _request(
        self, path: str, params: Optional[dict[str, Any]] = None, *,
        signed: bool = True, method: str = "GET", body: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Send one signed call. The signature covers the URL only — never the body."""
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base()}{path}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json"}
        if signed:
            key, secret = self._credentials()
            nonce = self._nonce()
            headers.update({
                "ACCESS-KEY": key,
                "ACCESS-NONCE": nonce,
                "ACCESS-SIGN": sign_request(secret, nonce, url),
            })
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, headers=headers, data=data, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("COINCHECK_UNAUTHORIZED", "Coincheck rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("COINCHECK_RATE_LIMITED", "Coincheck rate limit reached") from exc
            raise AdapterUnavailable("COINCHECK_HTTP", f"Coincheck returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("COINCHECK_UNREACHABLE", "Coincheck is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("COINCHECK_TIMEOUT", "Coincheck timed out") from exc
        try:
            parsed = json.loads(payload or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("COINCHECK_BAD_JSON", "Coincheck returned invalid JSON") from exc
        if isinstance(parsed, dict) and parsed.get("success") is False:
            raise AdapterUnavailable("COINCHECK_API_ERROR",
                                     str(parsed.get("error") or "Coincheck refused the request")[:300])
        return parsed

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

    def _balances(self) -> dict[str, float]:
        payload = self._request("/api/accounts/balance")
        if not isinstance(payload, dict):
            raise AdapterUnavailable("COINCHECK_BAD_RESPONSE", "Coincheck returned no balances")
        out: dict[str, float] = {}
        for name, value in payload.items():
            if name in ("success",):
                continue
            amount = _num(value, fallback=float("nan"))
            if amount != amount:      # not a number: not a balance field
                continue
            out[str(name).upper()] = amount
        return out

    def account(self) -> dict[str, Any]:
        balances = self._balances()
        total = sum(max(0.0, value) for value in balances.values())
        return {
            "login": 0, "mode": account_mode(self._base()),
            "server": "coincheck", "company": "Coincheck", "currency": "JPY",
            "balance": total, "equity": total, "margin": 0.0, "marginFree": total,
            "marginLevel": 0.0, "profit": 0.0, "leverage": 1.0, "marginMode": "spot",
            # Coincheck's balance payload has no trading switch.
            "tradeAllowed": True, "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        out: list[dict[str, Any]] = []
        for currency, quantity in self._balances().items():
            if quantity == 0:
                continue
            out.append({
                # Close by asset: Coincheck has no position id, and a market
                # sell of the held balance is the only close path.
                "ticket": currency, "symbol": currency, "side": "buy",
                "volume": quantity,
                "openPrice": 0.0,     # not derived: the adapter does not price assets
                "currentPrice": 0.0, "stopLoss": None, "takeProfit": None,
                "profit": 0.0, "swap": 0.0, "magic": 0,
                "comment": "coincheck spot balance (not marked to market)",
                "openedAt": now_ms,
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        payload = self._request("/api/exchange/orders/opens")
        rows = payload.get("orders") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            out.append({
                "ticket": str(row.get("id") or ""),
                "symbol": str(row.get("pair") or "").upper(),
                "type": str(row.get("order_type") or ""),
                "volumeCurrent": _num(row.get("amount")) - _num(row.get("executed_amount")),
                "openPrice": _num(row.get("rate")), "currentPrice": 0.0,
                "stopLoss": _num(row.get("stop_loss_rate")) or None, "takeProfit": None,
                "state": "open", "magic": 0,
                "comment": f"coincheck {str(row.get('order_type') or '').lower()}".strip(),
                "placedAt": 0,     # Coincheck's open-order payload carries no timestamp
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = "".join(ch for ch in str(symbol or "").lower() if ch.isalnum())
        if not pair:
            raise AdapterUnavailable("COINCHECK_SYMBOL", "a pair is required")
        row = self._request(f"/api/ticker?pair={pair}", signed=False)
        if not isinstance(row, dict) or row.get("last") in (None, ""):
            raise AdapterUnavailable("COINCHECK_NO_QUOTE", f"Coincheck has no ticker for {pair}")
        last = _num(row.get("last"))
        bid = _num(row.get("bid")) or last
        ask = _num(row.get("ask")) or last
        return {
            "symbol": pair.upper(), "requestedSymbol": symbol,
            "bid": bid, "ask": ask, "last": last,
            "spreadPoints": None, "digits": 8, "point": 0.00000001,
            "stopsLevelPoints": 0, "serverTimeMs": int(time.time() * 1000),
            "provider": "coincheck",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        # /api/rate/{pair} carries a rate, not a lot step. Reporting a made-up
        # step would let the policy engine "verify" a size the venue never
        # promised, so this adapter declines the question.
        raise AdapterUnavailable(
            "SYMBOL_INFO_NOT_SUPPORTED",
            "Coincheck publishes no lot step or minimum; pass the venue's own amount",
        )

    # -- execution -------------------------------------------------------
    @staticmethod
    def _stop_reasons(intent: Any) -> list[str]:
        reasons: list[str] = []
        if intent.take_profit is not None:
            reasons.append("Coincheck has no take-profit field; submit a limit exit instead")
        if intent.stop_loss is not None and intent.order_type != "limit":
            reasons.append("Coincheck accepts stop_loss_rate on a limit order only")
        return reasons

    def order_preview(self, intent: Any) -> dict[str, Any]:
        account, quote, positions = (
            self.account(), self.quote(_pair(intent.symbol)), self.positions())
        volume, reasons = step_volume(intent.volume)
        bid, ask = _fnum(quote.get("bid")), _fnum(quote.get("ask"))
        price = float(intent.price) if intent.order_type == "limit" and intent.price else (
            ask if intent.side == "buy" else bid)
        reasons += self._stop_reasons(intent)
        return build_preview(
            intent, account=account, quote=quote, volume=volume, price=price,
            contract_size=1.0, margin=max(0.0, volume * price),
            open_positions=len(positions), reasons=reasons, digits=8,
            extra={"venue": "coincheck"},
        )

    def _payload(self, intent: Any, volume: float, price: float) -> dict[str, Any]:
        payload: dict[str, Any] = {"pair": _pair(intent.symbol)}
        if intent.order_type == "market":
            if intent.side == "buy":
                # Coincheck spends JPY for a market buy; the intent's volume is
                # the asset quantity, so its notional is the amount to spend.
                payload.update(order_type="market_buy",
                               market_buy_amount=str(int(round(volume * price))))
            else:
                payload.update(order_type="market_sell", amount=_fmt(volume))
        else:
            payload.update(order_type=intent.side, rate=_fmt(price), amount=_fmt(volume))
            if intent.stop_loss is not None and price > 0:
                # Coincheck's field is a percentage, not a price.
                percent = abs(price - float(intent.stop_loss)) / price * 100
                payload["stop_loss_rate"] = f"{percent:.2f}"
        return payload

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            retcode = "STOP_UNSUPPORTED" if self._stop_reasons(intent) else "PREVIEW_REJECTED"
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0, retcode_name=retcode,
                reasons=reasons, account=preview.get("account") or {})
        row = self._request(
            "/api/exchange/orders", method="POST",
            body=self._payload(intent, float(preview["volume"]), float(preview["price"])))
        if not isinstance(row, dict) or row.get("id") in (None, ""):
            raise AdapterUnavailable("COINCHECK_BAD_RESPONSE", "Coincheck returned no order id")
        return build_result(
            ok=True, status="placed", symbol=intent.symbol, side=intent.side,
            order_type=intent.order_type, volume=preview["volume"], fill_volume=None,
            price=_num(row.get("rate")) or preview["price"], ticket=str(row["id"]),
            retcode="ACCEPTED", retcode_name="ACCEPTED",
            comment=str(row.get("order_type") or ""),
            reasons=[], account=preview.get("account") or {},
            # Accepted is not filled: Coincheck's ack carries no execution.
            filled=False,
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            row = self._request(f"/api/exchange/orders/{order_id}", method="DELETE")
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": order_id, "retcodeName": exc.code,
                    "reasons": [exc.reason]}
        ok = isinstance(row, dict) and row.get("success") is not False
        return {"ok": ok, "ticket": order_id, "retcodeName": "CANCELLED" if ok else "CANCEL_REJECTED",
                "reasons": [] if ok else ["Coincheck did not confirm the cancellation"]}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        rows = self.orders()
        if not rows:
            return {"ok": True, "cancelled": 0, "failed": 0, "results": [],
                    "retcodeName": "NO_ORDERS", "reasons": []}
        # Coincheck's open-order payload carries no client id, so the magic
        # cannot be matched to an order. Refusing is the only safe answer.
        return {"ok": False, "cancelled": 0, "failed": 0, "results": [],
                "retcodeName": "OWNERSHIP_UNAVAILABLE",
                "reasons": ["Coincheck open orders expose no client id, so Eclipse "
                            "cannot prove which orders are its own"]}

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        asset = str(ticket or "").strip().upper()
        if asset in ("", "JPY"):
            # JPY is the quote currency, not a position one can sell.
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_SYMBOL",
                    "reasons": ["pass the held asset symbol, not the JPY quote balance"]}
        balance = self._balances().get(asset, 0.0)
        if balance <= 0:
            return {"ok": False, "ticket": ticket, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Coincheck holds no {asset or 'requested'} balance"]}
        size = float(volume) if volume else balance
        if size > balance:
            return {"ok": False, "ticket": asset, "retcodeName": "INVALID_VOLUME",
                    "reasons": [f"volume {size:g} exceeds the {balance:g} balance"]}
        pair = _pair(asset)
        row = self._request(
            "/api/exchange/orders", method="POST",
            body={"pair": pair, "order_type": "market_sell", "amount": _fmt(size)})
        if not isinstance(row, dict) or row.get("id") in (None, ""):
            raise AdapterUnavailable("COINCHECK_BAD_RESPONSE", "Coincheck returned no close order")
        try:
            price = _fnum(self.quote(pair).get("bid"))
        except AdapterUnavailable:
            price = 0.0
        return {
            "ok": True, "ticket": asset, "symbol": asset, "side": "sell",
            "volume": size, "price": _num(row.get("rate")) or price,
            "retcode": "ACCEPTED", "orderId": str(row["id"]),
            "retcodeName": "PLACED", "partial": bool(volume) and size < balance,
            # A market sell is accepted, not necessarily filled; never claim it.
            "verified": False,
            "reasons": [],
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "MODIFY_NOT_SUPPORTED",
                "reasons": ["Coincheck has no position-edit endpoint; place a sell "
                            "order with a stop_loss_rate instead"]}

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "ORDER_MODIFY_NOT_SUPPORTED",
                "reasons": ["Coincheck cannot edit a working order; cancel it and "
                            "submit the replacement"]}

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        try:
            row = self._request(f"/api/exchange/orders/{order_id}")
        except AdapterUnavailable:
            return {"found": False, "ticket": order_id}
        if not isinstance(row, dict) or str(row.get("id") or "") != order_id:
            return {"found": False, "ticket": order_id}
        amount = _num(row.get("amount"))
        executed = _num(row.get("executed_amount"))
        state = "filled" if amount and executed >= amount else ("partial" if executed > 0 else "open")
        return {"found": True, "ticket": order_id, "state": state,
                "stateName": state.upper(), "filledVolume": executed,
                "avgPrice": _num(row.get("rate"))}


__all__ = ["CoincheckAdapter", "sign_request"]
