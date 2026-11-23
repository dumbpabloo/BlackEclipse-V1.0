"""Bitstamp v2 adapter: reads AND real order execution.

Bitstamp is the odd one out here: it does **not** use auth headers. The key,
signature and nonce travel in the **form body**, and the signature covers the
credential itself:

    nonce     = unix seconds (monotonic per call)
    signature = hex HMAC-SHA256(secret, nonce + customer_id + api_key)

`sign_request` is pure so that preimage — including the customer id, which is the
one people forget — is checked against an independent computation. The nonce must
strictly increase, so it is derived from a monotonic clock and bumped on repeat.
The order parameters are NOT part of the signature; they are appended to the same
form body next to `key`/`nonce`/`signature`.

Declared limits:

  * **Spot only.** Balances are per currency with `available` and `balance`;
    `positions()` reports non-zero balances and does not mark them to market. A
    spot close is an opposite MARKET order, so a position ticket is the currency
    and a close takes the market symbol (`btcusd`).
  * **No attached stops.** An intent carrying `stop_loss`/`take_profit` is refused
    (`STOP_UNSUPPORTED`); a stop is its own order here.
  * **No client order id.** Bitstamp's API has no tag to prove ownership, so
    `cancel_all(magic)` reports `OWNERSHIP_UNAVAILABLE` instead of cancelling
    orders Eclipse cannot prove it placed.
  * **Market BUY `amount` is in the QUOTE currency.** Sells and limit orders take
    `amount` in the base currency. `submit_order` converts a market buy's base
    volume into quote notional using the quote it just read, and refuses if there
    is no usable quote rather than guessing.

Config: `baseUrl` (default https://www.bitstamp.net), `credentialRef` naming an
env var/secret holding `KEY:SECRET:CUSTOMER_ID`, optional `magic` for close orders.
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
from ..model import KIND_BITSTAMP
from .credentials import resolve_secret_value, validate_http_url
from .orders import (
    account_mode,
    build_preview,
    build_result,
    quote_preference,
    resolve_close_pair,
    step_volume,
)

_TIMEOUT = 15

_STOP_REASON = (
    "a spot entry cannot carry an attached stop loss or take profit; "
    "submit the entry, then place a separate stop order on the filled position"
)
_NO_POSITION_MODIFY = (
    "Bitstamp spot has no position-level stop/target object; a close is an opposite "
    "market order and a stop is its own resting order"
)
_NO_ORDER_MODIFY = (
    "Bitstamp has no in-place order edit; cancel the order and submit the replacement"
)


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _fmt(value: float) -> str:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _digits_of(step: float) -> int:
    text = f"{float(step or 0):.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def sign_request(secret: str, nonce: str, customer_id: str, api_key: str) -> str:
    """Bitstamp v2: hex HMAC-SHA256 over `nonce + customer_id + api_key`."""
    message = f"{nonce}{customer_id}{api_key}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class BitstampAdapter(TradingAdapter):
    kind = KIND_BITSTAMP

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- credentials / http ----------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="BITSTAMP_CREDENTIALS",
            label="Bitstamp key/secret/customer id",
        )
        if not raw:
            raise AdapterUnavailable(
                "BITSTAMP_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret holding "
                "`KEY:SECRET:CUSTOMER_ID`",
            )
        parts = raw.split(":")
        if len(parts) != 3 or not all(parts):
            raise AdapterUnavailable(
                "BITSTAMP_CREDENTIALS",
                "the credential must be `KEY:SECRET:CUSTOMER_ID` (three parts)",
            )
        return parts[0], parts[1], parts[2]

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://www.bitstamp.net"),
            code="BITSTAMP_CONFIG",
            label="Bitstamp baseUrl",
        )

    def _nonce(self) -> str:
        """Strictly increasing seconds: a repeated nonce is rejected by Bitstamp."""
        candidate = int(time.time())
        last = int(getattr(self, "_last_nonce", 0) or 0)
        if candidate <= last:
            candidate = last + 1
        self._last_nonce = candidate
        return str(candidate)

    def _call(self, path: str, *, private: bool = True,
              params: Optional[dict[str, Any]] = None) -> Any:
        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if private:
            key, secret, customer_id = self._credentials()
            nonce = self._nonce()
            fields: dict[str, Any] = {
                "key": key,
                "nonce": nonce,
                "signature": sign_request(secret, nonce, customer_id, key),
            }
            # Order parameters ride in the same form body; Bitstamp signs only
            # the credential triple, never the payload.
            fields.update(params or {})
            data = urllib.parse.urlencode(fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        url = f"{self._base()}{path}"
        request = urllib.request.Request(url, headers=headers, data=data,
                                         method="POST" if private else "GET")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("BITSTAMP_UNAUTHORIZED", "Bitstamp rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("BITSTAMP_RATE_LIMITED", "Bitstamp rate limit reached") from exc
            detail = ""
            try:
                parsed = json.loads(exc.read() or b"{}")
                if isinstance(parsed, dict) and parsed.get("error"):
                    detail = f": {parsed['error']}"
            except Exception:  # noqa: BLE001 - the body is advisory
                detail = ""
            raise AdapterUnavailable(
                "BITSTAMP_HTTP", f"Bitstamp returned HTTP {exc.code}{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("BITSTAMP_UNREACHABLE", "Bitstamp is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BITSTAMP_TIMEOUT", "Bitstamp timed out") from exc
        try:
            return json.loads(body or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("BITSTAMP_BAD_JSON", "Bitstamp returned invalid JSON") from exc

    # -- BrokerAdapter ---------------------------------------------------

    def probe(self) -> dict[str, Any]:
        try:
            account = self.account()
        except AdapterUnavailable as exc:
            return {"available": True, "connected": False, "code": exc.code,
                    "reason": exc.reason, "terminal": {"api": self._base()},
                    "account": None, "warnings": []}
        return {"available": True, "connected": True, "code": None, "reason": None,
                "terminal": {"api": self._base()}, "account": account,
                "warnings": []}

    def _balances(self) -> dict[str, dict[str, float]]:
        payload = self._call("/api/v2/balance/")
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BITSTAMP_BAD_RESPONSE", "Bitstamp returned no balances")
        out: dict[str, dict[str, float]] = {}
        for key, value in payload.items():
            name = str(key)
            if name.endswith("_balance"):
                currency = name[: -len("_balance")].upper()
                out.setdefault(currency, {})["balance"] = _num(value)
            elif name.endswith("_available"):
                currency = name[: -len("_available")].upper()
                out.setdefault(currency, {})["available"] = _num(value)
            elif name.endswith("_fee"):
                continue
        return out

    def account(self) -> dict[str, Any]:
        balances = self._balances()
        total = sum(row.get("balance", 0.0) for row in balances.values())
        available = sum(row.get("available", 0.0) for row in balances.values())
        return {
            "login": 0,
            "mode": account_mode(self._base()),
            "server": "bitstamp",
            "company": "Bitstamp",
            "currency": "USD",
            "balance": total,
            "equity": total,
            "margin": total - available,
            "marginFree": available,
            "marginLevel": 0.0,
            "profit": 0.0,
            "leverage": 1.0,
            "marginMode": "spot",
            # Bitstamp's balance payload has no "trading disabled" flag; a refused
            # order is the venue's own answer, so this is a live venue.
            "tradeAllowed": True,
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        out: list[dict[str, Any]] = []
        for currency, row in self._balances().items():
            quantity = row.get("balance", 0.0)
            if quantity == 0:
                continue
            out.append({
                # Closed by an opposite market order, so the currency is the ticket.
                "ticket": currency,
                "symbol": currency,
                "side": "buy",
                "volume": quantity,
                "openPrice": 0.0,     # not derived: the adapter does not price assets
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "profit": 0.0,
                "swap": 0.0,
                "magic": 0,
                "comment": "bitstamp spot balance (not marked to market)",
                "openedAt": now_ms,
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        payload = self._call("/api/v2/open_orders/all/")
        out: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict):
                continue
            amount = _num(row.get("amount"))
            out.append({
                "ticket": str(row.get("id") or ""),
                "symbol": str(row.get("currency_pair") or "").upper(),
                "type": str(row.get("type") or ""),
                "volumeCurrent": amount,
                "openPrice": _num(row.get("price")),
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "state": "open",
                "magic": 0,
                "comment": f"bitstamp {str(row.get('type') or '').lower()}".strip(),
                "placedAt": int(_num(row.get("datetime")) * 1000),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = "".join(ch for ch in str(symbol or "").lower() if ch.isalnum())
        if not pair:
            raise AdapterUnavailable("BITSTAMP_SYMBOL", "a currency pair is required")
        row = self._call(f"/api/v2/ticker/{pair}/", private=False)
        if not isinstance(row, dict) or row.get("last") in (None, ""):
            raise AdapterUnavailable("BITSTAMP_NO_QUOTE", f"Bitstamp has no ticker for {pair}")
        last = _num(row.get("last"))
        bid = _num(row.get("bid")) or last
        ask = _num(row.get("ask")) or last
        return {
            "symbol": pair.upper(),
            "requestedSymbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "spreadPoints": None,
            "digits": 8,
            "point": 0.00000001,
            "stopsLevelPoints": 0,
            # Bitstamp's ticker timestamp is in SECONDS; the contract is ms.
            "serverTimeMs": int((_num(row.get("timestamp")) or time.time()) * 1000),
            "provider": "bitstamp",
        }

    # -- instrument metadata -------------------------------------------------
    def _all_pairs(self) -> dict[str, str]:
        cached = getattr(self, "_pair_cache", None)
        if cached is not None:
            return cached
        rows = self._call("/api/v2/markets", private=False)
        pairs: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url_symbol") or "")
            name = str(row.get("name") or "").replace("/", "")
            if url:
                pairs[url.upper()] = url
            if name:
                pairs[name.upper()] = url or name
        self._pair_cache = pairs
        return pairs

    def _market(self, symbol: str) -> dict[str, Any]:
        pair = "".join(ch for ch in str(symbol or "").lower() if ch.isalnum())
        if not pair:
            raise AdapterUnavailable("BITSTAMP_SYMBOL", "a market symbol is required")
        rows = self._call("/api/v2/markets", private=False)
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("url_symbol") or "").lower() == pair:
                return row
        raise AdapterUnavailable("BITSTAMP_NO_SYMBOL", f"Bitstamp lists no market {pair!r}")

    @staticmethod
    def _base_currency(market: dict[str, Any]) -> str:
        name = str(market.get("name") or "")
        if "/" in name:
            return name.split("/", 1)[0].strip().upper()
        return ""

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        row = self._market(symbol)
        base_step = 10 ** -int(_num(row.get("base_decimals"), 8))
        counter_step = 10 ** -int(_num(row.get("counter_decimals"), 8))
        base = self._base_currency(row)
        quote_asset = str(row.get("name") or "").split("/")[-1].strip().upper()
        trading = str(row.get("trading") or "").lower()
        return {
            "symbol": str(row.get("url_symbol") or symbol).upper(),
            "requestedSymbol": symbol,
            "description": str(row.get("name") or ""),
            "path": "spot",
            "tradeMode": "full" if trading == "enabled" else "disabled",
            "digits": _digits_of(counter_step) or 8,
            "point": counter_step,
            "tickSize": counter_step,
            "tickValue": counter_step,
            "currencyBase": base,
            "currencyProfit": quote_asset,
            "currencyMargin": quote_asset,
            "contractSize": 1.0,
            "volumeMin": 0.0,
            "volumeMax": 0.0,
            "volumeStep": base_step,
            "notionalMin": _num(row.get("minimum_order_value")),
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": trading == "enabled",
            "sessionNote": "Bitstamp trades continuously",
            "retrievedAt": time.time(),
            "provider": "bitstamp",
        }

    # -- execution -----------------------------------------------------------
    def order_preview(self, intent: Any) -> dict[str, Any]:
        account = self.account()
        quote = self.quote(intent.symbol)
        info = self.symbol_info(intent.symbol)
        step = _num(info.get("volumeStep"))
        volume, reasons = step_volume(
            intent.volume, step, _num(info.get("volumeMin")), _num(info.get("volumeMax")),
        )
        bid = _num(quote.get("bid"))
        ask = _num(quote.get("ask"))
        price = (
            float(intent.price)
            if intent.order_type in ("limit", "stop") and intent.price
            else (ask if intent.side == "buy" else bid)
        )
        extra: dict[str, Any] = {"venue": "bitstamp", "stopSupported": False}
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(_STOP_REASON)
            extra["retcodeName"] = "STOP_UNSUPPORTED"
        if info.get("tradeMode") != "full":
            reasons.append(f"Bitstamp reports {intent.symbol} as not tradable")
        return build_preview(
            intent,
            account=account,
            quote=quote,
            volume=volume,
            price=price,
            contract_size=1.0,
            margin=max(0.0, volume * price),
            open_positions=len(self.positions()),
            reasons=reasons,
            digits=int(info.get("digits") or 8),
            volume_step=step,
            volume_min=_num(info.get("volumeMin")),
            volume_max=_num(info.get("volumeMax")),
            extra=extra,
        )

    def _order_params(self, intent: Any, volume: float, price: float) -> dict[str, Any]:
        side = str(intent.side).lower()
        order_type = str(intent.order_type)
        params: dict[str, Any] = {"currency_pair": str(intent.symbol).strip().lower()}
        if order_type == "limit":
            params.update({
                "amount": _fmt(volume),
                "price": _fmt(float(intent.price)),
                "type": f"{side}_limit",
            })
        elif order_type == "stop":
            params.update({
                "amount": _fmt(volume),
                "price": _fmt(float(intent.price)),
                "type": f"{side}_stop",
            })
        elif side == "buy":
            # Bitstamp quotes a MARKET BUY's `amount` in the QUOTE currency, so
            # the base volume is converted with the price just read. No quote =>
            # no conversion => an honest refusal, never a guessed notional.
            params.update({"amount": _fmt(volume * price), "type": "buy"})
        else:
            params.update({"amount": _fmt(volume), "type": "sell"})
        return params

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        price = float(preview.get("price") or 0.0)
        if intent.order_type == "market" and intent.side == "buy" and price <= 0:
            reasons.append(
                "Bitstamp needs a quote to size a market buy (its `amount` is in "
                "the quote currency) and none was available"
            )
        if reasons:
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=price,
                retcode_name=str(preview.get("retcodeName") or "PREVIEW_REJECTED"),
                reasons=reasons,
                account=preview.get("account") or {},
            )
        volume = float(preview["volume"])
        params = self._order_params(intent, volume, price)
        row = self._call("/api/v2/order/", params=params)
        if not isinstance(row, dict) or row.get("id") in (None, "") or row.get("error"):
            raise AdapterUnavailable(
                "BITSTAMP_BAD_RESPONSE",
                f"Bitstamp returned no order id{': ' + str(row.get('error')) if isinstance(row, dict) and row.get('error') else ''}",
            )
        return build_result(
            ok=True,
            status="placed",
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            # Bitstamp acknowledges the id and reports no fill here.
            fill_volume=None,
            price=_num(row.get("price")) or price,
            ticket=str(row.get("id")),
            retcode="placed",
            retcode_name="ACCEPTED",
            comment=str(row.get("type") or ""),
            account=preview.get("account") or {},
            amountSent=params.get("amount"),
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._call("/api/v2/cancel_order/", params={"id": order_id})
        if not isinstance(row, dict) or row.get("error") or not row.get("id"):
            detail = ""
            if isinstance(row, dict) and row.get("error"):
                detail = str(row.get("error"))
            return {
                "ok": False, "ticket": order_id, "retcode": None,
                "retcodeName": "ORDER_NOT_FOUND",
                "reasons": [detail or f"Bitstamp did not cancel {order_id}"],
            }
        return {"ok": True, "ticket": order_id, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": []}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        # Bitstamp attaches no client-side tag to an order, so an "Eclipse order"
        # cannot be told apart from a manual one. Refusing is the only honest answer.
        return {
            "ok": False,
            "cancelled": 0,
            "failed": 0,
            "results": [],
            "retcode": None,
            "retcodeName": "OWNERSHIP_UNAVAILABLE",
            "reasons": [
                "Bitstamp has no client order id, so Eclipse cannot prove which open "
                "orders it placed; cancel named tickets instead of a scoped bulk cancel"
            ],
        }

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        pair = "".join(ch for ch in str(ticket or "").lower() if ch.isalnum())
        if not pair:
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_SYMBOL",
                    "reasons": ["pass the market symbol (e.g. btcusd) to close a balance"]}
        try:
            available = self._all_pairs()
        except AdapterUnavailable:
            available = {}
        resolved = resolve_close_pair(
            self.config, pair, available,
            quote_preference(self.config, ("USD", "USDT", "USDC", "EUR", "GBP", "BTC")),
            "{base}{quote}",
        )
        if not resolved:
            resolved = pair
        pair = resolved
        try:
            market = self._market(pair)
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": pair, "retcodeName": "INVALID_SYMBOL",
                    "reasons": [f"{pair} is not a Bitstamp market: {exc.reason}"]}
        base = self._base_currency(market)
        row = self._balances().get(base, {})
        free = max(0.0, _num(row.get("available"), _num(row.get("balance"))))
        quantity = float(volume) if volume else free
        step = 10 ** -int(_num(market.get("base_decimals"), 8))
        quantity, reasons = step_volume(quantity, step, 0.0, 0.0)
        if quantity <= 0:
            return {"ok": False, "ticket": pair, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Bitstamp reports no available {base} balance to close"]}
        params = {
            "currency_pair": str(market.get("url_symbol") or pair),
            "amount": _fmt(quantity),
            "type": "sell",
        }
        response = self._call("/api/v2/order/", params=params)
        if not isinstance(response, dict) or response.get("id") in (None, "") or response.get("error"):
            raise AdapterUnavailable("BITSTAMP_BAD_RESPONSE", "Bitstamp returned no close order")
        return {
            "ok": True,
            "ticket": pair.upper(),
            "symbol": pair.upper(),
            "side": "sell",
            "volume": quantity,
            "requestedVolume": quantity,
            "price": _num(response.get("price")),
            "orderTicket": str(response.get("id")),
            "retcode": "placed",
            "retcodeName": "CLOSING",
            "partial": bool(volume),
            # Bitstamp only acknowledged the order; no executed amount is claimed.
            "verified": False,
            "reasons": reasons,
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._call("/api/v2/order_status/", params={"id": order_id})
        if not isinstance(row, dict) or row.get("error") or row.get("status") in (None, ""):
            return {"found": False, "ticket": order_id}
        status = str(row.get("status") or "")
        return {
            "found": True,
            "ticket": str(row.get("id") or order_id),
            "state": status,
            "stateName": status.upper(),
            "filledVolume": _num(row.get("amount_transacted")),
            "avgPrice": _num(row.get("price")),
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "MODIFY_NOT_SUPPORTED", "reasons": [_NO_POSITION_MODIFY]}

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        return {"ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "ORDER_MODIFY_NOT_SUPPORTED", "reasons": [_NO_ORDER_MODIFY]}


__all__ = ["BitstampAdapter", "sign_request"]
