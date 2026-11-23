"""Interactive Brokers (Client Portal Web API) adapter: reads AND execution.

IBKR runs a local Client Portal Gateway that owns the SSO session. Eclipse reads
it over HTTPS and submits/cancels orders through it; it never logs in and never
stores a password. The connection config carries:

    baseUrl     default https://localhost:5000/v1/api
    accountId   the IBKR account (e.g. DU1234567)
    credentialRef NAME of an env var / secret:// containing the gateway cookie
                (`SESSION=...`) or JSON {"cookie": "..."}

Execution: market/limit/stop orders (`POST /iserver/account/{id}/orders`). The
gateway may answer with a confirmation QUESTION (`id` + `message`) instead of an
order id; Eclipse confirms exactly once (`POST /iserver/reply/{id}`) and refuses
with `IBKR_CONFIRMATION_REQUIRED` if a second question appears — it never loops.
Attached SL/TP are refused with `BRACKET_UNSUPPORTED`: CPAPI models protection as
separate orders, so an entry that claims a bracket would be a lie. CPAPI does not
echo an Eclipse ownership tag on `/iserver/account/orders`, so `cancel_all`
refuses with `OWNERSHIP_UNAVAILABLE` rather than cancel manual orders.

The gateway uses a self-signed certificate on loopback, so TLS verification is
relaxed ONLY for a loopback host. A remote baseUrl keeps normal verification.

Executions are normalized with FIFO into round-trip deals (IBKR's execution
stream has no position id). P&L is therefore Eclipse-computed, not a
broker-reported figure, until the CPAPI exposes a reliable per-execution P&L.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from ..model import KIND_IBKR
from .credentials import resolve_secret_value, validate_http_url
from .fifo import Execution, normalize_fifo
from .orders import build_preview, build_result, client_order_id

_TIMEOUT = 20
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_BRACKET_REASON = (
    "the IBKR Client Portal API attaches a stop/target as a separate order; "
    "submit the entry alone and place the protection as its own order"
)


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return fallback
    return number if number == number and number not in (float("inf"), float("-inf")) else fallback


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return fallback


def _ticket(value: Any) -> Any:
    """An opaque venue id: numeric ids stay ints, everything else stays text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip()
    if text.isdigit() and not (len(text) > 1 and text[0] == "0"):
        return int(text)
    return text


def _ms(value: Any) -> int:
    if isinstance(value, (int, float)) and value > 0:
        number = float(value)
        return int(number if number > 10_000_000_000 else number * 1000)
    text = str(value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y%m%d-%H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return 0


def _messages(row: dict[str, Any]) -> list[str]:
    message = row.get("message")
    if isinstance(message, list):
        return [str(item) for item in message]
    return [str(message)] if message else []


class IBKRAdapter(TradingAdapter):
    kind = KIND_IBKR

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config)
        # symbol -> (conid, secType): one search per symbol per adapter instance.
        self._contracts: dict[str, tuple[int, str]] = {}

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        return True, None

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _base(self) -> str:
        return validate_http_url(
            str(self.config.get("baseUrl") or "https://localhost:5000/v1/api"),
            code="IBKR_CONFIG",
            label="IBKR gateway baseUrl",
        )

    def _cookie(self) -> str:
        raw = resolve_secret_value(
            str(self.config.get("credentialRef") or ""),
            code="IBKR_CREDENTIALS",
            label="IBKR gateway cookie",
        )
        if not raw:
            raise AdapterUnavailable(
                "IBKR_CREDENTIALS",
                "set credentialRef to the NAME of an env var or secret containing the gateway cookie",
            )
        text = raw.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise AdapterUnavailable("IBKR_CREDENTIALS", "the cookie JSON is invalid") from exc
            text = str(data.get("cookie") or data.get("session") or "")
        if not text:
            raise AdapterUnavailable("IBKR_CREDENTIALS", "the gateway cookie is empty")
        return text if "=" in text else f"SESSION={text}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        *,
        allow_missing: bool = False,
        retry: Optional[bool] = None,
    ) -> Any:
        """One gateway call.

        Only GET is retried: an order POST/DELETE is sent exactly once, because a
        retried mutation can double-submit after an ambiguous 5xx.
        """
        method = method.upper()
        url = self._base() + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Cookie": self._cookie(),
            "X-Requested-With": "XMLHttpRequest",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers, data=data, method=method)
        parsed = urllib.parse.urlparse(self._base())
        host = parsed.hostname or ""
        kwargs: dict[str, Any] = {}
        if parsed.scheme == "https" and host in _LOOPBACK:
            # The local gateway's certificate is self-signed; only loopback is
            # allowed to skip verification.
            kwargs["context"] = ssl._create_unverified_context()  # noqa: SLF001
        attempts = 3 if (retry is not None and retry) or (retry is None and method == "GET") else 1
        raw: bytes | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=_TIMEOUT, **kwargs) as response:
                    raw = response.read(4_000_000)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    retry_after = (exc.headers.get("Retry-After") if exc.headers else None) or ""
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 2**attempt
                    time.sleep(min(max(delay, 0.5), 5.0))
                    continue
                if exc.code in (401, 403):
                    raise AdapterUnavailable(
                        "IBKR_UNAUTHORIZED",
                        "IBKR rejected the gateway cookie (re-authenticate the Client Portal)",
                    ) from exc
                if exc.code == 404 and (allow_missing or method == "GET"):
                    return None
                detail = ""
                try:
                    payload = json.loads(exc.read() or b"{}")
                    if isinstance(payload, dict):
                        detail = str(payload.get("error") or payload.get("message") or "")
                except Exception:  # noqa: BLE001 - the body is advisory
                    detail = ""
                raise AdapterUnavailable(
                    "IBKR_HTTP",
                    f"IBKR gateway returned HTTP {exc.code}" + (f": {detail}" if detail else ""),
                ) from exc
            except urllib.error.URLError as exc:
                raise AdapterUnavailable(
                    "IBKR_UNREACHABLE",
                    "the IBKR Client Portal Gateway is not reachable; start it and log in",
                ) from exc
            except TimeoutError as exc:
                raise AdapterUnavailable("IBKR_TIMEOUT", "the IBKR gateway timed out") from exc
        if raw is None:
            raise AdapterUnavailable("IBKR_RETRY_EXHAUSTED", "the IBKR gateway did not answer after retries")
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise AdapterUnavailable("IBKR_BAD_JSON", "the IBKR gateway returned invalid JSON") from exc

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params)

    def _account_id(self) -> str:
        configured = str(self.config.get("accountId") or "").strip()
        if configured:
            return configured
        data = self._get("/iserver/accounts") or {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, list) and accounts:
            return str(accounts[0])
        raise AdapterUnavailable("IBKR_NO_ACCOUNT", "no IBKR account is available in this gateway")

    # ── BrokerAdapter ────────────────────────────────────────────────────────
    def probe(self) -> dict[str, Any]:
        try:
            status = self._get("/iserver/auth/status") or {}
            if isinstance(status, dict) and status.get("authenticated") is False:
                raise AdapterUnavailable(
                    "IBKR_AUTH_EXPIRED",
                    "the IBKR Client Portal session is not authenticated; log in to the gateway again",
                )
            account = self.account()
        except AdapterUnavailable as exc:
            return {
                "available": True,
                "connected": False,
                "code": exc.code,
                "reason": exc.reason,
                "terminal": {"gateway": self._base()},
                "account": None,
                "warnings": [],
            }
        return {
            "available": True,
            "connected": True,
            "code": None,
            "reason": None,
            "terminal": {"gateway": self._base()},
            "account": account,
            "warnings": [],
        }

    def account(self) -> dict[str, Any]:
        account_id = self._account_id()
        row = self._get(f"/portfolio/{urllib.parse.quote(account_id)}/summary") or {}
        if not isinstance(row, dict):
            raise AdapterUnavailable("IBKR_BAD_RESPONSE", "IBKR returned no account summary")
        return {
            "login": 0,
            "armRef": account_id,
            # Trade vocabulary is demo/real. A DU-prefixed paper account is a
            # demo account; every other IBKR account is a real one.
            "mode": "demo" if account_id.upper().startswith("DU") else "real",
            "server": "ibkr",
            "company": "Interactive Brokers",
            "currency": str(row.get("currency") or "USD"),
            "balance": _num(row.get("availablefunds")),
            "equity": _num(row.get("netliquidation")),
            "margin": _num(row.get("initialmargin")),
            "marginFree": _num(row.get("availablefunds")),
            "marginLevel": 0.0,
            "profit": _num(row.get("unrealizedpnl")),
            "leverage": 0.0,
            "marginMode": "unknown",
            "tradeAllowed": True,
            "expertAllowed": True,
        }

    def positions(self) -> list[dict[str, Any]]:
        account_id = self._account_id()
        rows = self._get(f"/portfolio/{urllib.parse.quote(account_id)}/positions/0") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            qty = _num(row.get("position"))
            out.append(
                {
                    # The conid is what CPAPI can close: it identifies the contract
                    # and the close path re-reads the position by it.
                    "ticket": str(_int(row.get("conid"))),
                    "symbol": str(row.get("contractDesc") or row.get("ticker") or "").upper(),
                    "side": "buy" if qty >= 0 else "sell",
                    "volume": abs(qty),
                    "openPrice": _num(row.get("avgCost") or row.get("avgPrice")),
                    "currentPrice": _num(row.get("mktPrice")),
                    "stopLoss": None,
                    "takeProfit": None,
                    "profit": _num(row.get("unrealizedPnl")),
                    "swap": 0.0,
                    "magic": 0,
                    "comment": "ibkr (openedAt is first seen)",
                    # The CPAPI position row has no open time; the execution is
                    # in the deal window. Never date an unknown as 1970.
                    "openedAt": int(time.time() * 1000),
                }
            )
        return out

    def orders(self) -> list[dict[str, Any]]:
        data = self._get("/iserver/account/orders") or {}
        rows = data.get("orders") if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            out.append(
                {
                    # Full venue order id: numeric ids round-trip as ints, an
                    # opaque/zero-padded id stays exactly as the venue sent it.
                    "ticket": _ticket(row.get("orderId") or row.get("order_id")),
                    "symbol": str(row.get("ticker") or "").upper(),
                    "type": str(row.get("orderType") or ""),
                    "volumeCurrent": _num(row.get("remainingQuantity")),
                    "openPrice": _num(row.get("price")),
                    "currentPrice": 0.0,
                    "stopLoss": _num(row.get("stop_price")) or None,
                    "takeProfit": None,
                    "state": str(row.get("status") or ""),
                    "magic": 0,
                    "comment": "ibkr",
                    "placedAt": _ms(row.get("lastExecutionTime_r") or row.get("orderTime")),
                }
            )
        return out

    def _contract(self, symbol: str) -> tuple[int, str]:
        key = str(symbol or "").strip().upper()
        if key in self._contracts:
            return self._contracts[key]
        rows = self._get("/iserver/secdef/search", {"symbol": key}) or []
        for row in rows if isinstance(rows, list) else []:
            conid = _int(row.get("conid"))
            if not conid:
                continue
            sec_type = str(row.get("assetClass") or "").upper()
            if not sec_type:
                sections = row.get("sections")
                if isinstance(sections, list) and sections and isinstance(sections[0], dict):
                    sec_type = str(sections[0].get("secType") or "").upper()
            contract = (conid, sec_type or "STK")
            self._contracts[key] = contract
            return contract
        raise AdapterUnavailable("IBKR_NO_SYMBOL", f"IBKR has no contract for {symbol}")

    def _conid(self, symbol: str) -> int:
        return self._contract(symbol)[0]

    def quote(self, symbol: str) -> dict[str, Any]:
        requested = str(symbol or "").strip()
        conid = self._conid(requested)
        rows = self._get(
            "/iserver/marketdata/snapshot",
            {"conids": conid, "fields": "31,84,86"},
        ) or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        bid = _num(row.get("84"))
        ask = _num(row.get("86"))
        last = _num(row.get("31"))
        return {
            "symbol": requested.upper(),
            "requestedSymbol": requested,
            "bid": bid,
            "ask": ask,
            "last": last or bid or ask,
            "spreadPoints": None,
            "digits": 4,
            "point": 0.0001,
            "stopsLevelPoints": 0,
            "serverTimeMs": int(time.time() * 1000),
            "provider": "ibkr",
        }

    # ── execution ────────────────────────────────────────────────────────────
    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("orders")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            return [payload]
        return []

    @staticmethod
    def _is_question(row: dict[str, Any]) -> bool:
        """A confirmation question, not an order acknowledgement.

        CPAPI questions carry `id`, a non-empty `message`/`messageIds`, and no
        `order_status`; an accepted order carries `order_id`/`order_status`.
        """
        return (
            bool(row.get("id"))
            and not row.get("order_status")
            and not (row.get("order_id") or row.get("orderId"))
            and bool(row.get("message") or row.get("messageIds"))
        )

    @staticmethod
    def _order_id(row: dict[str, Any]) -> str:
        for key in ("order_id", "orderId", "id"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def _submit(self, path: str, body: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """POST an order and answer AT MOST ONE confirmation question.

        Returns `("order" | "refused", row, questions)`. A second question is a
        refusal: the order was not placed, and an automatic loop must never be
        used to push a human decision through.
        """
        questions: list[dict[str, Any]] = []
        rows = self._rows(self._request("POST", path, body=body))
        question = next((row for row in rows if self._is_question(row)), None)
        if question is None:
            return "order", (rows[0] if rows else {}), questions
        questions.append(question)
        reply_id = str(question.get("id") or "")
        if not reply_id:
            return "refused", question, questions
        answered = self._request(
            "POST", f"/iserver/reply/{urllib.parse.quote(reply_id)}", body={"confirmed": True}
        )
        rows = self._rows(answered)
        again = next((row for row in rows if self._is_question(row)), None)
        if again is not None:
            questions.append(again)
            return "refused", again, questions
        return "order", (rows[0] if rows else {}), questions

    def order_preview(self, intent: Any) -> dict[str, Any]:
        """Side-effect free: reads the account/quote, never posts an order."""
        account = self.account()
        quote = self.quote(intent.symbol)
        positions = self.positions()
        bid = _num(quote.get("bid"))
        ask = _num(quote.get("ask"))
        price = (
            float(intent.price)
            if str(intent.order_type) in ("limit", "stop") and intent.price
            else (ask if str(intent.side) == "buy" else bid)
        )
        reasons: list[str] = []
        if intent.stop_loss is not None or intent.take_profit is not None:
            reasons.append(_BRACKET_REASON)
        return build_preview(
            intent,
            account=account,
            quote=quote,
            volume=float(intent.volume),
            price=price,
            contract_size=1.0,
            open_positions=len(positions),
            reasons=reasons,
            digits=4,
            extra={"venue": "ibkr", "conid": self._conid(intent.symbol)},
        )

    def _order_body(self, intent: Any, volume: float) -> dict[str, Any]:
        conid, sec_type = self._contract(intent.symbol)
        order_type = {"market": "MKT", "limit": "LMT", "stop": "STP"}.get(
            str(intent.order_type), "MKT"
        )
        quantity: Any = int(volume) if float(volume).is_integer() else float(volume)
        body: dict[str, Any] = {
            "conid": conid,
            "secType": sec_type,
            "orderType": order_type,
            "side": str(intent.side).upper(),
            "quantity": quantity,
            "tif": "DAY" if intent.time_in_force == "day" else "GTC",
            "outsideRth": True,
            # Advisory ownership tag for a human in the portal; CPAPI does not
            # echo it back on the orders read, so it is not used for ownership.
            "cOID": client_order_id(intent.magic, intent.digest),
        }
        if order_type == "LMT":
            body["price"] = float(intent.price)
        elif order_type == "STP":
            # CPAPI reads `price` as the stop trigger; TWS-style clients read
            # `auxPrice`. Both carry the same trigger so either is honoured.
            body["price"] = float(intent.price)
            body["auxPrice"] = float(intent.price)
        return body

    def submit_order(self, intent: Any) -> dict[str, Any]:
        if intent.stop_loss is not None or intent.take_profit is not None:
            return build_result(
                ok=False,
                status="REFUSED",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                volume=float(intent.volume),
                price=float(intent.price or 0),
                retcode=None,
                retcode_name="BRACKET_UNSUPPORTED",
                reasons=[_BRACKET_REASON],
                account=self.account(),
            )
        preview = self.order_preview(intent)
        reasons = list(preview.get("reasons") or [])
        if reasons:
            return build_result(
                ok=False,
                status="REFUSED",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                volume=preview.get("volume") or intent.volume,
                price=preview.get("price") or 0.0,
                retcode=None,
                retcode_name="PREVIEW_REJECTED",
                reasons=reasons,
                account=preview.get("account") or {},
            )
        volume = float(preview.get("volume") or intent.volume)
        account_id = self._account_id()
        path = f"/iserver/account/{urllib.parse.quote(account_id)}/orders"
        kind, row, questions = self._submit(
            path, {"orders": [self._order_body(intent, volume)]}
        )
        if kind == "refused":
            return build_result(
                ok=False,
                status="REFUSED",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                volume=volume,
                price=preview.get("price") or 0.0,
                retcode_name="IBKR_CONFIRMATION_REQUIRED",
                reasons=[
                    "the IBKR gateway asked a second confirmation question; the order "
                    "was NOT placed. Confirm it in the Client Portal yourself."
                ],
                account=preview.get("account") or {},
                questions=[_messages(item) for item in questions],
            )
        order_id = self._order_id(row)
        if not order_id:
            detail = " ".join(_messages(row)) or str(row.get("error") or "")
            return build_result(
                ok=False,
                status="REJECTED",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                volume=volume,
                price=preview.get("price") or 0.0,
                retcode_name="IBKR_REJECTED",
                reasons=[detail or "the IBKR gateway rejected the order without an order id"],
                account=preview.get("account") or {},
            )
        state = str(row.get("order_status") or row.get("orderStatus") or "").lower()
        filled = state == "filled"
        return build_result(
            ok=True,
            status="FILLED" if filled else "placed",
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            volume=volume,
            fill_volume=_num(row.get("filledQuantity")) if filled else None,
            price=_num(row.get("avgPrice") or row.get("averagePrice")) or preview.get("price") or 0.0,
            ticket=order_id,
            # 10008 is the trade vocabulary's "accepted, not filled" code; an
            # IBKR "Submitted" order is placed, never reported as a fill.
            retcode=10009 if filled else 10008,
            retcode_name=state.upper() or ("FILLED" if filled else "SUBMITTED"),
            comment=str(intent.comment or ""),
            reasons=[],
            account=preview.get("account") or {},
            confirmed=bool(questions),
        )

    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        account_id = self._account_id()
        row = self._request(
            "DELETE",
            f"/iserver/account/{urllib.parse.quote(account_id)}/order/{urllib.parse.quote(order_id)}",
            allow_missing=True,
        )
        if row is None:
            return {
                "ok": False, "ticket": order_id, "retcode": 404,
                "retcodeName": "ORDER_NOT_FOUND", "reasons": [f"IBKR has no order {order_id}"],
            }
        detail = str((row.get("error") if isinstance(row, dict) else "") or "")
        if detail:
            return {
                "ok": False, "ticket": order_id, "retcode": None,
                "retcodeName": "CANCEL_REJECTED", "reasons": [detail],
            }
        # CPAPI answers with "request was submitted"; the final state is only
        # known from order_status, so `verified` is False rather than a claim.
        return {
            "ok": True, "ticket": order_id, "retcode": 200,
            "retcodeName": "CANCEL_REQUESTED", "verified": False,
            "note": "the gateway accepted the cancellation request; confirm with order_status",
            "reasons": [],
        }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        return {
            "ok": False,
            "cancelled": 0,
            "failed": 0,
            "results": [],
            "retcodeName": "OWNERSHIP_UNAVAILABLE",
            "reasons": [
                "CPAPI order rows carry no Eclipse ownership tag that the orders "
                "read echoes back, so cancelling by magic could cancel manual orders"
            ],
        }

    def _live_order(self, ticket: Any) -> Optional[dict[str, Any]]:
        data = self._get("/iserver/account/orders") or {}
        rows = data.get("orders") if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("orderId") or row.get("order_id")) == str(ticket):
                return row
        return None

    def close_position(
        self, ticket: Any, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        """Close a position by conid: one opposite market order for its own size."""
        conid_text = str(ticket)
        rows = [row for row in self.positions() if str(row.get("ticket")) == conid_text]
        if not rows:
            return {
                "ok": False, "ticket": conid_text, "retcode": None,
                "retcodeName": "POSITION_NOT_FOUND",
                "reasons": [f"no open IBKR position with conid {conid_text}"],
            }
        position = rows[0]
        held = abs(_num(position.get("volume")))
        wanted = held if volume is None else _num(volume)
        if wanted <= 0 or wanted - held > 1e-8:
            return {
                "ok": False, "ticket": conid_text, "symbol": position.get("symbol"),
                "retcode": None, "retcodeName": "INVALID_VOLUME",
                "reasons": [f"cannot close {wanted:g} of a {held:g}-size position"],
            }
        conid = _int(conid_text) or self._conid(str(position.get("symbol") or ""))
        sec_type = self._contract(str(position.get("symbol") or ""))[1]
        side = "SELL" if str(position.get("side")) == "buy" else "BUY"
        body: dict[str, Any] = {
            "conid": conid,
            "secType": sec_type,
            "orderType": "MKT",
            "side": side,
            "quantity": int(wanted) if float(wanted).is_integer() else wanted,
            "tif": "DAY",
            "outsideRth": True,
        }
        account_id = self._account_id()
        kind, row, _questions = self._submit(
            f"/iserver/account/{urllib.parse.quote(account_id)}/orders",
            {"orders": [body]},
        )
        order_id = self._order_id(row) if kind == "order" else ""
        if kind == "refused" or not order_id:
            return {
                "ok": False, "ticket": conid_text, "symbol": position.get("symbol"),
                "retcode": None,
                "retcodeName": "IBKR_CONFIRMATION_REQUIRED" if kind == "refused" else "CLOSE_REJECTED",
                "reasons": [
                    "the IBKR gateway did not accept the closing order"
                    + (" (a second confirmation question appeared)" if kind == "refused" else "")
                ],
            }
        remaining: list[dict[str, Any]] = []
        try:
            remaining = [row for row in self.positions() if str(row.get("ticket")) == conid_text]
        except AdapterUnavailable:
            remaining = [{"error": "position re-read failed"}]
        return {
            "ok": True,
            "ticket": conid_text,
            "symbol": str(position.get("symbol") or ""),
            "side": side.lower(),
            "volume": wanted,
            "price": _num(position.get("currentPrice")),
            "ticketOrder": order_id,
            "retcode": 10008,
            "retcodeName": "CLOSE_SUBMITTED",
            "partial": wanted < held - 1e-8,
            "verified": not remaining,
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
                "CPAPI attaches protection to a position as separate working orders "
                "that the orders read does not link back to the position; edit or "
                "place the stop order itself instead"
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
                "reasons": [_BRACKET_REASON],
            }
        if price is None:
            return {
                "ok": False, "ticket": ticket, "retcode": None,
                "retcodeName": "INVALID_REQUEST", "reasons": ["pass a new price"],
            }
        order_id = str(ticket)
        live = self._live_order(order_id)
        if live is None:
            return {
                "ok": False, "ticket": order_id, "retcode": 404,
                "retcodeName": "ORDER_NOT_FOUND",
                "reasons": [f"IBKR has no working order {order_id}"],
            }
        conid = _int(live.get("conid")) or self._conid(str(live.get("ticker") or ""))
        quantity = _num(live.get("totalSize")) or _num(live.get("remainingQuantity"))
        body: dict[str, Any] = {
            "conid": conid,
            "orderType": str(live.get("orderType") or "LMT"),
            "price": float(price),
            "quantity": int(quantity) if float(quantity).is_integer() else quantity,
            "side": str(live.get("side") or "").upper(),
            "tif": str(live.get("tif") or "DAY"),
            "outsideRth": True,
        }
        if live.get("secType"):
            body["secType"] = str(live.get("secType"))
        account_id = self._account_id()
        kind, row, _questions = self._submit(
            f"/iserver/account/{urllib.parse.quote(account_id)}/order/{urllib.parse.quote(order_id)}",
            body,
        )
        if kind == "refused":
            return {
                "ok": False, "ticket": order_id, "retcode": None,
                "retcodeName": "IBKR_CONFIRMATION_REQUIRED",
                "reasons": ["the gateway asked a second confirmation question; the order was NOT modified"],
            }
        confirmed_id = self._order_id(row)
        if not confirmed_id:
            return {
                "ok": False, "ticket": order_id, "retcode": None,
                "retcodeName": "MODIFY_REJECTED",
                "reasons": [" ".join(_messages(row)) or "the gateway did not confirm the modify"],
            }
        return {
            "ok": True, "ticket": order_id, "price": float(price),
            "retcode": 200, "retcodeName": "MODIFY_SUBMITTED", "verified": False,
            "reasons": [],
        }

    def order_status(self, ticket: Any) -> dict[str, Any]:
        order_id = str(ticket)
        row = self._request(
            "GET", f"/iserver/account/order/status/{urllib.parse.quote(order_id)}",
            allow_missing=True,
        )
        if not isinstance(row, dict) or not row:
            return {"found": False, "ticket": order_id}
        state = row.get("order_status") or row.get("orderStatus") or row.get("status")
        return {
            "found": True,
            "ticket": order_id,
            "state": state,
            "stateName": str(state or "").upper(),
            "filledVolume": _num(row.get("filledQuantity") or row.get("cumFill")),
            "remainingVolume": _num(row.get("remainingQuantity")),
            "avgPrice": _num(row.get("averagePrice") or row.get("avgPrice")),
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        requested = str(symbol or "").strip()
        conid, sec_type = self._contract(requested)
        info = self._request(
            "GET", f"/iserver/contract/{conid}/info", allow_missing=True
        )
        if not isinstance(info, dict) or not info:
            raise AdapterUnavailable(
                "SYMBOL_INFO_NOT_SUPPORTED",
                "this IBKR gateway does not expose /iserver/contract/{conid}/info",
            )
        tick = _num(info.get("tickSize") or info.get("tick_size") or info.get("increment"))
        digits = 0
        if tick > 0:
            text = f"{tick:.10f}".rstrip("0")
            digits = len(text.split(".")[1]) if "." in text else 0
        currency = str(info.get("currency") or "")
        return {
            "symbol": str(info.get("symbol") or requested).upper(),
            "requestedSymbol": requested,
            "description": str(
                info.get("company_name") or info.get("companyName") or info.get("name") or ""
            ),
            "path": sec_type,
            "tradeMode": "unknown",
            "digits": digits,
            "point": tick,
            "tickSize": tick,
            "tickValue": tick,
            "currencyBase": currency,
            "currencyProfit": currency,
            "currencyMargin": currency,
            "contractSize": _num(info.get("multiplier"), 1.0) or 1.0,
            # CPAPI contract info reports no lot step/min/max; report 0 rather
            # than inventing a size rule the gateway does not enforce.
            "volumeMin": 0.0,
            "volumeMax": 0.0,
            "volumeStep": 0.0,
            "stopsLevelPoints": 0,
            "freezeLevelPoints": 0,
            "spreadPoints": None,
            "sessionsAvailable": False,
            "sessionNote": "IBKR contract info does not report trading sessions",
            "retrievedAt": time.time(),
            "provider": "ibkr",
        }

    # ── reconciliation ───────────────────────────────────────────────────────
    def _executions(self) -> list[Execution]:
        rows = self._get("/iserver/account/trades") or []
        out: list[Execution] = []
        for row in rows if isinstance(rows, list) else []:
            qty = abs(_num(row.get("size")))
            price = _num(row.get("price"))
            when = _ms(row.get("trade_time_r") or row.get("trade_time"))
            symbol = str(row.get("symbol") or row.get("contractDesc") or "").upper()
            if qty <= 0 or price <= 0 or not symbol:
                continue
            side_raw = str(row.get("side") or "").upper()
            side = "buy" if side_raw in ("B", "BUY", "BOT") else "sell"
            fee = abs(_num(row.get("commission")))
            out.append(
                Execution(
                    id=str(row.get("execution_id") or row.get("id") or f"{symbol}-{when}-{len(out)}"),
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    time_ms=when or int(time.time() * 1000),
                    fee=fee,
                )
            )
        return out

    def deals_since(self, since_ms: int) -> list[dict[str, Any]]:
        since = int(since_ms or 0)
        executions = self._executions()
        if since > 0:
            executions = [e for e in executions if e.time_ms >= since]
        return normalize_fifo(executions)

    def deals_window(self, days: int) -> list[dict[str, Any]]:
        # The CPAPI executions endpoint is a recent-window read; filter locally
        # so the caller still gets an honest `days` contract.
        since = int(time.time() * 1000) - max(1, min(int(days or 7), 3650)) * 86_400_000
        return self.deals_since(since)


__all__ = ["IBKRAdapter"]
