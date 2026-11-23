"""Kraken adapter (spot): reads AND real order execution.

Kraken's private API signs a **path + nonce + body** digest with HMAC-SHA512:

    postdata  = "nonce=<ms>"
    digest    = sha256(nonce + postdata).digest()
    signature = base64(HMAC-SHA512(base64decode(secret), path.encode() + digest))

`sign_request` is pure so the exact input can be checked against an independent
computation — this scheme is easy to get subtly wrong (the path is signed, the
body is hashed first, and the secret arrives base64-encoded). `_call` already
signs `path + nonce + body`, so every private POST (AddOrder, CancelOrder,
OpenOrders, QueryOrders) reuses it unchanged.

Declared limits:

  * **Spot only.** `positions()` reports non-zero balances; nothing is priced.
    A spot close is an opposite MARKET order, so a position ticket is the asset
    and a close takes the PAIR (e.g. `XBTUSD`).
  * **No attached stops.** Kraken can carry a `close[...]` stop, but Eclipse
    refuses an intent that carries `stop_loss`/`take_profit` (`STOP_UNSUPPORTED`)
    so no venue silently drops or half-applies a stop; place it as its own order.
  * **Ownership tag is `userref`** (an integer), set from the intent's magic.
    `cancel_all` matches `userref`, never a blind all-orders cancel.
  * Kraken answers HTTP 200 with an `error` array on application failures, so that
    array is a refusal, not empty data.

Config: `baseUrl` (default https://api.kraken.com), `credentialRef` naming an env
var/secret holding `KEY:SECRET`, optional `magic` tagging close orders.
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
from ..model import KIND_KRAKEN
from .credentials import parse_key_pair, resolve_secret_value, validate_http_url
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
    "Kraken spot has no position-level stop/target object; a close is an opposite "
    "market order and a stop is a separate resting order"
)
_NO_ORDER_MODIFY = (
    "Kraken's EditOrder needs the pair and volume of the working order; cancel the "
    "order and submit the replacement instead"
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


def sign_request(secret_b64: str, path: str, nonce: str, postdata: str) -> str:
    """Kraken's documented API-Sign."""
    secret = base64.b64decode(secret_b64)
    digest = hashlib.sha256((nonce + postdata).encode("utf-8")).digest()
    return base64.b64encode(
        hmac.new(secret, path.encode("utf-8") + digest, hashlib.sha512).digest()
    ).decode("ascii")


class KrakenAdapter(TradingAdapter):
    kind = KIND_KRAKEN

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # -- http ------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="KRAKEN_CREDENTIALS",
            label="Kraken API key/secret",
        )
        if not raw:
            raise AdapterUnavailable(
                "KRAKEN_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing `KEY:SECRET`",
            )
        return parse_key_pair(raw)

    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://api.kraken.com"),
            code="KRAKEN_CONFIG",
            label="Kraken baseUrl",
        )

    def _call(self, path: str, params: Optional[dict[str, Any]] = None, *, private: bool = True) -> Any:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        url = self._base() + path
        if private:
            key, secret = self._credentials()
            nonce = str(int(time.time() * 1000))
            postdata = urllib.parse.urlencode(params or {})
            if postdata:
                postdata = f"nonce={nonce}&" + postdata
            else:
                postdata = f"nonce={nonce}"
            headers.update({
                "API-Key": key,
                "API-Sign": sign_request(secret, path, nonce, postdata),
                "Content-Type": "application/x-www-form-urlencoded",
            })
            data = postdata.encode("utf-8")
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers, data=data,
                                         method="POST" if private else "GET")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                body = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterUnavailable("KRAKEN_UNAUTHORIZED", "Kraken rejected the API key") from exc
            if exc.code == 429:
                raise AdapterUnavailable("KRAKEN_RATE_LIMITED", "Kraken rate limit reached") from exc
            raise AdapterUnavailable("KRAKEN_HTTP", f"Kraken returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable("KRAKEN_UNREACHABLE", "Kraken is unreachable") from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("KRAKEN_TIMEOUT", "Kraken timed out") from exc
        try:
            payload = json.loads(body or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("KRAKEN_BAD_JSON", "Kraken returned invalid JSON") from exc
        errors = payload.get("error") if isinstance(payload, dict) else None
        if errors:
            raise AdapterUnavailable("KRAKEN_API_ERROR", "; ".join(str(e) for e in errors)[:300])
        return payload.get("result") if isinstance(payload, dict) else payload

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

    def _balances(self) -> dict[str, float]:
        result = self._call("/0/private/Balance") or {}
        if not isinstance(result, dict):
            raise AdapterUnavailable("KRAKEN_BAD_RESPONSE", "Kraken returned no balances")
        return {str(asset): _num(amount) for asset, amount in result.items()}

    def account(self) -> dict[str, Any]:
        balances = self._balances()
        total = sum(max(0.0, value) for value in balances.values())
        return {
            "login": 0,
            "mode": account_mode(self._base()),
            "server": "kraken",
            "company": "Kraken",
            "currency": "USD",
            "balance": total,
            "equity": total,
            "margin": 0.0,
            "marginFree": total,
            "marginLevel": 0.0,
            "profit": 0.0,
            "leverage": 1.0,
            "marginMode": "spot",
            # Kraken's Balance payload carries no "trading disabled" flag, and a
            # rejected AddOrder is the venue's own refusal — so this is a live
            # venue unless the venue itself says otherwise.
            "tradeAllowed": True,
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for asset, quantity in self._balances().items():
            if quantity == 0:
                continue
            out.append({
                # Closed by an opposite market order, so the asset is the ticket.
                "ticket": asset.upper(),
                "symbol": asset.upper(),
                "side": "buy",
                "volume": quantity,
                "openPrice": 0.0,     # not derived: the adapter does not price assets
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "profit": 0.0,
                "swap": 0.0,
                "magic": 0,
                "comment": "kraken spot balance (not marked to market)",
                "openedAt": int(time.time() * 1000),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        result = self._call("/0/private/OpenOrders") or {}
        open_orders = result.get("open") if isinstance(result, dict) else None
        out: list[dict[str, Any]] = []
        for ticket, row in (open_orders or {}).items():
            if not isinstance(row, dict):
                continue
            descr = row.get("descr") if isinstance(row.get("descr"), dict) else {}
            out.append({
                "ticket": str(ticket),
                "symbol": str(descr.get("pair") or "").upper(),
                "type": str(descr.get("ordertype") or ""),
                "volumeCurrent": _num(row.get("vol")) - _num(row.get("vol_exec")),
                "openPrice": _num(descr.get("price")),
                "currentPrice": 0.0,
                "stopLoss": None,
                "takeProfit": None,
                "state": "open",
                "magic": 0,
                "comment": f"kraken {str(descr.get('type') or '').lower()}".strip(),
                "placedAt": int(_num(row.get("opentm")) * 1000),
                "userref": row.get("userref"),
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any]:
        pair = str(symbol or "").strip().upper()
        if not pair:
            raise AdapterUnavailable("KRAKEN_SYMBOL", "a pair is required")
        result = self._call("/0/public/Ticker", {"pair": pair}, private=False) or {}
        row = next(iter(result.values()), None) if isinstance(result, dict) else None
        if not isinstance(row, dict):
            raise AdapterUnavailable("KRAKEN_NO_QUOTE", f"Kraken has no ticker for {pair}")
        last = _num((row.get("c") or [0])[0])
        bid = _num((row.get("b") or [0])[0])
        ask = _num((row.get("a") or [0])[0])
        return {
            "symbol": pair,
            "requestedSymbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last or ((bid + ask) / 2 if bid and ask else 0.0),
            "spreadPoints": None,
            "digits": 8,
            "point": 0.00000001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(time.time() * 1000),
            "provider": "kraken",
        }

    # -- instrument metadata -------------------------------------------------
    def _all_pairs(self) -> dict[str, str]:
        cached = getattr(self, "_pair_cache", None)
        if cached is not None:
            return cached
        result = self._call("/0/public/AssetPairs", private=False) or {}
        pairs: dict[str, str] = {}
        for key, row in (result.items() if isinstance(result, dict) else []):
            if not isinstance(row, dict):
                continue
            alt = str(row.get("altname") or key)
            pairs[str(key).upper()] = alt
            pairs[alt.upper()] = alt
        self._pair_cache = pairs
        return pairs

    def _asset_pair(self, pair: str) -> tuple[str, dict[str, Any]]:
        key = str(pair or "").strip().upper()
        if not key:
            raise AdapterUnavailable("KRAKEN_SYMBOL", "a pair is required")
        result = self._call("/0/public/AssetPairs", {"pair": key}, private=False) or {}
        if not isinstance(result, dict) or not result:
            raise AdapterUnavailable("KRAKEN_NO_SYMBOL", f"Kraken lists no pair {key!r}")
        name = next(iter(result))
        row = result[name]
        if not isinstance(row, dict):
            raise AdapterUnavailable("KRAKEN_NO_SYMBOL", f"Kraken lists no pair {key!r}")
        return name, row

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        key, row = self._asset_pair(str(symbol or ""))
        lot_decimals = int(_num(row.get("lot_decimals"), 8))
        pair_decimals = int(_num(row.get("pair_decimals"), 8))
        status = str(row.get("status") or "").lower()
        base = str(row.get("base") or "")
        quote_asset = str(row.get("quote") or "")
        return {
            "symbol": str(row.get("altname") or key).upper(),
            "requestedSymbol": symbol,
            "description": str(row.get("wsname") or f"{base}/{quote_asset}"),
            "path": "spot",
            "tradeMode": "full" if status in ("online", "post_only") else "disabled",
            "digits": pair_decimals,
            "point": 10 ** -pair_decimals,
            "tickSize": 10 ** -pair_decimals,
            "tickValue": 10 ** -pair_decimals,
            "currencyBase": base,
            "currencyProfit": quote_asset,
            "currencyMargin": quote_asset,
            "contractSize": 1.0,
            "volumeMin": _num(row.get("ordermin")),
            "volumeMax": 0.0,
            "volumeStep": 10 ** -lot_decimals,
            "notionalMin": _num(row.get("costmin")),
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": True,
            "sessionNote": "Kraken spot trades continuously",
            "retrievedAt": time.time(),
            "provider": "kraken",
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
        extra: dict[str, Any] = {"venue": "kraken", "stopSupported": False}
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(_STOP_REASON)
            extra["retcodeName"] = "STOP_UNSUPPORTED"
        if info.get("tradeMode") != "full":
            reasons.append(f"Kraken reports {intent.symbol} as not tradable")
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

    def _order_params(self, intent: Any, volume: float) -> dict[str, Any]:
        order_type = str(intent.order_type)
        params: dict[str, Any] = {
            "pair": str(intent.symbol).strip().upper(),
            "type": str(intent.side).lower(),
            "ordertype": {"market": "market", "limit": "limit", "stop": "stop-loss"}[order_type],
            "volume": _fmt(volume),
            # Kraken's ownership tag is an integer; the magic is exactly that.
            "userref": int(intent.magic or 0),
        }
        if order_type == "limit":
            params["price"] = _fmt(float(intent.price))
            params["timeinforce"] = "gtc" if intent.time_in_force == "gtc" else "day"
        elif order_type == "stop":
            params["price"] = _fmt(float(intent.price))
        return params

    def submit_order(self, intent: Any) -> dict[str, Any]:
        preview = self.order_preview(intent)
        if preview.get("reasons"):
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0,
                retcode_name=str(preview.get("retcodeName") or "PREVIEW_REJECTED"),
                reasons=preview.get("reasons") or [],
                account=preview.get("account") or {},
            )
        volume = float(preview["volume"])
        result = self._call("/0/private/AddOrder", self._order_params(intent, volume)) or {}
        txids = result.get("txid") if isinstance(result, dict) else None
        if not isinstance(txids, list) or not txids:
            raise AdapterUnavailable("KRAKEN_BAD_RESPONSE", "Kraken returned no order txid")
        return build_result(
            ok=True,
            status="placed",
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            # AddOrder acknowledges an id and reports no fill; a fill is never
            # inferred from an acknowledgement.
            fill_volume=None,
            price=preview["price"],
            ticket=str(txids[0]),
            retcode="placed",
            retcode_name="ACCEPTED",
            comment=str((result.get("descr") or {}).get("order") or ""),
            account=preview.get("account") or {},
            userref=int(intent.magic or 0),
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        txid = str(ticket)
        try:
            result = self._call("/0/private/CancelOrder", {"txid": txid}) or {}
        except AdapterUnavailable as exc:
            if "Unknown order" in exc.reason or "unknown order" in exc.reason.lower():
                return {"ok": False, "ticket": txid, "retcode": None,
                        "retcodeName": "ORDER_NOT_FOUND", "reasons": [exc.reason]}
            raise
        count = int(_num(result.get("count"), 0)) if isinstance(result, dict) else 0
        if count <= 0:
            return {
                "ok": False, "ticket": txid, "retcode": None,
                "retcodeName": "ORDER_NOT_FOUND",
                "reasons": [f"Kraken did not cancel {txid} (no matching working order)"],
            }
        return {"ok": True, "ticket": txid, "retcode": 200,
                "retcodeName": "CANCELLED", "reasons": [], "pending": bool(result.get("pending"))}

    def _open_rows(self) -> dict[str, Any]:
        result = self._call("/0/private/OpenOrders") or {}
        rows = result.get("open") if isinstance(result, dict) else None
        return rows if isinstance(rows, dict) else {}

    def cancel_all(self, magic: int) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for txid, row in self._open_rows().items():
            if not isinstance(row, dict):
                continue
            if str(row.get("userref")) != str(int(magic or 0)):
                continue
            try:
                results.append(self.cancel_order(txid))
            except AdapterUnavailable as exc:
                results.append({"ok": False, "ticket": str(txid),
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

    @staticmethod
    def _asset_key(asset: str) -> str:
        return str(asset or "").strip().upper()

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        pair = str(ticket or "").strip().upper()
        if not pair:
            return {"ok": False, "ticket": ticket, "retcodeName": "INVALID_SYMBOL",
                    "reasons": ["pass the traded PAIR (e.g. XBTUSD) to close a spot balance"]}
        try:
            available = self._all_pairs()
        except AdapterUnavailable:
            available = {}
        resolved = resolve_close_pair(
            self.config, pair, available,
            quote_preference(self.config, ("USD", "USDT", "USDC", "EUR", "GBP", "JPY", "BTC")),
            "{base}{quote}",
        )
        if not resolved:
            resolved = pair
        pair = resolved
        try:
            _key, info = self._asset_pair(pair)
        except AdapterUnavailable as exc:
            return {"ok": False, "ticket": pair, "retcodeName": "INVALID_SYMBOL",
                    "reasons": [f"{pair} is not a Kraken pair: {exc.reason}"]}
        base = self._asset_key(info.get("base"))
        balances = self._balances()
        free = max(0.0, balances.get(base, balances.get(f"X{base}", 0.0)))
        quantity = float(volume) if volume else free
        step = 10 ** -int(_num(info.get("lot_decimals"), 8))
        quantity, reasons = step_volume(quantity, step, 0.0, 0.0)
        if quantity <= 0:
            return {"ok": False, "ticket": pair, "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"Kraken reports no free {base} balance to close"]}
        params = {
            "pair": str(info.get("altname") or pair),
            "type": "sell",
            "ordertype": "market",
            "volume": _fmt(quantity),
            "userref": int(self.config.get("magic") or 0),
        }
        result = self._call("/0/private/AddOrder", params) or {}
        txids = result.get("txid") if isinstance(result, dict) else None
        if not isinstance(txids, list) or not txids:
            raise AdapterUnavailable("KRAKEN_BAD_RESPONSE", "Kraken returned no close txid")
        return {
            "ok": True,
            "ticket": pair,
            "symbol": pair,
            "side": "sell",
            "volume": quantity,
            "requestedVolume": quantity,
            "price": 0.0,
            "orderTicket": str(txids[0]),
            "retcode": "placed",
            "retcodeName": "CLOSING",
            "partial": bool(volume),
            # Kraken only acknowledged the order; it reports no executed volume
            # here, so this is not claimed as a confirmed close.
            "verified": False,
            "reasons": reasons,
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        txid = str(ticket)
        result = self._call("/0/private/QueryOrders", {"txid": txid}) or {}
        row = result.get(txid) if isinstance(result, dict) else None
        if not isinstance(row, dict):
            return {"found": False, "ticket": txid}
        return {
            "found": True,
            "ticket": txid,
            "state": row.get("status"),
            "stateName": str(row.get("status") or "").upper(),
            "filledVolume": _num(row.get("vol_exec")),
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


__all__ = ["KrakenAdapter", "sign_request"]
