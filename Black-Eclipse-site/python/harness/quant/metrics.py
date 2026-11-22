"""Metrics engine — deterministic performance, risk and trade statistics.

Every metric carries a `unit`, a `definition` (the exact formula) and the
annualization `method`, so a number can always be explained — no ambiguous
metrics. All inputs are the backtest's own equity curve, trades and exposure
series; the engine computes, it never estimates.

The drawdown/Sharpe/Sortino formulas are the same standard definitions used by
`harness/research/calculations.py` (which remains the canonical cited research
metric); this module operates directly on the equity curve at the backtest's
bar frequency and additionally computes the full trade/exposure statistic set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .data import bars_per_year
from .models import EquityPoint, TradeRecord

TRADING_DAYS_PER_YEAR = 252


@dataclass
class MetricValue:
    name: str
    value: Optional[float]
    unit: str
    definition: str
    method: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "definition": self.definition, "method": self.method}


@dataclass
class MetricInputs:
    equity: list[float]
    timestamps: list[Optional[str]]
    trades: list[TradeRecord]
    exposure: list[EquityPoint]
    interval: str
    initial_capital: float
    risk_free_rate: float = 0.0


def _periods_per_year(interval: str, asset_class: Optional[str]) -> float:
    if asset_class == "crypto":
        return bars_per_year(interval)
    # Equities/FX/bonds: calendar weight. Intraday uses ~252 sessions.
    if interval == "1d":
        return 252.0
    if interval == "1w":
        return 52.0
    if interval == "1mo":
        return 12.0
    seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}.get(interval, 86400)
    return 252.0 * (6.5 * 3600 / seconds)


def _returns(equity: list[float], initial_capital: Optional[float] = None) -> list[float]:
    """Per-bar returns, optionally seeded with the pre-first-bar cash.

    The account held ``initial_capital`` before the first recorded bar, so that
    bar's return is measured from cash, not from itself. Without the seed the
    first bar's P&L is missing from every mean, volatility and ratio computed
    here — which is what the reference (`period_returns`) does and why it seeds.
    """
    out: list[float] = []
    if equity and initial_capital is not None and initial_capital > 0:
        out.append(equity[0] / float(initial_capital) - 1.0)
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev and prev != 0:
            out.append(equity[i] / prev - 1.0)
    return out


def _drawdown_series(equity: list[float], initial_capital: Optional[float] = None) -> list[float]:
    """Drawdown path with the pre-first-bar cash as the initial high-water mark.

    Starting the peak at ``equity[0]`` understates a first-bar loss: the account
    held ``initial_capital`` before that bar, so a curve that opens at 95 after
    risking 100 is in a 5% drawdown, not at zero. Parity with Vibe-Trading's
    ``calc_metrics`` (``peak = equity.cummax().clip(lower=initial_cash)``).
    """
    start = equity[0] if equity else 0.0
    if initial_capital is not None and initial_capital > start:
        start = float(initial_capital)
    peak = start
    out: list[float] = []
    for v in equity:
        if v > peak:
            peak = v
        out.append((v / peak - 1.0) if peak else 0.0)
    return out


def _safe(value: float) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


class MetricsEngine:
    def __init__(self, inputs: MetricInputs, *, asset_class: Optional[str] = None) -> None:
        self.inputs = inputs
        self.asset_class = asset_class
        self.periods = _periods_per_year(inputs.interval, asset_class)
        self.returns = _returns(inputs.equity, inputs.initial_capital)
        self.drawdowns = _drawdown_series(inputs.equity, inputs.initial_capital)

    # ── returns ──────────────────────────────────────────────────────────────
    def total_return(self) -> Optional[float]:
        eq = self.inputs.equity
        if len(eq) < 2 or not eq[0]:
            return None
        return (eq[-1] / self.inputs.initial_capital - 1.0) * 100.0

    def cagr(self) -> Optional[float]:
        eq = self.inputs.equity
        n = len(eq)
        if n < 2 or not eq[0] or eq[0] <= 0 or eq[-1] <= 0:
            return None
        years = (n - 1) / self.periods
        if years <= 0:
            return None
        return ((eq[-1] / self.inputs.initial_capital) ** (1.0 / years) - 1.0) * 100.0

    def annualized_return(self) -> Optional[float]:
        r = self.returns
        if not r:
            return None
        return (math.prod(1.0 + x for x in r) ** (self.periods / len(r)) - 1.0) * 100.0

    def volatility(self) -> Optional[float]:
        r = self.returns
        if len(r) < 2:
            return None
        arr = np.array(r)
        return float(arr.std(ddof=1) * math.sqrt(self.periods) * 100.0)

    def downside_deviation(self) -> Optional[float]:
        r = self.returns
        if len(r) < 2:
            return None
        rf = self.inputs.risk_free_rate / self.periods
        downside = [min(0.0, x - rf) for x in r]
        dd = math.sqrt(sum(d * d for d in downside) / (len(r) - 1))
        return dd * math.sqrt(self.periods) * 100.0

    # ── drawdown ─────────────────────────────────────────────────────────────
    def max_drawdown(self) -> Optional[float]:
        if not self.drawdowns:
            return None
        return min(self.drawdowns) * 100.0

    def average_drawdown(self) -> Optional[float]:
        negatives = [d for d in self.drawdowns if d < 0]
        if not negatives:
            return 0.0
        return (sum(negatives) / len(negatives)) * 100.0

    def ulcer_index(self) -> Optional[float]:
        if not self.drawdowns:
            return None
        return math.sqrt(sum((d * 100.0) ** 2 for d in self.drawdowns) / len(self.drawdowns))

    def var_cvar(self, level: float = 0.95) -> tuple[Optional[float], Optional[float]]:
        r = sorted(self.returns)
        if len(r) < 5:
            return None, None
        idx = max(0, int(math.floor((1.0 - level) * len(r))))
        var = r[idx] * 100.0
        tail = r[: idx + 1]
        cvar = (sum(tail) / len(tail)) * 100.0 if tail else None
        return var, cvar

    # ── risk-adjusted ────────────────────────────────────────────────────────
    def sharpe(self) -> Optional[float]:
        r = self.returns
        if len(r) < 2:
            return None
        rf = self.inputs.risk_free_rate / self.periods
        excess = [x - rf for x in r]
        mean = sum(excess) / len(excess)
        arr = np.array(excess)
        sd = float(arr.std(ddof=1))
        if sd == 0:
            return None
        return (mean / sd) * math.sqrt(self.periods)

    def sortino(self) -> Optional[float]:
        r = self.returns
        if len(r) < 2:
            return None
        rf = self.inputs.risk_free_rate / self.periods
        excess = [x - rf for x in r]
        mean = sum(excess) / len(excess)
        downside = [min(0.0, x - rf) for x in r]
        dd = math.sqrt(sum(d * d for d in downside) / (len(r) - 1))
        if dd == 0:
            return None
        return (mean / dd) * math.sqrt(self.periods)

    def calmar(self) -> Optional[float]:
        cagr = self.cagr()
        mdd = self.max_drawdown()
        if cagr is None or mdd is None or mdd == 0:
            return None
        return cagr / abs(mdd)

    def omega(self, threshold: float = 0.0) -> Optional[float]:
        r = self.returns
        if not r:
            return None
        thr = threshold / self.periods
        gains = sum(x - thr for x in r if x > thr)
        losses = sum(thr - x for x in r if x < thr)
        if losses == 0:
            return None
        return gains / losses

    # ── trade statistics ─────────────────────────────────────────────────────
    @staticmethod
    def _tget(trade: Any, snake: str, camel: str) -> Any:
        """Read a trade field from a TradeRecord or a serialized (camelCase) dict."""
        if isinstance(trade, dict):
            return trade.get(camel, trade.get(snake))
        return getattr(trade, snake)

    def _trade_stats(self) -> dict[str, Optional[float]]:
        trades = self.inputs.trades
        out: dict[str, Optional[float]] = {}
        n = len(trades)
        out["trade_count"] = float(n)
        if n == 0:
            for k in ("win_rate", "loss_rate", "profit_factor", "expectancy", "avg_win",
                      "avg_loss", "largest_win", "largest_loss", "average_holding_period",
                      "median_holding_period", "long_trades", "short_trades",
                      "max_winning_streak", "max_losing_streak"):
                out[k] = None if k not in ("long_trades", "short_trades") else 0.0
            out["trade_count"] = 0.0
            return out
        pnls = [self._tget(t, "net_pnl", "netPnl") for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        out["win_rate"] = len(wins) / n * 100.0
        out["loss_rate"] = len(losses) / n * 100.0
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        out["profit_factor"] = (gross_win / gross_loss) if gross_loss > 0 else None
        out["expectancy"] = sum(pnls) / n
        out["avg_win"] = (sum(wins) / len(wins)) if wins else None
        out["avg_loss"] = (sum(losses) / len(losses)) if losses else None
        out["largest_win"] = max(wins) if wins else None
        out["largest_loss"] = min(losses) if losses else None
        holds = sorted(self._tget(t, "bars_held", "barsHeld") for t in trades)
        out["average_holding_period"] = sum(holds) / len(holds)
        mid = len(holds) // 2
        out["median_holding_period"] = (
            float(holds[mid]) if len(holds) % 2 else (holds[mid - 1] + holds[mid]) / 2.0
        )
        out["long_trades"] = float(sum(1 for t in trades
                                         if self._tget(t, "direction", "direction") == "long"))
        out["short_trades"] = float(sum(1 for t in trades
                                          if self._tget(t, "direction", "direction") == "short"))
        # streaks
        max_w = max_l = cur_w = cur_l = 0
        for p in pnls:
            if p > 0:
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
            elif p < 0:
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
            else:
                cur_w = cur_l = 0
        out["max_winning_streak"] = float(max_w)
        out["max_losing_streak"] = float(max_l)
        return out

    # ── exposure ─────────────────────────────────────────────────────────────
    def _exposure_stats(self) -> dict[str, Optional[float]]:
        exp = self.inputs.exposure
        if not exp:
            return {}
        gross = [e.gross_exposure for e in exp]
        net = [e.net_exposure for e in exp]
        lev = [e.leverage for e in exp]
        equity = [e.equity for e in exp]
        gross_pct = [g / eq * 100.0 for g, eq in zip(gross, equity) if eq]
        net_pct = [x / eq * 100.0 for x, eq in zip(net, equity) if eq]
        return {
            "avg_gross_exposure_pct": (sum(gross_pct) / len(gross_pct)) if gross_pct else None,
            "max_gross_exposure_pct": max(gross_pct) if gross_pct else None,
            "avg_net_exposure_pct": (sum(net_pct) / len(net_pct)) if net_pct else None,
            "max_leverage": max(lev) if lev else None,
            "avg_leverage": (sum(lev) / len(lev)) if lev else None,
        }

    # ── public ───────────────────────────────────────────────────────────────
    def compute(self) -> dict[str, MetricValue]:
        var, cvar = self.var_cvar(0.95)
        tstats = self._trade_stats()
        estats = self._exposure_stats()
        m: dict[str, MetricValue] = {}

        def add(name: str, value: Optional[float], unit: str, definition: str, method: str = "") -> None:
            m[name] = MetricValue(name, _safe(value), unit, definition, method)

        add("total_return", self.total_return(), "%",
            "(final_equity / initial_capital - 1) * 100")
        add("cagr", self.cagr(), "%",
            "((final/initial)^(1/years) - 1) * 100",
            f"{self.periods:g} periods/yr")
        add("annualized_return", self.annualized_return(), "%",
            "geometric annualization of periodic returns", f"{self.periods:g} periods/yr")
        add("volatility", self.volatility(), "%",
            "stdev(periodic returns, ddof=1) * sqrt(periods)", f"{self.periods:g} periods/yr")
        add("downside_deviation", self.downside_deviation(), "%",
            "sqrt(sum(min(0, r - rf_period)^2) / (n - 1)) * sqrt(periods)")
        add("max_drawdown", self.max_drawdown(), "%",
            "min over t of (equity_t / running_peak_t - 1) * 100")
        add("average_drawdown", self.average_drawdown(), "%",
            "mean of all negative drawdown values * 100")
        add("ulcer_index", self.ulcer_index(), "index",
            "sqrt(mean(drawdown_percent^2))")
        add("var_95", var, "%", "historical 5th-percentile periodic return * 100 (no interpolation)")
        add("cvar_95", cvar, "%", "mean of returns at or below VaR * 100")
        add("sharpe", self.sharpe(), "ratio",
            "mean(excess)/stdev(excess, ddof=1) * sqrt(periods)",
            f"rf={self.inputs.risk_free_rate}/yr, {self.periods:g} periods/yr")
        add("sortino", self.sortino(), "ratio",
            "mean(excess)/downside_deviation * sqrt(periods)",
            f"rf={self.inputs.risk_free_rate}/yr")
        add("calmar", self.calmar(), "ratio", "CAGR / abs(max_drawdown)")
        add("omega", self.omega(0.0), "ratio", "sum(gains>0)/sum(losses<0) at threshold 0")

        add("trade_count", tstats.get("trade_count"), "count", "number of closed trades")
        add("win_rate", tstats.get("win_rate"), "%", "winning trades / total * 100")
        add("loss_rate", tstats.get("loss_rate"), "%", "losing trades / total * 100")
        add("profit_factor", tstats.get("profit_factor"), "ratio",
            "gross profit / gross loss")
        add("expectancy", tstats.get("expectancy"), "currency",
            "mean net P&L per trade")
        add("avg_win", tstats.get("avg_win"), "currency", "mean net P&L of winners")
        add("avg_loss", tstats.get("avg_loss"), "currency", "mean net P&L of losers")
        add("largest_win", tstats.get("largest_win"), "currency", "max net P&L")
        add("largest_loss", tstats.get("largest_loss"), "currency", "min net P&L")
        add("average_holding_period", tstats.get("average_holding_period"), "bars",
            "mean bars held per trade")
        add("median_holding_period", tstats.get("median_holding_period"), "bars",
            "median bars held per trade")
        add("long_trades", tstats.get("long_trades"), "count", "number of long trades")
        add("short_trades", tstats.get("short_trades"), "count", "number of short trades")
        add("max_winning_streak", tstats.get("max_winning_streak"), "count",
            "longest run of consecutive winning trades")
        add("max_losing_streak", tstats.get("max_losing_streak"), "count",
            "longest run of consecutive losing trades")

        add("avg_gross_exposure", estats.get("avg_gross_exposure_pct"), "% equity",
            "mean(gross_exposure / equity) * 100")
        add("max_gross_exposure", estats.get("max_gross_exposure_pct"), "% equity",
            "max(gross_exposure / equity) * 100")
        add("avg_net_exposure", estats.get("avg_net_exposure_pct"), "% equity",
            "mean(net_exposure / equity) * 100")
        add("avg_leverage", estats.get("avg_leverage"), "x", "mean(gross_exposure / equity)")
        add("max_leverage", estats.get("max_leverage"), "x", "max(gross_exposure / equity)")
        return m

    def values(self) -> dict[str, Optional[float]]:
        return {name: mv.value for name, mv in self.compute().items()}

    def definitions(self) -> dict[str, dict]:
        return {name: mv.to_dict() for name, mv in self.compute().items()}


__all__ = ["MetricInputs", "MetricValue", "MetricsEngine", "TRADING_DAYS_PER_YEAR"]
