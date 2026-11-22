---
title: Quant Engine — Trading, Backtesting & Research
status: Current
last-verified: 2026-11-18
---

# Quant Engine — Trading, Backtesting & Research

**Who this is for:** anyone working on backtesting, strategy evaluation, or any
part of `python/harness/quant/`.

**The one rule:** the LLM orchestrates (authors/edits strategy specifications,
calls tools, explains results); the **deterministic Python Quant Engine computes
everything** — fills, P&L, position sizes, drawdown, Sharpe, VaR. No layer
outside `harness/quant/` may compute a trading statistic, and the engine never
fabricates a value: unavailable inputs produce a typed error or an explicit
warning, never a silent zero.

## What it is

A modular, event-driven backtesting engine. The pipeline is explicit:

```
market data → indicator context → signal (rules) → risk → order
            → execution simulator → fill → portfolio → equity/exposure → metrics
```

## Ownership map

| Concern | File |
|---|---|
| Strategy contract, rule DSL, validation, semver | `quant/strategy.py` |
| Strategy IR schema identity, compatibility, migration, content hash | `quant/schema.py` |
| Portable strategy bundles (export/import + safety) | `quant/bundle.py` |
| Typed strategy graph model (visual builder authoring front) | `quant/graph_model.py` |
| Versioned strategy node registry + built-in nodes | `quant/nodes.py` |
| Strategy graph compiler (graph → canonical IR) | `quant/compiler.py` |
| Python Strategy SDK (in-process driver) | `quant/sdk.py` |
| Isolated Python strategy worker + managed runtime | `quant/sdk_worker.py`, `quant/sdk_runtime.py` |
| Reference Python strategies | `quant/examples/` |
| AI copilot support (signals, research plan, report, rule/sizing helpers) | `quant/copilot.py` |
| AI copilot agent tools | `quant/copilot_tools.py` |
| Benchmark-relative metrics (alpha/beta/IR/tracking error) | `quant/benchmark.py` |
| Governance, source inspection, multi-file projects, paper shadow runs | `quant/ops_tools.py` |
| Approved dependency policy | `quant/sdk_deps.py` |
| Rule evaluation over arrays | `quant/rules.py` |
| Causal indicators (EMA/SMA/RSI/ATR/MACD/Bollinger/…) | `quant/indicators.py` |
| Market-data adapter, point-in-time dataset, quality checks, MTF alignment | `quant/data.py` |
| Gateway → dataset resolution | `quant/datasets.py` |
| Operator-declared dataset → dataset resolution (their own REST API) | `quant/custom_source.py` |
| Order lifecycle + state machine | `quant/orders.py` |
| Commission / slippage / spread / impact / latency | `quant/execution.py` |
| Position sizing methods | `quant/sizing.py` |
| Pre-trade risk limits | `quant/risk.py` |
| Backtest simulation portfolio (cash/positions/P&L/exposure) | `quant/portfolio.py` |
| Performance / risk / trade statistics + definitions | `quant/metrics.py` |
| Event-driven loop + `BacktestResult` | `quant/engine.py` |
| Vectorized research engine (representable subset only) | `quant/vector_engine.py` |
| Standalone commission/slippage/spread/impact diagnostics | `quant/costs.py` |
| Bootstrap CIs + chronological out-of-sample split | `quant/validation.py` |
| Deflated Sharpe + Probability of Backtest Overfitting (CSCV) | `quant/multiple_testing.py` |
| Options pricing/IV/Greeks + multi-leg strategy analytics | `quant/options.py` |
| Continuous futures contract splice + back-adjustment | `quant/futures.py` |
| Advanced tools (engine selection, cost sim, OOS, bootstrap, compare) | `quant/advanced_tools.py` |
| Parameter spaces (dotted paths, ranges, grid/sample) | `quant/params.py` |
| Optimization (grid/random/evolutionary/Bayesian, objectives, Pareto, stability) | `quant/optimize.py` |
| Walk-forward (train/test, IS/OOS/combined, divergence) | `quant/walkforward.py` |
| Monte-Carlo (reshuffle/bootstrap/regime-block/parameter/execution perturbation) | `quant/montecarlo.py` |
| Causal market-regime classification (trend × volatility) + trade attribution | `quant/regime.py` |
| The Edge Verdict (A–F grade over edge/robustness/risk/sample + overfit tells) | `quant/verdict.py` |
| Prop-firm evaluation rules as data (targets, drawdown, daily loss, consistency) | `quant/propfirm.py` |
| Prop-firm Monte-Carlo (pass probability, days-to-pass, payout EV) | `quant/propfirm_mc.py` |
| The Edge Report — the grade, the evidence and the figures as one artifact | `quant/edge_report.py` |
| Method families, cards, fit ranking and market-structure diagnostics | `quant/discovery/` (taxonomy, library, diagnostics, hypotheses, synthesize, miner) |
| Research intelligence: scholarly search, method specs, extraction with provenance, suitability, reproduction, dossiers | `quant/research_intel/` + `routers/quant_research.py` |
| Cost of carry (borrow, financing, margin), partial fills, corporate actions | `quant/carry.py`, `quant/corporate_actions.py`, `quant/execution.py` |
| Run manifests + reproducibility | `quant/manifest.py` |
| Research sessions, hypotheses, trials, discovered methods | `quant/store.py` (`QuantResearchSession`, `QuantHypothesis`, `QuantTrial`, `QuantMethod`) |
| Portfolio construction, risk model, stress | `quant/portfolio_optimizer.py`, `quant/risk_model.py`, `quant/scenario.py` |
| Historical option-chain backtest | `quant/options_backtest.py` |
| Report / notebook export | `quant/report_export.py` |
| QuantBench | `quant_bench/` |
| The Quant Figure Kit — deterministic chart specs from stored artifacts | `quant/visuals.py` |
| The Quant Figure Kit's agent tool (`render_quant_figures`) | `quant/visual_tools.py` |
| Pine Script runtime (a TradingView script on Eclipse's own bars) | `quant/pine.py` + `pine-engine/` |
| Sensitivity + robustness + overfitting safeguards | `quant/robustness.py` |
| Shared research-result envelope | `quant/research.py` |
| Strategy + experiment persistence, artifacts | `quant/store.py` |
| Golden synthetic datasets | `quant/fixtures.py` |
| Background run runner (status/progress/cancel) | `quant/runner.py` |
| Agent tools | `quant/tools.py` |
| HTTP routes | `routers/quant.py` (engines), `routers/quant_reports.py` (the surfaces that judge a result) |
| Boot wiring | `quant/bootstrap.py` |
| UI tab | `src/components/screens/backtest/` + `src/lib/backtest/` |

## Point-in-time correctness (the anti-lookahead contract)

1. Every bar carries `data_available_at` (its close). The engine processes the
   merged timeline and only exposes a bar once it has closed.
2. Indicators are **causal**: the value at `i` uses only `≤ i`; the warm-up
   region is `NaN`, and a rule referencing a `NaN` operand is `False`.
3. Bar-processing order is fixed and tested: (1) fill orders submitted on an
   earlier bar, (2) check protective legs against the stop levels carried *into*
   the bar — a trailing stop is advanced with this bar's favorable extreme only
   if it did **not** trigger, so a same-bar high cannot tighten a stop that the
   same bar's low already breached, (3) evaluate exit signals, (4) evaluate entry
   signals at the close, (5) snapshot. Nothing computed from bar `i` fills an
   order on bar `i`. Position sizing and protective-bracket sizing are likewise
   causal: volatility sizing uses only bars up to and including the decision bar,
   and an ATR-based stop/trail is measured from the **decision** bar
   (`order.submitted_index`), never the later fill bar.
4. Multi-timeframe: a higher-TF bar is available to a primary bar only when the
   higher bar's close is at or before the primary bar's close
   (`data.align_higher_to_primary`). Confirmation indicators are namespaced
   `key@tf`; higher-TF price fields are exposed as `close@tf`, etc.

## Data sources — built-in providers vs. your own dataset

A run has exactly one origin. By default it is the Market Data Gateway (the
provider router). A caller may instead name a dataset the operator declared in
**Settings → Data Sources** (`Custom REST API`: their own endpoint + key, mapped
into a dataset and read through the bridge capability
`custom_dataset_records`).

The contract (`quant/custom_source.py` + `quant/datasets.py`):

1. **Exclusive, never mixed.** Naming a `dataSource` bypasses the gateway and its
   durable history cache entirely. One run never blends operator data with
   provider data.
2. **No fallback.** A source that cannot be read is `CUSTOM_SOURCE_UNAVAILABLE`
   (bridge/dataset/fetch) or `MISSING_DATA` (no usable rows), and the run stops.
   Silently switching providers would report a result built on data the caller
   did not choose.
3. **The window is enforced locally.** `start`/`end` are passed to the source as
   `from`/`to` *and* re-applied to the returned rows, so an endpoint that ignores
   them cannot widen a backtest.
4. **Declared shape only.** Records are mapped to
   `timestamp/open/high/low/close/volume` (common aliases such as `time`, `o`,
   `c`, `v` are accepted). Rows without a timestamp + close are reported as
   unusable — never inferred.
5. **Provenance is stamped.** `result.metadata.dataSource` records
   `{kind: "custom", dataset, provenance: "operator_declared", verified: false}`
   and a `CUSTOM_DATA_SOURCE` warning is appended, so a run on user data can
   never be mistaken for Eclipse-verified market data.

Surface: `run_backtest`, `run_portfolio_backtest`, `run_python_strategy`,
`optimize_parameters`, `run_walk_forward`, `run_monte_carlo`,
`run_sensitivity_analysis` and `run_robustness_analysis` accept
`dataSource: {dataset, values?}`. A strategy may also pin
`metadata.dataSource` (the tool argument wins). The Backtest screen's
**Data source** selector sends the same object through
`POST /quant/backtest` / `POST /quant/portfolio-backtest` (typed in
`src/lib/backtest/backtest-client.ts`), listing only datasets that have a
connector attached. Other quant tools read the gateway only and do not accept
the parameter.

## Execution model

- Commission: `none | fixed | percentage | per_share | per_contract | tiered`
  (tiered is marginal on notional).
- Slippage: `none | fixed | percentage | volatility | volume | randomized`
  (randomized is seeded for reproducibility).
- Spread: `none | fixed | percentage | bps | historical`. `historical` uses the
  bar's own absolute spread when the dataset carries a `spread` column; if it
  does not, the cost is zero **with a recorded `HISTORICAL_SPREAD_UNAVAILABLE`
  warning**, never silently.
- Market impact: `none | linear | sqrt` (linear and square-root participation laws;
  both are *assumed* relationships, not calibrated estimates).
- Latency: `latency_bars`.
- A filled `Fill.price` already embeds slippage/spread/impact adversely; those
  components are reported for audit but only `commission` is charged separately
  to cash. A frictionless run only ever results from an explicit `none`
  configuration (`ExecutionAssumptions.is_frictionless`).
- A completed trade's `net_pnl` is gross P&L **minus both the entry and the exit
  commission** (the entry share is allocated proportionally on partial closes),
  so `sum(trade.net_pnl)` reconciles with the cash ledger. `Position.realized_pnl`
  is maintained per symbol.

## Reproducibility & experiments

Each experiment stores the full envelope: strategy id+version, symbols,
interval, range, initial capital, random seed, engine version, execution
assumptions and metrics. Metadata lives in the ONE `agent-memory.db` connection
(additive, idempotent DDL); the large result series (equity/drawdown/trades/
orders/fills) are written as gzipped JSON artifacts under
`.black-eclipse/quant/experiments/`. Same inputs + same seed ⇒ identical result.

## Strategy IR identity, immutable versions & portability

The strategy contract (`StrategySpec`) is the canonical **Strategy Intermediate
Representation**; the visual builder, the Python lab and the AI copilot all
converge on it, and it is the source of truth — never the graph.

- **Schema identity** (`schema.py`): `STRATEGY_SCHEMA_VERSION` names the wire
  format. `schema_compatible` accepts any same-major version (additive changes
  stay loadable) and refuses a different major. `migrate` applies an ordered,
  lossless migration registry (empty at 1.0.0) and **preserves unknown keys**
  rather than dropping them. `spec_content_hash` is a sha256 over the
  canonicalized IR dict (order-independent).
- **Immutable versions** (`store.py`): `QuantStrategyVersion` carries
  `contentHash` + `schemaVersion`. Once an experiment references a version, the
  version is **frozen** — re-saving *different* content under it raises
  `STRATEGY_VERSION_IMMUTABLE` (`409`) and the caller must bump the version;
  identical content re-saves are a no-op. Editing is always a new version.
  Unreferenced versions may still be corrected (backward compatible).
  `version_meta` reports hash/schema/reference-count; `diff_versions` returns a
  structural added/removed/changed diff (`GET /quant/strategies/{id}/diff?a=&b=`).
- **Portable bundles** (`bundle.py`): `export_strategy` / `import_strategy`
  produce/consume a self-describing zip (`manifest.json` + `strategy.json` +
  `README.md`) and a JSON form (`GET /quant/strategies/{id}/export?format=json`).
  Import is **untrusted input**: size caps (compressed + uncompressed), member
  cap, path-traversal rejection, no on-disk extraction, schema gating, a
  hash-mismatch warning, and re-validation through the normal schema path with
  unknown keys preserved. The Backtest UI surfaces export/import/diff in the
  `StrategyPortability` panel; three agent tools (`export_strategy`,
  `import_strategy`, `diff_strategy_versions`) expose the same store/engine.

## Visual Strategy Builder — the graph compiler

The Visual Strategy Builder is a **typed strategy graph** that compiles to the
SAME canonical IR as every other authoring mode. It is deliberately NOT a
general-purpose workflow graph.

- **Graph** (`graph_model.py`): identity + nodes (`id`, `type`, semver, config) +
  typed edges. Shape-checked before compilation.
- **Node registry** (`nodes.py`): each node has a stable `type_id`, its own
  semver (same-major compatible), typed ports (`series`/`number`/`rule`/`any`),
  a config schema/defaults and a pure `compile(ctx, node, inputs)`. Built-ins
  span data/universe, every catalog indicator (+ Bollinger/MACD output selectors),
  logic (compare / cross-above-below / group / constant), trading (entry/exit),
  sizing, stops/targets and risk limits. Add a node without touching the compiler.
- **Compiler** (`compiler.py`): parse → resolve types/versions → validate
  ports/types → deterministic topological order (cycle detection) → compile →
  assemble `StrategySpec` → shared `validate_strategy`. Returns structured
  diagnostics (`UNKNOWN_NODE_TYPE`, `UNSUPPORTED_NODE_VERSION`, `TYPE_MISMATCH`,
  `MISSING_INPUT`, `CYCLE_DETECTED`, `DUPLICATE_NODE_ID`, `BAD_CONFIG`, plus the
  IR validation codes) and a **source map** (spec path → node id). Compilation is
  **deterministic** for equivalent graphs.
- **Surfaces**: `compile_strategy_graph` / `validate_strategy_graph` /
  `list_strategy_nodes` agent tools and `POST /quant/graph/compile`,
  `POST /quant/graph/validate`, `GET /quant/graph/nodes`.
- **No separate UI**: the graph is authored through the CHATBOT (the agent
  composes a `StrategyGraph` — often compiled from a natural-language request —
  and calls `compile_strategy_graph`/`validate_strategy_graph`). The graph is a
  strategy representation only; arbitrary Python is never forced into a graph —
  unsupported constructs remain Python-first.

## Python Strategy Lab — the SDK and managed runtime

A strategy can be authored as ordinary Python (`quant/sdk.py`) and run on the
SAME engine as every other mode.

- **SDK**: subclass `Strategy` with `initialize(ctx)`, `on_data(ctx, bar)`,
  `on_fill`, `on_order_update`, `on_stop`. `Context` exposes params, declared
  indicators (`ctx.indicator(id)`), causal `ctx.history(field, n)`, position and
  portfolio state (`ctx.position/cash/equity`), a seeded `ctx.rng`, and order
  intents (`ctx.buy/sell/close/order`). Stops/targets/sizing/risk are set in
  `initialize`.
- **Shared accounting**: the driver subclasses `BacktestEngine`, so fills
  (next-bar, with commission/slippage/spread/impact/latency), protective legs,
  the risk engine, portfolio accounting and metrics are the SAME code path as a
  declarative backtest. The SDK only supplies per-bar decisions.
- **Managed runtime**: `run_strategy_source` writes the source + data to a temp
  dir and launches `sdk_worker.py` in a **separate process** with `python -I`, a
  **scrubbed environment** (no PYTHONPATH, no credentials/provider keys), a
  **wall-clock timeout** (killed on overrun), an **output-size cap**, and POSIX
  CPU/address-space limits where supported. Results are rehydrated into a typed
  `BacktestResult`. This is process isolation, not a container: a hard
  filesystem/network boundary is the harness OS sandbox's responsibility, so the
  capability is advertised `partial` (honest, not overclaimed).
- **Surfaces**: the `run_python_strategy` agent tool (used from the chatbot) +
  `POST /quant/python/run`; reference strategies in `quant/examples/`
  (`ma_crossover`, `mean_reversion`, `vol_target_trend`).

## AI Strategy Copilot (grounded, not generative of results)

The copilot's LLM proposes and explains; deterministic code computes. It uses the
SAME store/engine/validator as manual strategies and cites what it references.

- **Signals** (`calculate_signals`): the entry/exit decision timeline with reason
  codes and optional indicator snapshots — **no orders are submitted**, no
  portfolio is touched.
- **Research plan** (`create_research_plan`): hypothesis, universe, data
  requirements, IS/validation/holdout splits, benchmark, declared costs, an
  inferred parameter search space, objective, metrics, walk-forward, robustness
  checks, acceptance criteria and known biases (survivorship, overfitting,
  look-ahead). The holdout is used once, never for repeated selection.
- **Report** (`generate_strategy_report`): Markdown grounded ONLY in a stored
  experiment + its artifact — metrics, warnings, trade extremes, reproducibility
  and the "no live orders, not investment advice" note.
- **Iteration helpers**: `generate_entry_exit_rules` (plain-language rules),
  `configure_position_sizing`, `configure_stops_and_targets` (validated copies).
- **Research conveniences**: `get_experiment_status`, `cancel_experiment`,
  `calculate_performance_metrics`, `analyze_trade_log`, `compare_strategies`,
  `run_parameter_sweep`.
- **Routes**: `/quant/signals`, `/quant/strategies/{id}/research-plan`,
  `/quant/experiments/{id}/report`. The `quant_*` agent tools (incl. the
vectorized engine, standalone cost diagnostics, out-of-sample, bootstrap,
multiple-testing, option analytics, futures continuous contracts and comparison
tools in `advanced_tools.py`). These are also exposed over REST:
`POST /quant/vectorized-backtest`, `/quant/costs`, `/quant/out-of-sample`,
`/quant/bootstrap`, `/quant/multiple-testing`, `/quant/option-price`,
`/quant/implied-volatility`, `/quant/option-strategy`, `/quant/continuous-contract`
(thin adapters over the same core functions).

Grounding is carried by the tools themselves: their outputs are deterministic
artifacts (a signal timeline, a plan, a report) or explicit typed errors, so the
model has no tool that returns a fabricated result. Reports and plans always
reference the experiment id / strategy version / seed they were built from.

### Asking for a model (ADR-H242)

Two things used to make "build me a quant model" unreachable:

- **The request never routed here.** The intent classifier did not know that a
  quantitative model is a software artifact, so the turn read as `not_code`
  and never reached the quant tools or the sandbox. The vocabulary now carries
  the SPECIFIC phrases (`quant model`, `trading model`, `alpha model`,
  `factor model`, `momentum strategy`, `trading strategy`, `quant strategy`,
  `backtest strategy`) — specific on purpose, because a bare "model" is a
  finance word ("the business model") and a bare "strategy" is a conversation.
- **The spec schema was opaque.** `create_strategy` declared its `strategy`
  argument as a bare `object`, so the model had to guess the shape from
  validation errors. The schema now publishes the real `StrategySpec` fields
  (`id`, `name`, `universe.symbols`, `timeframes.primary`, `indicators[]`,
  `rules.entry`/`rules.exit`, `risk`, `position_sizing`, `execution`) with a
  minimal working example.

## Indicators, expressions & benchmark metrics

- **Indicator library** (`indicators.py`): SMA, EMA, WMA, RMA, RSI, ATR, StdDev,
  ROC, Highest, Lowest, Bollinger, MACD, VWAP, plus **Stochastic (%K/%D), ADX
  (+DI/-DI), Donchian, Keltner, OBV, MFI, Williams %R and CCI**. All are causal
  (warm-up is NaN); each declares its outputs, so rule references resolve exactly.
- **Expression DSL** (`strategy.py` + `rules.py`): rule operands may be a
  constant, a reference, or a typed **expression** — arithmetic (`+ - * /`),
  `min`/`max`, unary `neg`/`abs`, and a causal `lag`/shift — evaluated per bar.
  Division by zero and missing operands yield no value (the condition is `False`,
  never `inf`/fabricated). Validation recurses into expressions (`UNKNOWN_REFERENCE`,
  `BAD_LAG`). The DSL is safe (no `eval`).
- **Benchmark-relative metrics** (`benchmark.py`): `analyze_against_benchmark`
  computes alpha, beta, correlation, tracking error, information ratio, and
  total/excess return from an experiment's equity curve vs a real benchmark
  series; fewer than 3 aligned observations returns `available: false` with a
  reason (never a fabricated number).

## Governance, inspection & multi-file projects

- **Approvals** (`store.request_approval`/`decide_approval`): a review gate per
  strategy version (`requested → approved|rejected`), recorded on
  `agent-memory.db` (`QuantStrategyApproval`).
- **Deployments** (`store.create_deployment`): a governed, **paper-only** record
  (`QuantStrategyDeployment`). It requires an approved version
  (`APPROVAL_REQUIRED`); `mode="live"` is refused with `LIVE_TRADING_DISABLED`
  and **no code path routes an order** — a deployment is bookkeeping, never an
  execution (`liveExecution: false`).
- **Source inspection** (`inspect_python_strategy`): a static AST pass over
  imports/third-party modules/restricted modules/dangerous calls. Explicitly
  advisory — the isolated worker is the security boundary.
- **Multi-file projects** (`run_python_project`): a `{entry, files}` project runs
  in the same isolated, env-scrubbed, time-limited worker; sibling modules import
  by name.
- **Static validation depth**: `AMBIGUOUS_FILL_TIMING` (warning) and
  `INDICATOR_TF_UNORDERED` (error) join the existing checks.

**Runtime hardening & dependency policy**: approved numerical dependencies
(`sdk_deps.py`) — `inspect_source` flags `UNAPPROVED_DEPENDENCY`, and packages are
never installed on demand. Isolation is process + scrubbed env + timeout + output
cap + POSIX `RLIMIT_CPU`/`RLIMIT_AS`, with an **optional Docker backend**
(`resolve_sandbox`/`docker_command`; `--network none --read-only --memory 512m
--cpus 1 --pids-limit 256`) used when `HARNESS_QUANT_SDK_DOCKER_IMAGE` is set and
Docker is present, else an open fallback to the process worker.

**Paper shadow loop**: `run_paper_session` runs a deployed (approved, active)
strategy as a REAL backtest over recent data and records a `QuantPaperRun`
(experiment id, metrics, status). Live order routing is absent everywhere; the
only reachable deployment mode is `paper` (`liveExecution: false`).

## Vectorized engine (fast path, restricted on purpose)

`quant/vector_engine.py` is a **restricted** fast path, not a second general
backtester. It runs only the strategy semantics that reduce to a causal
signal → target position → mark-to-market pipeline and **refuses everything
else** with a structured `VECTOR_UNSUPPORTED` error (routed to the event engine):
no protective legs, no path-dependent risk rules, no confirmation timeframes, no
execution latency, `fill_at=next_open`, sizing in {fixed_quantity, fixed_dollar,
percent_equity}, one symbol. It reuses the SAME `build_context` indicators,
`rules.evaluate`, `sizing.compute_quantity`, `ExecutionSimulator` and
`MetricsEngine`, so a supported strategy cannot silently change meaning. For the
supported subset the two engines agree **exactly** on the equity curve
(`test_quant_vector_advanced.py::test_vector_event_parity_fixed_quantity`).

## Standalone execution-cost diagnostics

`quant/costs.py` exposes the SAME cost models the engine uses as four standalone
tools — `simulate_transaction_costs`, `simulate_slippage`, `simulate_spread`,
`simulate_market_impact`. Given a list of hypothetical orders (side/quantity/price
+ optional bar context: `atr`, `avgVolume`, `barSpread`, OHLCV) and an
`execution` assumptions block, each returns a per-order audit, the total, the
model used, and warnings where a model lacked a required input (a missing input
contributes zero and is reported — never presented as a measured zero cost). It
accepts camelCase cost keys (`marketImpact`, `fillAt`, …) as well as the field
names, and a frictionless configuration is labelled `frictionless: true`.

## Out-of-sample and bootstrap

- `run_out_of_sample_test` (`quant/validation.py` + `advanced_tools.py`) resolves
  the full dataset, splits it chronologically into an in-sample head and an
  out-of-sample tail (`split_index`), runs the **real event engine** on each
  disjoint slice, and reports both metric sets plus a `leakage`/`split` block
  (`overlapBars: 0`). It persists the OOS run. It cannot itself verify that
  parameters were frozen before the window was seen, so it emits
  `OOS_PARAMETERS_MUST_BE_FROZEN` — an explicit, honest caveat, not a certification.
- `run_bootstrap_test` computes a **percentile** bootstrap CI for a return-series
  statistic (`mean`/`total`/`std`/`sharpe`) using IID, block (circular) or
  stationary resampling. It is seeded and reproducible; IID resampling warns
  `BOOTSTRAP_IID_ASSUMES_INDEPENDENCE`; block size is recorded. Input is either an
  experiment id (its equity curve is converted to per-period returns) or an
  explicit returns array. Only the experiment slice, not a fabricated distribution.
- `compare_backtests` compares stored experiments on a fixed metric set and adds
  comparability cautions when ranges/instruments/capital differ or a run is
  frictionless, so a fair ranking is never implied when assumptions differ.

## Multiple-testing & overfitting diagnostics

`quant/multiple_testing.py` (tool `run_multiple_testing_diagnostics`) implements
two referenced methods from first principles (no SciPy):

- **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014) — the probability the
  selected strategy's per-period Sharpe is positive *after* deflating for the
  number of trials and the returns' skew/kurtosis. Needs the trial-Sharpe variance
  (`trialSharpes` or `srVariance`+`trialCount`); without it the result is
  `available: false` with a reason.
- **Probability of Backtest Overfitting** via Combinatorially Symmetric
  Cross-Validation (Bailey, Borwein, López de Prado & Zhu, 2016) — over all
  balanced IS/OOS partitions of a configs×periods return matrix, how often the
  in-sample best lands in the worse out-of-sample half.

The tool **runs a real bounded parameter sweep** (`strategyId` + `paramSpace`) to
build the trial Sharpes and matrix, or accepts an explicit return series / trial
Sharpes / matrix. It states its assumptions and never applies a test whose inputs
are missing. It is intentionally separate from `robustness.py` (which aggregates
stability/sample-size/regime evidence into a verdict) — neither is applied
mechanically.

## Engine selection

`run_backtest` / `run_event_driven_backtest` run the authoritative event engine;
`run_vectorized_backtest` runs the restricted vector engine and refuses
unsupported strategies. Both produce the same `BacktestResult` envelope, tagged
`metadata.engine` (`event-driven` | `vectorized`).

## Capability state (never overclaimed)

`BacktestResult.capability` and `GET /quant/capabilities` report:

```
available    stocks, crypto, fx, multi-symbol, multi-timeframe,
             commission, slippage, spread (incl. historical), latency, market-impact,
             strategies, experiments, indicators, metrics, portfolio-backtest,
             optimization (grid/random/evolutionary/bayesian), walk-forward,
             monte-carlo, sensitivity, robustness, graph-compiler,
             vectorized-backtest (restricted subset), cost-diagnostics,
             out-of-sample, bootstrap, backtest-comparison, multiple-testing,
             option-pricing/greeks/implied-vol, multi-leg option strategy analytics,
             continuous futures contracts, borrow, financing, margin, partial fills,
             corporate actions, run manifests, research sessions, method discovery,
             method library, strategy mining, current setups, portfolio optimization,
             risk model, stress testing, report export, QuantBench
partial      futures (continuous-contract construction yes; backtest trades the
             continuous PROXY, not deliverables),
             options (pricing/Greeks/IV/strategy analytics + live chains yes;
             historical options BACKTESTING no),
             python-strategies (isolated worker process; not a container)
unsupported  — (nothing advertised unavailable that is implemented)
```

Capabilities not listed are **not registered as tools** and are advertised
unavailable rather than faked.

## Options & futures analytics

`quant/options.py` and `quant/futures.py` fill the last asset-class gaps. Options
**pricing does not get re-implemented**: they reuse `market.greeks.black_scholes`
(the one options model) and add:

- `price_option` — theoretical price + Greeks (assumptions stated with the result);
- `implied_volatility` — the inverse of Black-Scholes by bisection, refusing prices
  outside the no-arbitrage bounds so it never returns a fabricated vol;
- `analyze_option_strategy` — multi-leg expiry payoff curve, net premium,
  break-evens, and **max profit/loss reported `null` when unbounded** (never a
  fabricated grid maximum), plus position-level net Greeks (× multiplier).

`build_continuous_contract` splices ordered futures contracts at a roll schedule
with `difference` / `ratio` back-adjustment (or `none`), records every roll (date,
contracts, gap, factor), leaves the newest contract unadjusted, and labels the
result an explicit backtest **proxy**. Option **backtesting** and deliverable
futures contracts are deliberately not claimed.

## Research engines

- **Optimization** (`optimize.py`): grid, random, evolutionary, and Bayesian
  (GP-EI) search over a `params.ParamSpace` of dotted strategy paths. Objectives
  are oriented higher-is-better (`max_drawdown` negated, `return_drawdown` =
  CAGR/|DD|). Multi-objective runs return a Pareto front. Results carry the
  leaderboard, per-axis parameter **stability**, and warnings for low sample size
  and raw-return objectives — the engine never presents the highest raw return as
  the winner by default.
- **Walk-forward** (`walkforward.py`): rolls train→test windows; parameters are
  optimized **only** on the training slice (a test that mutates test-only bars
  asserts the chosen params do not move), then evaluated on the unseen test
  slice. Reports in-sample / out-of-sample / combined and the IS→OOS divergence.
  `holdout_bars` reserves a tail **no train or test window may touch**, so the
  verdict is read from bars selection never saw; `max_windows` caps the folds oldest-
  first. Both default to the historical behaviour (`0` / `None`) and the fold
  arithmetic is asserted against Vibe-Trading's formula on five shared fixtures
  (`python/tests/test_quant_walkforward_parity.py`).
- **Artifact decay** (`decay.py`): a strategy or factor is not pass/fail — it moves
  `ACTIVE → MONITORING → DECAYED → DISABLED` on *consecutive* weak readings (3 non-
  healthy to monitor, 2 decayed-or-worse to decay, one healthy reading to recover,
  3 critical to disable), with the reference's thresholds for IC ratio, IR, IC
  positive ratio and Sharpe. The weakest metric wins, so a healthy IC cannot hide a
  collapsed Sharpe. Pure logic: no I/O, no clock.
- **Monte-Carlo** (`montecarlo.py`): trade reshuffle, return bootstrap, regime-block
  bootstrap, parameter perturbation, execution perturbation; percentiles, probability
  of loss/ruin, expected/worst drawdown; seeded and reproducible.
- **Sensitivity / robustness** (`robustness.py`): one-at-a-time parameter sweeps
  (best value, collapse, stability), cost sensitivity, trade concentration,
  sample size and regime dependence combine into a deterministic verdict
  (`robust` / `fragile` / `insufficient_evidence`) with plain-language
  conclusions derived from measured evidence.

## Regimes, the regime-block Monte-Carlo and the Edge Verdict

Two questions a single backtest cannot answer: **where did the P&L come from**,
and **should this edge be trusted with real capital**. Two modules answer them,
and they are deliberately separate from the other research engines — those say
*how sensitive* and *how uncertain*; these say *which conditions carried the
result* and *is the evidence good enough*.

### Regimes (`quant/regime.py`)

A regime is the state of the market on two independent axes:

- **trend** — `up` / `down` / `flat`, measured as the distance between a fast and
  a slow EMA **in ATR units**, so the measure is comparable across instruments
  and across volatility regimes;
- **volatility** — `high` / `normal` / `low`, measured as realised volatility
  against its own trailing median.

Two properties are enforced, not assumed:

1. **Causality.** Every value at bar *i* uses bars *<= i* only, so a regime
   label can be attached to a trade taken at *i* without smuggling future
   information into it.
2. **Self-containment.** The regime is derived from the *same bars the backtest
   used* — not a second vendor, not a macro series, not a hand-set label — so a
   regime attribution is reproducible from the stored artifact alone.

The module then places each trade on the regime of its **entry bar** and
attributes realised P&L per regime. `grossPositiveShare` is relative to
**gross positive** P&L, so a regime that only ever lost cannot claim a positive
share by accident.

### The regime-block Monte-Carlo (`montecarlo.py`, method `regime_block_bootstrap`)

The three resampling families are not interchangeable. `trade_reshuffle` and
`return_bootstrap` treat observations as independent, which **understates the
tail** of any strategy whose edge clusters in one market condition. The regime
block method partitions the trade sequence into maximal same-regime runs and
resamples *whole blocks*, so the clustering of good and bad conditions
survives the resample.

Two mixes, both explicit:

- `resample` — blocks are drawn with replacement and re-ordered, so the
  *arrival* of conditions changes between paths while the clustering inside a
  block does not;
- `preserve` — every original block is replaced by a drawn block **of the same
  regime**, so the chronology and the regime mix are kept exactly and only the
  content changes.

Output adds `regimeAttribution` (realised P&L per regime), `regimeBlocks`,
`regimeMix` and `regimeContributions` (the mean / p5 / p95 P&L each regime
contributes across paths). Trades that cannot be placed on a regime are counted
and reported (`REGIME_LABEL_UNAVAILABLE`), never silently dropped.

### The Edge Verdict (`quant/verdict.py`)

`grade_edge` reads the metrics, trades, equity curve, and optionally a
Monte-Carlo result, and grades the **evidence** on four axes — `edge`
(profit factor, Sharpe, expectancy), `robustness` (Monte-Carlo probability of
loss and tail return), `risk` (max drawdown, ulcer index) and `sample` (trade
count) — into a grade from **A to F**.

- An axis that cannot be judged (no Monte-Carlo, no trades) has its weight
  **redistributed** and is named in `assumptions.notGradedOn` — never scored as
  zero and never assumed perfect.
- A **critical flag caps the grade**: a non-positive expectancy, a profit
  factor below one, or a sample too small to tell an edge apart from luck cannot
  be averaged away by a good Sharpe.
- The **overfit tells** it names: a suspiciously smooth equity curve (lag-1
  autocorrelation of per-period returns), P&L concentrated in a few trades, a
  high Monte-Carlo probability of loss, and dependence on a single market
  regime.

The verdict grades what was measured and says what it could not measure. It is
a judgement about evidence, not advice.

Surfaces: agent tools `run_regime_monte_carlo` and `grade_strategy_edge`;
routes `POST /quant/regime-monte-carlo` and `POST /quant/edge-verdict` (the
latter persists the verdict as an `edge_verdict` experiment artifact). Both are
reachable from the front door through the existing `/api/backtest/*` proxy.

## Prop-firm evaluation (the rules as data)

A prop firm sells a funded account for a fee, and the fee is only worth paying
if the trader reaches the profit target **without breaching the risk limits**.
Two modules make that question answerable instead of a matter of opinion.

### The rules (`quant/propfirm.py`)

Every limit — profit target, maximum loss, daily loss limit, consistency rule,
access period, payout split, payout cap — is a **field on a spec**, not a
branch in the code. A firm the engine has never heard of is still supported:
the caller passes their own spec. Three properties are enforced:

1. **The rules are data.** `PropFirmSpec` holds an evaluation phase and a
   funded phase plus the account sizes.
2. **Every spec is sourced and dated.** A spec carries `source` and `as_of`.
   Firm rules change constantly, so the engine reports the spec it used and
   tells the trader to confirm it against the firm's own page. It never
   pretends a built-in spec is current.
3. **The simulation states its own blindness.** Prop firms liquidate on
   *intraday* equity, including unrealised P&L. A log of closed trades cannot
   reconstruct an intraday drawdown that recovered before the close, so every
   report carries `PROP_INTRADAY_EQUITY_NOT_MODELED`.

Built-in specs (read from each firm's own published page on **2026-10-20**):

| Firm | Sizes | Profit target | Maximum loss | Daily loss | Consistency | Access | Payout |
|---|---|---|---|---|---|---|---|
| `topstep` (Trading Combine / XFA) | 50K/100K/150K | $3,000/$6,000/$9,000 | $2,000/$3,000/$4,500 (trailing, locks at the start) | $1,000/$2,000/$3,000 (session break, **not** a breach) | best day ≤ 55% of the target — **raises the target**, never fails | — | 90/10, cap $5,000, min $125, 5 qualifying days |
| `apex_eod` (EOD evaluation / EOD PA) | 25K/50K/100K/150K | $1,500/$3,000/$6,000/$9,000 | $1,000/$2,000/$3,000/$4,000 (EOD-recalculated, static ceiling at start + $100) | $500/$1,000/$1,500/$2,000 (session break, **not** a breach) | none | 30 days | 100% (EOD PA) |
| `ftmo_pro` (2-step challenge / Sim-funded) | 50K/100K/150K | $3,000/$6,000/$9,000 | 10% of initial, static | 5% of initial, recalculated daily — **is** a breach | best day ≤ 50% of total profit — **is** a breach | — | 90%, 5 qualifying days, every 5 days |
| `tpt_pro` (Test / PRO) | 50K | $3,000 | $2,000 (EOD trailing) | none | best day < 50% of the target — **warns only** | — | 80/20, day one, daily |

Only sizes whose numbers appear on the firm's own page are encoded (TPT
publishes the 50K comparison on its product page; the others are not encoded
rather than guessed). An unknown size is refused.

### The rules engine

`simulate_phase` applies one phase's rules to a sequence of trading days and
returns exactly one outcome: `passed`, `failed_max_loss`, `failed_daily_loss`,
`failed_consistency`, `failed_access_period`, `inconclusive_history` or
`inconclusive_no_trades`.

Two behaviours are worth naming because they are easy to get wrong:

- **A log shorter than the access period is `inconclusive`, not `failed`.**
  Ten days of history cannot decide a 30-day evaluation.
- **A consistency rule measured against the *total profit* is decided at the
  decision points, not day by day.** On the first profitable day the best day
  *is* the whole profit, so a mid-flight reading would condemn every trader.
  A rule measured against the *profit target* (Topstep, TPT) is read day by
  day, because that is how the target itself moves.

### The Monte-Carlo (`quant/propfirm_mc.py`)

The uncertainty is expressed by resampling **trading days**, not trades. The
rules are day-based (a daily loss limit, an end-of-day drawdown, an access
period), and a trader's days cluster — a good streak is three good days, not
three good trades scattered across a month. Two modes: `block` (a stationary
bootstrap over the day sequence, so streaks survive) and `iid` (which flatters
the result and is the honest lower bar).

Every path is evaluated against the **real** rules, so a path can fail exactly
the way a live account fails. The report carries the pass probability, the
outcome distribution, days-to-pass, the funded-phase payout distribution, and
the economics — net EV per attempt, expected attempts, expected resets to
pass and cost to pass. **Fees come from the caller**; the engine never invents
a price.

Surfaces: agent tools `run_prop_firm_simulation` and `list_prop_firm_rules`;
routes `POST /quant/prop-firm` and `GET /quant/prop-firm/firms`. A real
trading log comes from the `journal_trades` tool, whose output the simulation
tool consumes.

## The trade-log analysis (`quant/trade_log.py`)

A trade log on its own says what you made. It does not say **when** you made
it, which is the question that decides whether a strategy is worth trading:
did the money come from one lucky hour, or from a condition you could expect
to meet again?

The analysis answers that from the same machinery the Edge Report uses:

- every entry is placed on the **regime of its entry bar** - the same causal
  trend x volatility classifier - so the log is read in the SAME vocabulary as
  the verdict, the Monte-Carlo and the report. One regime language, not two.
- the realised P&L is attributed **per regime**, with the share of gross
  positive P&L each regime carries;
- the **concentration** of the profit is measured: the share of gross positive
  P&L the best few entries carry, which is the difference between an edge and a
  streak. A concentrated log is flagged, because a streak is not a rule;
- each entry carries its own context (regime, trend, volatility, ATR, the
  distance from entry to exit in ATR terms), so one trade can be interrogated.

Entries that cannot be placed on a bar of their own symbol's series are
counted in the totals and reported as **unclassified** - they are never given
a regime they were not measured in.

It is a **measurement**, not a verdict: it never says a strategy is good. It
says where the money came from, and what it could not measure.

Surfaces: the agent tool `analyse_trade_log` (which reads the Journal's own
trades when no log is passed); the route `POST /quant/trade-log`.

## The durable history cache

The gateway caches history **in process**, so every restart re-fetched every bar
from the provider. `market/history_cache.py` adds the durable half on top of
the existing `SeriesStore`: the bars the runtime has already paid for are kept
on the ONE `agent-memory.db` connection, and a later request for the same
window is served from them.

That is the engineering half of "bundled data". It does not buy a vendor's
history - it buys **one copy of the history Eclipse has already seen**, on the
machine, offline, reproducible. The same bars then feed the backtest, the
regime classifier, the Monte-Carlo, the Pine engine and the trade-log
analysis, so all five agree by construction.

Two rules keep it honest:

- **A cached window is served only when it actually covers the request.** A
  partial cache is a miss, not a short answer.
- **A stale tail is refetched.** With no explicit window the cache is only the
  whole history if its last bar is still fresh; otherwise the market has moved
  on and a cache that never refreshed would silently freeze it. A backtest that
  pins its window is served from the cache; an open-ended request refetches the
  tail.

### Seeing it: the local data vault

A cache nobody can see is a cache nobody trusts. The **Local History** widget on
the Command Center shows what the machine actually holds - which instruments,
how many bars, how fresh, from which provider - and the coverage of one
instrument on demand. The runtime's own routes serve it
(`GET /market/history`, `/market/history/{symbol}`, `/market/history/{symbol}/bars`),
the agent can ask the same question through the `history_catalog` tool, and the
bars are served from the local copy, so it works offline.

### Bundling it: the backfill ? no subscription required

The durable cache only holds what has been fetched. `backfill_history` (tool
and `POST /market/history/backfill`) fetches a window **once** and keeps it, so
a local-first runtime can hold a universe the way a hosted platform "bundles"
one: the user decides what to keep, and every later run is served from the
machine.

**No paid data tier is needed.** The keyless providers ? Yahoo Finance, Stooq,
Binance, the ECB reference rates, Treasury/FRED ? are enough to bundle a real
universe, and the backfill says so out loud: it names the provider that served
each symbol, whether that provider needs a key, and which providers are usable
right now with **no** credential. A keyed provider that is not configured is
reported as needing a key, never as free.

That is the difference between hosting and owning the data: the hosted
platform rents you a decade of ticks; this one *keeps* what it fetched, on
your machine, and serves it from there forever.

## Market execution rules — what each market permits

`quant/market_rules.py` is a **different axis** from `quant/venues.py` below: a
**market** is a place that trades instruments (US, A-shares, HK, crypto); a
*venue* is a language a strategy is written in. Do not conflate them.

`StrategySpec.market` (default `us_equity`) names the market whose mandates
apply. It is serialized with the spec and **named on every run-manifest strategy
record** (`"market"`), so a reader does not have to infer it from the content
hash:

| `market` | Settlement | Lot | Price limits | Taxes | Retail short |
|---|---|---|---|---|---|
| `us_equity` | same-day sell | fractional | none | none | allowed |
| `a_share` | **T+1** | 100-share lots | ±10% main / ±20% ChiNext+STAR / ±30% Beijing | 5 bp sell stamp + 0.1 bp transfer | **banned** |
| `hk_equity` | same-day sell | per instrument | none | 10 bp **both** sides | banned |
| `crypto` | same-day sell | fractional (8 dp) | none | none | allowed |

Rules that depend on instrument state are never guessed: `band()` takes an
explicit `board` and returns `None` for an unknown one; `round_size()` leaves
size alone when the market has no fixed lot.

Enforcement (wired into the event engine):

- **Lot mandate** — every entry size is floored to the market's tradeable
  increment; a sub-lot size becomes no trade with `MARKET_LOT_BELOW_ONE`.
  (A `lot_size` a strategy declares is not allowed to override the market.)
- **Price limits** — before a fill is applied, the fill price is checked
  against the band computed from the **previous close**; an out-of-band print is
  cancelled with `MARKET_LIMIT_BAND`. The board comes from the code prefix
  (`board_for_code`: 688/689 STAR, 300/301 ChiNext, 8xx/4xx/920 Beijing, else
  main) — a best-effort mapping, since ST status and listing-day exemptions are
  instrument state the code cannot know.
- **Settlement** — a sell is clamped to the quantity acquired on an earlier
  calendar day; a fully-unsettled exit is refused with `MARKET_T1_UNSETTLED`
  instead of inventing a fill the market would reject.
- `BacktestConfig.market` overrides the per-strategy market (useful for
  A/B-testing the same data under different mandates).

Known limit, stated: the end-of-run **forced liquidation** path does not consult
the T+1 gate. It is a synthetic close of the book, not a market order, and its
fills are labelled `margin_call`/forced, never as signal exits.

## The venues ? one seam, four languages

`quant/venues.py` makes the multi-DSL promise explicit instead of implied. A
**venue** is a language a strategy can be written in, and each one declares
whether it can run, what it needs, and how to reach its engine:

| Venue | Engine | State |
|---|---|---|
| `pine` | `pine-engine` (pinets), isolated | **runs** ? the bars come from Eclipse |
| `ninjascript` | ? | named gap: *no NinjaScript engine is installed; the venue needs a C# runtime* |
| `powerlanguage` | ? | named gap: *no PowerLanguage engine is installed* |
| `easylanguage` | ? | named gap: *no EasyLanguage engine is installed* |

A venue never claims to work. `available` is False and `reason` says exactly
what is missing, until an engine is actually installed and probed. The seam
exists so that adding a venue is a **registration, not a refactor**.

Surfaces: the tool `list_strategy_venues`; the route `GET /quant/venues`.

## The gallery ? a local catalogue of published strategies

`python/harness/gallery/` is the community layer, done honestly for a
local-first, single-operator runtime. QuantPad has a community of strangers; a
local runtime cannot, and pretending to would be a lie. What it CAN have is the
part that matters: a catalogue of **frozen, graded** strategies.

- **publish** freezes a strategy ? and optionally the Edge Report that graded
  it ? into an immutable record. The live strategy may move on; the published
  record does not.
- **clone** copies a published spec into the quant store as a **new**
  strategy. The published record and the live strategy never share an
  identity, and the original is untouched.
- **favourite** counts interest locally.

Surfaces: the tools `publish_strategy`, `list_gallery`,
`favourite_published_strategy`, `clone_published_strategy`; the routes
`/gallery*`; and the **Strategy Gallery** widget on the Command Center.

## Method discovery (`quant/discovery/`)

The engine can test a method; discovery is how a user *finds* one. The loop is
explicit and lives in its own package (documented in
`docs/quant-method-discovery.md`):

```
method card -> structure scan -> falsifiable hypothesis -> spec
            -> bounded mining (IS/OOS) -> the normal validation suite
```

- **Taxonomy/library** (`taxonomy.py`, `library.py`) — 14 families (trend,
  cross-sectional momentum, mean reversion, pairs, carry, crypto funding, low
  vol, seasonality, volatility breakout, ML, options income, event drift) with
  thesis, edge source, signals, failure modes, regime priors, capacity and
  validation plan. `recommend_methods` is a deterministic, explained fit score —
  a prior, never an edge claim.
- **Diagnostics** (`diagnostics.py`) — variance ratios, Hurst, AR(1) half-life,
  absolute-return autocorrelation, weekday buckets, breakout frequency and an
  optional IS/OOS stability check. Descriptive only.
- **Hypotheses** (`hypotheses.py`) — every hypothesis carries a test, acceptance
  rule and invalidation condition.
- **Synthesis** (`synthesize.py`) — method → valid `StrategySpec` (with explicit
  costs/sizing/risk); non-single-asset methods refuse with their `requires`.
  `build_combination` composes two methods into a new validated conjunction.
- **Mining** (`miner.py`) — bounded parameter grid, chronological IS/OOS split,
  the REAL trial count and a Deflated Sharpe; candidates are `candidate | weak |
  rejected`, never "validated".
- **Current setups** — `scan_current_setups` evaluates the entry rule on the
  latest closed bar. A condition, not an order.

## System hardening (`data_quality`, `lifecycle`, `risk_engine`, `oms`)

The 10/10 foundation is in code:

- **Data gate** (`quant/data_quality.py`): duplicate/conflict detection, gap
  classification, broker parity and a content hash. A critical finding blocks
  promotion.
- **Lifecycle** (`quant/lifecycle.py`): `draft → specified → implemented →
  backtested → validated → paper → live_trial → live → monitored → retired`.
  Every transition needs an actor and evidence; an illegal jump is refused.
- **Risk** (`quant/risk_engine.py`): risk-per-trade sizing, lot/notional/margin/
  spread/exposure checks, daily-loss and drawdown kill. A blocked order is
  explained, not silently sent.
- **OMS** (`quant/oms.py`): idempotency key, legal state machine, bracket
  validation, partial-fill accounting, reconciliation and TCA.
- **Runner** (`quant/live_runner.py`): one claim per strategy/version/timeframe/
  bar; restart-safe; pause/resume.
- **Monitoring** (`quant/monitor.py`): signal parity, drift tolerances and P&L
  attribution.
- **Capital** (`quant/capital.py`): inverse-vol risk budgeting with a hard cap.
- **Health** (`quant/system_health.py`): one operator view. A blocked system must
  not send a new order.
- **Routes** (`routers/quant_system.py`) and the Strategy Studio health panel
  surface these checks.

The live trial now claims a run key, runs the risk engine before submit and
records OMS state. Full plan: `docs/quant-system-blueprint.md`.

## Live strategy trials (`quant/live.py`)

A tested strategy can be promoted to a governed live trial. The trial is not a
new execution path: it uses the SAME broker connection, arm, mandate, margin
cap, order count, expiry, money guardrails, reconciliation and kill switch.

- **Gates:** approved strategy version, a completed backtest, the typed phrase
  `LIVE TRIAL <id> <version>`, a live connection, a future `armedUntil`, and a
  positive `maxMargin`. A missing paper run is a visible warning.
- **Pin:** the live mandate uses the `workflowId` field with
  `strategy:<id>:<version>`, so the existing mandate verification applies.
- **Signal:** `copilot.calculate_signals` on the last closed bar. A signal is a
  condition, not an order.
- **Execution:** `run_once` submits one market order under the mandate when no
  position exists for the symbol; an exit signal closes the matching positions.
  A second run does not stack a position.
- **Results:** account, positions, working orders and open P&L come from the
  broker. The broker is the source of truth.
- **Stop:** revokes the mandate. Arming stays a separate operator action in the
  broker surface.

Routes: `/quant/live/trials/*` in `routers/quant_live.py`. UI: Strategy Studio
`live-strategy-panel.tsx`. Doc: `docs/quant-live-strategy.md`.

## Research intelligence (`quant/research_intel/`)

Method discovery starts from Eclipse's own library. Research intelligence adds
the missing front half: what does the literature already say, and can Eclipse
understand and reproduce it?

- **Scholarly discovery** (`scholarly.py`, `query_planner.py`) expands a problem
  into a topic query set and merges OpenAlex, Semantic Scholar, Crossref and
  arXiv, deduplicating by DOI, arXiv id and title. SSRN/NBER/RePEc are reached
  through a scoped web query and labelled `pointer`. A result is a pointer,
  never evidence.
- **Method specification** (`schema.py`, `catalog.py`) is a formal contract:
  objective, inputs (with frequency), horizons, parameters, sourced equations,
  estimation, assumptions, outputs, validation and limitations. The catalog is
  an open, horizon-wide seed (33 methods from intraday to long-term); literature
  extraction and `quant_register_method` add methods at runtime. Open-access PDF
  links and citation pointers from the provider are stored. A specification
  without an equation source fails validation and is never stored.
- **Extraction** (`extraction.py`, `llm_extract.py`) reads sections and equations
  from a document, keeps section + approximate page provenance, and names every
  field it could not find in `unresolved`. An optional model proposal is accepted
  only when each field's quote exists verbatim in the text.
- **Suitability** (`suitability.py`) scores objective fit, data compatibility,
  frequency, horizon, market, assumption transparency, research support,
  reproducibility and computational feasibility. It is a methodological fit
  score, never a probability of profit.
- **Reproduction** (`repro.py`, `implementation.py`) is separate from trading
  adaptation. Twelve built-in estimators run today (OU/AR(1), EWMA, realized
  variance, GARCH, GJR-GARCH, EGARCH, HAR-RV, Engle-Granger, HMM, EVT/GPD,
  CAPM/FF, Almgren-Chriss). `quant_attach_implementation` tests a Python
  `run(rows, params)` and attaches it only on a pass. The worker uses Docker when
  configured, otherwise a separate process with a scrubbed environment, a
  timeout and an output cap. The validation matrix reports only the checks that
  are possible.
- **Figures** (`visuals.py`) compute method coverage, suitability, price,
  rolling volatility, return distribution and reproduction fit from real
  inputs. A figure with no input is `unavailable` with a reason.
- **Dossier** (`dossier.py`) binds the question, literature, equations,
  comparison, reproduction, validation, figures and limitations into one
  artifact in the Artifact Center. Missing evidence is named, not hidden.

Tools: `research_*` (external research, method matrix, figures) and
`quant_list_methods` / `quant_get_method` / `quant_specify_method` /
`quant_reproduce_method` / `quant_validate_method` / `quant_register_method` /
`quant_build_dossier` / `quant_list_dossiers`. Routes live in
`routers/quant_research.py`. The Strategy Studio panel is
`research-intelligence-panel.tsx`. Full specification:
`docs/quant-research-intelligence-plan.md`.

## Realism: carry, margin, partial fills and corporate actions

A short that is free and always borrowable, or a leveraged book that is never
margin-called, is not a defensible backtest. The engine now models:

- **Borrow** (`carry.py`, `strategy.BorrowModel`): locate fee on opening a short,
  recurring borrow accrual by calendar time, and `BORROW_UNAVAILABLE` rejection.
- **Financing** (`carry.py`, `strategy.FinancingModel`): debit/credit interest on
  negative/positive cash, with the day-count convention recorded.
- **Margin** (`carry.py`, `strategy.MarginModel`): maintenance/initial requirement
  and a liquidation at the bar close (`MARGIN_CALL`), through the same execution
  simulator.
- **Partial fills** (`strategy.FillModel`, `execution.py`): participation-capped
  fills; a zero-volume or missing-volume bar does not fill rather than inventing
  liquidity. Fills record `orderQuantity` and `fillRatio`.
- **Corporate actions** (`corporate_actions.py`): dividends as cash and splits as
  quantity/price basis events, applied only when the caller supplies them (an
  adjusted series is never double-counted).
- **Crypto funding / FX carry** helpers live in `carry.py`.

All are **off/zero by default**, so existing strategies keep their exact
behaviour; a caller opts into each cost explicitly.

## Run manifests and research sessions

- **Manifest** (`manifest.py`): every run carries a content-hashed record of the
  strategies, data fingerprint (timestamp + OHLCV hash per symbol), configuration,
  metrics, engine/code version, dependency versions and platform. The digest is
  attached to `BacktestResult.manifest`, stored in `QuantRunManifest`, and
  `reproduce_experiment` re-runs from it and compares metrics + fingerprint.
- **Research sessions** (`store.py`): `QuantResearchSession` →
  `QuantHypothesis` (pre-registered with an acceptance rule) → `QuantTrial`
  (every tested configuration). The journal is the anti-p-hacking record; the
  trial count is what multiple-testing diagnostics should use.

## Portfolio construction, risk and stress

- `risk_model.py`: sample / EWMA / Ledoit-Wolf covariance, correlations, Euler
  risk contributions, factor exposures (OLS betas/R²/residuals), tracking error.
- `portfolio_optimizer.py`: equal-weight, min-variance, mean-variance,
  max-Sharpe, risk parity, HRP and Black-Litterman; explicit net/gross/bounds/
  turnover constraints and a feasibility report.
- `scenario.py`: parametric VaR/ES + component VaR, historical VaR/ES and worst
  windows, explicit asset/factor scenarios (naming uncovered assets), reverse
  stress.
- `options_backtest.py`: multi-leg option position over dated chain snapshots,
  marked with the same Black-Scholes model, settled at expiry. European exercise;
  early exercise/assignment remain `partial`/`unsupported` and are reported.

## Export

`report_export.py` writes a stored experiment as a self-contained HTML report or
a replayable notebook. It reads the artifact and manifest and computes nothing;
the disclaimer and warnings travel with it.

The HTML report also embeds the artifact's **figures** — the same
`quant/visuals` builders and the same chart engine the app's figure panel uses,
rendered to PNG and inlined as `data:` URIs. There is no JavaScript and no
external asset, so the file opens anywhere and stays a single artifact. Figure
rendering is best-effort: if the renderer is unavailable the report says
`Figures unavailable: <reason>` and still prints every number.

## Alpha Zoo and strict bench

`factors/zoo/academic.py` holds documented price/volume factors (overnight gap,
intraday momentum, close-location value, Bollinger position, RSI reversal,
price-volume correlation, Amihud illiquidity, volume shock, drawdown proximity,
up-day ratio, high-low spread, price-volume trend). Each record carries
`zoo`, `source` and `reference`, and its `expression` runs on the ONE safe
formula engine (`stats.formula`) — no Python `eval`. They are merged by name
into `factors.library.BUILTIN_FACTORS`; a zoo entry can never shadow a builtin,
and a duplicate is dropped. A factor whose inputs the data surface cannot supply
ships `available=False` with `unavailableReason` instead of a fake series.

`factors/zoo/alpha101.py` adds the Alpha101 formulas that map **one-to-one** onto
available operators: **101 of 101 — the paper is complete**, each naming its alpha number and paper. Many
need a **cross-sectional** rank and are flagged `requiresPanel` (#8, #10, #18,
#19, #20, #22, #30, #42, #45, #52, #60 among others); they are evaluated by
`factors/panel.py`, the universe-panel evaluator added for exactly this. It takes
`{symbol: rows}` into a TA-N panel, evaluates the same formula AST with
cross-sectional `rank`/`scale` and column-wise rolling operators, and refuses any
function it does not implement (`UNSUPPORTED_FUNCTION`) rather than applying a
per-series implementation to a 2-D array. It is causal by construction: rolling
windows read only the past, cross-sectional operators read one row.

**Inputs are a declared contract, not an assumption.** `adv20` is *derived*
(average dollar volume = `sma(volume * close, 20)`) and inlined in every formula
that needs it, because the bar carries no dollar-volume field. `vwap` is the
opposite case: it **cannot** be derived from OHLCV (`(high+low+close)/3` is a
typical price, not a volume-weighted average), so a factor that needs it declares
`requiresInputs: ["vwap"]` and `harness.factors.contracts` raises a named
`MISSING_INPUT` when the caller has not supplied one — a refusal, never a
plausible NaN column. Two such alphas ship (#41, #42); the rest of the vwap family
stays unported for a narrower reason (below).

The remaining 48 alphas are **not approximated**. Three specific reasons, none of
them an operator gap the engine could close cheaply:

- **`IndClass` neutralisation** (sector/subindustry) — the data surface has no
  industry classification, so those formulas cannot be transcribed without
  inventing one.
- ~~a cross-sectional rank *inside* a rolling correlation~~ — **this was wrong**.
  The panel evaluator evaluates `correlation(rank(volume), rank(vwap), 5)`: `rank`
  produces a T×N array of per-date cross-sectional ranks and the correlation runs
  column-wise over time, which is exactly the published operation. A smoke test
  disproved the old claim and tranche 11 of the zoo ports the family. The claim is
  kept here, struck through, because a doc that quietly deletes its own error
  teaches the next reader nothing.
- **`min`/`max` of two full series, `product`, and `SignedPower` with a
  data-dependent exponent** — none is in `stats.formula`, and adding them for one
  alpha each would be a contract change, not a port.

**The vwap family is verified, not remembered.** Tranche 5's fourteen formulas
were checked against a published reference implementation
(`WorldQuant_alpha101_code`, whose inline comments reproduce the paper's
expressions verbatim), and the paper's fractional windows are **rounded** — the
paper's own convention — with the original constant quoted in each description
(`3.51013 -> 4`, `6.72611 -> 7`, `2.95011 -> 3`, `14.7444 -> 15`). Each of those
alphas declares `requiresInputs: ["vwap"]`, so the contract refuses a caller that
cannot supply a true volume-weighted average.

`factors/zoo/qlib158.py` ports **all 158 of Qlib Alpha158's features**:

- **10 base**: KMID, KLEN, KMID2, KUP, KLOW, KSFT, OPEN0, HIGH0, LOW0 and
  **VWAP0** (the three level features are each price relative to **close**);
- **29 per horizon** at 5/10/20/30/60 sessions: ROC, MA, STD, BETA, RSQR, RESI,
  MAX, MIN, QTLU, QTLD, RANK, RSV, IMAX, IMIN, IMXD, CORR, CORD, CNTP, CNTN, CNTD,
  SUMP, SUMN, SUMD, VMA, VSTD, WVMA, VSUMP, VSUMN, VSUMD.

`PORTED = 10 + 29 * len(HORIZONS)` is derived, and a test asserts it equals the
list length. **VWAP0 is the first Qlib feature to use the input contract**:
`requiresInputs: ["vwap"]`, because a typical price is not a volume-weighted
average and substituting one would measure something else.

Two subtleties are recorded because both are silent when wrong:

- **qlib's `Greater(x, 0)` keeps the value** (`np.maximum`), it is not a 0/1
  comparison. SUMP/SUMN/SUMD and VSUMP/VSUMN/VSUMD therefore use
  `where(greater_than(x, 0), x, 0)`; the comparison alone summed *day-counts* over
  a *magnitude* denominator and produced shares that did not add up — caught by
  the invariant `SUMP + SUMN ≈ S/(S+1) < 1`.
- qlib's `+1` denominators are kept verbatim, so a flat window is `0`, never a
  division by zero.

`TOTAL` is qlib's published 158. The **3 features this list does not name are
counted as unported**, not guessed: claiming a name we cannot verify would ship a
different factor under qlib's label.

`factors/zoo/fundamental.py` declares **15 documented value/quality factors**
(gross profitability, accruals, ROE/ROA, leverage, liquidity, FCF yield, EV/EBITDA,
dividend and buyback yield, asset and sales growth, earnings stability). **All
ship `available=False`** with one reason: the surface does not supply
point-in-time fundamentals, and a restated figure is lookahead that would produce
a beautiful false IC. `fundamental_positions_port()` reports
`declared / available / total` and the blocker.

The library is now 18 builtin + 16 academic + 101 Alpha101 + 155 Qlib + 191 GTJA +
15 fundamental = **499**.

`factors/zoo/gtja191.py` (base tranches) + `gtja191_batches.py` (tranches 5-10) together port **151 of GTJA's 191**. Tranches 2's formulas were
verified against a published GTJA191 implementation (`lrb777/gtja191`,
`src/factors/alpha_001_050.py`, whose comments carry each Chinese-alpha formula),
and the list is kept **sorted by alpha number** so a duplicate or a transposed
number is visible at a glance.

Two things this tranche established:

- **GTJA's `SMA(X,N,M)` is not this DSL's `sma`** — it is the Wilder recursion
  `Y = (M·X + (N−M)·Y') / N`, and it is now a first-class operator (`wilder`, also
  aliased `sma_cn`) in both evaluators, together with GTJA's elementwise
  `MAX(x,y)`/`MIN(x,y)` (`maximum`/`minimum`), and `wma` (weights `N…1`, oldest heaviest — the
  mirror of `decay_linear`). `wilder` also **resets** on a missing input and
  reseeds from the next valid one, which is the published convention.
- **A bug in the previous port was found and fixed:** GTJA #11 is `SUM(…, 6)`, not
  `MEAN(…, 6)` — a factor-of-six error that no IC comparison would have attributed
  to the right cause. A test now asserts `SUM == 6 × MEAN` on the same window.

**All 191 are now ported.** The seven that were briefly unported are resolved:
five needed operators or sources that were one fetch away, and the last two are
explained below. Kept as history:

| # | Blocker |
|---|---|
| 69 | needs `DTM`/`DBM`, a GTJA-specific operator pair whose definition differs between published revisions |
| 91 | the published `MAX(CLOSE,5)` is ambiguous (elementwise vs rolling); the two readings are different factors |
| 137, 172, 186 | the ATR/DMI construction nests elementwise `MAX` inside conditional branches whose published form does not fix the argument order |
| 159 | the publication carries a corrected-weight erratum, so the weights are not verifiable |
| 190 | depends on the same ambiguous pieces as 137/172/186 (asymmetric `SUMIF` guards) |

That is the whole gap: **184 of 191**, with the seven named above. Qlib is
**155 of 158** — the three unclaimed features are not named by any source this
project could verify, so they are counted as unported rather than guessed.

## QuantBench

`quant_bench/` is the measurable contract: routing/intent, honesty/adversarial
answers, deterministic math invariants (causality, costs, partial fills, borrow,
corporate actions, optimizer, stress, discovery), workflow tool order and
reproducibility. Run `python -m harness.quant_bench` (or
`npm run test:quant-bench`); live honesty cases require a supplied answers file.

## The Edge Report (`quant/edge_report.py`)

The verdict, the regimes and the Monte-Carlo used to be three answers in three
places. The Edge Report binds them into **one durable document**: a grade, the
four axes behind it, the flags, the measured numbers, the figures, and — always
— what was *not* measured.

The report computes nothing of its own. Every number in it was measured by the
quant engine, and every picture is a chart spec drawn by the **ONE** chart
engine (ADR-050) and recorded in the Artifact Center. The panels are:

| Panel | Source | Kind |
|---|---|---|
| Equity curve | the artifact's `equityCurve` | `line` |
| Drawdown | the artifact's `drawdownCurve` | `area` |
| Monte-Carlo return percentiles | the Monte-Carlo artifact | `bar` |
| Simulated return distribution | the Monte-Carlo artifact | `bar` |
| Realised P&L by market regime | the regime attribution | `bar` |

A panel with no data is **absent**, not empty: a report without a Monte-Carlo
has no Monte-Carlo panel, and says so (`EDGE_REPORT_WITHOUT_MONTE_CARLO`).

Two honesty rules ride along:

- **A figure is computed, never evidence.** Every chart carries the `computed`
  label the chart contract requires, and the report says it in words too.
- **A long series is sampled, and the figure says so.** The chart engine bounds
  a series at 5,000 points and a category axis at 2,000; the report samples to
  the tighter bound, keeps the first and last points, and writes the number of
  dropped points into the figure's footnote.

The document itself is registered in the Artifact Center as a Markdown
**REPORT** (`edge-report-<experiment>.md`), and each figure is registered as a
CHART artifact, so the report is downloadable and versioned like any other
produced file.

Surfaces: agent tool `generate_edge_report`; route `POST /quant/edge-report`
(both reachable through the existing `/api/backtest/*` proxy); and the
**Edge Report panel** on the Backtest screen
(`src/components/screens/backtest/edge-report-panel.tsx`), which picks a stored
experiment and an optional Monte-Carlo, shows the grade, the four axis scores,
the flags and the figures, and links the document.

The routes that *judge* a result — `regime-monte-carlo`, `edge-verdict`,
`prop-firm`, `prop-firm/firms`, `edge-report` — live in their own router
(`routers/quant_reports.py`) rather than in `routers/quant.py`, which is at its
size budget. A route on a router is not a route until the app includes it, and
a test now asserts the OpenAPI schema carries them.

## Visual deliverables — the Quant Figure Kit

Quant mode is **figure-first**: when a run produces a time series or a
distribution, the answer leads with the computed figure and the prose
interprets it. A figure is a deliverable, not decoration, and it is never
authored by the model.

- **One deterministic kit** (`quant/visuals.py`). Each builder reads a stored
  experiment artifact and returns a chart-engine `ChartSpec`. It never computes
  from a memo, a prompt or a second copy of the data, and it never invents a
  point. A missing input raises `FigureUnavailable(reason)`; the caller receives
  `available: false` instead of an empty picture.
- **The vocabulary** (presets in parentheses):
  `equity`, `underwater`, `monthly_returns`, `trade_distribution` (`core`);
  `exposure`, `rolling_risk`, `cost_waterfall` (`risk`);
  `montecarlo`, `regime_attribution`, `parameter_surface` (`robustness`);
  `walkforward` (`validation`); `everything` is the union.
- **3D is a diagnostic, not a default.** `parameter_surface` draws a real
  `surface` only when the optimization grid is complete (one score per
  parameter pair); an incomplete grid falls back to a `scatter3d` point cloud and
  says so. Time series, compositions and distributions stay 2D.
- **The block names its source.** A `chart` block may carry
  `quant: {experimentId, figure}` instead of a series. The browser asks
  `GET /quant/experiments/{id}/figures/{figure}` for the spec, so a 2,000-point
  curve never travels through the model or the message.
- **Surfaces**: the agent tool `render_quant_figures` (returns compact blocks
  to embed), the route
  `GET /quant/experiments/{id}/figures[/{figure}]`, the chat chart surface
  (`response-chart.tsx`), and the shared `QuantFigurePanel` on the Backtest
  bench (which reuses the chat surface, not a second renderer).
- **The protocol enforces it.** `quant_intent_block` carries the visual
  contract and figure budget; `evaluate_quant_outcome` reports
  `a computed figure` when a series-producing tool ran and no chart was drawn.
  A concept explanation or a one-number answer is still complete without a
  chart: the check only applies to series-producing runs.
- **Honesty rules carry over.** Every figure is `computed`, never evidence. A
  long series is sampled with the number of dropped points in the footnote. A
  monthly heatmap cell for a month the run did not cover is `0.0` for layout and
  the footnote says it is not a measured return.

## Relationship to the Portfolio Accounting Engine

`harness/portfolio/` is the ledger for **real holdings** (long-only, Decimal,
tax lots, FIFO/LIFO/average cost). `quant/portfolio.py` is the **backtest
simulation** portfolio (long/short, float, bar-level mark-to-market). They are
deliberately separate because the accounting core refuses shorts (a required
trading capability) and is tax-lot oriented rather than tick-loop oriented. For
long-only runs the two MUST agree on realized P&L; this is asserted by
`python/tests/test_quant_portfolio_sizing_risk.py::test_reconciles_with_portfolio_accounting_engine_long_only`.
Wiring short-capable strategies into the ledger is deferred and would be an
extension of the accounting core, not a fork (see ADR-H150).

## Production notes

**Live market-data path.** The engine reads bars through the ONE Market Data
Gateway. Two fixes make real history actually loadable: `_fetch_provider` now
forwards each provider's OWN ticker (`providerSymbols`, so `BTC-USD` → `BTCUSDT`
for Binance instead of asking Binance for a pair it does not trade), and
intraday crypto (`1m`…`4h`) prefers Binance over the yfinance/stooq adapters
that collapse intraday bars to a calendar date. Covered by
`python/tests/test_market_intraday_routing.py`.

**Background execution.** Backtests and research runs accept `background: true`
on their POST body; the work is submitted to `quant/runner.py` (an asyncio task
behind a concurrency semaphore) and the caller polls
`GET /quant/runs/{id}[/cancel]`. The runner reports a stage + percent
(optimization reports evaluated/total, Monte-Carlo reports simulations, etc.),
supports cancellation, and retains the result in memory for an hour (the durable
copy is the experiment artifact). The Backtest UI has a "Run in background"
toggle and polls the same endpoints. Heavy runs therefore never block the
request loop.

**Performance.** Indicators are O(n) and the engine is a single pass over the
merged timeline; `python/tests/test_quant_performance.py` guards against an
accidental quadratic (a 5000-bar/eight-indicator backtest runs in well under a
second, asserted under a generous 20s ceiling).

## Extending it

- **New indicator**: add to `INDICATOR_CATALOG` + `compute_one` in
  `indicators.py`; validation picks it up automatically.
- **New data provider**: it must be declared on the TS market bridge
  (`src/lib/harness/market/providers/`) mapped to the `market_data` capability;
  the Python engine only ever calls the gateway.
- **New sizing/risk/cost method**: extend the corresponding model + engine.

## Deferred (documented, not hidden)

**Options analytics are now available** (`options.py`: pricing, implied vol,
Greeks, multi-leg strategy payoff/risk) and the live options-chain/greeks surface
lives in the market layer (`market.greeks`, `get_options_chain`). Two things are
deliberately still not claimed and are advertised `partial`:

- **historical options backtesting** — needs dated option chains and expiry
  handling in the event engine; the analytics tools do not pretend otherwise;
- **deliverable futures contracts** — `build_continuous_contract` produces an
  explicit backtest *proxy* (back-adjusted continuous series), not a physically
  settleable contract.

Everything else in the required tool list has a working, tested engine behind it.

Heavy runs are offloaded in-process (see **Background execution** above), so a
long backtest/optimization no longer blocks the request. Deeper scaling — a
distributed worker pool or the harness job scheduler hosting quant targets with
cross-process persistence — is a later step; today background runs are bounded
by the process lifetime and retained in memory for an hour.

Data-quality findings from the dataset (gaps, duplicates, out-of-order,
incomplete bars) are surfaced as `DATA_*` warnings on the result; they are never
silently dropped.

**Portfolio backtests honour per-strategy assumptions.** A portfolio run shares
ONE portfolio but builds one `ExecutionSimulator` + `RiskEngine` **per strategy
binding**, so each symbol's fills and risk limits follow its own strategy's
costs/limits (a test asserts two strategies with different commission pay their
own rate).
