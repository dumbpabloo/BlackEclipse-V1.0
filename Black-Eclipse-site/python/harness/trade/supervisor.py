"""The trading supervisor: portfolio guards and automatic bot stand-down.

Two jobs, both measured from broker/ledger facts and both fail-closed in the
honest direction (an unreadable account is `unmeasured`, never "healthy"):

  1. **Portfolio guards** - total margin vs equity, total open positions, and the
     equity loss since the day/week OPEN (a snapshot taken once, so a restart
     cannot move the baseline). A breach says `block`: no new entries.
  2. **Bot stand-down** - a bot that has taken `minTrades` real fills and is still
     not profitable after commission and swap, or is on a losing streak, is
     `pause`d through the automation service. Pausing stops future triggers; it
     does not close what is already open.

Nothing here closes a position or sends an order. It gates and it disables.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .model import ticket_ref

log = logging.getLogger("harness.trade.supervisor")


@dataclass(frozen=True)
class SupervisorLimits:
    max_portfolio_margin_pct: float = 60.0
    max_portfolio_positions: int = 15
    daily_loss_pct: float = 2.0
    weekly_loss_pct: float = 5.0
    warn_at_pct: float = 80.0
    bot_min_trades: int = 30
    bot_max_consecutive_losses: int = 8
    bot_min_realized_pnl: float = 0.0


def limits_from_config(config: Optional[Mapping[str, Any]]) -> SupervisorLimits:
    raw = dict((config or {}).get("supervisor") or {})

    def _f(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _i(key: str, default: int) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        # A zero/negative count would stop all trading (0) or invert the rule;
        # a malformed override falls back to the safe default, never widens.
        return value if value > 0 else default

    return SupervisorLimits(
        max_portfolio_margin_pct=_f("maxPortfolioMarginPct", 60.0),
        max_portfolio_positions=_i("maxPortfolioPositions", 15),
        daily_loss_pct=_f("dailyLossPct", 2.0),
        weekly_loss_pct=_f("weeklyLossPct", 5.0),
        warn_at_pct=_f("warnAtPct", 80.0),
        bot_min_trades=_i("botMinTrades", 30),
        bot_max_consecutive_losses=_i("botMaxConsecutiveLosses", 8),
        bot_min_realized_pnl=_f("botMinRealizedPnl", 0.0),
    )


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _snapshot_total(accounts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    equity = sum(_num(a.get("equity")) or _num(a.get("balance")) for a in accounts)
    margin = sum(_num(a.get("margin")) for a in accounts)
    positions = sum(int(_num(a.get("positions"))) for a in accounts)
    return {"equity": equity, "margin": margin, "positions": positions}


def assess_portfolio(
    *,
    accounts: Iterable[Mapping[str, Any]],
    day_start_equity: Optional[float],
    week_start_equity: Optional[float],
    limits: Optional[SupervisorLimits] = None,
) -> dict[str, Any]:
    """One verdict for the whole book. `block` means: no new entries."""
    limits = limits or SupervisorLimits()
    rows = [dict(a) for a in accounts if isinstance(a, Mapping)]
    totals = _snapshot_total(rows)
    equity = totals["equity"]
    margin_pct = (
        totals["margin"] / equity * 100.0 if equity > 0 else None
    )
    daily_loss_pct = (
        max(0.0, (day_start_equity - equity) / day_start_equity * 100.0)
        if day_start_equity and equity > 0 else None
    )
    weekly_loss_pct = (
        max(0.0, (week_start_equity - equity) / week_start_equity * 100.0)
        if week_start_equity and equity > 0 else None
    )

    reasons: list[str] = []
    warnings: list[str] = []
    unmeasured: list[str] = []

    if margin_pct is None:
        unmeasured.append("equity: no connected account reported a positive equity")
    elif margin_pct > limits.max_portfolio_margin_pct:
        reasons.append(
            f"Portfolio margin is {margin_pct:.0f}% of equity "
            f"(limit {limits.max_portfolio_margin_pct:g}%); no new entries."
        )
    elif margin_pct > limits.max_portfolio_margin_pct * limits.warn_at_pct / 100.0:
        warnings.append(
            f"Portfolio margin is {margin_pct:.0f}% of equity, near the "
            f"{limits.max_portfolio_margin_pct:g}% limit."
        )

    if totals["positions"] > limits.max_portfolio_positions:
        reasons.append(
            f"{totals['positions']} open positions across connections "
            f"(limit {limits.max_portfolio_positions}); no new entries."
        )

    if daily_loss_pct is None:
        unmeasured.append("day loss: no day-open equity snapshot")
    elif daily_loss_pct >= limits.daily_loss_pct:
        reasons.append(
            f"The book is down {daily_loss_pct:.2f}% today "
            f"(daily stop {limits.daily_loss_pct:g}%); stop for the day."
        )

    if weekly_loss_pct is None:
        unmeasured.append("week loss: no week-open equity snapshot")
    elif weekly_loss_pct >= limits.weekly_loss_pct:
        reasons.append(
            f"The book is down {weekly_loss_pct:.2f}% this week "
            f"(weekly stop {limits.weekly_loss_pct:g}%); stop for the week."
        )

    verdict = "block" if reasons else ("warn" if warnings else "ok")
    return {
        "verdict": verdict,
        "blocking": bool(reasons),
        "reasons": reasons,
        "warnings": warnings,
        "unmeasured": unmeasured,
        "equity": round(equity, 2) if equity else None,
        "margin": round(totals["margin"], 2) if totals["margin"] else None,
        "marginPct": round(margin_pct, 1) if margin_pct is not None else None,
        "openPositions": totals["positions"],
        "dayStartEquity": round(day_start_equity, 2) if day_start_equity else None,
        "weekStartEquity": round(week_start_equity, 2) if week_start_equity else None,
        "dailyLossPct": round(daily_loss_pct, 2) if daily_loss_pct is not None else None,
        "weeklyLossPct": round(weekly_loss_pct, 2) if weekly_loss_pct is not None else None,
        "limits": {
            "maxPortfolioMarginPct": limits.max_portfolio_margin_pct,
            "maxPortfolioPositions": limits.max_portfolio_positions,
            "dailyLossPct": limits.daily_loss_pct,
            "weeklyLossPct": limits.weekly_loss_pct,
        },
    }


def assess_bot(
    *,
    automation_id: str,
    orders: Iterable[Mapping[str, Any]],
    fills: Iterable[Mapping[str, Any]],
    limits: Optional[SupervisorLimits] = None,
) -> dict[str, Any]:
    """Has this bot earned the right to keep trading?

    Only REAL fills count. Fewer than `minTrades` is `insufficient`, which is not
    a verdict - it is the honest state of a new bot.
    """
    limits = limits or SupervisorLimits()
    order_rows = [dict(o) for o in orders if isinstance(o, Mapping)]
    fill_rows = sorted(
        (dict(f) for f in fills if isinstance(f, Mapping)),
        key=lambda f: _num(f.get("dealtAt")),
    )
    pnl = sum(
        _num(f.get("profit")) - _num(f.get("commission")) - _num(f.get("swap"))
        for f in fill_rows
    )
    streak = 0
    for fill in reversed(fill_rows):
        net = (
            _num(fill.get("profit"))
            - _num(fill.get("commission"))
            - _num(fill.get("swap"))
        )
        if net < 0:
            streak += 1
        else:
            break
    trades = len(fill_rows)

    reasons: list[str] = []
    if trades >= limits.bot_min_trades and pnl <= limits.bot_min_realized_pnl:
        reasons.append(
            f"{trades} closed trades and realized P&L {pnl:,.2f} after costs - "
            "the bot has not earned its risk."
        )
    if (
        limits.bot_max_consecutive_losses
        and streak >= limits.bot_max_consecutive_losses
    ):
        reasons.append(
            f"{streak} consecutive losing trades (limit "
            f"{limits.bot_max_consecutive_losses})."
        )

    verdict = "pause" if reasons else ("insufficient" if trades < limits.bot_min_trades else "ok")
    return {
        "automationId": automation_id,
        "verdict": verdict,
        "orders": len(order_rows),
        "trades": trades,
        "realizedPnl": round(pnl, 2),
        "consecutiveLosses": streak,
        "reasons": reasons,
        "limits": {
            "botMinTrades": limits.bot_min_trades,
            "botMaxConsecutiveLosses": limits.bot_max_consecutive_losses,
            "botMinRealizedPnl": limits.bot_min_realized_pnl,
        },
    }


def _default_limits(service: Any) -> SupervisorLimits:
    """Portfolio limits from the first connection's `config.supervisor`.

    Per-account config is the natural home; the supervisor is portfolio-wide, so
    the first explicit block wins and the rest fall back to defaults.
    """
    try:
        connections = service.connections.list()
    except Exception:  # noqa: BLE001 - a read failure must not invent limits
        return SupervisorLimits()
    for connection in connections:
        config = getattr(connection, "config", None) or {}
        if "supervisor" in config:
            return limits_from_config(config)
    return SupervisorLimits()


class TradeSupervisor:
    """Ties the pure assessments to the broker + automation services."""

    def __init__(self, service: Any, *, automations: Any = None) -> None:
        self.service = service
        self.automations = automations

    # -- portfolio ---------------------------------------------------------
    def portfolio(self) -> dict[str, Any]:
        limits = _default_limits(self.service)
        accounts: list[dict[str, Any]] = []
        unmeasured: list[str] = []
        for connection in self.service.connections.list():
            account = self.service.account(connection.id)
            if not account.get("available"):
                unmeasured.append(connection.id)
                continue
            payload = dict(account.get("data") or {})
            positions = self.service.positions(connection.id)
            payload["positions"] = len(positions.get("data") or []) if positions.get("available") else 0
            accounts.append(payload)
        totals = _snapshot_total(accounts)
        now = datetime.now(timezone.utc).isoformat()
        baselines: dict[str, Optional[float]] = {}
        for period in ("day", "week"):
            if totals["equity"] > 0:
                self.service.connections.set_risk_snapshot(
                    "portfolio", period, totals["equity"], now,
                )
            row = self.service.connections.get_risk_snapshot("portfolio", period)
            baselines[period] = _num(row.get("equity")) if row else None
        report = assess_portfolio(
            accounts=accounts,
            day_start_equity=baselines.get("day"),
            week_start_equity=baselines.get("week"),
            limits=limits,
        )
        report["unmeasured"].extend(f"connection {cid}: account unreadable" for cid in unmeasured)
        report["connections"] = len(accounts)
        return report

    # -- bots --------------------------------------------------------------
    def bot_reports(self) -> list[dict[str, Any]]:
        limits = _default_limits(self.service)
        orders = self.service.ledger_orders(limit=500)
        by_bot: dict[str, list[dict[str, Any]]] = {}
        for order in orders:
            bot_id = str(order.get("automationId") or "")
            if bot_id:
                by_bot.setdefault(bot_id, []).append(order)
        reports: list[dict[str, Any]] = []
        for bot_id, bot_orders in sorted(by_bot.items()):
            tickets = {ticket_ref(o.get("ticket") or 0) for o in bot_orders}
            fills: list[dict[str, Any]] = []
            connections = {str(o.get("connectionId") or "") for o in bot_orders}
            for connection_id in connections:
                for fill in self.service.connections.list_fills(connection_id, limit=500):
                    if ticket_ref(fill.get("orderTicket") or 0) in tickets:
                        fills.append(fill)
            reports.append(
                assess_bot(
                    automation_id=bot_id,
                    orders=bot_orders,
                    fills=fills,
                    limits=limits,
                )
            )
        return reports

    # -- act ---------------------------------------------------------------
    def apply(self) -> dict[str, Any]:
        """Pause every bot the assessment says should stand down.

        Idempotent: an already-paused bot is reported, not re-paused. Without an
        automation service nothing is changed and the reports say so.
        """
        reports = self.bot_reports()
        paused: list[str] = []
        failed: list[dict[str, str]] = []
        if self.automations is None:
            for report in reports:
                if report["verdict"] == "pause":
                    failed.append(
                        {"automationId": report["automationId"],
                         "error": "the automation service is not available"}
                    )
            return {"paused": paused, "failed": failed, "reports": reports}
        for report in reports:
            if report["verdict"] != "pause":
                continue
            bot_id = report["automationId"]
            try:
                current = self.automations.get(bot_id)
                if current is None or getattr(current, "state", "") in ("paused", "disabled"):
                    continue
                self.automations.pause(bot_id, actor="supervisor")
                paused.append(bot_id)
            except Exception as exc:  # noqa: BLE001 - report, never crash the sweep
                failed.append({"automationId": bot_id, "error": str(exc)})
        return {"paused": paused, "failed": failed, "reports": reports}

    def report(self) -> dict[str, Any]:
        portfolio = self.portfolio()
        bots = self.bot_reports()
        return {
            "portfolio": portfolio,
            "bots": bots,
            "standingDown": [b["automationId"] for b in bots if b["verdict"] == "pause"],
        }


class SupervisorLoop:
    """One best-effort background sweep. Opt-in via TRADING_SUPERVISOR_SECONDS.

    Each pass takes the day/week baseline (once) and pauses bots that have lost
    the right to keep trading. It never sends an order and never closes one.
    """

    def __init__(self, supervisor: TradeSupervisor, interval_seconds: int) -> None:
        self.supervisor = supervisor
        self.interval = max(30, int(interval_seconds))
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.to_thread(self.supervisor.apply)
            except Exception:  # noqa: BLE001 - a loop must outlive one bad pass
                log.warning("trading supervisor pass failed", exc_info=True)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return

    def start(self) -> None:
        if self._task is not None or self._stopped:
            return
        self._task = asyncio.create_task(self._run(), name="trading-supervisor")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def interval_from_env() -> int:
    try:
        return max(0, int(os.environ.get("TRADING_SUPERVISOR_SECONDS", "0") or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "SupervisorLimits",
    "SupervisorLoop",
    "limits_from_config",
    "assess_portfolio",
    "assess_bot",
    "interval_from_env",
    "TradeSupervisor",
]
