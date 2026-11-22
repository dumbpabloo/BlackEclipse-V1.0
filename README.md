# Black Eclipse

### The open-source financial AI harness that grows with you

**Research. Analyze. Build. Automate. Trade.**

[![Download for Windows](.github/assets/download-windows.svg)](https://github.com/dumbpabloo/black-eclipse/releases/latest/download/BlackEclipse-Setup.exe)

<sub>The Windows installer is unsigned — [verify the SHA-256](#install) before running. macOS and Linux builds are not published yet. [All releases](https://github.com/dumbpabloo/black-eclipse/releases).</sub>

![Core license: MIT](https://img.shields.io/badge/core%20license-MIT-green)
![Pine engine: AGPL-3.0-only](https://img.shields.io/badge/pine%20engine-AGPL--3.0--only-orange)
![Next.js](https://img.shields.io/badge/Next.js-15-black?logo=next.js)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python)

![Bots: a scheduled audit bot reporting what it could and could not retrieve](Black-Eclipse-site/docs/figures/readme/06-bots.png)

Financial intelligence is fragmented.

Research happens in one place. Market data lives somewhere else. Quantitative
models require separate infrastructure. Trading operates independently. And AI
tools often stop at generating answers.

Investors and developers are forced to connect these pieces themselves.

**Black Eclipse brings them together.**

An open-source, AI-native financial harness that connects financial intelligence,
quantitative computing, research, AI agents, portfolio analysis, and trading into
one extensible environment.

Not just an AI assistant that talks about financial markets.

An environment that can research, calculate, develop models, test strategies,
operate tools, and execute controlled financial workflows.

Built with Python and Next.js: provider-agnostic LLM agents, a deterministic
quant engine, backtesting, portfolio analysis, and automated research workflows
— all running on your own machine.

**From financial information to actionable intelligence.**

**Jump to:** [Screenshots](#screenshots) · [Problems](#the-problems-were-solving) · [Different](#what-makes-black-eclipse-different) · [Capabilities](#what-you-can-do-with-eclipse) · [Architecture](#built-as-a-financial-ai-harness) · [Data sources](#data-sources-and-commercial-use) · [Workflow](#from-a-research-question-to-a-tested-strategy) · [Install](#install) · [Benchmarks](#benchmarks) · [Docs](#documentation)

## Screenshots

| | |
|---|---|
| ![Bots: scheduled agents and group rooms](Black-Eclipse-site/docs/figures/readme/06-bots.png) | ![Workflows: the Graph Studio library of typed, versioned graphs](Black-Eclipse-site/docs/figures/readme/05-graph-studio.png) |
| **Bots** — 13 scheduled agents, each `Active` or `Completed`, with the group rooms that run them as a committee | **Workflows** — every graph is typed, versioned and tagged; open one in the editor, or generate one from a prompt |
| ![Eclipse Agent research surface](Black-Eclipse-site/docs/figures/readme/02-agent.png) | ![Turnover map of real instruments sized by traded value](Black-Eclipse-site/docs/figures/readme/03-market-bubbles.png) |
| **Eclipse Agent** — the research surface, driven by slash commands and tools | **Market Bubbles** — real instruments sized by 24h traded value |

These are captures of the running application. They are not mockups.

The bot above is shown mid-report on purpose: it lists what each source returned,
marks the ones it could not retrieve as `UNAVAILABLE`, and names the next permitted
route instead of filling the gap. That is the product.

<!--
  STAGED, NOT YET CAPTURED:

  | ![Dashboard with a live book](Black-Eclipse-site/docs/figures/readme/01-dashboard.png) | ![Journal with demo trades](Black-Eclipse-site/docs/figures/readme/07-journal.png) |
  | **Dashboard** - the daily decision surface | **Journal** - trades, analytics and reviews |

  Both need FRESH prices and a warm harness: a stale vault correctly renders
  LIVE PRICES UNAVAILABLE, which is honest but unusable as a screenshot. See
  docs/public-readme.md section 3.
-->


## The Problems We're Solving

| # | Problem | What Eclipse does about it |
|---|---|---|
| **1** | **Fragmented Financial Information** — data providers, exchanges, websites, filings and news all live apart. | Brings collection, source routing, research and analysis into one environment. Sources carry authority scores by data type, and conflicting figures are disclosed rather than averaged. |
| **2** | **Research Takes Too Much Time** — a company means statements, fundamentals, earnings, management, news, industry. | AI research workflows collect, organize, interpret and evaluate that material, with each claim traceable to the source that produced it. |
| **3** | **Quantitative Analysis Is Inaccessible** — models need specialist tooling and infrastructure. | An environment to develop, test, evaluate and improve models and strategies with AI assistance. The engine computes; you read the result. |
| **4** | **Research and Trading Are Disconnected** — tools analyze or they execute, rarely both. | Connects research, strategy development, trade analysis and execution. With MetaTrader 5 integration it can work with trading operations. |
| **5** | **Financial Workflows Are Difficult to Automate** — the same research and monitoring, done by hand, forever. | An AI-agent architecture that makes financial workflows programmable, reusable and increasingly autonomous. |

## What Makes Black Eclipse Different?

Most financial platforms provide tools. Most AI assistants provide answers.
Black Eclipse connects the tools, the intelligence, and the execution capability
behind a financial workflow.

| | |
|---|---|
| **A harness, not just a chatbot** | Models run inside a structured environment of tools, permissions, memory, execution and financial capabilities — not a bare chat loop with a stock-price plugin. |
| **Computation, not guesswork** | Financial calculations run in deterministic engines. The model decides *what* to calculate and explains the result; it never produces the arithmetic. Every figure carries its formula, inputs, assumptions and seed. |
| **Evidence-based intelligence** | Market information is collected, cross-checked against independent lineage, and evaluated against its source. Conflicts surface instead of being silently blended. |
| **Persistent memory and learning** | Agents retain context, accept corrections, and build on prior work instead of starting from zero each session. |
| **Research connected to execution** | Research, strategy development, trade analysis and a controlled trading integration live in one environment — with the boundary between them enforced. |
| **Extensible by design** | Swap model providers, write tools, define agents, and build workflows without being locked to one vendor or one model. |

That last point is the difference between "use ChatGPT next to your broker" and
having a system: the models are interchangeable, the computation is not.

## What You Can Do With Eclipse

| Capability | What it covers | Where it lives |
|---|---|---|
| **Financial Research** | Investigate companies, securities, industries, financial statements, and market developments. | [`research/`](Black-Eclipse-site/python/harness/research/) · [docs](Black-Eclipse-site/docs/harness/search-retrieval.md) |
| **Market Intelligence** | Collect and analyze market data, fundamentals, macroeconomic information, news, and financial events. | [`market/`](Black-Eclipse-site/python/harness/market/) · [docs](Black-Eclipse-site/docs/data-sources.md) |
| **Quantitative Analysis** | Calculate financial metrics, evaluate securities, analyze portfolios, and investigate market behavior. | [`finance_facts/`](Black-Eclipse-site/python/harness/finance_facts/) · [docs](Black-Eclipse-site/docs/harness/financial-analysis-engine.md) |
| **Strategy Development** | Develop quantitative strategies, generate models, write code, and evaluate trading hypotheses. | [`quant/`](Black-Eclipse-site/python/harness/quant/) · [docs](Black-Eclipse-site/docs/quant-engine.md) |
| **Backtesting & Evaluation** | Test strategies against historical data and evaluate their performance. | [docs](Black-Eclipse-site/docs/backtesting-engine.md) · [Strategy Studio](Black-Eclipse-site/docs/strategy-studio.md) |
| **Portfolio Analysis** | Examine portfolio performance, risk, returns, exposure, and allocation. | [`portfolio/`](Black-Eclipse-site/python/harness/portfolio/) · [docs](Black-Eclipse-site/docs/portfolio-accounting.md) |
| **AI Agents** | Build an extensible environment for specialized financial agents and collaborative AI workflows. | [`agents.py`](Black-Eclipse-site/python/harness/agents.py) · [docs](Black-Eclipse-site/docs/bot-mode.md) |
| **Trading Integration** | Connect analysis and trading workflows through MetaTrader 5 integration. | [`trade/`](Black-Eclipse-site/python/harness/trade/) · [docs](Black-Eclipse-site/docs/trading.md) |
| **Automation** | Develop repeatable financial workflows that reduce manual research and operational work. | [`automation/`](Black-Eclipse-site/python/harness/automation/) · [docs](Black-Eclipse-site/docs/harness/automation-engine.md) |

## Built as a Financial AI Harness

Eclipse is not designed to be another chatbot with access to stock prices.

It is built around an extensible harness architecture that connects AI models,
tools, financial data, code execution, research capabilities, and specialized
workflows. The goal is to give agents the ability to do more than generate
answers: research, calculate, write code, execute tools, test hypotheses,
evaluate results, and improve their workflows.

| Area | What exists today | Source |
|---|---|---|
| **Extensible AI models and providers** | Provider-agnostic model runtime: OpenAI, Anthropic, Google, Mistral, Groq, any OpenAI-compatible endpoint, or Ollama locally — switched from a setting. | [`models.py`](Black-Eclipse-site/python/harness/models.py) · [`model_runtime/`](Black-Eclipse-site/python/harness/model_runtime/) |
| **Financial data and research infrastructure** | A local market-data vault, source routing, authority scoring, and cross-checks that never average a conflict away. | [`retrieval/`](Black-Eclipse-site/python/harness/retrieval/) · [`market/verification.py`](Black-Eclipse-site/python/harness/market/verification.py) |
| **Specialized skills and reusable tools** | A tool registry where every tool declares its permission and effect class. A tool that cannot declare them is not published. | [`code/tools.py`](Black-Eclipse-site/python/harness/code/tools.py) · [`tool-registry.json`](Black-Eclipse-site/docs/harness/tool-registry.json) |
| **Persistent memory and learning** | Durable agent memory with contradiction detection and a retention policy, plus a store you can correct the agent in. | [`eclipse_memory/`](Black-Eclipse-site/python/harness/eclipse_memory/) · [docs](Black-Eclipse-site/docs/harness/personal-memory.md) |
| **Code execution and quantitative computing** | Sandboxed Python: `python -I`, scrubbed environment, no provider keys, wall-clock timeout, optional container with no network. | [`quant/sdk_runtime.py`](Black-Eclipse-site/python/harness/quant/sdk_runtime.py) · [docs](Black-Eclipse-site/docs/harness/quant-sdk-sandbox.md) |
| **Agent orchestration and automation** | Durable tasks and plans with leases, fencing and retries; A2A delegation; team runs over a dependency graph; goals, loops and heartbeats. | [`tasks/`](Black-Eclipse-site/python/harness/tasks/) · [`team/`](Black-Eclipse-site/python/harness/team/) · [docs](Black-Eclipse-site/docs/harness/bot-team-orchestration.md) |
| **Trading and strategy integrations** | MetaTrader 5 read paths, single-use order approvals, demo mandates, margin caps, reconciliation and a kill switch. | [`trade/`](Black-Eclipse-site/python/harness/trade/) · [docs](Black-Eclipse-site/docs/trading.md) |

### Why it is a harness and not a wrapper

The model plans and explains. It never does the arithmetic: calculations run in
Python and return their formula, inputs, assumptions and seed, so the same
question produces the same number or an honest `UNAVAILABLE`. The same rule
applies to trading — **no layer outside
[`quant/`](Black-Eclipse-site/python/harness/quant/) may compute a trading
statistic**, and research and execution stay separate products.

## Data sources and commercial use

The app does not ship a data licence of its own. Sources fall into three tiers, and
the difference matters the moment money is involved:

| Tier | What it means | Default |
|---|---|---|
| **Included** | Open data with no account. Ships with the app. | **On** |
| **Connect your own** | You supply a key or account; the provider's terms bind you. | Off |
| **Advanced / community** | Personal-use sources. Never part of a commercial default. | Off |

**Can be used commercially** — SEC EDGAR, BEA, BLS, USGS; FRED, World Bank,
Eurostat/ECB and GDELT **with the attribution the app already displays**.

**Commercial, with your own key** — Polygon, Twelve Data, Finnhub, Alpha Vantage,
Marketstack, Financial Modeling Prep, Trading Economics, and the research search
providers (Tavily, Brave, Exa, Serper). CoinGecko paid requires attribution;
CoinMarketCap requires you to check its current terms.

**Personal use only — do not ship these in a paid product** — the TradingView
scanner, Yahoo Finance (`yfinance`), the Binance public ticker, CoinGecko keyless /
demo, and the DuckDuckGo HTML search. They are off by default for this reason, and
turning one on is an explicit operator choice.

Every toggle is enforced at the fetch path (`src/lib/data-source-preferences.ts`), so
a source that is off cannot quietly serve a screen. The full table — every source,
what it powers, its commercial status and its terms link — is generated from
`src/lib/setup/source-catalog.ts` into
[`docs/data-sources.md`](Black-Eclipse-site/docs/data-sources.md).

**Adding your own** is a first-class path: add or edit an entry in
`src/lib/setup/source-catalog.ts`, run `npm run docs:data-sources`, and the docs and
the Settings surface follow. For a commercial deployment, use the *connect your own*
tier on a commercial plan, or plug in a source you have the rights to.

> This is an informational snapshot of each provider's published terms, not legal
> advice and not a licence grant. Terms change — verify the current ones before a
> paid launch. Language models are separate again: you bring your own provider key
> or run Ollama locally, and that provider's terms apply.

## From a research question to a tested strategy

The six steps below are the reason the pieces exist in one place.

| Step | What happens | What you get |
|---|---|---|
| **1. Research** | Ask about a company, market or instrument. The agent plans and selects tools. | A plan and the tool calls it made, visible in the run. |
| **2. Collect evidence** | Retrieval pulls from the local vault, configured providers and the web; sources are scored by authority for their data type. | Claims with source handles; a leg it cannot source returns `UNAVAILABLE`. |
| **3. Analyze** | Metrics, ratios, valuation and risk run through deterministic calculators. | Numbers with formula, inputs and assumptions attached. |
| **4. Build** | Author the strategy as a versioned IR, a visual graph, or sandboxed Python. | A strategy version you can point at. |
| **5. Evaluate** | Backtest with an anti-lookahead loop, walk forward, stress the assumptions. | Equity curve, Deflated Sharpe, probability of backtest overfitting, and a grade. |
| **6. Execute — where authorized** | A live trial needs an approved version, a completed backtest, a typed confirmation, an armed connection and a margin cap. | One order under a mandate — or a named refusal explaining what is missing. |

## Who Is Black Eclipse For?

| | |
|---|---|
| **Individual investors** | Research companies, understand financial information, evaluate portfolios, and make more informed decisions — with the tooling to check the reasoning. |
| **Quantitative traders** | Develop strategies, build models, backtest hypotheses, analyze trades, and investigate market behaviour. |
| **Financial researchers** | Work with financial data, fundamentals, filings, market intelligence and quantitative analysis in one environment. |
| **Developers** | Build financial agents, extend the harness, add tools, connect models, and automate workflows. |
| **Researchers and builders** | Experiment with AI-driven financial systems and build specialised applications on an extensible harness. |

## Install

**The desktop app** — Windows 10/11 (x64):
[Download for Windows](https://github.com/dumbpabloo/black-eclipse/releases/latest/download/BlackEclipse-Setup.exe) ·
[all releases](https://github.com/dumbpabloo/black-eclipse/releases).

macOS and Linux builds are not published yet. The build pipeline keeps producing
them on request; there is no download button until one has been built,
notarised and tested.

Or from a terminal — the same artifact, verified and installed:

```powershell
# Windows (per-user, no administrator prompt)
irm https://raw.githubusercontent.com/dumbpabloo/black-eclipse/main/Black-Eclipse-site/scripts/install-release.ps1 -OutFile install-release.ps1
./install-release.ps1
```

The script resolves `releases/latest`, compares the download against the
release's SHA-256, and can pin an exact version (`-Version`) or digest
(`-Sha256`). Details: [docs/install.md](Black-Eclipse-site/docs/install.md).

**From source** — for contributors, and it needs the installed shell (or Rust):

```bash
cd Black-Eclipse-site
npm install              # also provisions the Python agent runtime
npm run desktop:refresh  # build + open the desktop app (port 3100)
```

### One download, nothing to install first

The installer **is** the application: a bundled Node runtime, a vendored Python
runtime, the agent stack, the database schema and a seeded vault. You do not
install Node.js, Python or a database, and there is no setup step before the first
window opens.

| Requirement | Detail |
|---|---|
| **Operating system** | Windows 10/11 (x64). macOS and Linux builds exist but are not published yet |
| **Administrator rights** | **None.** Per-user install to `%LOCALAPPDATA%\Programs\BlackEclipse`; the NSIS manifest is `RequestExecutionLevel user` |
| **Node.js, Python, SQLite** | **Not required** — all three ship inside the payload |
| **Account, sign-in, telemetry** | None. The app runs entirely on your machine |
| **Download size** | ~141 MB for the core, and that is the whole app; optional components download on demand |
| **Internet** | Needed for live prices, news and every model call. The app still opens, seeds its vault and runs offline — data simply reads `UNAVAILABLE` until something can be fetched |
| **A language model** | A key for a hosted provider, or Ollama running locally. Without one, everything except the agent still works |

Optional feature packs (extra Python capabilities: browser automation, local ML,
document reading, desktop control, advanced charting) are **lazy**: the harness
imports them only when the folder is present, so a missing pack degrades one
capability instead of blocking the install. The slim installer leaves them out to
keep the download small; **Settings · Data & Storage · Feature packs** lists what
is missing and downloads it from the same release, verifying a published SHA-256
before anything is written. The offline `BlackEclipse-Setup-<version>-full.exe`
bundles every pack for machines with no network at all.

No account and no manual database setup. First boot provisions and seeds the local
vault and starts the agent runtime. To use a model, either add a key for a
supported hosted provider or configure a local Ollama provider in
**Settings · AI**.

| Platform | Artifact | Signing |
|---|---|---|
| Windows 10/11 (x64) | `BlackEclipse-Setup.exe` (NSIS) | unsigned |

**Verify before you run it.** The installer is unsigned. Windows releases publish
`SHA256SUMS.txt` next to the binary; the one-command script checks it for you and
prints the digest in every case. To check by hand:

```powershell
# Windows PowerShell
Get-FileHash .\BlackEclipse-Setup.exe -Algorithm SHA256
# compare the output with the line for that file in SHA256SUMS.txt
```

A mismatch means the download is not the artifact we published — delete it.

**The harness alone** — the runtime with no app data, vault or credentials:

```bash
# PyPI release is pending; install from source today:
pip install "git+https://github.com/dumbpabloo/black-eclipse.git#subdirectory=Black-Eclipse-site"
```

**Troubleshooting** (the first three failures, in order):

| Symptom | Cause | Fix |
|---|---|---|
| Agent runs fail with `503 runtime unavailable` | Python runtime missing or broken | `npm run desktop:doctor` from `Black-Eclipse-site/`, then relaunch |
| Every model call fails | No provider configured | **Settings · AI** — add a key, or point at Ollama on `http://localhost:11434` |
| Empty screens after first launch | Vault seed interrupted | Relaunch; the boot hook re-seeds. `npm run desktop:doctor` reports what is missing |

## First working example

**Smoke test** — prove the runtime is alive without a provider, network or data
file:

```bash
python -m harness.evaluation.conformance --quick
python -m harness.acp --self-test
```

**Embed it** — the same agents, tools and providers without the app:

```python
import asyncio
from harness.public import AgenticService, RunInput, RunRequest, ModelSettings

async def main():
    service = AgenticService()
    result = await service.run(RunRequest(
        runId="research-1",
        input=RunInput(query="Summarise the last quarter for this ticker and cite the filings."),
        modelSettings=ModelSettings(provider="ollama", model="llama3.1",
                                    base_url="http://localhost:11434/v1"),
        callbackBaseUrl="http://localhost:3000",
    ))
    print(result.status, result.output)

asyncio.run(main())
```

A run returns a status and an output whose claims carry source handles. A claim
the run could not source is returned as `UNAVAILABLE` rather than estimated, and
the whole run — plan, tool calls, receipts — stays inspectable afterwards. That
is the contract; the numbers you see are the ones the engine computed.

## Benchmarks

Two tiers, kept apart on purpose: the offline tier measures the **runtime** with a
scripted model, the live tier measures the **agent** with a real one.

| Offline · runtime | Live · agent |
|---|---|
| task_success **0.9970** · safety **1.0000** · hallucination **0.0000** · gate **PASS** | task_success **0.9748** · financial_accuracy **0.9963** · citation_accuracy **1.0000** · gate **PASS** |

`evalrun-1794502740168` (tier 0) and `evalrun-1794281772755` (tier 1, pinned
`live:27c280164943e63520e14422a6c953cf`).

**Methodology, so you can attack the numbers properly:**

| Question | Answer |
|---|---|
| How many tasks? | **347** across **15** versioned datasets (`corpus_manifest.json`, recorded 2026-11-12), split validation 253 / optimization 88 / held-out 6 |
| How many graded by an oracle rather than prose? | 271 of 347 (78.1% oracle coverage); 76 tasks are deliberately oracle-exempt |
| How many trials in the pinned live run? | 278 measured trials, 278 tasks |
| Is it reproducible? | Yes — the corpus manifest is checksummed and recomputed by `verify_manifest()`, and each published run records its dataset checksum, agent version, git commit, grader version and pinned live id |
| How is `financial_accuracy` measured? | Not by a model. `FinancialGrader` independently recomputes the expected value with `harness.research.calculations` and compares the agent's number within a declared tolerance |
| Which model produced the live numbers? | The pinned run records its model and provider in the run artefact. This table does not pin a vendor because the harness is provider-agnostic; re-running on another model produces different numbers, and we would rather say so |
| What does it **not** measure? | Returns. Robustness of a strategy. Real-world citation coverage. Several dimensions are unmeasured and print `UNKNOWN` — not zero, not pass |

A `1.0000` citation score is a statement about this corpus, not about the open
web. Method and provenance:
[`docs/harness/benchmarks.md`](Black-Eclipse-site/docs/harness/benchmarks.md) ·
[`docs/harness/agent-evaluation.md`](Black-Eclipse-site/docs/harness/agent-evaluation.md).

## Documentation

| Start here | What it answers |
|---|---|
| [QUICKSTART](Black-Eclipse-site/docs/QUICKSTART.md) | How do I run it and see something working? |
| [OVERVIEW](Black-Eclipse-site/docs/OVERVIEW.md) | What is this product, and what is the mental model? |
| [ARCHITECTURE](Black-Eclipse-site/docs/ARCHITECTURE.md) | What are the layers and the big moving parts? |
| [NAVIGATION](Black-Eclipse-site/docs/NAVIGATION.md) | Which file do I open to change X? |
| [Harness](Black-Eclipse-site/docs/harness/architecture.md) | How the runtime, tools, policy and evaluation work |
| [Trading](Black-Eclipse-site/docs/trading.md) | Broker connections, mandates, approvals and the kill switch |

## Roadmap and current limitations

There is no dated roadmap, on purpose. These two documents are the roadmap, and
a capability that is not in them is not promised:

- **[PRODUCTION-READINESS](Black-Eclipse-site/docs/PRODUCTION-READINESS.md)** —
  the gates still open for production use.
- **[OPEN-SOURCE-READINESS](Black-Eclipse-site/docs/OPEN-SOURCE-READINESS.md)** —
  what is left before this repository is public.

Known limits, stated plainly:

- **`v0.4.22` is single-operator and self-hosted.** It is not a multi-tenant
  service and does not pretend to be.
- **Updates are signed artifact swaps.** v0.4.22 and later download the release's
  compiled build (SHA-256 verified against the signed manifest) and promote it;
  no local rebuild, so a working checkout may stay dirty. See
  [`docs/UPDATES.md`](Black-Eclipse-site/docs/UPDATES.md).
- **The Python package is not on PyPI yet.** The install above says so.
- **Installers are unsigned**, so SmartScreen and Gatekeeper will warn. Verify
  the checksum instead.
- **Coverage is uneven across markets.** The vault fills over time; a thin market
  shows `UNAVAILABLE` rather than a synthetic quote.
- **Benchmarks measure the agent, not a strategy.** A passing gate says nothing
  about returns.

## Open Source

Black Eclipse is being built as an open-source project. Our goal is to give
developers, researchers, quantitative traders and investors the tools to build,
extend and customize their own financial AI environments.

- [**Contributing**](Black-Eclipse-site/CONTRIBUTING.md) — setup, DCO, CLA, release gate
- [**Security**](SECURITY.md) — private advisories, never a public issue
- [**Code of conduct**](CODE_OF_CONDUCT.md)
- [**Trademark**](TRADEMARK.md) — the code is MIT; the name is not
- [**Issues**](https://github.com/dumbpabloo/black-eclipse/issues) — bug reports, feature requests, and questions

### Licensing, precisely

| Component | License |
|---|---|
| The application and this repository's original code | **MIT** — see [`LICENSE`](LICENSE) |
| `Black-Eclipse-site/pine-engine/` — the isolated Pine Script engine | **AGPL-3.0-only**, because it depends on `pinets`. It is a separate program that talks to the app only through files — see [`NOTICE.md`](Black-Eclipse-site/pine-engine/NOTICE.md) |
| The **name, wordmark, and logo** | **Not licensed by MIT.** Fork it, rename it — see [`TRADEMARK.md`](TRADEMARK.md) |
| Contributions | MIT/AGPL as above, plus the [`CLA`](CLA.md) |

If you redistribute the app, the MIT terms cover it. If you run the Pine engine
as part of a network service, AGPL-3.0-only applies to **that component** and its
obligations are described in its NOTICE.

The code is free to use, change, and sell. What you may not do is ship a fork
under the BlackEclipse name or logo — MIT gives away the code, not the identity,
which is what stops a copy from taking the project's users.

The locked dependency trees are inventoried in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) (grouped by licence) and
[`sbom.cdx.json`](sbom.cdx.json) (CycloneDX 1.5). Both are generated with
`npm run sbom` from `package-lock.json` and `python/requirements.lock`; do not
hand-edit them.

---

**Trading disclaimer.** Black Eclipse is research and analysis software. It is
not investment advice, and it does not make decisions for you. Research does not
authorize trading: an order requires the operator's approval, and live execution
additionally requires an armed connection and a capped mandate. Demo and live are
distinct modes. Backtest results do not guarantee live performance. Black Eclipse
is not affiliated with, endorsed by, or derived from any terminal vendor.

**Black Eclipse**

*An open-source financial AI harness.*
