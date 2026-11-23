"""MetaTrader 4 (and generic) HTTP bridge adapter: reads AND execution.

MT4 has no supported local Python IPC, so the supported shape is a small bridge
the operator already runs next to the terminal. The bridge owns credentials and
the terminal connection; Eclipse speaks this JSON contract:

    GET  {bridgeUrl}/account
    GET  {bridgeUrl}/positions
    GET  {bridgeUrl}/orders
    GET  {bridgeUrl}/orders/{ticket}
    GET  {bridgeUrl}/quote?symbol=EURUSD            (may include volumeMin/
                                                     volumeStep/volumeMax/
                                                     contractSize)
    GET  {bridgeUrl}/deals?from=<epoch_ms>&to=<epoch_ms>
    GET  {bridgeUrl}/symbol?symbol=EURUSD           (optional)

    POST {bridgeUrl}/orders                         {symbol, side, volume,
                                                     type, price, stopLoss,
                                                     takeProfit, magic,
                                                     comment, deviation}
    POST {bridgeUrl}/orders/{ticket}/cancel
    POST {bridgeUrl}/orders/{ticket}/modify         {price?, stopLoss?,
                                                     takeProfit?}
    GET  {bridgeUrl}/orders/{ticket}/status
    POST {bridgeUrl}/positions/{ticket}/close       {volume?}
    POST {bridgeUrl}/positions/{ticket}/modify      {stopLoss?, takeProfit?}

Every response is JSON. Lists may be a bare array or wrapped as
`{"positions": [...]}`, `{"orders": [...]}` or `{"deals": [...]}`. A write
result is `{ok, ticket, price, retcode, retcodeName, reasons, message}`. A bridge
that answers 404/405 on a write endpoint is a bridge that does not implement
execution: the adapter returns the named refusal `BRIDGE_EXECUTION_UNSUPPORTED`
instead of pretending or crashing. The adapter never invents a fill, a cancel or
a bridge-reported volume rule: an unreported limit stays 0 (no rule), never a
guessed step or size.

`bridgeSecretRef` is a NAME (an environment variable, or a `secret://...` vault
reference), never a value; when set it is sent as `Authorization: Bearer <value>`.
The account's `mode` is mapped to the trade vocabulary (`demo`/`contest` -> demo,
`real`/`live` -> real) and stays `unknown` when the bridge omits it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_MT4_BRIDGE
from .orders import build_preview, build_result, step_volume

_TIMEOUT_SECONDS = 10
_MAX_BODY_BYTES = 2_000_000
#: A write endpoint the bridge does not implement. Never a fabricated result.
_UNSUPPORTED = object()


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ticket(value: Any) -> Any:
    """A venue id: numeric tickets stay ints, an opaque id stays text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = _text(value)
    if text.isdigit() and not (len(text) > 1 and text[0] == "0"):
        return int(text)
    return text


def _reasons(row: dict[str, Any]) -> list[str]:
    reasons = row.get("reasons")
    if isinstance(reasons, list):
        return [str(item) for item in reasons if str(item)]
    message = row.get("message") or row.get("reason")
    return [str(message)] if message else []


def _account_mode(value: Any) -> str:
    """The bridge's trade mode in the trade vocabulary; never invented."""
    text = _text(value).lower()
    if text in ("demo", "contest"):
        return "demo"
    if text in ("real", "live"):
        return "real"
    return "unknown"


def _resolve_secret(config: dict[str, Any]) -> Optional[str]:
    ref = _text(config.get("bridgeSecretRef"))
    if not ref:
        return None
    if ref.startswith("secret://"):
        try:
            from ...security.secrets import build_default_secrets_manager

            return build_default_secrets_manager().resolve(
                ref,
                consumer="trade",
                action="read",
                resource="broker_bridge",
                require_grant=False,
            )
        except Exception as exc:  # noqa: BLE001 - a missing secret is a named refusal
            raise AdapterUnavailable(
                "BRIDGE_SECRET",
                f"the bridge secret reference {ref!r} could not be resolved",
            ) from exc
    # A bare name is an environment variable name. The value is never stored in
    # the connection config.
    value = os.environ.get(ref)
    if value is None:
        raise AdapterUnavailable(
            "BRIDGE_SECRET",
            f"environment variable {ref!r} for the bridge secret is not set",
        )
    return value


def _base_url(config: dict[str, Any]) -> str:
    raw = _text(config.get("bridgeUrl")).rstrip("/")
    if not raw:
        raise AdapterUnavailable(
            "BRIDGE_CONFIG",
            "bridgeUrl is required (for example http://127.0.0.1:8090)",
        )
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AdapterUnavailable("BRIDGE_CONFIG", "bridgeUrl must be an http(s) URL")
    return raw


class MT4BridgeAdapter(TradingAdapter):
    kind = KIND_MT4_BRIDGE

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        # The adapter itself is stdlib-only. Whether a bridge answers is a
        # connection-level fact, reported by `probe()`.
        return True, None

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        *,
        execution: bool = False,
    ) -> Any:
        url = _base_url(self.config) + path
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        headers = {"Accept": "application/json"}
        secret = _resolve_secret(self.config)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=data, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            if execution and exc.code in (404, 405):
                # A bridge may answer a write with a structured refusal instead
                # of a bare 404; honour that body, otherwise the bridge simply
                # does not implement execution.
                try:
                    payload = json.loads(exc.read() or b"{}")
                except ValueError:
                    payload = {}
                if (
                    exc.code == 404
                    and isinstance(payload, dict)
                    and ("retcode" in payload or "ok" in payload or payload.get("reasons")
                         or payload.get("reason") or payload.get("message"))
                ):
                    return payload
                return _UNSUPPORTED
            if not execution and exc.code == 404:
                return None
            raise AdapterUnavailable(
                "BRIDGE_HTTP",
                f"the MT4 bridge returned HTTP {exc.code}",
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterUnavailable(
                "BRIDGE_UNREACHABLE",
                f"the MT4 bridge at {_base_url(self.config)} is unreachable",
            ) from exc
        except TimeoutError as exc:
            raise AdapterUnavailable("BRIDGE_TIMEOUT", "the MT4 bridge timed out") from exc
        try:
            return json.loads(raw or b"{}")
        except ValueError as exc:
            raise AdapterUnavailable(
                "BRIDGE_BAD_JSON",
                "the MT4 bridge did not return valid JSON",
            ) from exc

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params)

    def _write(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("POST", path, body=body, execution=True)

    @staticmethod
    def _list(payload: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    @staticmethod
    def _unsupported(ticket: Any, action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "ticket": ticket,
            "retcode": None,
            "retcodeName": "BRIDGE_EXECUTION_UNSUPPORTED",
            "reasons": [
                f"the MT4 bridge answered 404/405 for {action}: it does not "
                "implement the execution contract"
            ],
        }

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
                "terminal": {"bridge": _text(self.config.get("bridgeUrl"))},
                "account": None,
                "warnings": [],
            }
        return {
            "available": True,
            "connected": True,
            "code": None,
            "reason": None,
            "terminal": {"bridge": _text(self.config.get("bridgeUrl"))},
            "account": account,
            "warnings": [],
        }

    def account(self) -> dict[str, Any]:
        payload = self._get("/account")
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no account object")
        row = payload.get("account") if isinstance(payload.get("account"), dict) else payload
        raw_mode = _text(row.get("mode"))
        return {
            "login": _int(row.get("login")),
            "mode": _account_mode(raw_mode),
            "modeRaw": raw_mode,
            "server": _text(row.get("server")),
            "company": _text(row.get("company") or "MT4 bridge"),
            "currency": _text(row.get("currency") or "USD"),
            "balance": _num(row.get("balance")),
            "equity": _num(row.get("equity")),
            "margin": _num(row.get("margin")),
            "marginFree": _num(row.get("marginFree")),
            "marginLevel": _num(row.get("marginLevel")),
            "profit": _num(row.get("profit")),
            "leverage": _num(row.get("leverage")),
            "marginMode": _text(row.get("marginMode") or "unknown"),
            "tradeAllowed": bool(row.get("tradeAllowed", False)),
            "expertAllowed": bool(row.get("expertAllowed", False)),
        }

    def positions(self) -> list[dict[str, Any]]:
        rows = self._list(self._get("/positions"), "positions")
        return [
            {
                # The MT4 position ticket is the id the close/modify paths use.
                "ticket": _ticket(row.get("ticket")),
                "symbol": _text(row.get("symbol")).upper(),
                "side": _text(row.get("side")).lower(),
                "volume": _num(row.get("volume")),
                "openPrice": _num(row.get("openPrice")),
                "currentPrice": _num(row.get("currentPrice")),
                "stopLoss": None if row.get("stopLoss") in (None, "", 0) else _num(row.get("stopLoss")),
                "takeProfit": None if row.get("takeProfit") in (None, "", 0) else _num(row.get("takeProfit")),
                "profit": _num(row.get("profit")),
                "swap": _num(row.get("swap")),
                "magic": _int(row.get("magic")),
                "comment": _text(row.get("comment")),
                "openedAt": _int(row.get("openedAt")),
            }
            for row in rows
        ]

    def orders(self) -> list[dict[str, Any]]:
        rows = self._list(self._get("/orders"), "orders")
        return [
            {
                # The full venue order id; numeric tickets round-trip as ints.
                "ticket": _ticket(row.get("ticket")),
                "symbol": _text(row.get("symbol")).upper(),
                "type": _text(row.get("type")),
                "volumeCurrent": _num(row.get("volumeCurrent")),
                "openPrice": _num(row.get("openPrice")),
                "currentPrice": _num(row.get("currentPrice")),
                "stopLoss": None if row.get("stopLoss") in (None, "", 0) else _num(row.get("stopLoss")),
                "takeProfit": None if row.get("takeProfit") in (None, "", 0) else _num(row.get("takeProfit")),
                "state": _text(row.get("state")),
                "magic": _int(row.get("magic")),
                "comment": _text(row.get("comment")),
                "placedAt": _int(row.get("placedAt")),
            }
            for row in rows
        ]

    def quote(self, symbol: str) -> dict[str, Any]:
        requested = _text(symbol)
        payload = self._get("/quote", {"symbol": requested})
        if not isinstance(payload, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no quote object")
        row = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
        return {
            "symbol": _text(row.get("symbol") or requested).upper(),
            "requestedSymbol": requested,
            "bid": _num(row.get("bid")),
            "ask": _num(row.get("ask")),
            "last": _num(row.get("last")),
            "spreadPoints": None if row.get("spreadPoints") is None else _num(row.get("spreadPoints")),
            "digits": _int(row.get("digits")),
            "point": _num(row.get("point")),
            "stopsLevelPoints": _int(row.get("stopsLevelPoints")),
            # Optional symbol facts. 0 = the bridge did not report them; the
            # adapter then applies no lot rule rather than inventing one.
            "volumeMin": _num(row.get("volumeMin")),
            "volumeMax": _num(row.get("volumeMax")),
            "volumeStep": _num(row.get("volumeStep")),
            "contractSize": _num(row.get("contractSize")),
            "serverTimeMs": _int(row.get("serverTimeMs")) or int(time.time() * 1000),
            "provider": "mt4_bridge",
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        requested = _text(symbol)
        payload = self._get("/symbol", {"symbol": requested})
        if not isinstance(payload, dict):
            raise AdapterUnavailable(
                "SYMBOL_INFO_NOT_SUPPORTED",
                "this MT4 bridge does not implement GET /symbol?symbol=...",
            )
        row = payload.get("symbol") if isinstance(payload.get("symbol"), dict) else payload
        if not row:
            raise AdapterUnavailable(
                "SYMBOL_INFO_NOT_SUPPORTED",
                "this MT4 bridge returned no symbol facts",
            )
        tick = _num(row.get("tickSize") or row.get("point"))
        return {
            "symbol": _text(row.get("symbol") or requested).upper(),
            "requestedSymbol": requested,
            "description": _text(row.get("description")),
            "path": _text(row.get("path")),
            "tradeMode": _text(row.get("tradeMode") or "unknown"),
            "digits": _int(row.get("digits")),
            "point": _num(row.get("point")),
            "tickSize": tick,
            "tickValue": _num(row.get("tickValue")),
            "currencyBase": _text(row.get("currencyBase")),
            "currencyProfit": _text(row.get("currencyProfit")),
            "currencyMargin": _text(row.get("currencyMargin")),
            "contractSize": _num(row.get("contractSize")),
            "volumeMin": _num(row.get("volumeMin")),
            "volumeMax": _num(row.get("volumeMax")),
            "volumeStep": _num(row.get("volumeStep")),
            "stopsLevelPoints": _int(row.get("stopsLevelPoints")),
            "freezeLevelPoints": _int(row.get("freezeLevelPoints")),
            "spreadPoints": None if row.get("spreadPoints") is None else _num(row.get("spreadPoints")),
            "sessionsAvailable": bool(row.get("sessionsAvailable", False)),
            "sessionNote": "the bridge did not report trading sessions",
            "retrievedAt": time.time(),
            "provider": "mt4_bridge",
        }

    # ── execution ────────────────────────────────────────────────────────────
    def order_preview(self, intent: Any) -> dict[str, Any]:
        """Side-effect free: reads account/quote/positions, posts nothing."""
        account = self.account()
        quote = self.quote(intent.symbol)
        positions = self.positions()
        step = _num(quote.get("volumeStep"))
        minimum = _num(quote.get("volumeMin"))
        maximum = _num(quote.get("volumeMax"))
        volume, reasons = step_volume(intent.volume, step, minimum, maximum)
        bid = _num(quote.get("bid"))
        ask = _num(quote.get("ask"))
        price = (
            float(intent.price)
            if str(intent.order_type) in ("limit", "stop") and intent.price
            else (ask if str(intent.side) == "buy" else bid)
        )
        contract = _num(quote.get("contractSize")) or 1.0
        notional = float(volume) * contract * float(price or 0)
        leverage = _num(account.get("leverage"))
        margin = notional / leverage if leverage > 0 else notional
        return build_preview(
            intent,
            account=account,
            quote=quote,
            volume=volume,
            price=price,
            contract_size=contract,
            margin=margin,
            open_positions=len(positions),
            reasons=reasons,
            digits=_int(quote.get("digits")),
            volume_step=step,
            volume_min=minimum,
            volume_max=maximum,
            extra={"venue": "mt4_bridge"},
        )

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
        volume = float(preview.get("volume") or intent.volume)
        body = {
            "symbol": str(intent.symbol).upper(),
            "side": str(intent.side),
            "volume": volume,
            "type": str(intent.order_type),
            "price": float(intent.price) if intent.price else 0.0,
            "stopLoss": intent.stop_loss,
            "takeProfit": intent.take_profit,
            "magic": int(intent.magic or 0),
            "comment": str(intent.comment or ""),
            "deviation": int(intent.deviation_points or 0),
        }
        row = self._write("/orders", body)
        if row is _UNSUPPORTED:
            result = self._unsupported(None, "order submission")
            return build_result(
                ok=False, status="REFUSED", symbol=intent.symbol, side=intent.side,
                order_type=intent.order_type, volume=volume,
                price=preview.get("price") or 0.0, retcode=None,
                retcode_name=result["retcodeName"], reasons=result["reasons"],
                account=preview.get("account") or {},
            )
        if not isinstance(row, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no order result")
        ok = bool(row.get("ok"))
        retcode = row.get("retcode")
        if ok:
            status = "FILLED" if _int(retcode, 10009) == 10009 else "placed"
        else:
            status = "REJECTED"
        return build_result(
            ok=ok,
            status=status,
            symbol=row.get("symbol") or intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            fill_volume=_num(row.get("volume")) if row.get("volume") is not None else None,
            price=_num(row.get("price")) or preview.get("price") or 0.0,
            ticket=_ticket(row.get("ticket")) if row.get("ticket") not in (None, "") else None,
            retcode=retcode,
            retcode_name=_text(row.get("retcodeName")) or ("ACCEPTED" if ok else "REJECTED"),
            comment=_text(row.get("comment")) or str(intent.comment or ""),
            reasons=_reasons(row),
            account=preview.get("account") or {},
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        ticket_id = _ticket(ticket)
        row = self._write(f"/orders/{urllib.parse.quote(str(ticket_id))}/cancel")
        if row is _UNSUPPORTED:
            return self._unsupported(ticket_id, "order cancellation")
        if not isinstance(row, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no cancel result")
        ok = bool(row.get("ok"))
        return {
            "ok": ok,
            "ticket": ticket_id,
            "retcode": row.get("retcode"),
            "retcodeName": _text(row.get("retcodeName")) or ("CANCELLED" if ok else "CANCEL_REJECTED"),
            "reasons": _reasons(row) or ([] if ok else ["the bridge did not cancel the order"]),
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        magic = int(magic or 0)
        if magic <= 0:
            return {
                "ok": False, "cancelled": 0, "failed": 0, "results": [],
                "retcodeName": "MAGIC_REQUIRED",
                "reasons": ["a non-zero magic is required; a blind cancel could hit manual orders"],
            }
        rows = self._list(self._get("/orders"), "orders")
        targets = [row for row in rows if _int(row.get("magic")) == magic]
        results = [self.cancel_order(row.get("ticket")) for row in targets]
        cancelled = sum(1 for item in results if item.get("ok"))
        return {
            "ok": all(item.get("ok") for item in results),
            "cancelled": cancelled,
            "failed": len(results) - cancelled,
            "results": results,
            "retcodeName": "CANCELLED" if cancelled else "NO_ORDERS",
            "reasons": [],
        }

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        ticket_id = _ticket(ticket)
        rows = [row for row in self.positions() if str(row.get("ticket")) == str(ticket_id)]
        if not rows:
            return {
                "ok": False, "ticket": ticket_id, "retcode": None,
                "retcodeName": "POSITION_NOT_FOUND",
                "reasons": [f"no open position with ticket {ticket_id}"],
            }
        position = rows[0]
        held = abs(_num(position.get("volume")))
        wanted = held if volume is None else _num(volume)
        if wanted <= 0:
            return {
                "ok": False, "ticket": ticket_id, "symbol": position.get("symbol"),
                "retcode": None, "retcodeName": "INVALID_VOLUME",
                "reasons": ["the close volume must be greater than zero"],
            }
        if wanted - held > 1e-8:
            return {
                "ok": False, "ticket": ticket_id, "symbol": position.get("symbol"),
                "retcode": None, "retcodeName": "INVALID_VOLUME",
                "reasons": [f"cannot close {wanted:g} of a {held:g}-volume position"],
            }
        body = {"volume": wanted} if volume is not None else {}
        row = self._write(
            f"/positions/{urllib.parse.quote(str(ticket_id))}/close", body
        )
        if row is _UNSUPPORTED:
            return self._unsupported(ticket_id, "position close")
        if not isinstance(row, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no close result")
        ok = bool(row.get("ok"))
        remaining: list[dict[str, Any]] = []
        if ok:
            try:
                remaining = [
                    item for item in self.positions()
                    if str(item.get("ticket")) == str(ticket_id)
                ]
            except AdapterUnavailable:
                remaining = [{"error": "position re-read failed"}]
        return {
            "ok": ok,
            "ticket": ticket_id,
            "symbol": _text(row.get("symbol")) or position.get("symbol"),
            "side": _text(row.get("side")),
            "volume": _num(row.get("volume")) or wanted,
            "price": _num(row.get("price")),
            "retcode": row.get("retcode"),
            "retcodeName": _text(row.get("retcodeName")) or ("CLOSED" if ok else "CLOSE_REJECTED"),
            "partial": wanted < held - 1e-8,
            "verified": bool(ok and not remaining),
            "reasons": _reasons(row) or ([] if ok else ["the bridge did not close the position"]),
        }

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        if stop_loss is None and take_profit is None:
            return {
                "ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "INVALID_REQUEST",
                "reasons": ["pass stopLoss and/or takeProfit"],
            }
        ticket_id = _ticket(ticket)
        if not any(str(row.get("ticket")) == str(ticket_id) for row in self.positions()):
            return {
                "ok": False, "ticket": ticket_id, "retcode": None,
                "retcodeName": "POSITION_NOT_FOUND",
                "reasons": [f"no open position with ticket {ticket_id}"],
            }
        body = {
            key: value for key, value in (("stopLoss", stop_loss), ("takeProfit", take_profit))
            if value is not None
        }
        row = self._write(
            f"/positions/{urllib.parse.quote(str(ticket_id))}/modify", body
        )
        if row is _UNSUPPORTED:
            return self._unsupported(ticket_id, "position modify")
        if not isinstance(row, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no modify result")
        ok = bool(row.get("ok"))
        verified = False
        if ok:
            try:
                current = next(
                    (item for item in self.positions()
                     if str(item.get("ticket")) == str(ticket_id)),
                    {},
                )
                verified = bool(current) and all(
                    current.get(field) is not None
                    and abs(_num(current.get(field)) - float(value)) < 1e-9
                    for field, value in (("stopLoss", stop_loss), ("takeProfit", take_profit))
                    if value is not None
                )
            except AdapterUnavailable:
                verified = False
        return {
            "ok": ok,
            "ticket": ticket_id,
            "stopLoss": _num(row.get("stopLoss"), _num(stop_loss)) if stop_loss is not None else None,
            "takeProfit": _num(row.get("takeProfit"), _num(take_profit)) if take_profit is not None else None,
            "retcode": row.get("retcode"),
            "retcodeName": _text(row.get("retcodeName")) or ("MODIFIED" if ok else "MODIFY_REJECTED"),
            "verified": verified,
            "reasons": _reasons(row) or ([] if ok else ["the bridge did not modify the position"]),
        }

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        if price is None and stop_loss is None and take_profit is None:
            return {
                "ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "INVALID_REQUEST",
                "reasons": ["pass price, stopLoss and/or takeProfit"],
            }
        ticket_id = _ticket(ticket)
        body = {
            key: value for key, value in (
                ("price", price), ("stopLoss", stop_loss), ("takeProfit", take_profit)
            ) if value is not None
        }
        row = self._write(f"/orders/{urllib.parse.quote(str(ticket_id))}/modify", body)
        if row is _UNSUPPORTED:
            return self._unsupported(ticket_id, "order modify")
        if not isinstance(row, dict):
            raise AdapterUnavailable("BRIDGE_BAD_RESPONSE", "the bridge returned no modify result")
        ok = bool(row.get("ok"))
        verified = False
        if ok:
            try:
                current = next(
                    (item for item in self.orders()
                     if str(item.get("ticket")) == str(ticket_id)),
                    {},
                )
                checks = [
                    (current.get("openPrice"), price),
                    (current.get("stopLoss"), stop_loss),
                    (current.get("takeProfit"), take_profit),
                ]
                verified = bool(current) and all(
                    wanted is None or abs(_num(actual) - wanted) < 1e-9
                    for actual, wanted in checks
                )
            except AdapterUnavailable:
                verified = False
        return {
            "ok": ok,
            "ticket": ticket_id,
            "price": _num(row.get("price"), _num(price)) if price is not None else None,
            "retcode": row.get("retcode"),
            "retcodeName": _text(row.get("retcodeName")) or ("MODIFIED" if ok else "MODIFY_REJECTED"),
            "verified": verified,
            "reasons": _reasons(row) or ([] if ok else ["the bridge did not modify the order"]),
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        ticket_id = _ticket(ticket)
        quoted = urllib.parse.quote(str(ticket_id))
        payload = self._get(f"/orders/{quoted}/status")
        if isinstance(payload, dict):
            row = payload.get("order") if isinstance(payload.get("order"), dict) else payload
            if row.get("found") is False:
                return {"found": False, "ticket": ticket_id}
            if row:
                return self._status_dict(row, ticket_id)
        # Older bridges only expose the read endpoint; keep it working.
        payload = self._get(f"/orders/{quoted}")
        if not isinstance(payload, dict):
            return {"found": False, "ticket": ticket_id}
        row = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        if not row:
            return {"found": False, "ticket": ticket_id}
        return self._status_dict(row, ticket_id)

    @staticmethod
    def _status_dict(row: dict[str, Any], ticket: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "found": True,
            "ticket": _ticket(row.get("ticket")) if row.get("ticket") not in (None, "") else ticket,
            "state": _int(row.get("state")),
            "stateName": _text(row.get("stateName")),
        }
        if row.get("filledVolume") is not None:
            result["filledVolume"] = _num(row.get("filledVolume"))
        if row.get("avgPrice") is not None:
            result["avgPrice"] = _num(row.get("avgPrice"))
        return result

    # ── reconciliation ───────────────────────────────────────────────────────
    def _deals(self, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        payload = self._get("/deals", {"from": int(from_ms), "to": int(to_ms)})
        rows = self._list(payload, "deals")
        magic = _int(self.config.get("magic"))
        out: list[dict[str, Any]] = []
        for row in rows:
            symbol = _text(row.get("symbol")).upper()
            if not symbol:
                continue
            deal_magic = _int(row.get("magic"))
            out.append(
                {
                    "dealTicket": _int(row.get("dealTicket")),
                    "orderTicket": _int(row.get("orderTicket")) or None,
                    "positionTicket": _int(row.get("positionTicket")) or None,
                    "symbol": symbol,
                    "side": _text(row.get("side")).lower(),
                    "entry": _text(row.get("entry")).lower() or "unknown",
                    "volume": _num(row.get("volume")),
                    "price": _num(row.get("price")),
                    "commission": _num(row.get("commission")),
                    "swap": _num(row.get("swap")),
                    "profit": _num(row.get("profit")),
                    "magic": deal_magic,
                    "comment": _text(row.get("comment")),
                    "dealtAt": _int(row.get("dealtAt")),
                    "eclipseOwned": bool(magic and deal_magic == magic),
                }
            )
        return out

    def deals_since(self, since_ms: int) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        from_ms = int(since_ms or 0)
        if from_ms <= 0:
            # No cursor means an incremental read, not "everything since 1970".
            from_ms = now - 7 * 86_400_000
        return self._deals(from_ms, now + 2 * 86_400_000)

    def deals_window(self, days: int) -> list[dict[str, Any]]:
        safe_days = max(1, min(int(days or 7), 3650))
        now = int(time.time() * 1000)
        return self._deals(now - safe_days * 86_400_000, now + 2 * 86_400_000)


__all__ = ["MT4BridgeAdapter"]
