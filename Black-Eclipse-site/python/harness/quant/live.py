"""Governed live-strategy trials.

This is the bridge between a tested strategy and the EXISTING broker layer. It
does not create a second order path and it does not bypass an arm, a mandate, a
cap or a kill switch.

Lifecycle:

    approved strategy + recorded backtest
        -> operator arms the live connection
        -> operator creates a live trial (a live mandate pinned to the strategy)
        -> run once: last closed bar -> signal -> one order under the mandate
        -> results: broker fills, positions and P&L
        -> stop: revoke the mandate

The trial is a strategy-specific pin. The broker layer sees it as a workflow
pin (`strategy:<id>:<version>`), so the existing mandate verification, margin
cap, order count, expiry, guardrails, reconciliation and kill switch all apply.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .errors import InvalidSymbol, MissingData, QuantError
from .copilot import calculate_signals
from .data import MarketDataset, series_from_rows
from .data_quality import inspect_dataset
from .datasets import dataset_from_strategy
from .live_runner import LiveRunner
from .oms import order_key
from .oms_store import OrderStore
from .risk_engine import DEFAULT_LIMITS, check_order
from .risk_state import RiskState
from .store import QuantStore
from .strategy import parse_strategy


class LiveGateError(QuantError):
    """A named gate refusal; the code is stable for the UI and the tests."""

    def __init__(self, code: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, detail=detail)
        self.code = code

_SCHEMA = """
CREATE TABLE IF NOT EXISTS QuantLiveTrial (
  id TEXT PRIMARY KEY,
  strategyId TEXT NOT NULL,
  strategyVersion TEXT NOT NULL,
  connectionId TEXT NOT NULL,
  mandateId TEXT,
  pin TEXT NOT NULL,
  symbolsJson TEXT,
  timeframe TEXT,
  volume REAL NOT NULL DEFAULT 0.01,
  stopLossPoints REAL NOT NULL DEFAULT 0,
  takeProfitPoints REAL NOT NULL DEFAULT 0,
  maxOrders INTEGER NOT NULL DEFAULT 5,
  maxMargin REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  note TEXT,
  mode TEXT NOT NULL DEFAULT 'live',
  botId TEXT,
  botVersion INTEGER,
  automationId TEXT,
  lastSignalJson TEXT,
  lastResultJson TEXT,
  ordersUsed INTEGER NOT NULL DEFAULT 0,
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL,
  stoppedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_qlive_strategy ON QuantLiveTrial(strategyId);
CREATE INDEX IF NOT EXISTS idx_qlive_status ON QuantLiveTrial(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _future(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when > datetime.now(timezone.utc)
    except ValueError:
        return False


class LiveStrategyService:
    """Prepare, run, inspect and stop one governed live strategy trial."""

    def __init__(self, quant_store: QuantStore, broker: Any = None) -> None:
        self.store = quant_store
        self._db = quant_store._db
        self._broker = broker
        self._runner = LiveRunner(quant_store)
        self._oms = OrderStore(quant_store)
        self._risk = RiskState(quant_store)
        self._db.executescript(_SCHEMA)
        # Additive migrations: a trial gained SL/TP points, then the bot binding
        # (mode + bot/automation pins) for strategy bots. CREATE-if-not-exists is
        # not enough for an existing database.
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(QuantLiveTrial)")}
        for column, ddl in (
            ("stopLossPoints", "REAL NOT NULL DEFAULT 0"),
            ("takeProfitPoints", "REAL NOT NULL DEFAULT 0"),
            ("mode", "TEXT NOT NULL DEFAULT 'live'"),
            ("botId", "TEXT"),
            ("botVersion", "INTEGER"),
            ("automationId", "TEXT"),
        ):
            if column not in cols:
                self._db.execute(f"ALTER TABLE QuantLiveTrial ADD COLUMN {column} {ddl}")
        self._db.commit()

    @property
    def broker(self) -> Any:
        if self._broker is None:
            from ..trade.bootstrap import get_service

            self._broker = get_service()
        if self._broker is None:
            raise QuantError("the broker layer is not configured in this runtime")
        return self._broker

    # ── gates ────────────────────────────────────────────────────────────────

    def _approval(self, strategy_id: str, version: str) -> dict[str, Any]:
        approved = [
            row for row in self.store.list_approvals(strategy_id)
            if str(row.get("version")) == str(version)
            and str(row.get("status")) == "approved"
        ]
        if not approved:
            raise LiveGateError("APPROVAL_REQUIRED", "the strategy version has no approved review; approve it before a live trial", detail={"strategyId": strategy_id, "version": version})
        return approved[0]

    def _backtest(self, strategy_id: str, version: str) -> dict[str, Any]:
        experiments = self.store.list_experiments(strategy_id=strategy_id, limit=100)
        matching = [
            row for row in experiments
            if str(row.get("strategyVersion") or row.get("version") or "") == str(version)
            and str(row.get("status") or "").lower() in ("completed", "complete", "ok")
        ]
        if not matching:
            raise LiveGateError("BACKTEST_REQUIRED", "no completed backtest is recorded for this strategy version", detail={"strategyId": strategy_id, "version": version})
        return matching[0]

    def _paper_warning(self, strategy_id: str) -> list[str]:
        runs = self.store.list_paper_runs(strategy_id=strategy_id)
        return ([] if runs else
                ["no paper run is recorded; a live trial without a paper run is a "
                 "larger step than it needs to be"])

    # ── lifecycle ────────────────────────────────────────────────────────────

    def prepare(
        self,
        *,
        strategy_id: str,
        version: str,
        connection_id: str = "",
        symbols: list[str],
        mode: str = "live",
        timeframe: str = "H1",
        volume: float = 0.01,
        stop_loss_points: float = 100.0,
        take_profit_points: float = 200.0,
        max_orders: int = 5,
        max_margin: float = 0.0,
        ttl_seconds: int = 3600,
        confirmation: str = "",
        note: str = "",
        bot_id: str = "",
        bot_version: int | None = None,
        automation_id: str = "",
        standing_consent: bool = False,
    ) -> dict[str, Any]:
        """Prepare a governed trial for a strategy, a live trial or a bot.

        `shadow` needs no broker and no phrase: it only evaluates the strategy.
        `demo`/`live` create a bounded mandate (the executive authority) and the
        operator types the phrase ONCE. A bot carries `botId` + `botVersion`; the
        mandate is pinned to both, so editing the bot invalidates it.
        """
        mode = str(mode or "live").lower()
        if mode not in ("shadow", "demo", "live"):
            raise QuantError("mode must be shadow, demo or live")
        spec = self.store.get_strategy(strategy_id, version)
        if spec is None:
            raise LiveGateError(
                "STRATEGY_UNAVAILABLE",
                f"strategy {strategy_id!r} version {version!r} does not exist",
            )
        # Evidence is required for every mode; approval is a money gate.
        self._backtest(strategy_id, version)
        if not symbols:
            raise QuantError("symbols are required")
        cleaned = [str(s).strip().upper() for s in symbols if str(s).strip()]
        pin = f"strategy:{strategy_id}:{version}"

        if mode == "shadow":
            return self._insert_trial(
                strategy_id=strategy_id, version=version, connection_id="",
                mandate_id="", pin=pin, symbols=cleaned, mode=mode,
                timeframe=timeframe, volume=volume,
                stop_loss_points=stop_loss_points,
                take_profit_points=take_profit_points,
                max_orders=max_orders, max_margin=max_margin,
                note=note, bot_id=bot_id, bot_version=bot_version,
                automation_id=automation_id, warnings=[],
            )

        self._approval(strategy_id, version)
        if not connection_id:
            raise QuantError("a demo/live trial needs a broker connection")
        connection = self.broker.get_connection(connection_id)
        if str(connection.get("mode") or "") != mode:
            raise LiveGateError(
                "MODE_MISMATCH",
                f"the selected connection is {connection.get('mode')!s}, not {mode}",
            )
        label = str(connection.get("label") or "")
        if mode == "live":
            expected = f"LIVE TRIAL {strategy_id} {version}"
            if not _future(connection.get("armedUntil")):
                raise LiveGateError(
                    "LIVE_NOT_ARMED",
                    "arm the live connection before creating a live trial",
                    detail={"connectionId": connection_id},
                )
            if max_margin <= 0:
                raise LiveGateError(
                    "MAX_MARGIN_REQUIRED",
                    "a positive maxMargin is required for a live trial",
                )
        else:
            expected = f"MANDATE DEMO {label}"
        if str(confirmation or "").strip() != expected and not standing_consent:
            raise LiveGateError(
                "CONFIRMATION_REQUIRED",
                f"type {expected!r} exactly to create this trial",
                detail={"requiredPhrase": expected},
            )

        mandate_payload: dict[str, Any] = {
            "mode": mode,
            "symbols": cleaned,
            "maxOrders": int(max_orders),
            "maxMargin": float(max_margin),
            "ttlSeconds": int(ttl_seconds),
            "confirmation": f"MANDATE {mode.upper()} {label}",
            "note": f"{mode} strategy trial {strategy_id} v{version}",
        }
        if bot_id:
            # Exactly one pin: a live mandate requires it; a demo mandate accepts
            # it so the same invalidation rule applies to demo authority too.
            mandate_payload["botId"] = bot_id
            mandate_payload["botVersion"] = bot_version
        else:
            mandate_payload["workflowId"] = pin
            mandate_payload["workflowVersion"] = 1
        mandate = self.broker.create_mandate(connection_id, mandate_payload)

        return self._insert_trial(
            strategy_id=strategy_id, version=version,
            connection_id=str(connection_id), mandate_id=mandate["id"], pin=pin,
            symbols=cleaned, mode=mode, timeframe=timeframe, volume=volume,
            stop_loss_points=stop_loss_points,
            take_profit_points=take_profit_points,
            max_orders=max_orders, max_margin=max_margin, note=note,
            bot_id=bot_id, bot_version=bot_version, automation_id=automation_id,
            warnings=self._paper_warning(strategy_id) if mode == "live" else [],
            mandate=mandate,
        )

    def _insert_trial(
        self, *, strategy_id: str, version: str, connection_id: str,
        mandate_id: str, pin: str, symbols: list[str], mode: str,
        timeframe: str, volume: float, stop_loss_points: float,
        take_profit_points: float, max_orders: int, max_margin: float,
        note: str, bot_id: str, bot_version: int | None, automation_id: str,
        warnings: list[str], mandate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trial_id = f"live_{uuid.uuid4().hex[:16]}"
        now = _now()
        self._db.execute(
            "INSERT INTO QuantLiveTrial "
            "(id,strategyId,strategyVersion,connectionId,mandateId,pin,symbolsJson,"
            "timeframe,volume,stopLossPoints,takeProfitPoints,maxOrders,maxMargin,"
            "status,note,mode,botId,botVersion,automationId,lastSignalJson,"
            "lastResultJson,ordersUsed,createdAt,updatedAt,stoppedAt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (trial_id, strategy_id, version, connection_id, mandate_id, pin,
             _json(symbols), timeframe, float(volume), float(stop_loss_points),
             float(take_profit_points), int(max_orders), float(max_margin),
             "ready", note, mode, bot_id or None, bot_version, automation_id or None,
             None, None, 0, now, now),
        )
        self._db.commit()
        trial = self.get(trial_id)
        trial["warnings"] = list(warnings)
        if mandate is not None:
            trial["mandate"] = mandate
        return trial

    def get(self, trial_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM QuantLiveTrial WHERE id=?", (trial_id,)
        ).fetchone()
        if row is None:
            raise QuantError(f"live trial {trial_id!r} not found")
        return self._row(row)

    def list(self, *, strategy_id: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        if strategy_id:
            rows = self._db.execute(
                "SELECT * FROM QuantLiveTrial WHERE strategyId=? "
                "ORDER BY createdAt DESC LIMIT ?", (strategy_id, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM QuantLiveTrial ORDER BY createdAt DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "strategyId": row["strategyId"],
            "strategyVersion": row["strategyVersion"],
            "connectionId": row["connectionId"],
            "mandateId": row["mandateId"],
            "pin": row["pin"],
            "symbols": _loads(row["symbolsJson"], []),
            "timeframe": row["timeframe"],
            "volume": row["volume"],
            "stopLossPoints": row["stopLossPoints"],
            "takeProfitPoints": row["takeProfitPoints"],
            "maxOrders": row["maxOrders"],
            "maxMargin": row["maxMargin"],
            "status": row["status"],
            "note": row["note"],
            "mode": row["mode"] if "mode" in row.keys() else "live",
            "botId": row["botId"] if "botId" in row.keys() else None,
            "botVersion": row["botVersion"] if "botVersion" in row.keys() else None,
            "automationId": row["automationId"] if "automationId" in row.keys() else None,
            "lastSignal": _loads(row["lastSignalJson"], None),
            "lastResult": _loads(row["lastResultJson"], None),
            "ordersUsed": row["ordersUsed"],
            "createdAt": row["createdAt"],
            "updatedAt": row["updatedAt"],
            "stoppedAt": row["stoppedAt"],
            "liveExecution": True,
        }

    def _update(self, trial_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "lastSignalJson", "lastResultJson", "ordersUsed",
                   "stoppedAt", "mandateId"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return self.get(trial_id)
        sets.append("updatedAt=?")
        args.extend([_now(), trial_id])
        self._db.execute(
            f"UPDATE QuantLiveTrial SET {', '.join(sets)} WHERE id=?", args
        )
        self._db.commit()
        return self.get(trial_id)

    # ── signal + execution ───────────────────────────────────────────────────

    # The quant interval vocabulary -> the broker's timeframe vocabulary. A
    # trading bot must be able to signal on the SAME bars its broker quotes,
    # even when the market-data universe does not cover the instrument.
    _BROKER_TIMEFRAMES = {
        "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
        "1h": "H1", "4h": "H4", "1d": "D1", "1w": "W1",
    }

    _BROKER_STEP_SECONDS = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
    }

    def _broker_server_now(self, connection_id: str, symbol: str) -> float | None:
        """The broker's own clock, when a fresh tick can tell us."""
        try:
            reading = self.broker.quote(connection_id, symbol)
        except Exception:  # noqa: BLE001 - no tick means "unknown", never a guess
            return None
        data = (reading or {}).get("data") or {}
        server_ms = data.get("serverTimeMs")
        if not server_ms:
            return None
        server = float(server_ms) / 1000.0
        # A closed market's last tick can be days old; it cannot date a bar.
        if abs(server - datetime.now(timezone.utc).timestamp()) > 12 * 3600:
            return None
        return server

    def _broker_series(self, trial: dict, symbol: str, interval: str, *, count: int = 500) -> Any:
        """The broker's OWN bars for its own symbol, or an honest typed error."""
        timeframe = self._BROKER_TIMEFRAMES.get(str(interval).lower())
        if timeframe is None:
            raise MissingData(
                f"{interval} bars are not a broker timeframe",
                detail={"symbol": symbol, "interval": interval},
            )
        reading = self.broker.bars(
            trial["connectionId"], symbol, timeframe=timeframe, count=count,
        )
        payload = (reading or {}).get("data") or {}
        bars = payload.get("bars") or []
        if not (reading or {}).get("available") or not bars:
            reason = (reading or {}).get("reason") or "the broker returned no bars"
            raise MissingData(
                f"no {interval} bars for {symbol} from the market gateway or "
                f"the broker: {reason}",
                detail={"symbol": symbol, "interval": interval,
                        "code": (reading or {}).get("code") or "BARS_UNAVAILABLE"},
            )
        rows = [
            {
                "timestamp": bar.get("time"),
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": bar.get("tickVolume") or bar.get("volume") or 0,
            }
            for bar in bars
        ]
        # `copy_rates*` includes the FORMING bar. A signal must be computed on
        # the last CLOSED bar, so drop trailing bars whose window is still open
        # (judged on the broker's clock, which is what stamped them).
        server_now = self._broker_server_now(trial["connectionId"], symbol)
        step = self._BROKER_STEP_SECONDS.get(timeframe)
        if server_now is not None and step:
            while rows and rows[-1]["timestamp"] is not None and (
                float(rows[-1]["timestamp"]) + step > server_now
            ):
                rows.pop()
            if not rows:
                raise MissingData(
                    f"every {interval} bar for {symbol} is still forming",
                    detail={"symbol": symbol, "interval": interval},
                )
        return series_from_rows(
            symbol, interval, rows,
            source=f"broker:{(reading or {}).get('kind') or 'unknown'}",
        )

    def _broker_dataset(self, trial: dict, symbol: str, spec: Any) -> MarketDataset:
        """Build the dataset from the broker when the gateway has no series.

        This is the accuracy path for a broker-only instrument: the bars are the
        ones the broker itself will fill against, never a guessed or proxied
        series. Confirmation timeframes are best-effort, exactly like the
        gateway adapter.
        """
        series = {symbol: self._broker_series(trial, symbol, spec.timeframes.primary)}
        higher: dict[tuple[str, str], Any] = {}
        for tf in spec.timeframes.confirmation:
            try:
                higher[(symbol, tf)] = self._broker_series(trial, symbol, tf)
            except (MissingData, InvalidSymbol):
                continue
        return MarketDataset(series=series, higher=higher)

    async def _resolve_dataset(self, trial: dict, target: str, spec: Any,
                               gateway: Any) -> MarketDataset:
        """Gateway first, then the broker's OWN bars.

        A broker symbol is a real, tradable FX pair the market universe often
        does not carry - and a proxied series can be dirty. The broker's bars are
        what the order will actually be filled against, so they win whenever the
        gateway has nothing usable. An honest typed error is raised when neither
        source yields a clean series - never a fabricated one.
        """
        gateway_error: Exception | None = None
        gateway_dataset: MarketDataset | None = None
        try:
            gateway_dataset = await dataset_from_strategy(
                gateway, spec, symbols=[target],
            )
        except (MissingData, InvalidSymbol) as exc:
            gateway_error = exc
        if gateway_dataset is not None:
            quality = inspect_dataset(
                gateway_dataset, symbols=[target], interval=spec.timeframes.primary,
            )
            if quality["gate"]["allowed"]:
                return gateway_dataset
        try:
            # MT5 IPC is blocking; never stall the harness event loop for it.
            broker_dataset = await asyncio.to_thread(
                self._broker_dataset, trial, target, spec,
            )
        except (MissingData, InvalidSymbol) as broker_error:
            if gateway_dataset is not None:
                return gateway_dataset  # let the quality gate explain itself
            raise MissingData(
                f"no bars for {target} from the gateway or the broker "
                f"({gateway_error}; {broker_error})",
                detail={"symbol": target, "interval": spec.timeframes.primary,
                        "gateway": str(gateway_error or ""),
                        "broker": str(broker_error)},
            ) from broker_error
        broker_quality = inspect_dataset(
            broker_dataset, symbols=[target], interval=spec.timeframes.primary,
        )
        if broker_quality["gate"]["allowed"] or gateway_dataset is None:
            return broker_dataset
        return gateway_dataset

    async def signal(self, trial_id: str, *, gateway: Any = None,
                     symbol: str | None = None,
                     dataset: Any = None) -> dict[str, Any]:
        trial = self.get(trial_id)
        record = self.store.get_strategy(
            trial["strategyId"], trial["strategyVersion"])
        spec = parse_strategy(record.get("spec") or record)
        target = (symbol or trial["symbols"][0]).upper()
        if dataset is None:
            dataset = await self._resolve_dataset(trial, target, spec, gateway)
        quality = inspect_dataset(
            dataset, symbols=[target], interval=spec.timeframes.primary,
        )
        if not quality["gate"]["allowed"]:
            raise LiveGateError(
                "DATA_QUALITY_BLOCKED",
                "the live signal is blocked by critical data findings",
                detail={"quality": quality},
            )
        result = calculate_signals(spec, dataset, symbols=[target])
        points = (result.get("signals") or {}).get(target) or []
        series = dataset.series.get(target)
        last_index = series.length - 1 if series is not None else -1
        current = next((p for p in reversed(points)
                        if int(p.get("index", -1)) == last_index), None)
        action = "none"
        if current:
            action = {
                "long_entry": "buy", "short_entry": "sell",
                "long_exit": "close", "short_exit": "close",
            }.get(str(current.get("signal")), "none")
        payload = {
            "trialId": trial_id,
            "symbol": target,
            "bar": current.get("ts") if current else (
                series.timestamps[last_index] if series is not None else None),
            "signal": current.get("signal") if current else "none",
            "action": action,
            "reason": current.get("reason") if current else "no signal on the last closed bar",
            "indicators": current.get("indicators") if current else {},
            "bars": series.length if series is not None else 0,
            "warnings": result.get("warnings") or [],
        }
        self._update(trial_id, lastSignalJson=_json(payload))
        return payload

    async def run_once(
        self, trial_id: str, *, gateway: Any = None, volume: float | None = None,
        symbol: str | None = None, dataset: Any = None,
        automation_id: str = "", bot_version: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        trial = self.get(trial_id)
        if trial["status"] not in ("ready", "running", "paused"):
            raise QuantError(f"live trial is {trial['status']}; it cannot run")
        signal = await self.signal(trial_id, gateway=gateway, symbol=symbol,
                                   dataset=dataset)
        action = signal["action"]
        result: dict[str, Any] = {"signal": signal, "order": None, "closed": []}
        if action == "none":
            result["message"] = "No order: no signal on the last closed bar."
        elif action == "close":
            closed = self._close_symbol(trial, signal["symbol"])
            result["closed"] = closed
            result["message"] = f"Closed {len(closed)} position(s) for {signal['symbol']}."
        else:
            positions = self._open_positions(trial, signal["symbol"])
            if positions:
                result["message"] = (
                    f"No order: {len(positions)} open position(s) already exist for "
                    f"{signal['symbol']}."
                )
                result["code"] = "POSITION_ALREADY_OPEN"
            else:
                bar_ts = str(signal.get("bar") or "")
                claim = self._runner.claim(
                    strategy_id=trial["strategyId"],
                    version=trial["strategyVersion"],
                    timeframe=str(trial.get("timeframe") or "H1"),
                    bar_ts=bar_ts,
                )
                if not claim.get("claimed"):
                    result["message"] = (
                        f"No order: this bar is {claim.get('reason')}.")
                    result["run"] = claim.get("run")
                    result["code"] = "BAR_ALREADY_PROCESSED"
                    self._update(trial_id, lastResultJson=_json(result))
                    return result
                run_id = claim["run"]["id"]
                try:
                    quote_reading = self.broker.quote(
                        trial["connectionId"], signal["symbol"])
                    quote = (quote_reading.get("data") or {}) if isinstance(
                        quote_reading, dict) else {}
                    if not quote:
                        raise QuantError("no broker quote for the risk check")
                    account = (self.broker.account(trial["connectionId"]).get("data")
                               or {})
                    positions = self._open_positions(trial, signal["symbol"])
                    risk_state = self._risk.check(trial_id, account)
                    result["riskState"] = risk_state
                    if risk_state["kill"]["triggered"]:
                        self._runner.release(run_id)
                        self._auto_kill(trial)
                        result["message"] = (
                            "Trading stopped by the risk kill: "
                            + "; ".join(risk_state["kill"]["reasons"]))
                        self._update(trial_id, lastResultJson=_json(result))
                        return result
                    side = "buy" if action == "buy" else "sell"
                    price = float(quote.get("ask") if side == "buy"
                                  else quote.get("bid") or quote.get("ask") or 0)
                    point = float(quote.get("point") or 0.00001)
                    stop_points = float(trial.get("stopLossPoints") or 0)
                    take_points = float(trial.get("takeProfitPoints") or 0)
                    stop_loss = None
                    take_profit = None
                    if stop_points > 0:
                        stop_loss = price - stop_points * point if side == "buy" \
                            else price + stop_points * point
                    if take_points > 0:
                        take_profit = price + take_points * point if side == "buy" \
                            else price - take_points * point
                    payload = {
                        "symbol": signal["symbol"],
                        "side": side,
                        "type": "market",
                        "volume": float(volume or trial["volume"]),
                        "stopLoss": round(stop_loss, 8) if stop_loss else None,
                        "takeProfit": round(take_profit, 8) if take_profit else None,
                        "comment": f"strategy:{trial['strategyId']}",
                    }
                    limits = {
                        **DEFAULT_LIMITS,
                        "maxMargin": float(trial["maxMargin"]),
                        "maxLot": max(float(trial["volume"]), 0.01),
                        "maxOpenPositions": 2,
                        "requireStop": True,
                    }
                    risk = check_order(
                        intent=payload, quote=quote, account=account,
                        positions=positions, limits=limits,
                        data_healthy=True, broker_healthy=True,
                    )
                    result["risk"] = risk
                    if not risk["allowed"]:
                        # Nothing was sent, so the closed bar must stay retryable:
                        # keep the claim only when an order actually reached the
                        # broker.
                        self._runner.release(run_id)
                        result["message"] = "Risk refused: " + "; ".join(risk["reasons"])
                        result["code"] = "RISK_REFUSED"
                        self._update(trial_id, lastResultJson=_json(result))
                        return result
                    key = order_key(trial["strategyId"], trial["strategyVersion"],
                                    trial.get("timeframe"), bar_ts)
                    if dry_run:
                        # Preview only: the broker's own order_check + policy +
                        # guardrails. No send, no approval row, no mandate use.
                        order = self.broker.preview_order(
                            trial["connectionId"], payload,
                            record_approval=False, run_id="",
                        )
                        result["order"] = order
                        result["dryRun"] = True
                        result["message"] = (
                            "Preview allowed: " + str(order.get("status") or "checked")
                            if order.get("status") not in ("REFUSED",)
                            else "Preview refused: " + "; ".join(order.get("reasons") or [])
                        )
                        self._runner.finish(run_id, state="done")
                        self._update(trial_id, lastResultJson=_json(result))
                        return result
                    # Auto mode: renew the arm + mandate at the last moment
                    # before a real send, and only while the operator's standing
                    # consent is still valid (the runner checked it). A renewal
                    # refusal is a decision, not a crash, and the bar stays
                    # retryable. Dry runs never consume or renew authority.
                    # Only a BOT (automation_id) renews: a manual/graph trial was
                    # prepared by a human at the keyboard and must never renew
                    # itself (and a broker double need not answer its calls).
                    try:
                        if automation_id:
                            from ..trade.renewal import ensure_authority  # noqa: PLC0415

                            ensure_authority(
                                self.broker, self, trial_id,
                                automation_id=automation_id, bot_version=bot_version,
                            )
                            # The renewal moved the trial to a new mandate; submit
                            # must read the refreshed row, not the stale snapshot.
                            trial = self.get(trial_id)
                    except Exception as exc:  # noqa: BLE001 - typed refusal
                        self._runner.release(run_id)
                        result["message"] = f"No order: {exc}"
                        result["code"] = str(
                            getattr(exc, "code", "") or "AUTHORITY_RENEWAL_FAILED")
                        self._update(trial_id, lastResultJson=_json(result))
                        return result
                    oms = self._oms.create(payload, key=key)
                    self._oms.transition(oms["id"], "validated",
                                         detail="risk passed")
                    self._oms.transition(oms["id"], "sent",
                                         detail="submitted to broker")
                    order = self.broker.submit_order(
                        trial["connectionId"], payload,
                        mandate_id=trial["mandateId"] or "",
                        run_id=trial_id, workflow_id=trial["pin"],
                        node_id="quant_strategy",
                        automation_id=automation_id,
                        automation_version=bot_version,
                        bot_id=automation_id,
                        bot_version=bot_version,
                    )
                    oms = self._oms.reconcile(oms["id"], order)
                    result["oms"] = oms
                    result["order"] = order
                    result["message"] = order.get("status") or "order returned"
                    self._runner.finish(
                        run_id,
                        state="done" if order.get("status") else "failed",
                        error=str(order.get("reasons") or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - a failed run must be visible
                    self._runner.finish(run_id, state="failed", error=str(exc))
                    result["message"] = f"No order: {exc}"
                    # Preserve the typed refusal so the bot's decision does not
                    # masquerade as "position already open".
                    result["code"] = str(getattr(exc, "code", "") or "") or "BROKER_UNAVAILABLE"
                    self._update(trial_id, lastResultJson=_json(result))
                    return result
        self._update(
            trial_id,
            status="running" if action != "none" else trial["status"],
            lastResultJson=_json(result),
            ordersUsed=int(trial["ordersUsed"]) + (
                1 if result.get("order") and not dry_run else 0),
        )
        return result

    def stop_by_bot(self, bot_id: str) -> list[dict[str, Any]]:
        """Stop every trial pinned to this bot. Positions are never closed here."""
        if not bot_id:
            return []
        rows = self._db.execute(
            "SELECT id FROM QuantLiveTrial WHERE botId=? AND status!='stopped'",
            (str(bot_id),),
        ).fetchall()
        stopped: list[dict[str, Any]] = []
        for row in rows:
            try:
                stopped.append(self.stop(row["id"]))
            except Exception:  # noqa: BLE001 - one bad row must not block the rest
                continue
        return stopped

    def _open_positions(self, trial: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
        try:
            reading = self.broker.positions(trial["connectionId"])
        except Exception:  # noqa: BLE001 - a read failure must not send an order
            return []
        data = reading.get("data") if isinstance(reading, dict) else None
        rows = data if isinstance(data, list) else []
        return [row for row in rows
                if str(row.get("symbol") or "").upper() == symbol.upper()]

    def _close_symbol(self, trial: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
        closed = []
        for position in self._open_positions(trial, symbol):
            ticket = position.get("ticket")
            if ticket is None:
                continue
            try:
                closed.append(self.broker.close_position(
                    trial["connectionId"], ticket))
            except Exception as exc:  # noqa: BLE001
                closed.append({"ticket": ticket, "status": "ERROR", "error": str(exc)})
        return closed

    # ── results + stop ───────────────────────────────────────────────────────

    def results(self, trial_id: str) -> dict[str, Any]:
        trial = self.get(trial_id)
        account = self.broker.account(trial["connectionId"])
        positions = self.broker.positions(trial["connectionId"])
        orders = self.broker.orders(trial["connectionId"])
        position_rows = (positions.get("data") or []) if isinstance(positions, dict) else []
        if not isinstance(position_rows, list):
            position_rows = []
        pnl = sum(float(row.get("profit") or 0) + float(row.get("swap") or 0)
                  + float(row.get("commission") or 0) for row in position_rows)
        return {
            "trial": trial,
            "account": account,
            "positions": positions,
            "orders": orders,
            "openPositions": len(position_rows),
            "openPnl": pnl,
            "liveExecution": True,
            "note": "The broker is the source of truth. This is not a backtest.",
        }

    def _auto_kill(self, trial: dict[str, Any]) -> None:
        """Stop new orders and revoke the mandate. Positions stay for the operator."""
        if trial.get("mandateId"):
            try:
                self.broker.revoke_mandate(trial["mandateId"])
            except Exception:  # noqa: BLE001
                pass
        self._runner.set_control(trial["strategyId"], trial["strategyVersion"],
                                 "paused")
        self._update(trial["id"], status="stopped", stoppedAt=_now())

    def stop(self, trial_id: str) -> dict[str, Any]:
        trial = self.get(trial_id)
        if trial["mandateId"]:
            try:
                self.broker.revoke_mandate(trial["mandateId"])
            except Exception:  # noqa: BLE001 - stop must still mark the trial closed
                pass
        return self._update(trial_id, status="stopped", stoppedAt=_now())


__all__ = ["LiveStrategyService"]
