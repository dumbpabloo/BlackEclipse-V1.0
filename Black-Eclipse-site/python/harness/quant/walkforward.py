"""Walk-forward analysis.

Rolls a train→test window across the timeline. Parameters are optimized **only
on the training window**; the chosen configuration is then evaluated on the
unseen test window. The training dataset is a strict prefix of the test
dataset — there is no code path by which a test bar reaches the optimizer
(asserted by a test that a mutated test period cannot change the chosen params).

The report distinguishes in-sample, out-of-sample and combined, and computes the
IS/OOS divergence so a curve-fit configuration is visible rather than hidden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .data import MarketDataset
from .engine import ENGINE_VERSION, BacktestConfig, run_backtest
from .metrics import MetricInputs, MetricsEngine
from .optimize import OptimizationConfig, OptimizationEngine, objective_score
from .params import ParamSpace, apply_params
from .research import ResearchResult, warn
from .strategy import StrategySpec


class WalkForwardError(ValueError):
    pass


@dataclass
class WalkForwardConfig:
    train_bars: int = 250
    test_bars: int = 60
    step_bars: Optional[int] = None
    method: str = "random"
    objective: str = "sharpe"
    param_space: dict[str, Any] = field(default_factory=dict)
    max_evaluations: int = 40
    seed: int = 42
    initial_capital: float = 100_000.0
    risk_free_rate: float = 0.0
    anchored: bool = False
    #: Bars at the end of the timeline that no train or test window may touch.
    #: 0 keeps the historical behaviour; >0 reserves a final holdout so selection
    #: never sees the data the verdict is read from (the reference harness's
    #: ``holdout_bars``).
    holdout_bars: int = 0
    #: Cap on the number of folds, oldest first (the reference's ``max_folds``).
    max_windows: Optional[int] = None


def _aggregate(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ["total_return", "sharpe", "sortino", "max_drawdown", "profit_factor",
            "win_rate", "trade_count", "expectancy", "cagr"]
    out: dict[str, Any] = {}
    for key in keys:
        vals = [m.get(key) for m in metrics_list if m.get(key) is not None]
        if not vals:
            out[key] = None
        elif key == "trade_count":
            out[key] = sum(vals)
        else:
            out[key] = sum(vals) / len(vals)
    return out


class WalkForwardEngine:
    def __init__(self, strategy: StrategySpec, dataset: MarketDataset,
                 config: Optional[WalkForwardConfig] = None,
                 *, progress: Optional[Any] = None) -> None:
        self.strategy = strategy
        self.dataset = dataset
        self.config = config or WalkForwardConfig()
        self._progress = progress
        self.space = ParamSpace.from_dict(self.config.param_space)
        if self.space.is_empty():
            raise WalkForwardError("walk-forward needs a non-empty parameter space")
        self.timeline = dataset.timeline()

    def windows(self) -> list[tuple[int, int, int, int]]:
        cfg = self.config
        step = cfg.step_bars or cfg.test_bars
        n = len(self.timeline)
        if cfg.holdout_bars < 0:
            raise WalkForwardError("holdout_bars cannot be negative")
        if cfg.max_windows is not None and cfg.max_windows < 0:
            raise WalkForwardError("max_windows cannot be negative")
        # A reserved holdout is never part of a train or test window, so the
        # verdict is read from bars that selection could not see.
        limit = n - cfg.holdout_bars
        out: list[tuple[int, int, int, int]] = []
        start = 0
        while True:
            train_start = 0 if cfg.anchored else start
            train_end = start + cfg.train_bars           # exclusive
            test_start = train_end
            test_end = test_start + cfg.test_bars        # exclusive
            if test_end > limit:
                break
            out.append((train_start, train_end, test_start, test_end))
            if cfg.max_windows is not None and len(out) >= cfg.max_windows:
                break
            start += step
        return out

    def _slice(self, a: int, b: int) -> MarketDataset:
        # timeline[b-1] is the last included bar; slice_by_timestamps is inclusive.
        return self.dataset.slice_by_timestamps(self.timeline[a], self.timeline[b - 1])

    def run(self) -> ResearchResult:
        started = time.monotonic()
        windows = self.windows()
        if not windows:
            return ResearchResult(
                kind="walk_forward", status="INSUFFICIENT_HISTORY",
                metadata={"strategyId": self.strategy.id, "engineVersion": ENGINE_VERSION},
                configuration={"trainBars": self.config.train_bars,
                               "testBars": self.config.test_bars, "bars": len(self.timeline)},
                warnings=[warn("INSUFFICIENT_HISTORY",
                               "the dataset is too short for one train/test window")],
                observability={"windows": 0}, capability={"walkForward": "available"},
                payload={"windows": []},
            )

        segments: list[dict[str, Any]] = []
        combined_equity: list[float] = []
        combined_ts: list[Optional[str]] = []
        combined_trades: list[Any] = []
        running = self.config.initial_capital
        oos_metrics_all: list[dict[str, Any]] = []
        is_metrics_all: list[dict[str, Any]] = []

        for window_index, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
            if self._progress is not None:
                try:
                    self._progress("window", window_index + 1, len(windows))
                except Exception:  # noqa: BLE001 - progress must never break a run
                    pass
            train_ds = self._slice(tr_s, tr_e)
            test_ds = self._slice(te_s, te_e)
            opt = OptimizationEngine(
                self.strategy, train_ds, self.space,
                OptimizationConfig(
                    method=self.config.method, objective=self.config.objective,
                    max_evaluations=self.config.max_evaluations, seed=self.config.seed,
                    initial_capital=self.config.initial_capital,
                    risk_free_rate=self.config.risk_free_rate,
                ),
            ).run()
            best = opt.payload.get("best")
            if not best:
                continue
            best_params = best["params"]
            spec = StrategySpec.model_validate(apply_params(self.strategy.to_dict(), best_params))
            oos = run_backtest(spec, test_ds, BacktestConfig(
                initial_capital=self.config.initial_capital,
                risk_free_rate=self.config.risk_free_rate, random_seed=self.config.seed,
            ))
            # Chain this OOS segment onto the running equity.
            seg_equity = [p["equity"] for p in oos.equity_curve]
            seg_ts = [p["ts"] for p in oos.equity_curve]
            if seg_equity:
                scale = running / seg_equity[0] if seg_equity[0] else 1.0
                for v, ts in zip(seg_equity, seg_ts):
                    combined_equity.append(v * scale)
                    combined_ts.append(ts)
                running = combined_equity[-1]
            combined_trades.extend(oos.trades)
            oos_metrics_all.append(oos.metrics)
            is_metrics_all.append(best.get("metrics", {}))
            segments.append({
                "train": {"start": self.timeline[tr_s], "end": self.timeline[tr_e - 1]},
                "test": {"start": self.timeline[te_s], "end": self.timeline[te_e - 1]},
                "bestParams": best_params,
                "inSample": {k: best.get("metrics", {}).get(k) for k in
                             ("total_return", "sharpe", "max_drawdown", "trade_count")},
                "outOfSample": {k: oos.metrics.get(k) for k in
                                ("total_return", "sharpe", "max_drawdown", "trade_count")},
            })

        if not segments:
            return ResearchResult(
                kind="walk_forward", status="NO_VALID_TRIALS",
                metadata={"strategyId": self.strategy.id, "engineVersion": ENGINE_VERSION},
                configuration={"trainBars": self.config.train_bars, "testBars": self.config.test_bars},
                warnings=[warn("NO_VALID_TRIALS", "no window produced a valid configuration")],
                observability={"windows": len(windows)}, capability={"walkForward": "available"},
                payload={"windows": []},
            )

        engine = MetricsEngine(MetricInputs(
            equity=combined_equity, timestamps=combined_ts, trades=combined_trades,
            exposure=[], interval=self.dataset.primary_interval(),
            initial_capital=self.config.initial_capital,
            risk_free_rate=self.config.risk_free_rate,
        ), asset_class=self.strategy.asset_class)
        combined_metrics = engine.values()
        is_agg = _aggregate(is_metrics_all)
        oos_agg = _aggregate(oos_metrics_all)

        is_score = objective_score(self.config.objective, is_agg)
        oos_score = objective_score(self.config.objective, oos_agg)
        divergence: dict[str, Any] = {"inSample": is_score, "outOfSample": oos_score}
        warnings: list[dict[str, Any]] = []
        if is_score is not None and oos_score is not None:
            divergence["ratio"] = (oos_score / is_score) if is_score else None
            if is_score > 0 and oos_score < is_score * 0.5:
                warnings.append(warn(
                    "IS_OOS_DIVERGENCE",
                    "out-of-sample objective is materially weaker than in-sample "
                    f"({oos_score:.3f} vs {is_score:.3f})",
                    {"inSample": is_score, "outOfSample": oos_score},
                ))

        best_ratio = len(combined_equity)
        payload = {
            "objective": self.config.objective,
            "windows": segments,
            "inSampleAggregate": is_agg,
            "outOfSampleAggregate": oos_agg,
            "combined": combined_metrics,
            "divergence": divergence,
            # Bounded combined OOS equity (full series is in the stored artifact).
            "combinedEquity": [{"index": i, "ts": ts, "equity": v}
                               for i, (ts, v) in enumerate(zip(combined_ts, combined_equity))][:5000],
        }
        metadata = {
            "engineVersion": ENGINE_VERSION, "strategyId": self.strategy.id,
            "strategyVersion": self.strategy.version,
            "symbols": self.strategy.universe.symbols,
            "interval": self.dataset.primary_interval(),
        }
        configuration = {
            "trainBars": self.config.train_bars, "testBars": self.config.test_bars,
            "stepBars": self.config.step_bars or self.config.test_bars,
            "anchored": self.config.anchored, "method": self.config.method,
            "objective": self.config.objective, "seed": self.config.seed,
            "maxEvaluations": self.config.max_evaluations,
            "initialCapital": self.config.initial_capital,
            "space": self.config.param_space,
        }
        return ResearchResult(
            kind="walk_forward", status="OK", metadata=metadata,
            configuration=configuration, metrics=combined_metrics, warnings=warnings,
            observability={"windows": len(segments), "combinedPoints": best_ratio,
                           "durationMs": int((time.monotonic() - started) * 1000)},
            capability={"walkForward": "available"}, payload=payload,
        )


__all__ = ["WalkForwardConfig", "WalkForwardEngine", "WalkForwardError"]
