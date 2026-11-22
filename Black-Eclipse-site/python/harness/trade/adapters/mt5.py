"""MetaTrader 5 adapter (Windows, local IPC).

The official `MetaTrader5` package talks to a running `terminal64.exe`; it is not
a REST API and there is no API key. In the preferred ATTACH mode the operator has
already logged the terminal into a demo (or live) account, and Eclipse passes no
credentials at all.

The adapter implements reads AND the low-level order calls. `order_send` is only
reachable through `trade/service.py`, which requires an approval or mandate, a
digest bound to the exact order, live-arm state for a real account, and the risk
policy. A missing package, terminal, login or broker connection is reported with
a distinct code, never as an empty account.
"""

from __future__ import annotations

import os
import math
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from ..adapter import AdapterUnavailable, TradingAdapter
from .mt5_contract import (
    _DEAL_ENTRY,
    _DEAL_TYPE,
    _FILLED_RETCODES,
    _IPC_ERRORS,
    _MARGIN_MODE,
    _OK_CHECK_RETCODES,
    _ORDER_STATE,
    _ORDER_TYPE,
    _RETCODE,
    _SYMBOL_TRADE_MODE,
    _TIMEFRAMES,
    _TRADE_MODE,
    _account_dict,
    _bars_from_rates,
    _int,
    _notional,
    _num,
    _order_dict,
    _own_margin,
    _position_dict,
    _retcode,
    _row_value,
    _session_rows,
    _symbol_quote_dict,
    _symbol_trade_mode,
    _terminal_dict,
)
from ..model import KIND_MT5
from ..terminal import terminal_running

_MODULE: Any = None
_IMPORT_ERROR: Optional[str] = None
# MT5's Python API is not safe to initialize concurrently from several threads.
# The agent can issue parallel broker tool calls, so every session/probe is
# serialized. This lock is process-wide (one terminal connection per process).
_MT5_LOCK = threading.Lock()

_TERMINAL_CANDIDATES = (
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 Terminal\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 IC Markets\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
)


#: MT5 exposes timeframes as `TIMEFRAME_*` attributes; this is the one place the
#: tool vocabulary maps onto them. Anything else is refused, never guessed.


#: MetaTrader's `order_send` retcodes. Only 10008/10009/10010 are fills.

#: `mt5.last_error()` codes that name an actionable cause.


def _import_mt5() -> Any:
    global _MODULE, _IMPORT_ERROR
    if _MODULE is not None:
        return _MODULE
    if sys.platform != "win32":
        raise AdapterUnavailable(
            "PLATFORM_UNSUPPORTED",
            "the MetaTrader5 Python API is Windows-only; run the runtime on Windows",
        )
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - a missing package is a supported state
        _IMPORT_ERROR = str(exc)
        raise AdapterUnavailable(
            "PACKAGE_MISSING",
            "the MetaTrader5 package is not installed; run "
            "`pip install MetaTrader5` in the Eclipse Python environment",
        ) from exc
    _MODULE = mt5
    return mt5


def _last_error(mt5: Any) -> tuple[Optional[int], str]:
    try:
        code, description = mt5.last_error()
        return int(code), str(description or "")
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return None, ""


def _ipc_error(mt5: Any, fallback: str) -> AdapterUnavailable:
    code, description = _last_error(mt5)
    name, reason = _IPC_ERRORS.get(code, ("MT5_UNAVAILABLE", description or fallback))
    return AdapterUnavailable(name, reason, detail={"mt5Code": code})


def _terminal_path(config: dict[str, Any]) -> Optional[str]:
    explicit = str(config.get("terminalPath") or os.environ.get("MT5_TERMINAL_PATH") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for candidate in _TERMINAL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("terminal64.exe")
    return found or None


def _auto_launch(config: dict[str, Any]) -> bool:
    """Whether this connection may START the terminal when it is closed.

    Default is False: the documented mode is attach. `mt5.initialize(path=...)`
    launches terminal64.exe whenever it is not running, so an unguarded call from
    a background read pops the terminal open over and over.
    """
    value = config.get("autoLaunchTerminal")
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() in {
        "1", "true", "yes", "on",
    }


_TERMINAL_CLOSED_REASON = (
    "the MetaTrader 5 terminal is not running; start it and log in, or enable "
    "'Start the terminal automatically' on this connection"
)


def _filling_mode(mt5: Any, info: Any, order_type: str = "market") -> tuple[int, list[str]]:
    """Pick an allowed filling mode; never guess one blindly.

    MT5 exposes the *symbol's* allowed modes as a bitmask (`1=FOK, 2=IOC,
    4=BOC`). BOC is book-only and is not a market-execution mode.
    """
    flags = _int(getattr(info, "filling_mode", 0), 0)
    fok = _int(getattr(mt5, "ORDER_FILLING_FOK", 0), 0)
    ioc = _int(getattr(mt5, "ORDER_FILLING_IOC", 1), 1)
    ret = _int(getattr(mt5, "ORDER_FILLING_RETURN", 2), 2)
    boc = _int(getattr(mt5, "ORDER_FILLING_BOC", 3), 3)
    if flags & 1:
        return fok, []
    if flags & 2:
        return ioc, []
    if flags & 4:
        if order_type in ("limit", "stop"):
            return boc, []
        return ret, [
            "this symbol only advertises book-only filling; market orders are not available"
        ]
    return ret, ["the symbol reports no permitted filling mode"]


def _normalise_volume(info: Any, volume: float) -> tuple[float, list[str]]:
    """Volume must already be a valid step. Resizing silently would break the approval."""
    reasons: list[str] = []
    try:
        requested = float(volume)
    except (TypeError, ValueError):
        return 0.0, ["volume must be a number"]
    step = _num(getattr(info, "volume_step", 0), 0) or 0.01
    minimum = _num(getattr(info, "volume_min", 0), 0) or step
    maximum = _num(getattr(info, "volume_max", 0), 0)
    steps = math.floor(requested / step + 1e-9)
    normalised = round(steps * step, 8)
    if abs(normalised - requested) > 1e-8:
        reasons.append(
            f"volume {requested:g} is not a multiple of this symbol's step {step:g}"
        )
    if normalised < minimum - 1e-9:
        reasons.append(f"volume {requested:g} is below this symbol's minimum {minimum:g}")
    if maximum > 0 and normalised > maximum + 1e-9:
        reasons.append(f"volume {requested:g} is above this symbol's maximum {maximum:g}")
    return normalised, reasons


def _stops_reasons(
    side: str, price: float, point: float, stops_level: int,
    stop_loss: Optional[float], take_profit: Optional[float],
) -> list[str]:
    reasons: list[str] = []
    distance = max(0.0, float(stops_level or 0) * (point or 0))
    if side == "buy":
        if stop_loss is not None and stop_loss > price - distance + 1e-12:
            reasons.append(
                f"stop loss must be at least {distance:g} below the entry price"
            )
        if take_profit is not None and take_profit < price + distance - 1e-12:
            reasons.append(
                f"take profit must be at least {distance:g} above the entry price"
            )
    else:
        if stop_loss is not None and stop_loss < price + distance - 1e-12:
            reasons.append(
                f"stop loss must be at least {distance:g} above the entry price"
            )
        if take_profit is not None and take_profit > price - distance + 1e-12:
            reasons.append(
                f"take profit must be at least {distance:g} below the entry price"
            )
    return reasons


def _build_request(
    mt5: Any, intent: Any, info: Any, tick: Any, filling: int,
) -> tuple[Optional[dict[str, Any]], list[str], float]:
    """Build the exact `order_send` request and validate what the broker forbids."""
    reasons: list[str] = []
    side = str(intent.side)
    order_type = str(intent.order_type)
    bid = _num(getattr(tick, "bid", 0))
    ask = _num(getattr(tick, "ask", 0))
    if bid <= 0 or ask <= 0:
        return None, ["the broker returned no usable bid/ask"], 0.0
    price_ref = ask if side == "buy" else bid

    trade_mode = _symbol_trade_mode(info)
    if trade_mode == "disabled":
        reasons.append("this symbol is disabled for trading")
    elif trade_mode == "close_only":
        reasons.append("this symbol only allows closing positions")
    elif trade_mode == "long_only" and side == "sell":
        reasons.append("this symbol only allows buy orders")
    elif trade_mode == "short_only" and side == "buy":
        reasons.append("this symbol only allows sell orders")

    request: dict[str, Any] = {
        "symbol": str(getattr(info, "name", "") or ""),
        "volume": float(intent.volume),
        "deviation": int(intent.deviation_points),
        "magic": int(intent.magic),
        "comment": str(intent.comment)[:31],
        "type_time": (
            _int(getattr(mt5, "ORDER_TIME_DAY", 1), 1)
            if intent.time_in_force == "day"
            else _int(getattr(mt5, "ORDER_TIME_GTC", 0), 0)
        ),
        "type_filling": filling,
    }
    if intent.stop_loss is not None:
        request["sl"] = float(intent.stop_loss)
    if intent.take_profit is not None:
        request["tp"] = float(intent.take_profit)

    if order_type == "market":
        request["action"] = _int(getattr(mt5, "TRADE_ACTION_DEAL", 1), 1)
        request["type"] = (
            _int(getattr(mt5, "ORDER_TYPE_BUY", 0), 0)
            if side == "buy"
            else _int(getattr(mt5, "ORDER_TYPE_SELL", 1), 1)
        )
        request["price"] = price_ref
    else:
        request["action"] = _int(getattr(mt5, "TRADE_ACTION_PENDING", 5), 5)
        if order_type == "limit":
            request["type"] = (
                _int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2), 2)
                if side == "buy"
                else _int(getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3), 3)
            )
        else:
            request["type"] = (
                _int(getattr(mt5, "ORDER_TYPE_BUY_STOP", 4), 4)
                if side == "buy"
                else _int(getattr(mt5, "ORDER_TYPE_SELL_STOP", 5), 5)
            )
        request["price"] = float(intent.price or 0)
        if order_type == "limit":
            if side == "buy" and request["price"] >= ask:
                reasons.append("a buy limit must be below the current ask")
            if side == "sell" and request["price"] <= bid:
                reasons.append("a sell limit must be above the current bid")
        else:
            if side == "buy" and request["price"] <= ask:
                reasons.append("a buy stop must be above the current ask")
            if side == "sell" and request["price"] >= bid:
                reasons.append("a sell stop must be below the current bid")

    reasons.extend(_stops_reasons(
        side,
        float(request["price"]),
        _num(getattr(info, "point", 0)),
        _int(getattr(info, "trade_stops_level", 0)),
        intent.stop_loss,
        intent.take_profit,
    ))
    return request, reasons, float(request["price"])


def _tick(mt5: Any, symbol: str) -> Any:
    """A live tick, subscribing the symbol on demand.

    MT5 returns an all-zero tick for a symbol that exists but is not in Market
    Watch (XAUUSD on this demo did exactly that). `symbol_select` subscribes it;
    a short bounded retry covers the feed's first update. Selecting a symbol is a
    local terminal subscription, not an order or account mutation.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None and (_num(getattr(tick, "bid", 0)) > 0 or _num(getattr(tick, "ask", 0)) > 0):
        return tick
    try:
        mt5.symbol_select(symbol, True)
    except Exception:  # noqa: BLE001 - selection is best-effort
        pass
    for _ in range(10):
        time.sleep(0.2)
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and (_num(getattr(tick, "bid", 0)) > 0 or _num(getattr(tick, "ask", 0)) > 0):
            return tick
    return tick


def _verify_fill(mt5: Any, payload: dict[str, Any], intent: Any) -> dict[str, Any]:
    """Read the filled position back and confirm the bracket the broker stored.

    A market order's reply says what was accepted; it does not prove the stop is
    on the position. This closes that gap: `verified` is only true when the
    stored SL/TP match the requested ones (or none were requested).
    """
    if payload.get("status") != "FILLED":
        return {}
    if str(getattr(intent, "order_type", "market")) != "market":
        return {}
    want_sl = _num(getattr(intent, "stop_loss", 0) or 0)
    want_tp = _num(getattr(intent, "take_profit", 0) or 0)
    if not want_sl and not want_tp:
        return {"verified": True, "verifyNote": None}
    ticket = int(payload.get("ticket") or 0)
    rows = mt5.positions_get(ticket=ticket) or ()
    if not rows:
        return {
            "verified": False,
            "verifyNote": "the position could not be read back after the fill",
        }
    actual_sl = _num(getattr(rows[0], "sl", 0))
    actual_tp = _num(getattr(rows[0], "tp", 0))
    sl_ok = not want_sl or abs(actual_sl - want_sl) < 1e-6
    tp_ok = not want_tp or abs(actual_tp - want_tp) < 1e-6
    ok = sl_ok and tp_ok
    return {
        "verified": ok,
        "verifyNote": None if ok else (
            f"stored bracket is SL {actual_sl:g} / TP {actual_tp:g}, requested "
            f"SL {want_sl:g} / TP {want_tp:g}"
        ),
        "storedStopLoss": actual_sl,
        "storedTakeProfit": actual_tp,
    }


class MT5Adapter(TradingAdapter):
    kind = KIND_MT5

    @classmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        if sys.platform != "win32":
            return False, "the MetaTrader5 Python API is Windows-only"
        try:
            _import_mt5()
        except AdapterUnavailable as exc:
            return False, exc.reason
        return True, None

    # ── session ──────────────────────────────────────────────────────────────
    @contextmanager
    def _session(self) -> Iterator[Any]:
        mt5 = _import_mt5()
        path = _terminal_path(self.config)
        if path is None:
            raise AdapterUnavailable(
                "TERMINAL_NOT_FOUND",
                "no terminal64.exe was found; set the terminal path in Settings › "
                "Trading (or MT5_TERMINAL_PATH)",
            )
        if not _auto_launch(self.config) and not terminal_running(path):
            # Guard BEFORE initialize: it would start terminal64.exe itself.
            raise AdapterUnavailable("TERMINAL_NOT_RUNNING", _TERMINAL_CLOSED_REASON)
        kwargs: dict[str, Any] = {"path": path, "timeout": 10_000}
        if bool(self.config.get("portable")):
            kwargs["portable"] = True
        _MT5_LOCK.acquire()
        try:
            try:
                initialized = mt5.initialize(**kwargs)
            except Exception as exc:  # noqa: BLE001 - the package raises on some builds
                raise AdapterUnavailable(
                    "IPC_FAILED", f"MetaTrader 5 initialization failed: {exc}"
                ) from exc
            if not initialized:
                raise _ipc_error(mt5, "MetaTrader 5 initialization failed")
            try:
                yield mt5
            finally:
                try:
                    mt5.shutdown()
                except Exception:  # noqa: BLE001 - shutdown is best-effort
                    pass
        finally:
            _MT5_LOCK.release()

    def _require_account(self, mt5: Any) -> Any:
        info = mt5.account_info()
        if info is None:
            raise AdapterUnavailable(
                "NOT_LOGGED_IN",
                "the MetaTrader 5 terminal is running but not logged in to an account",
                detail={"mt5Code": _last_error(mt5)[0]},
            )
        return info

    def _resolve_symbol(self, mt5: Any, symbol: str) -> str:
        requested = str(symbol or "").strip()
        if not requested:
            raise AdapterUnavailable("SYMBOL_REQUIRED", "a symbol is required")
        mapping = self.config.get("symbolMap") or {}
        if isinstance(mapping, dict) and mapping.get(requested):
            return str(mapping[requested])
        symbols = mt5.symbols_get() or ()
        names = [str(getattr(item, "name", "") or "") for item in symbols]
        for name in names:
            if name == requested:
                return name
        lowered = requested.upper()
        for name in names:
            if name.upper() == lowered:
                return name
        for name in names:
            if name.upper().startswith(lowered) or name.upper().endswith(lowered):
                return name
        raise AdapterUnavailable(
            "SYMBOL_NOT_FOUND",
            f"{requested!r} is not offered by this broker; set a symbol mapping",
            detail={"candidates": [n for n in names if lowered[:3] in n.upper()][:10]},
        )

    # ── contract ─────────────────────────────────────────────────────────────
    def probe(self) -> dict[str, Any]:
        """Test the connection with the MT5 IPC lock held (never concurrent)."""
        with _MT5_LOCK:
            return self._probe_locked()

    def _probe_locked(self) -> dict[str, Any]:
        mt5 = _import_mt5()
        path = _terminal_path(self.config)
        if path is None:
            return {
                "available": False,
                "connected": False,
                "code": "TERMINAL_NOT_FOUND",
                "reason": "no terminal64.exe was found on this machine",
                "terminalPath": None,
                "terminal": None,
                "account": None,
                "warnings": [],
            }
        if not _auto_launch(self.config) and not terminal_running(path):
            return {
                "available": False,
                "connected": False,
                "code": "TERMINAL_NOT_RUNNING",
                "reason": _TERMINAL_CLOSED_REASON,
                "terminalPath": path,
                "terminal": None,
                "account": None,
                "warnings": [],
            }
        kwargs: dict[str, Any] = {"path": path, "timeout": 10_000}
        if bool(self.config.get("portable")):
            kwargs["portable"] = True
        try:
            initialized = mt5.initialize(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "connected": False,
                "code": "IPC_FAILED",
                "reason": f"MetaTrader 5 initialization failed: {exc}",
                "terminalPath": path,
                "terminal": None,
                "account": None,
                "warnings": [],
            }
        if not initialized:
            failure = _ipc_error(mt5, "MetaTrader 5 initialization failed")
            return {
                "available": False,
                "connected": False,
                "code": failure.code,
                "reason": failure.reason,
                "terminalPath": path,
                "terminal": None,
                "account": None,
                "warnings": [],
                **failure.detail,
            }
        try:
            terminal_info = mt5.terminal_info()
            terminal = _terminal_dict(terminal_info) if terminal_info is not None else None
            account_info = mt5.account_info()
            account = _account_dict(account_info) if account_info is not None else None
            warnings: list[str] = []
            if terminal is not None and not terminal["connected"]:
                warnings.append("the terminal is not connected to its broker")
            if terminal is not None and not terminal["tradeAllowed"]:
                warnings.append("Algo Trading is disabled in the terminal")
            if account is not None and not account["tradeAllowed"]:
                warnings.append("trading is disabled for this account")
            payload: dict[str, Any] = {
                "available": account is not None,
                "connected": bool(terminal and terminal["connected"]),
                "code": None if account is not None else "NOT_LOGGED_IN",
                "reason": None if account is not None else (
                    "the terminal is running but no account is logged in"
                ),
                "terminalPath": path,
                "terminal": terminal,
                "account": account,
                "warnings": warnings,
            }
            return payload
        finally:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def account(self) -> dict[str, Any]:
        with self._session() as mt5:
            return _account_dict(self._require_account(mt5))

    def positions(self) -> list[dict[str, Any]]:
        with self._session() as mt5:
            self._require_account(mt5)
            rows = mt5.positions_get()
            if rows is None:
                code, description = _last_error(mt5)
                if code:
                    raise _ipc_error(mt5, "positions_get returned no rows")
                return []
            return [_position_dict(row) for row in rows]

    def orders(self) -> list[dict[str, Any]]:
        with self._session() as mt5:
            self._require_account(mt5)
            rows = mt5.orders_get()
            if rows is None:
                code, description = _last_error(mt5)
                if code:
                    raise _ipc_error(mt5, "orders_get returned no rows")
                return []
            return [_order_dict(row) for row in rows]

    def quote(self, symbol: str) -> dict[str, Any]:
        with self._session() as mt5:
            self._require_account(mt5)
            resolved = self._resolve_symbol(mt5, symbol)
            info = mt5.symbol_info(resolved)
            tick = _tick(mt5, resolved)
            if info is None or tick is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND",
                    f"no live tick for {resolved!r}",
                )
            payload = _symbol_quote_dict(resolved, info, tick)
            payload["requestedSymbol"] = symbol
            return payload

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        """Static symbol facts, with NO tick required (so it works when closed).

        This is what the quote cannot give on a closed market: the trade mode,
        contract size, volume limits, stops level and filling mask are all
        symbol-level. Trade SESSIONS are read best-effort: the MetaTrader5 Python
        package does not reliably expose them, so `sessionsAvailable` is explicit
        and a caller falls back to its own window when it is false.
        """
        with self._session() as mt5:
            self._require_account(mt5)
            resolved = self._resolve_symbol(mt5, symbol)
            info = mt5.symbol_info(resolved)
            if info is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND", f"the broker does not report {resolved!r}"
                )
            sessions, session_note = _session_rows(mt5, resolved)
            return {
                "symbol": resolved,
                "requestedSymbol": symbol,
                "description": str(getattr(info, "description", "") or ""),
                "path": str(getattr(info, "path", "") or ""),
                "tradeMode": _symbol_trade_mode(info),
                "digits": _int(getattr(info, "digits", 0)),
                "point": _num(getattr(info, "point", 0)),
                "tickSize": _num(getattr(info, "trade_tick_size", 0)),
                # Money per tick for ONE lot in the ACCOUNT currency. This is what
                # makes risk-based sizing exact instead of a quote-currency guess.
                "tickValue": _num(getattr(info, "trade_tick_value", 0)),
                "currencyBase": str(getattr(info, "currency_base", "") or ""),
                "currencyProfit": str(getattr(info, "currency_profit", "") or ""),
                "currencyMargin": str(getattr(info, "currency_margin", "") or ""),
                "contractSize": _num(getattr(info, "trade_contract_size", 0)),
                "volumeMin": _num(getattr(info, "volume_min", 0)),
                "volumeMax": _num(getattr(info, "volume_max", 0)),
                "volumeStep": _num(getattr(info, "volume_step", 0)),
                "stopsLevelPoints": _int(getattr(info, "trade_stops_level", 0)),
                "freezeLevelPoints": _int(getattr(info, "trade_freeze_level", 0)),
                "fillingMask": _int(getattr(info, "filling_mode", 0)),
                "spreadPoints": _int(getattr(info, "spread", 0)),
                "sessionsAvailable": bool(sessions),
                "sessionDeals": sessions,
                "sessionDayIndex": "0=Monday",
                "sessionBasis": "broker server time",
                "sessionNote": session_note,
                "retrievedAt": time.time(),
                "provider": "metatrader5",
            }

    def bars(
        self, symbol: str, timeframe: str = "H1", count: int = 120,
    ) -> dict[str, Any]:
        """The broker's OWN OHLCV series for a symbol (server time, no guessing).

        This is the accuracy path for instruments the market universe does not
        cover (XAUUSD). The timeframe vocabulary is fixed; a count is clamped to
        1..500 so one call cannot pull an unbounded series. A symbol that is not
        in Market Watch is subscribed once and retried, exactly like `_tick`.
        """
        key = str(timeframe or "H1").strip().upper()
        if key not in _TIMEFRAMES:
            raise AdapterUnavailable(
                "INVALID_TIMEFRAME",
                f"timeframe {timeframe!r} is not one of {', '.join(_TIMEFRAMES)}",
            )
        wanted = max(1, min(int(count or 120), 500))
        with self._session() as mt5:
            self._require_account(mt5)
            resolved = self._resolve_symbol(mt5, symbol)
            info = mt5.symbol_info(resolved)
            tf = _int(getattr(mt5, _TIMEFRAMES[key], 0), 0)
            rates = mt5.copy_rates_from_pos(resolved, tf, 0, wanted)
            if rates is None or len(rates) == 0:
                try:
                    mt5.symbol_select(resolved, True)
                except Exception:  # noqa: BLE001 - subscription is best-effort
                    pass
                for _ in range(5):
                    time.sleep(0.2)
                    rates = mt5.copy_rates_from_pos(resolved, tf, 0, wanted)
                    if rates is not None and len(rates) > 0:
                        break
            if rates is None:
                raise _ipc_error(
                    mt5, f"copy_rates_from_pos returned no series for {resolved}"
                )
            bars = _bars_from_rates(rates)
            if not bars:
                raise AdapterUnavailable(
                    "BARS_UNAVAILABLE", f"no bars for {resolved!r} on {key}"
                )
            return {
                "symbol": resolved,
                "requestedSymbol": symbol,
                "timeframe": key,
                "count": len(bars),
                "bars": bars,
                "timeBasis": "broker server time",
                "contractSize": _num(getattr(info, "trade_contract_size", 0)),
                "tickSize": _num(getattr(info, "trade_tick_size", 0)),
                "retrievedAt": time.time(),
                "provider": "metatrader5",
            }

    # ── P1 demo execution ────────────────────────────────────────────────────
    def order_preview(self, intent: Any) -> dict[str, Any]:
        """Normalize, validate and run the broker's own `order_check` (no send)."""
        with self._session() as mt5:
            account = _account_dict(self._require_account(mt5))
            resolved = self._resolve_symbol(mt5, intent.symbol)
            info = mt5.symbol_info(resolved)
            tick = _tick(mt5, resolved)
            if info is None or tick is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND", f"no live tick for {resolved!r}"
                )
            volume, reasons = _normalise_volume(info, intent.volume)
            normalised_intent = intent
            if abs(volume - float(intent.volume)) > 1e-8:
                normalised_intent = type(intent)(
                    **{**intent.__dict__, "volume": volume}
                )
            filling, fill_reasons = _filling_mode(mt5, info, intent.order_type)
            request, build_reasons, price = _build_request(
                mt5, normalised_intent, info, tick, filling
            )
            reasons = reasons + fill_reasons + build_reasons
            quote = _symbol_quote_dict(resolved, info, tick)
            positions = mt5.positions_get() or ()
            terminal = mt5.terminal_info()
            preview: dict[str, Any] = {
                "ok": False,
                "symbol": resolved,
                "requestedSymbol": intent.symbol,
                "side": intent.side,
                "type": intent.order_type,
                "volume": volume,
                "price": price,
                "notional": _notional(volume, info, price),
                "stopLoss": intent.stop_loss,
                "takeProfit": intent.take_profit,
                "contractSize": _num(getattr(info, "trade_contract_size", 0)),
                "tickSize": _num(getattr(info, "trade_tick_size", 0)),
                "tickValue": _num(getattr(info, "trade_tick_value", 0)),
                "point": _num(getattr(info, "point", 0)),
                "margin": None,
                "marginFree": account.get("marginFree"),
                "spreadPoints": quote.get("spreadPoints"),
                "openPositions": len(positions),
                "account": account,
                "terminalTradeAllowed": bool(getattr(terminal, "trade_allowed", False)),
                "quote": quote,
                "request": request,
                "retcode": None,
                "retcodeName": None,
                "reasons": reasons,
            }
            if reasons or request is None:
                preview["reasons"] = reasons or ["the order could not be built"]
                return preview
            check = mt5.order_check(request)
            if check is None:
                raise _ipc_error(mt5, "order_send check returned no result")
            code = _int(getattr(check, "retcode", -1), -1)
            name, message = _retcode(code)
            ok = code in _OK_CHECK_RETCODES
            if not ok:
                reasons.append(message)
            # `check.margin` is the ACCOUNT margin AFTER this order, not the
            # margin this order asks for. On this demo the two were 113.74 and
            # 40.00 for a 0.01 XAUUSD lot (three EURUSD positions were already
            # open), and comparing the total against a mandate's cap refused a
            # valid order. Charge the order its own increment, never the account
            # total; a reducing order needs no new margin.
            account_margin = _num(account.get("margin", 0))
            check_margin = _num(getattr(check, "margin", 0))
            own_margin = _own_margin(account_margin, check_margin)
            preview.update({
                "ok": ok,
                "retcode": code,
                "retcodeName": name,
                "margin": own_margin,
                "accountMarginAfter": check_margin,
                "marginFree": _num(getattr(check, "margin_free", account.get("marginFree", 0))),
                "balance": _num(getattr(check, "balance", account.get("balance", 0))),
                "equity": _num(getattr(check, "equity", account.get("equity", 0))),
                "checkComment": str(getattr(check, "comment", "") or ""),
                "reasons": reasons,
            })
            return preview

    def submit_order(self, intent: Any) -> dict[str, Any]:
        """Send exactly one order. An ambiguous result is never retried here."""
        with self._session() as mt5:
            account = _account_dict(self._require_account(mt5))
            resolved = self._resolve_symbol(mt5, intent.symbol)
            info = mt5.symbol_info(resolved)
            tick = _tick(mt5, resolved)
            if info is None or tick is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND", f"no live tick for {resolved!r}"
                )
            volume, reasons = _normalise_volume(info, intent.volume)
            if abs(volume - float(intent.volume)) > 1e-8:
                raise AdapterUnavailable(
                    "INVALID_VOLUME",
                    f"volume {intent.volume:g} is not a multiple of the symbol's step",
                )
            filling, fill_reasons = _filling_mode(mt5, info, intent.order_type)
            request, build_reasons, _price = _build_request(
                mt5, intent, info, tick, filling
            )
            reasons = reasons + fill_reasons + build_reasons
            if reasons or request is None:
                return {
                    "ok": False,
                    "status": "REFUSED",
                    "symbol": resolved,
                    "retcode": None,
                    "retcodeName": "PREVIEW_REJECTED",
                    "reasons": reasons,
                    "account": account,
                }
            result = mt5.order_send(request)
            if result is None:
                raise _ipc_error(mt5, "order_send returned no result")
            code = _int(getattr(result, "retcode", -1), -1)
            name, message = _retcode(code)
            payload = {
                "ok": code in _FILLED_RETCODES,
                "status": "FILLED" if code in _FILLED_RETCODES else "REJECTED",
                "symbol": resolved,
                "side": intent.side,
                "type": intent.order_type,
                "volume": _num(getattr(result, "volume", volume)),
                "price": _num(getattr(result, "price", 0)),
                "ticket": _int(getattr(result, "order", 0)),
                "deal": _int(getattr(result, "deal", 0)),
                "retcode": code,
                "retcodeName": name,
                "comment": str(getattr(result, "comment", "") or ""),
                "requestId": _int(getattr(result, "request_id", 0)),
                "reasons": [] if code in _FILLED_RETCODES else [message],
                "account": account,
            }
            payload.update(_verify_fill(mt5, payload, intent))
            return payload

    def cancel_order(self, ticket: int) -> dict[str, Any]:
        with self._session() as mt5:
            self._require_account(mt5)
            request = {
                "action": _int(getattr(mt5, "TRADE_ACTION_REMOVE", 8), 8),
                "order": int(ticket),
            }
            result = mt5.order_send(request)
            if result is None:
                raise _ipc_error(mt5, "cancel returned no result")
            code = _int(getattr(result, "retcode", -1), -1)
            name, message = _retcode(code)
            ok = code in (10008, 10009)
            return {
                "ok": ok,
                "ticket": int(ticket),
                "retcode": code,
                "retcodeName": name,
                "reasons": [] if ok else [message],
            }

    def close_position(
        self, ticket: int, deviation: int = 20, volume: float | None = None,
    ) -> dict[str, Any]:
        """Close all or part of one position with the opposite market deal.

        The position ticket is the join key (`position` in the request), so the
        close can only ever hit the position the operator named. The magic is
        carried over so the deal still belongs to Eclipse. A send is attempted
        once: an ambiguous reply is reported, never retried. After a successful
        send the position is read back, so "closed" is verified, not assumed.
        """
        with self._session() as mt5:
            account = _account_dict(self._require_account(mt5))
            rows = mt5.positions_get(ticket=int(ticket)) or ()
            if not rows:
                return {
                    "ok": False,
                    "ticket": int(ticket),
                    "retcode": None,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"no open position with ticket {int(ticket)}"],
                    "account": account,
                }
            position = rows[0]
            symbol = str(getattr(position, "symbol", "") or "")
            info = mt5.symbol_info(symbol)
            tick = _tick(mt5, symbol)
            if info is None or tick is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND", f"no live tick for {symbol!r}"
                )
            held = _num(getattr(position, "volume", 0))
            wanted = held if volume is None else _num(volume)
            if wanted <= 0:
                return {
                    "ok": False, "ticket": int(ticket), "symbol": symbol,
                    "retcode": None, "retcodeName": "INVALID_VOLUME",
                    "reasons": ["the close volume must be greater than zero"],
                    "account": account,
                }
            if wanted - held > 1e-8:
                return {
                    "ok": False, "ticket": int(ticket), "symbol": symbol,
                    "retcode": None, "retcodeName": "INVALID_VOLUME",
                    "reasons": [
                        f"cannot close {wanted:g} of a {held:g}-volume position"
                    ],
                    "account": account,
                }
            normalised, volume_reasons = _normalise_volume(info, wanted)
            if volume_reasons or abs(normalised - wanted) > 1e-8:
                return {
                    "ok": False, "ticket": int(ticket), "symbol": symbol,
                    "retcode": None, "retcodeName": "INVALID_VOLUME",
                    "reasons": volume_reasons or [
                        f"{wanted:g} is not a multiple of the symbol's volume step"
                    ],
                    "account": account,
                }
            partial = held - normalised > 1e-8
            closing_buy = _int(getattr(position, "type", 1), 1) == 0
            side = "sell" if closing_buy else "buy"
            price = (
                _num(getattr(tick, "bid", 0)) if closing_buy
                else _num(getattr(tick, "ask", 0))
            )
            filling, fill_reasons = _filling_mode(mt5, info, "market")
            if fill_reasons:
                return {
                    "ok": False,
                    "ticket": int(ticket),
                    "symbol": symbol,
                    "retcode": None,
                    "retcodeName": "PREVIEW_REJECTED",
                    "reasons": fill_reasons,
                    "account": account,
                }
            request = {
                "action": _int(getattr(mt5, "TRADE_ACTION_DEAL", 1), 1),
                "symbol": symbol,
                "volume": normalised,
                "type": (
                    _int(getattr(mt5, "ORDER_TYPE_SELL", 1), 1) if closing_buy
                    else _int(getattr(mt5, "ORDER_TYPE_BUY", 0), 0)
                ),
                "position": int(ticket),
                "price": price,
                "deviation": max(int(deviation or 0), 0),
                "magic": _int(getattr(position, "magic", 0), 0),
                "comment": "eclipse close",
                "type_time": _int(getattr(mt5, "ORDER_TIME_GTC", 0), 0),
                "type_filling": filling,
            }
            result = mt5.order_send(request)
            if result is None:
                raise _ipc_error(mt5, "close returned no result")
            code = _int(getattr(result, "retcode", -1), -1)
            name, message = _retcode(code)
            ok = code in _FILLED_RETCODES
            closed_volume = _num(getattr(result, "volume", normalised))
            after = mt5.positions_get(ticket=int(ticket)) or ()
            remaining = _num(getattr(after[0], "volume", 0)) if after else 0.0
            expected = held - closed_volume if partial else 0.0
            verified = ok and abs(remaining - expected) <= 1e-8
            return {
                "ok": ok,
                "ticket": int(ticket),
                "symbol": symbol,
                "side": side,
                "partial": partial,
                "volume": closed_volume,
                "remainingVolume": remaining,
                "price": _num(getattr(result, "price", price)),
                "order": _int(getattr(result, "order", 0)),
                "deal": _int(getattr(result, "deal", 0)),
                "retcode": code,
                "retcodeName": name,
                "comment": str(getattr(result, "comment", "") or ""),
                "reasons": [] if ok else [message],
                "verified": verified,
                "verifyNote": None if verified else (
                    f"broker reported {closed_volume:g} closed but the position "
                    f"shows {remaining:g} remaining"
                ),
                "account": account,
            }

    def modify_position(
        self, ticket: int, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Move a position's stop/target with `TRADE_ACTION_SLTP`, then verify.

        `None` keeps the existing value; 0 removes it (MT5's convention). The
        broker's own stops distance is enforced before the send, and the position
        is read back afterwards so the reported bracket is the stored bracket.
        """
        if stop_loss is None and take_profit is None:
            return {
                "ok": False, "ticket": int(ticket), "retcode": None,
                "retcodeName": "INVALID_STOPS",
                "reasons": ["pass stopLoss and/or takeProfit"],
            }
        with self._session() as mt5:
            account = _account_dict(self._require_account(mt5))
            rows = mt5.positions_get(ticket=int(ticket)) or ()
            if not rows:
                return {
                    "ok": False, "ticket": int(ticket), "retcode": None,
                    "retcodeName": "POSITION_NOT_FOUND",
                    "reasons": [f"no open position with ticket {int(ticket)}"],
                    "account": account,
                }
            position = rows[0]
            symbol = str(getattr(position, "symbol", "") or "")
            info = mt5.symbol_info(symbol)
            tick = _tick(mt5, symbol)
            if info is None or tick is None:
                raise AdapterUnavailable(
                    "SYMBOL_NOT_FOUND", f"no live tick for {symbol!r}"
                )
            closing_buy = _int(getattr(position, "type", 1), 1) == 0
            side = "buy" if closing_buy else "sell"
            price = (
                _num(getattr(tick, "bid", 0)) if closing_buy
                else _num(getattr(tick, "ask", 0))
            )
            current_sl = _num(getattr(position, "sl", 0))
            current_tp = _num(getattr(position, "tp", 0))
            new_sl = current_sl if stop_loss is None else _num(stop_loss)
            new_tp = current_tp if take_profit is None else _num(take_profit)
            reasons = _stops_reasons(
                side, price, _num(getattr(info, "point", 0)),
                _int(getattr(info, "trade_stops_level", 0)),
                new_sl or None, new_tp or None,
            )
            if reasons:
                return {
                    "ok": False, "ticket": int(ticket), "symbol": symbol,
                    "retcode": None, "retcodeName": "PREVIEW_REJECTED",
                    "reasons": reasons, "account": account,
                }
            request = {
                "action": _int(getattr(mt5, "TRADE_ACTION_SLTP", 6), 6),
                "symbol": symbol,
                "position": int(ticket),
                "sl": new_sl,
                "tp": new_tp,
                "magic": _int(getattr(position, "magic", 0), 0),
            }
            result = mt5.order_send(request)
            if result is None:
                raise _ipc_error(mt5, "modify returned no result")
            code = _int(getattr(result, "retcode", -1), -1)
            name, message = _retcode(code)
            ok = code in _FILLED_RETCODES
            after = mt5.positions_get(ticket=int(ticket)) or ()
            actual_sl = _num(getattr(after[0], "sl", 0)) if after else 0.0
            actual_tp = _num(getattr(after[0], "tp", 0)) if after else 0.0
            verified = ok and abs(actual_sl - new_sl) < 1e-6 and abs(actual_tp - new_tp) < 1e-6
            return {
                "ok": ok,
                "ticket": int(ticket),
                "symbol": symbol,
                "stopLoss": actual_sl,
                "takeProfit": actual_tp,
                "requestedStopLoss": new_sl,
                "requestedTakeProfit": new_tp,
                "retcode": code,
                "retcodeName": name,
                "reasons": [] if ok else [message],
                "verified": verified,
                "verifyNote": None if verified else (
                    "the broker accepted the request but the stored bracket differs"
                ),
                "account": account,
            }

    def modify_order(
        self, ticket: int, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Change a WORKING (pending) order's price and/or bracket, then verify.

        `None` keeps the existing value. The order ticket is the join key, so the
        request can only hit the order that was named; the order's own type/time/
        expiration are carried over so a modify cannot silently turn a day order
        into a resting GTC one. One send, then the order is read back — `verified`
        is evidence, not optimism.
        """
        if price is None and stop_loss is None and take_profit is None:
            return {
                "ok": False, "ticket": int(ticket), "retcode": None,
                "retcodeName": "INVALID_REQUEST",
                "reasons": ["pass price, stopLoss and/or takeProfit"],
            }
        with self._session() as mt5:
            account = _account_dict(self._require_account(mt5))
            rows = mt5.orders_get(ticket=int(ticket)) or ()
            if not rows:
                return {
                    "ok": False, "ticket": int(ticket), "retcode": None,
                    "retcodeName": "ORDER_NOT_FOUND",
                    "reasons": [f"no working order with ticket {int(ticket)}"],
                    "account": account,
                }
            order = rows[0]
            symbol = str(getattr(order, "symbol", "") or "")
            current_price = _num(getattr(order, "price_open", 0))
            current_sl = _num(getattr(order, "sl", 0))
            current_tp = _num(getattr(order, "tp", 0))
            new_price = current_price if price is None else _num(price)
            new_sl = current_sl if stop_loss is None else _num(stop_loss)
            new_tp = current_tp if take_profit is None else _num(take_profit)
            if new_price <= 0:
                return {
                    "ok": False, "ticket": int(ticket), "symbol": symbol,
                    "retcode": None, "retcodeName": "INVALID_PRICE",
                    "reasons": ["the order price must be greater than zero"],
                    "account": account,
                }
            request: dict[str, Any] = {
                "action": _int(getattr(mt5, "TRADE_ACTION_MODIFY", 7), 7),
                "order": int(ticket),
                "price": new_price,
                "sl": new_sl,
                "tp": new_tp,
                "type_time": _int(getattr(order, "type_time", 0), 0),
            }
            expiration = _int(getattr(order, "time_expiration", 0), 0)
            if expiration:
                request["expiration"] = expiration
            result = mt5.order_send(request)
            if result is None:
                raise _ipc_error(mt5, "modify returned no result")
            code = _int(getattr(result, "retcode", -1), -1)
            name, message = _retcode(code)
            ok = code in _FILLED_RETCODES
            after = mt5.orders_get(ticket=int(ticket)) or ()
            actual_price = _num(getattr(after[0], "price_open", 0)) if after else 0.0
            actual_sl = _num(getattr(after[0], "sl", 0)) if after else 0.0
            actual_tp = _num(getattr(after[0], "tp", 0)) if after else 0.0
            verified = (
                ok
                and abs(actual_price - new_price) < 1e-6
                and abs(actual_sl - new_sl) < 1e-6
                and abs(actual_tp - new_tp) < 1e-6
            )
            return {
                "ok": ok,
                "ticket": int(ticket),
                "symbol": symbol,
                "price": actual_price,
                "stopLoss": actual_sl,
                "takeProfit": actual_tp,
                "requestedPrice": new_price,
                "requestedStopLoss": new_sl,
                "requestedTakeProfit": new_tp,
                "retcode": code,
                "retcodeName": name,
                "reasons": [] if ok else [message],
                "verified": verified,
                "verifyNote": None if verified else (
                    "the broker accepted the request but the stored order differs"
                ),
                "account": account,
            }

    def cancel_all(self, magic: int) -> dict[str, Any]:
        """Kill switch: cancel only orders Eclipse owns (by magic), never all."""
        if int(magic) <= 0:
            raise AdapterUnavailable(
                "MAGIC_REQUIRED",
                "a non-zero magic is required to cancel Eclipse's orders safely",
            )
        with self._session() as mt5:
            self._require_account(mt5)
            rows = mt5.orders_get() or ()
            targets = [
                row for row in rows
                if _int(getattr(row, "magic", 0), 0) == int(magic)
            ]
            results: list[dict[str, Any]] = []
            cancelled = 0
            for row in targets:
                ticket = _int(getattr(row, "ticket", 0))
                result = mt5.order_send({
                    "action": _int(getattr(mt5, "TRADE_ACTION_REMOVE", 8), 8),
                    "order": ticket,
                })
                code = _int(getattr(result, "retcode", -1), -1) if result else -1
                name, message = _retcode(code)
                ok = code in (10008, 10009)
                cancelled += 1 if ok else 0
                results.append({
                    "ticket": ticket,
                    "retcode": code,
                    "retcodeName": name,
                    "ok": ok,
                    "reason": None if ok else message,
                })
            return {
                "ok": cancelled == len(targets),
                "magic": int(magic),
                "matched": len(targets),
                "cancelled": cancelled,
                "failed": len(targets) - cancelled,
                "results": results,
            }

    # ── reconciliation reads ─────────────────────────────────────────────────
    def deals_since(self, since_ms: int) -> list[dict[str, Any]]:
        """Deals from a wide, server-time-safe window.

        `history_deals_get` expects SERVER time. The Python process clock can be
        hours off (this demo is +2h), so a tight local-time cursor silently drops
        the newest deals. Query a 7-day window with a 2-day future margin and let
        `(connectionId, dealTicket)` dedupe; the cursor is advisory only.
        """
        return self._deals_between(days=7)

    def deals_window(self, days: int) -> list[dict[str, Any]]:
        """Deals from an explicit backfill window (1–3650 days).

        Used by the journal's first import. The window is still server-time
        tolerant (2-day future margin) and deduped by deal ticket, so requesting
        a wide range never duplicates a fill.
        """
        safe_days = max(1, min(int(days or 7), 3650))
        return self._deals_between(days=safe_days, future_days=2)

    def _deals_between(self, *, days: int, future_days: int = 2) -> list[dict[str, Any]]:
        with self._session() as mt5:
            self._require_account(mt5)
            # `history_deals_get` interprets the bounds in terminal/server time,
            # which can differ from this process clock by hours. Widen the lower
            # bound by 3 days so a clock skew can never drop the oldest deals of
            # the requested window; the mirror dedupes by deal ticket, so extra
            # deals are harmless.
            since = datetime.now() - timedelta(days=max(1, int(days)) + 3)
            until = datetime.now() + timedelta(days=future_days)
            rows = mt5.history_deals_get(since, until)
            if rows is None:
                code, _ = _last_error(mt5)
                if code:
                    raise _ipc_error(mt5, "history_deals_get returned no rows")
                return []
            magic = int(self.config.get("magic") or 0)
            out: list[dict[str, Any]] = []
            for row in rows:
                symbol = str(getattr(row, "symbol", "") or "")
                if not symbol:
                    continue
                deal_type = _int(getattr(row, "type", -1), -1)
                entry = _int(getattr(row, "entry", -1), -1)
                deal_magic = _int(getattr(row, "magic", 0), 0)
                out.append({
                    "dealTicket": _int(getattr(row, "ticket", 0)),
                    "orderTicket": _int(getattr(row, "order", 0)),
                    "positionTicket": _int(getattr(row, "position_id", 0)),
                    "symbol": symbol,
                    "side": _DEAL_TYPE.get(deal_type, "other"),
                    "entry": _DEAL_ENTRY.get(entry, "unknown"),
                    "volume": _num(getattr(row, "volume", 0)),
                    "price": _num(getattr(row, "price", 0)),
                    "commission": _num(getattr(row, "commission", 0)),
                    "swap": _num(getattr(row, "swap", 0)),
                    "profit": _num(getattr(row, "profit", 0)),
                    "magic": deal_magic,
                    "comment": str(getattr(row, "comment", "") or ""),
                    "dealtAt": _int(getattr(row, "time_msc", 0)) or (
                        _int(getattr(row, "time", 0)) * 1000
                    ),
                    "eclipseOwned": bool(magic and deal_magic == magic),
                })
            return out

    def order_status(self, ticket: int) -> dict[str, Any]:
        """The broker's latest state for an order ticket (history first)."""
        with self._session() as mt5:
            self._require_account(mt5)
            rows = None
            try:
                rows = mt5.history_orders_get(ticket=int(ticket))
            except Exception:  # noqa: BLE001 - older builds only accept a range
                rows = None
            if not rows:
                try:
                    rows = mt5.orders_get(ticket=int(ticket))
                except Exception:  # noqa: BLE001
                    rows = None
            if not rows:
                return {"found": False, "ticket": int(ticket)}
            row = rows[0]
            state = _int(getattr(row, "state", -1), -1)
            return {
                "found": True,
                "ticket": int(ticket),
                "state": state,
                "stateName": _ORDER_STATE.get(state, "unknown"),
                "symbol": str(getattr(row, "symbol", "") or ""),
                "volumeInitial": _num(getattr(row, "volume_initial", 0)),
                "priceOpen": _num(getattr(row, "price_open", 0)),
            }


__all__ = ["MT5Adapter"]
