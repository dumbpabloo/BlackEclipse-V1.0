# ARCHITECT.md — System Architecture & Engineering Knowledge

> Read this file **before** making any significant architectural change to BlackEclipse.
> If the codebase conflicts with anything here, **the actual working implementation is the source of truth** until it is intentionally changed and this file is updated.

---

## 1. Product Definition

### What BlackEclipse is
An **open-source, agentic AI Harness and investment research platform** presented as a Bloomberg-inspired terminal UI. It provides market screening, crypto bubble visualization, portfolio tracking, financial news, and an AI research chat — all in a dark, glassmorphic interface.

### Long-term vision
BlackEclipse will grow into a **zero-configuration, agentic finance research tool** where users bring their own data sources (or use free built-in ones) and an AI agent performs research, screening, monitoring, and analysis. The immediate goal (Phase 1) is a working **data system**; the next is **agentic research agents** in the style of Hermes.

### What problem it solves
Free, good-quality market data APIs are scarce or expensive. Most users can't afford premium data feeds. BlackEclipse solves this by:
1. Working **out of the box with zero configuration** (demo data → free sources).
2. Letting users plug in their **own API keys** (Finnhub, Alpha Vantage, etc.) when they have them.
3. Providing a **free agentic scraper** (TradingView whole-market scanner + a yfinance Python bridge, with HTML/Playwright fallbacks) for markets without paid coverage.

### Who the intended users are
- Technical professionals: developers, quant researchers, analysts, and power users.
- Self-hosters who want a free, open, privacy-respecting research terminal.
- Open-source contributors extending the platform.

### What makes BlackEclipse different
- **Zero-config by default** — runs immediately with a self-seeding SQLite vault (`jdds.db` ships as a seed snapshot), demo data, and no keys required.
- **Provider-agnostic** data + AI layers — swap sources/models freely.
- **Agentic direction** — a chat that becomes a real tool-using research agent.
- **Bloomberg-style density** — information-rich, data-first, professional.

### Core product principles
1. **Zero configuration** — the user should configure as little as possible.
2. **Free by default** — prefer open/free sources; paid APIs are optional opt-ins.
3. **Reuse before rebuild** — reuse existing components, hooks, and services.
4. **Data accuracy over appearance** — financial correctness is paramount.
5. **Open-source friendly** — no secret-only behavior, no hardcoded keys, clear docs.
6. **Dark, dense, professional** UI — not a generic AI dashboard.

---

## 2. System Architecture

### Architecture Map (current, accurate as of this writing)

> This is the deep reference. The canonical, readable summary with the same
> layer model lives in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and
> [`../docs/OVERVIEW.md`](../docs/OVERVIEW.md). Where they overlap, **the code
> wins**; fix the doc.

```
User
 │
 ▼
UI / Shell (StockChat terminal — ONE page, keep-alive tabs)
 ├── ChatBot tab        → src/components/screens/stock-chat.tsx → src/lib/agent/*
 ├── Command Center     → src/components/screens/dashboard/ → src/lib/workspace/ + src/lib/dashboard/
 ├── Chart Workspace    → src/components/market-workspace/ → src/lib/market-workspace/
 ├── Market Screener    → MarketOverviewScreen (market-overview.tsx + use-screener-filters.ts,
 │                        use-global-markets.ts, use-market-tabs.ts, market-*-tables.tsx)
 ├── Portfolio          → src/components/screens/portfolio/ → use-portfolio-engine.ts
 ├── Journal            → src/components/journal/ → src/lib/journal/
 ├── News               → MarketNews → /api/news (reads JDDS; RSS ingested in background)
 ├── Market Bubbles     → market-bubbles.tsx
 ├── Economics          → src/components/screens/economics/ → /api/economics
 ├── Price Alerts       → src/app/alerts/ → src/lib/alerts/ (server poll loop)
 ├── Investor Eye       → src/components/investor-eye/ → src/lib/investor-eye/ (world-state tier)
 └── Chart (embedded)   → embedded-chart.tsx / tradingview-chart.tsx
 │
 ▼
Client Data Layer
 ├── DataHub (src/lib/workspace/data-hub.ts) — pub/sub topics, TTL, fan-out
 ├── Zustand stores — sidebar, dashboard, search, market-workspace, aurora, investor-eye
 └── Portfolio engine (src/hooks/use-portfolio-engine.ts) — pure FIFO/LIFO math
 │
 ▼
API / Services (src/app/api/**/route.ts — thin, server-only, api-guard protected)
 └── stocks · quote · price-history · asset-details · symbols · markets ·
     indices · forex · commodities · heatmap · news · economics · search · agent ·
     journal/* · alerts/* · scraper · refresh · settings · cron ·
     connectors (data-sources, datasets, providers, provider-keys) ·
     investor-eye/* (state, events, countries, markets, layers, economy,
     companies, portfolio, risk, analogues, query, graph, health)
 │
 ▼
JDDS — jdds.db (SQLite via Prisma + better-sqlite3, src/lib/db.ts)
 ├── Market universe: Market, Asset, Price, IndexQuote
 ├── News: NewsItem, SourceHealth
 ├── Macro: MacroSeries, CronJob, SystemConfig
 ├── Research memory: ResearchReport, AgentRun, ResearchFinding
 ├── Alerts: PriceAlert, PriceAlertEvent, Candles
 ├── Connectors: DataConnector, ConnectorMapping, CustomDataset*
 ├── Investor Eye: Country, GeopoliticalEvent, EventEntity, Relationship, SourceRun
 └── Trading journal: JournalProfile, TradingAccount, Strategy, … , Review
 │
 ▼
Background workers (all in-process, started from src/instrumentation.ts)
 ├── Price refresh (src/lib/scraper/scheduler.ts)      — default 15 min
 ├── Universe sync (daily) + News ingest (default 5 min)
 ├── Boot warm-up (prod-only unless BE_WARMUP=1)
 └── Alert monitor (src/lib/alerts/alertMonitor.ts)
 │
 ▼
Data Sources
 └── TradingView scanner (whole-market) · yfinance Python bridge ·
     RSS feeds (free) · crypto/macro providers · user API keys · demo fallback ·
     Investor Eye event feeds (GDELT, USGS, NASA FIRMS, conflict RSS)
```

### Frontend
- **Next.js 15 App Router**, React 19, TypeScript, Tailwind CSS v4.
- Client components marked `'use client'` at the top of each file.
- The app is **one page**: `src/app/page.tsx` renders `<StockChat />`, which owns
  the `activeTab` state machine and keep-alive tabs (`AppTab` in
  `src/components/layout/sidebar.tsx`). There is no URL routing for main screens.
- Screens are loaded with `next/dynamic` + `ssr: false` (memory decision — do not
  convert to static imports).
- Reusable primitives in `src/components/ui/` (button, card, dialog, tab-bar, …).

### Backend
- Many route groups under `src/app/api/**/route.ts` — see the map above. Handlers
  are thin and server-only; mutating routes go through `src/lib/api-guard.ts`.

### Zero Configuration Policy
- The app runs with **zero setup**: `npm install && npm run dev`. `instrumentation.ts`
  seeds the US universe and starts the schedulers; `jdds.db` ships as a seed snapshot.
- **Data source priority (user configures as little as possible):**
  1. **Agentic scraper** (primary) — free, built-in, works for any market with zero setup.
  2. **User-provided API** (optional) — used only if the user supplies a key (Finnhub, Alpha Vantage, etc.).
  3. **Demo data** (fallback) — always works when no live source is available, behind a visible `DEMO` banner.
- The user should **never be required** to configure anything. The agentic scraper is the default.

### API Layer
- Route groups are listed in the Architecture Map above. `/api/news` reads from
  JDDS (`NewsItem`) rather than fetching feeds per request; RSS ingestion happens
  in the background (`src/lib/news/ingest.ts`).

### Database — JDDS (SQLite)
- **`jdds.db`** (SQLite) via **Prisma + `@prisma/adapter-better-sqlite3`**;
  `src/lib/db.ts` owns the client and exports `prisma` + `yieldToEventLoop()`.
- Schema in `prisma/schema.prisma`, organized in domains (market universe, news,
  macro, scheduling/config, research memory, alerts, trading journal). See the
  Architecture Map above for the model list.
- **Dedup by constraint.** `Asset` unique on `[marketId, symbol]`; `Price` keyed
  by `assetId`; `NewsItem` has a `dedupKey`. Imports are idempotent upserts.
- **Auth-ready, not auth-ful.** The journal carries an optional owner concept so a
  `User` relation can be added later. There are no accounts today.
- **Synchronous-write discipline.** better-sqlite3 blocks the event loop, so bulk
  writers chunk and `yieldToEventLoop()`. See `docs/DATA-FLOW.md`.
- Client-only state (sidebar, dashboard layouts, search, market-workspace,
  aurora) persists to **localStorage**, not the DB. The committed `jdds.db` is a
  **seed snapshot** — do not re-commit it on routine changes (see the
  `black-eclipse-db` skill).

### Data Ingestion
- **PRIMARY: agentic scraper** — a smart scheduler that scrapes market data for free. It is timezone-aware (only scrapes open markets), priority-based, and de-duplicated. This is the default source so users configure nothing.
- **SECONDARY: user-provided API** — optional keys (Finnhub, Alpha Vantage, etc.) override the scraper for that source when supplied.
- **FALLBACK: demo data** — used when no live source is available or reachable, behind a `DEMO` banner.
- Full detail lives in [`../docs/DATA-FLOW.md`](../docs/DATA-FLOW.md) — read it before touching ingestion.

### Market data providers
- **PRIMARY: TradingView scanner** (`fetchers/tradingview.ts`) — a whole-market scan prices an entire market in ~1 request; the primary path and covers markets Yahoo misses.
- **SECONDARY: yfinance bridge** (`fetchers/yfinance.ts` + `scripts/yfinance_bridge.py`) — batched Python bridge for markets TradingView covers poorly (optional Python 3.9+).
- **FALLBACK chain:** `fetchers/http.ts`, `fetchers/browser.ts` (Playwright), `fetchers/scrapling.ts`, `fetchers/crypto.ts`. `SourceHealth` learns which source is reliable.
- **OPTIONAL: user-supplied** Finnhub/Alpha Vantage/Twelve Data when a user configures them.
- Symbols map to TradingView via `getTradingViewSymbol()` in `src/lib/utils.ts`.

### Background workers / scheduled jobs
All in-process, started from `src/instrumentation.ts` (no external daemon). They
coordinate through mutex flags so two heavy SQLite write storms never collide.

| Worker | Cadence | Source |
|---|---|---|
| Price refresh | default 15 min | `src/lib/scraper/scheduler.ts` |
| Universe sync | daily | `src/lib/scraper/scheduler.ts` |
| News ingest | default 5 min | `src/lib/news/ingest.ts` |
| Boot warm-up | once (prod-only unless `BE_WARMUP=1`) | `src/lib/scraper/scheduler.ts` |
| Alert monitor | polling loop | `src/lib/alerts/alertMonitor.ts` |

### Agent/AI architecture
- `src/lib/agent/types.ts` defines `AgentProvider`, `AgentTool`, `ChatMessage`.
- `src/lib/agent/mock-provider.ts` — built-in keyword-based responder (last-resort fallback).
- `src/lib/agent/openai-provider.ts` — OpenAI-compatible provider (OpenAI / Ollama / Anthropic-style) that runs **server-side**; the client reaches it via `POST /api/agent/chat`.
- `src/lib/agent/registry.ts` — `getAgentProvider()` returns the first configured provider else mock. `NEXT_PUBLIC_LLM_CONFIGURED` (set by `instrumentation.ts`) tells the client whether a real LLM exists.
- `src/lib/agent/loop.ts` + `tools.ts` — the tool-calling loop and tool registry (query data, scrape, search); `research.ts` is the multi-agent research pipeline that persists findings (`ResearchReport` / `AgentRun` / `ResearchFinding`).
- `src/components/screens/stock-chat.tsx` calls `getAgentProvider().chat(...)` for replies; research-worded questions hit the research engine.

### Charts & visualization
- ECharts (`echarts` + `useEChart` in `src/components/screens/portfolio/use-echart.ts`) for dashboard/chart widgets; TradingView widget via `src/components/screens/embedded-chart.tsx` and `tradingview-chart.tsx`; Recharts for portfolio charts.
- `ogl` WebGL for the **Aurora background** (`src/components/ui/Aurora.tsx`).

---

## 3. Technology Stack

### CURRENT (verified in package.json)
| Layer | Tech |
|---|---|
| Framework | Next.js **15.5.18** |
| Language | TypeScript **5** |
| UI | React **19.1.0** |
| Styling | Tailwind CSS **v4** + tw-animate-css + class-variance-authority |
| Components | @base-ui/react **1.5.0** (shadcn-style), @phosphor-icons/react icons (aliased to legacy Lucide names in `src/lib/icons/`) |
| DB | Prisma **7.9.1** + @prisma/adapter-better-sqlite3 + better-sqlite3 |
| Workspace | @dnd-kit/core + @dnd-kit/sortable + zustand + immer |
| Charts | echarts **6.1.0**, Recharts **3.8.1**, TradingView widget |
| Search | flexsearch **0.7.31** |
| Scraping | cheerio, playwright, `scripts/yfinance_bridge.py` (Python) |
| Scheduling | node-cron **4.6.0** (in-process) |
| 3D/GL | ogl **1.0.11** |
| News | rss-parser **3.13.0** |
| Alerts/agent | nodemailer, axios, duck-duck-scrape |
| PDF | jspdf + jspdf-autotable |
| Utils | clsx, tailwind-merge |
| Tests | Vitest **4.1.10** |

### PLANNED (not yet implemented — do not treat as current)
- A broker / paper-trading integration (the `trading`-category dashboard
  widgets are `UNAVAILABLE` stubs today).
- Vercel/serverless support for the in-process SQLite scheduler (a
  `POST /api/cron/run` endpoint exists for hosted schedulers).

### EXPERIMENTAL / DEPRECATED
- The original **JDdS daemon** and its PostgreSQL provider system were **removed**. Do not reintroduce them without updating this file.
- `src/lib/customize/` — the older customization kernel, **superseded** by
  `src/lib/workspace/` (ADR-005). The folder no longer exists; do not rebuild it.
- `@base-ui/react` v1.5.0 has **SSR/hydration quirks** — Portal-based components (Tooltip, Dialog) must be mounted-gated.

---

## 4. Data Architecture

### Core entities (DB — see `prisma/schema.prisma`)
- **Market universe** — `Market`, `Asset` (unique `[marketId, symbol]`), `Price`
  (one per `assetId`, `source: 'scraper' | 'csv'`), `IndexQuote`.
- **News** — `NewsItem` (unique `dedupKey`), `SourceHealth`.
- **Macro** — `MacroSeries` (JSON cache keyed by `cacheKey`).
- **Scheduling & config** — `CronJob`, `SystemConfig`.
- **Research memory** — `ResearchReport`, `AgentRun`, `ResearchFinding`.
- **Alerts** — `PriceAlert`, `PriceAlertEvent`, `Candles`.
- **Trading journal** — `JournalProfile`, `TradingAccount`, `Strategy`,
  `StrategyRule`, `Setup`, `Tag`, `JournalDay`, `DailyReview`, `DailyEmotion`,
  `Trade`, `TradeExecution`, `TradeScreenshot`, `TradeAnnotation`, `TradeTag`,
  `MarketContext`, `JournalEntry`, `Review`.
- **Connector layer** — `DataConnector`, `ConnectorMapping`, `CustomDataset`,
  `CustomDatasetField`, `CustomDatasetRecord` (custom API data is isolated here
  and can never write `Asset`/`Price`/`Candles` — ADR-010).
- **Investor Eye (world-state)** — `Country`, `GeopoliticalEvent`,
  `EventEntity`, `Relationship`, `SourceRun` (ADR-011). The relationship graph
  is this relational edge table, aggregated by a pure builder — no graph DB.

Client-side shapes:
- `StockItem` — demo/screener record (`src/lib/screener/data.ts`).
- `Message` (`src/types/chat.ts`) — chat messages.
- Portfolio/transaction shapes come from `use-portfolio-engine.ts`; persisted client-side.

### Relationships
- The journal domain is the most relational (profiles → accounts → strategies →
  trades → executions/annotations). Market data is upsert-only and keyed by
  unique constraints. Optional journal owner is reserved for a future `User`.

### Market data flow
1. Background workers scrape/import into **JDDS** (see the map above).
2. Screens read through an API route (`/api/stocks`, `/api/indices`, …) or a
   DataHub topic that wraps it.
3. `use-screener-filters.ts` applies filter/sort logic as a pure hook; the
   screener filters/paginates **server-side**.
4. Demo data (`src/lib/screener/data.ts`) is the fallback when no universe is loaded.

### Symbol/universe management
- `getTradingViewSymbol()` maps raw symbols → TradingView format (crypto, indices, PSX `.KA`, India `.NS`, etc.).
- `universe-sync.ts` scans TradingView markets, upserts new listings, and
  deactivates symbols that miss repeatedly (daily).

### Data normalization & validation
- Filter logic in `use-screener-filters.ts` parses market-cap/volume strings (`1.3T`, `28.4B`, `540M`) to numbers.
- News sentiment is a keyword heuristic (`src/lib/sentiment.ts`).

### Data freshness
- **`Price.source`** is the provenance field: `'scraper'` = live scrape, `'csv'` = imported.
- `Price.updatedAt` / `CronJob.lastRunAt` tell you how old data is.
- DataHub entries carry a `DataSource` label
  (`LIVE | CACHED | HISTORICAL | DEMO | ESTIMATED | UNAVAILABLE`).
- Never present stale or simulated data as live. See `docs/DATA-FLOW.md`.

---

## 5. Important Architectural Rules

1. **Do not duplicate business logic.** Reuse `use-screener-filters.ts`, `use-portfolio-engine.ts`, and the agent layer.
2. **Reuse existing services before creating new ones.**
3. **Do not bypass the established patterns** (client components, ui primitives, screens).
4. **Do not introduce unnecessary dependencies.** If a new dep is needed, justify it in REMEMBER.md.
5. **Keep frontend and backend responsibilities separated.** Server-only code (RSS parsing, secrets) must live in API routes, not client components.
6. **Prefer existing patterns over new ones.**
7. **Preserve backward compatibility** unless explicitly instructed otherwise.
8. **Never silently change data shapes** that screens depend on (e.g., `StockItem` fields).
9. **Never remove existing functionality without confirming its dependency chain.**
10. **Never put secrets or API keys in client-side code.**
11. **Memory discipline:** large screens are split into hooks/modules (the screener lives in `market-overview.tsx` + `use-global-markets.ts` + `use-market-tabs.ts` + `market-overview-global-markets.tsx` + `market-*-tables.tsx`). Keep components small; avoid re-bloating a single file.

---

## 6. Architectural Decision Record (ADR)

**What is an ADR?** It is a short note recording *why* a significant architecture decision was made — like a git commit message for architecture. It exists so future AI agents understand the reasoning instead of re-deciding or re-litigating it. Write one whenever you make a meaningful choice. Keep it to a few lines.

### ADR-001 — Demo data + zero-config (2026)
**Decision:** Ship with demo data seeded into JDDS; no keys required. `jdds.db` is a committed seed snapshot so the app boots populated with zero setup.
**Reason:** Zero-config policy + open-source friendliness.
**Impact:** All screens work immediately; real data is a future opt-in. **Superseded in part:** the DB was added later (SQLite JDDS) — see ADR-004 and `docs/ARCHITECTURE.md`. The "no DB" clause is historical, not current.

### ADR-002 — RSS for news (2026)
**Decision:** Use free RSS feeds (Yahoo/CoinDesk/CoinTelegraph/CNBC) for news.
**Reason:** No API key, no rate limits, open standard.
**Impact:** RSS is ingested in the background into `NewsItem`; `/api/news` serves from JDDS and returns `source: "jdds"` (`"error"` on a DB failure) with `lastIngestAt`. The client may fall back to its local mock when the feed is empty. **Correction:** the old claim that the route fetches live with an offline mock fallback is stale — the route never fetches feeds.

### ADR-003 — Agent provider abstraction (2026)
**Decision:** Define `AgentProvider` interface; mock by default; plug in real LLMs later.
**Reason:** Keeps the chat functional now and enables Hermes-style agents later.
**Impact:** UI calls `getAgentProvider().chat()`; adding a real provider is additive.

### ADR-004 — Agentic scraper as primary data source (2026)
**Decision:** The default market-data source is a free, built-in agentic scraper; user APIs are optional opt-ins; demo data is the fallback.
**Reason:** Users should configure as little as possible; free data must work out of the box.
**Impact:** Data source priority is always: agentic scraper → user API → demo.

### ADR-005 — Customization kernel (2026)
**Decision:** All page customization is driven by the shared kernel in `src/lib/workspace/` (`useWorkspace` + `WorkspaceCanvas` + widget registry). Layouts use the **section → column → widget model (v2)**: sections hold rows of 1–4 columns with relative weight shares (1–6); widget cards stack inside columns with optional fixed pixel heights. Stored layouts are validated/normalized on load (corrupt data falls back without overwrite); presets are curated section templates. Pages get edit-mode chrome (drag/drop, add/remove widgets, height + column-width resize, column counts, templates) through the kernel. No page-local layout state or storage code.
**Reason:** The flat span model could not express free-form placement (multiple widgets per column, individual column widths). A prior v3 "element model" (`src/lib/customize/`) was sketched in docs but never shipped — the workspace kernel is the real implementation.
**Impact:** Adding a customizable page = one widget registry + `useWorkspace()` + `<WorkspaceCanvas renderWidget={...}>`. The fund workspace and portfolio pages are migrated; other screens follow when touched (see `skills/CUSTOMIZE.md`).

### ADR-006 — Workspace kernel stack: dnd-kit + Zustand + Immer (2026)
**Decision:** The customization kernel is built on three libraries: **dnd-kit** (`@dnd-kit/core`) owns drag & drop (`DndContext` + `PointerSensor` + `useDraggable`/`useDroppable` + `DragOverlay`), **Zustand** owns editor/layout/selection state (one cached store per workspace storageKey, selector subscriptions, `selectedWidgetId`/`dragging`), **Immer** owns layout mutations (all ops in `ops.ts` are draft mutators invoked via `produce` in the store's `commit()`).
**Reason:** Replaced hand-rolled pointer-drag DOM surgery, useState plumbing, and manual structural clones. dnd-kit gives accessible, collision-driven DnD; Zustand gives cross-mount state without prop drilling; Immer removes clone boilerplate and makes ops trivially testable (tests wrap calls in `produce`).
**Impact:** `npm i @dnd-kit/core zustand immer` are required deps. New layout ops must follow the draft-mutator signature in `ops.ts` (mutate the draft, return void) and get a unit test. Persisting happens automatically inside `commit()`; never call `saveWorkspace` elsewhere.

### ADR-007 — Page-header icon tiles (2026)
**Decision:** Every page header uses the portfolio-style icon tile: a 56px (`h-14 w-14`) rounded-2xl container with `border-primary/25 bg-primary/15 shadow-lg shadow-primary/10` wrapping a 30px primary-colored lucide icon.
**Reason:** Bare inline icons looked inconsistent next to the redesigned portfolio header.
**Impact:** New screens copy the tile pattern (see market-overview.tsx or economics.tsx headers); do not revert to bare `w-7 h-7` icons.

### ADR-008 — Two-click placement confirm (2026)
**Decision:** Widget placement is **preview-then-confirm**, not drop-to-commit. Grab a card (click or drag-release), hover to preview a labeled ghost slot, then click again (or the ghost / Confirm button) to commit; **Escape or right-click cancels** at any point. Empty columns and inter-section strips are clickable while grabbing; auto-scroll runs during both drag and grab so off-screen rows are reachable.
**Reason:** Drop-to-commit was unreliable across devices (touch pan-gestures, scroll-adjusted deltas, off-screen targets) and gave users no chance to verify placement before committing. A two-click model is deterministic, works on mouse/touch/trackpad, and never mutates the layout until the user confirms.
**Impact:** The drop target is projected from the **real pointer** (native `pointermove` + `elementFromPoint`), not dnd-kit's scroll-adjusted delta. New ops: `moveWidgetToNewSectionAt` (insert above/below any section), `createSectionAt` (click strips to create empty sections), and `DropTarget.columnIndex` (insert new columns before/after an anchor). See `skills/CUSTOMIZE.md` for the exact interaction contract.

### ADR-009 — Central command palette + query grammar (2026)
**Decision:** Global search (⌘K) is a single command palette over
`src/lib/search/`, structured as two **pure, framework-free** steps: a **grammar**
(`grammar.ts`) that parses scopes/operators, then a **resolver** (`resolver.ts`)
that classifies intent, scores the static registry + FlexSearch universe, and
returns a `ResolvedSearch`. The palette dispatches a **`Destination`** on a bus
(`pendingDestination`) rather than navigating directly.
**Reason:** The command-center vocabulary (scopes/operators) is a user-facing
contract that must grow without touching the UI; keeping the query path fully
offline and deterministic avoids an LLM on every keystroke.
**Impact:** Extending scopes/operators means editing `grammar.ts` + its tests
together. Ranking stays pure; personalization (visit history), pins, and collapse
state live in the Zustand command store. `calc.ts`/`commands.ts` add offline
calculation and parameterized-command autocomplete. See `docs/ARCHITECTURE.md`
→ "The command center".

### ADR-010 — Universal Data Connector Layer (2026)
**Decision:** A capability-addressed connector tier under `src/lib/connectors/`
sits after the scraper chain and before route-level demo fallbacks. Consumers
request a *capability* (`PRICE`), never a provider. Custom API data lands in
dedicated `CustomDataset*` tables and can never write `Asset`/`Price`/`Candles`.
**Reason:** Operators need to bring their own REST API without a rewrite per
provider, while preserving ADR-004 precedence and the "never fabricate" rule.
**Impact:** `DataSource` gained `CUSTOM`; topics may return a provenance
envelope. Credentials are AES-256-GCM encrypted (`.connector-key` locally or
`CONNECTOR_SECRET_KEY` hosted). No automatic cross-provider fallback.

### ADR-011 — Investor Eye is a separate event/intelligence tier, reused not forked (2026)
**Decision:** Investor Eye ("God's Eye") is built as a new **event + world-state
tier** under `src/lib/investor-eye/`, **not** as another connector capability and
**not** as a second market/portfolio/news stack. It:
1. registers its own `EventSource` adapters (GDELT, USGS) instead of forking the
   existing `DataConnector` contract, but reuses its hardened `safeGet` transport;
2. persists only **additive** JDDS models (`Country`,
   `GeopoliticalEvent`, `EventEntity`, `SourceRun`) — **no graph database**, no
   Redis, no microservice;
3. visualizes through a provider-agnostic `MapLayer` abstraction rendered by
   ECharts (already a dependency), so the visualization can be swapped later
   without touching the data layer;
4. sources market observations from the **existing** `/api/indices` path
   (`IndexQuote` + the yfinance bridge) and portfolio exposure from the
   **existing** `use-portfolio-engine`, never duplicating them;
5. keeps the query path fully deterministic (keyword + gazetteer NL search, no
   LLM), matching ADR-009.
**Reason:** The repository is a single-page, SQLite-backed SPA with no geo/map
infrastructure and no geopolitical-event model. Building the smallest robust
foundation that reuses existing services avoids the duplicate-architecture and
over-build traps the spec explicitly warns against.
**Impact:** A new `investor-eye` tab, `country-fill`/`scatter`/`effect-scatter`/
`lines`/`heatmap` layer types (extensible via `registerLayer()`), and honest
provenance (`LIVE`/`CACHED`/`HISTORICAL`/`UNAVAILABLE`). A failed source is
`UNAVAILABLE`, never a fake zero; the map and page still load on partial source
failure. Optional authenticated sources (e.g. ACLED) are supported but never
required. See `docs/investor-eye-architecture.md` and `docs/ARCHITECTURE.md`
→ "Investor Eye".

### ADR-012 — Data-source governance is its own registry, enforced by test (2026)
**Decision:** Every external Investor Eye source must have an entry in
`src/lib/investor-eye/source-catalog.ts` (provider, licence, terms URL,
commercial-use, API-key requirement, attribution, rate limit, freshness),
kept separate from the `EventSource` adapter and enforced by
`source-catalog.test.ts`.
**Reason:** "Public API" ≠ "free for commercial use"; fusing governance into
adapters makes a licence question answerable only by constructing a fetcher.
**Impact:** A new source = connector + normalizer + registry entry + catalog
entry, the last two held in sync mechanically. AIS/ADS-B/OSM/CelesTrak are
deliberately not wired. See `docs/DECISIONS.md` ADR-012.

### ADR-013 — Operator API keys are an encrypted vault over env vars (2026)
**Decision:** Settings → API Keys credentials are AES-256-GCM-encrypted in
`SystemConfig` via `connectors/secrets`, declared once in `PROVIDER_KEYS`
(`src/lib/provider-keys.ts`), and resolved **env var first, vault second**;
`applyStoredKeysToEnv()` copies them into `process.env` at boot.
**Reason:** Env vars suit a deployment but not a zero-config local install, and
there was no single place to enter a credential.
**Impact:** The key value never crosses the API boundary (only `stored`,
`fromEnv`, masked hint). Adding a provider is one `PROVIDER_KEYS` entry. See
`docs/DECISIONS.md` ADR-013.

### ADR-014 — Map-local loading states must never portal to the body (2026)
**Decision:** A region-scoped loading/empty overlay renders inside that region;
`TopLoader` is reserved for whole-screen loads and must never be used inside a
card, panel, or widget.
**Reason:** `TopLoader` portals to `document.body` at `z-[45]`, so a map-local
spinner silently became a full-app blocker frozen at 95%.
**Impact:** The rule applies to any region-scoped loader; the globe halo
contract is now covered by a test. See `docs/DECISIONS.md` ADR-014.

### ADR-015 — Media widgets tear down their own player when not visible (2026)
**Decision:** A widget that plays media must not rely on the shell to stop it.
The keep-alive tab model keeps visited screens MOUNTED and hides them with
`display:none`, and `WidgetDefinition.lazy` is declared but never read, so an
embed left mounted keeps streaming. Such a widget owns its teardown: an
`IntersectionObserver` on its root (zero intersection inside a `display:none`
ancestor, and when scrolled off) plus a `visibilitychange` listener (browser
tab). Both are required — each misses the case the other covers.
**Reason:** Nothing in the repo tore an embed down on visibility; a live-stream
widget would burn bandwidth and CPU indefinitely on an unwatched tab.
**Impact:** Applies to any future media widget (video, audio, streaming, WebGL
animation). `lazy` remains dead — do not treat it as a teardown mechanism. See
`docs/DECISIONS.md` ADR-015 and `docs/ARCHITECTURE.md` → "Live Intelligence".

### ADR-016 — Track board replaces the row-band layout grammar (2026)
**Decision:** The widget workspace's default arrangement is a masonry track
board. A section's columns are vertical tracks; the pure packer
`src/lib/workspace/pack.ts` (`rebalanceColumns`/`rebalanceLayout`) distributes
each section's cards into the shortest track by estimated height. Every
placement sizes to content (`height: null`) with an intrinsic floor
(`WidgetPlacement.minHeight`, from `WidgetDefinition.minHeight` /
`WIDGET_MIN_HEIGHT`). `WorkspaceLayout.version` is `2`;
`buildMarketViewLayout`/`composeBoard` replace `composeRows`; `HEIGHTS`,
`LayoutRow` and the `items-start` row renderer are removed.
**Reason:** The row-band grammar was structurally gappy — a rigid full-width row
of content-height columns left dead space beside any short widget, and the pinned
side rails left ~490px blank under the Watchlist. Pinned `HEIGHTS` were too
coarse for real content, so cards clipped or left empty tails.
**Impact:** Packing is COMPOSE-TIME only (defaults, templates, imports) — never
inside the store's `commit`, because masonry has no stable fixed point for
equal-height cards and re-packing on edit would silently reshuffle dropped cards.
Storage reset to v7 dashboards / v3 funds / `be_dashboards_index_v7`; old layouts
are not migrated. The 4-track capability remains for user drag/drop. See
`docs/DECISIONS.md` ADR-016 and `docs/ARCHITECTURE.md` → "Dashboard layout grammar
(personas + track board)".

### ADR-017 — Frame vs grow card sizing and resizable side panels (2026)
**Decision:** Every track-board placement carries EITHER a frame
(`WidgetPlacement.defaultHeight` — a definite box whose body stretches, for
charts/maps/live/scrollable tables) OR a grow floor (`WidgetPlacement.minHeight`
— a floor under self-sizing content, for compact tiles). The renderer keys its
body class off `isDefiniteCard(placement)`. Side zones get a persisted,
user-resizable width (`WorkspaceLayout.zoneWidths`, `ops.setZoneWidth`, clamped
180–520px via a drag handle; `main` is not resizable).
**Reason:** The single `minHeight` field collapsed widgets — a grow card got a
`min-h` frame but the body used `flex-1`/`flex-basis:0`, so content with no
intrinsic height (a chart, a scrollable list) rendered at zero height. Splitting
definite frames from growing tiles is what makes them render. Fixed side widths
also wasted space.
**Impact:** `defaultHeight` added and preserved by `normalizeLayout`;
`WIDGET_FRAME_HEIGHT` / `WidgetDefinition.defaultHeight` alongside
`WIDGET_MIN_HEIGHT` / `minHeight`. Personas redesigned into nine different band
shapes and renamed. Frame/grow confusion is the first thing to check when a
widget renders empty. See `docs/DECISIONS.md` ADR-017 and `docs/ARCHITECTURE.md`
→ "Dashboard layout grammar (personas + track board)".

### ADR-018 — Agentic runtime is Python (LangChain + LangGraph); separate agent-memory DB (2026)
**Decision:** The agentic harness runtime is **Python** — FastAPI + LangChain +
LangGraph in `python/harness/` — and owns agents, tools, model providers, harness
events and run persistence. Next.js is a thin front door that validates input,
resolves the agent preset + LLM config from the settings vault, assigns the run
id, and proxies. It also serves a guarded, read-only JDdS **data bridge** for the
Python tools. Run state and agent memory/skills live in a **separate** SQLite
file `agent-memory.db`, NOT JDdS.
**Reason:** The agentic side must not be written in TypeScript, and it must
connect every model. Python is where the LangChain/LangGraph ecosystem and
provider integrations live, making universal model coverage and agent authoring
configuration rather than bespoke TS adapters. The vault and Prisma are the two
things Python cannot own, which is why the front door keeps exactly those two
jobs. A separate DB keeps high-churn operational state off the committed JDdS
snapshot.
**Impact:** Agent runs require the Python stack, installed by `postinstall` /
`doctor.js`; without it runs return 503 — never a fake agent. Model coverage is a
provider catalog (`PROVIDER_CATALOG`) plus the OpenAI-compatible protocol. The
TypeScript Phase-1 kernel was retired; only validation, vault resolution, the
proxy and the data bridge remain in TS. A later phase must add an explicit
approval path before any non-`read` tool becomes callable. See
`docs/harness/architecture.md`, `docs/harness/decisions.md` (ADR-H1–H7) and
`docs/DECISIONS.md` ADR-018.

### ADR-025 — Skills are versioned procedures on the existing runtime; generated skills are inert (2026)
**Decision:** A Phase-3 "skill" is a versioned, machine-readable procedure
(`skill.json` + `SKILL.md` under `python/harness/skills/library/`) executed by the
**existing** Phase-1 `execute_run` graph — not a second runtime, tool registry or
agent loop. Manifests, permissions and lifecycle live in Python
(`python/harness/skills/`), persisted in `agent-memory.db`. Permissions are a
single **default-deny** policy enforced in code; Markdown instructions are data,
not a security boundary. The learning engine creates inert `LearningCandidate`
records from run traces; a candidate can only become an active skill through an
explicit operator-gated promote path, never automatically.
**Reason:** The brief forbids duplicate infrastructure and unrestricted
self-modification; a skill must reuse the hardened Phase-1/2 runtime and every
capability must stay reviewable. Instructions cannot be a boundary because they
are untrusted prose, so enforcement must be code (permission check + tool
allowlist on the composed `AgentConfig`).
**Impact:** New Phase-3 tables in `agent-memory.db` (idempotent DDL, no manual
migration). Runtime skills live under `python/harness/skills/library/`, distinct
from the unrelated `Black-Eclipse-site/skills/` *coding-agent* operating system.
The front door adds `/api/skills/**` + `/api/learning/**` proxies and a Skills
tab; the browser talks only to those routes (never the Python service or Prisma
directly). pydantic is reused for schema validation — no Zod. See
`docs/harness/phase-3-architecture.md` and `docs/DECISIONS.md` ADR-025.

### ADR-026 — Eclipse-Memory is a separate SQLite database and service (2026)
**Decision:** Memory, skills, learning, evaluations, corrections and mistakes
persist in a dedicated SQLite file **`eclipse-memory.db`**, owned by one service
(`python/harness/eclipse_memory/`), separate from `jdds.db` (market data +
Trading Journal) and `agent-memory.db` (run/event traces). Skill *definitions*
stay as reviewable files in `skills/library/`; all runtime state lives in the
DB (Option C hybrid). The Phase-3 `SkillStore` is now an alias for
`EclipseMemoryStore`, so there is one skill registry; existing Phase-3 skill
rows are imported once, idempotently and non-destructively.
**Reason:** The audit found memory was never written or recalled, and the system
prompt promised tools (`memory`, `skill_view`, `skill_manage`, `terminal`…) that
did not exist. A dedicated, inspectable database with an explicit write/retrieve
pipeline — plus real tools — fixes the behaviour; keeping authoring in files
keeps skills reviewable.
**Impact:** New `eclipse-memory.db`, `python/harness/eclipse_memory/`,
`memory_tools.py`, `/api/eclipse-memory/**`, and an **Eclipse Memory** tab.
`graph.execute_run` recalls scoped memory into each run and registers the
memory/skill tools on the existing registry. The system prompt was corrected to
name only real tools. Path config: `ECLIPSE_MEMORY_DB_PATH`. See
`docs/harness/phase-3-memory-progress.md` and `docs/DECISIONS.md` ADR-026.

### ADR-030 — Resilient multi-source research: structured failure diagnostics, independent routes, bounded retry (2026)
**Decision:** Web search and page fetch return a structured RetrievalDiagnostic (provider, category, HTTP status, latency, timeout, result count, bytes, parse outcome, retryability) instead of collapsing every failure into an opaque UNAVAILABLE; a JSON source document is accepted rather than rejected as 'not HTML'. Structured retrieval is one route among several: a direct SEC EDGAR source (tier primary, US issuers, identity-gated) and an operator-configurable second search provider (vault keys SEARCH_API_CONFIG/SEARCH_API_KEY) join the keyless DuckDuckGo default, with selection policy as explicit data in 
etrieval/planner.py (inancial_statements → [SEC, JDDS, WEB]). The loop retries only transient failures with bounded backoff, switches to the next independent provider, targets unresolved legs, and records a stopReason when it ends.
**Reason:** The NVDA run could not distinguish 'the web had nothing' from 'every route is broken'. Verified root causes: DuckDuckGo answered HTTP 202 with an anti-bot page that parsed to zero results and returned []; a valid pplication/json SEC XBRL response was rejected by a content-type gate; and escalation inspected only status == 'OK', discarding the reason. All web routes shared one transport, so there was no independent fallback.
**Impact:** src/lib/{retrieval-diagnostics,web-search,web-research}.ts, data-bridge.ts (sec_filings, 
esearch_health, diagnostics on web_search/web_fetch), python/harness/retrieval/{retry,sources,structured,planner,web,layer}.py, python/harness/research/{engine,run,receipt,escalation}.py; NVDA labeled eval python/harness/research/eval/nvda.py; operator smoke python/harness/research/smoke.py. Full decisions: ADR-H25–H28 in docs/harness/decisions.md.

### ADR-031 — Global market research: truthful capability matrix, suffix-aware identity, declared UA, provider eligibility (2026)
**Decision:** Coverage is reported as a per-market CAPABILITY MATRIX (recognition / searchable universe / quote / history / fundamentals / official filings / web+documents / observed health / live-checked) so a listed country is never read as 'every capability works'. Source health is keyed by the route dimensions (source x market x assetClass), never a market-level aggregate copied onto every provider. Instrument identity is global: a recognised exchange suffix (.TW, .KS, .L, …) is split into an implied market and the vault is queried for both the bare local code and the suffixed form; an unknown suffix is never guessed. Providers are gated by eligibility — SEC is skipped (not attempted-then-failed) for a non-US issuer. The shared fetch transport sends a DECLARED, email-contact User-Agent (required by SEC's fair-access policy) and handles JSON/XML/XBRL plus bounded text-PDF extraction. Escalation queries are market-aware (no global '10-K' assumption). Learning candidates carry the run's structured diagnostics and an explicit regional scope, remain inert, and promotion stays operator-gated.
**Reason:** Users reported US-centric failures on real non-US runs (TSMC/Samsung/Petrobras): all web fetch returned UNAVAILABLE, non-US tickers were 'not in the universe', SEC was attempted for a Taiwanese issuer, and coverage/health implied more than was true. Verified causes: (1) a spoofed browser UA gets 403 from SEC/TSMC IR, while a declared email-contact UA gets 200; (2) the vault stores bare local codes while users type Yahoo suffixes, so 2330.TW returned not_found though 2330 (TW) existed; (3) the SEC adapter's supports() gate was never consulted by the retrieval planner; (4) source-health collapsed recorded runs to (market, assetClass) and copied one aggregate onto every declared provider row, producing duplicates; (5) get_coverage instructed the agent to stop when JDdS lacked a ticker.
**Impact:** src/lib/{markets,web-research,web-search,retrieval-diagnostics}.ts, src/lib/harness/{capability,data-bridge,types}.ts, src/lib/fundamentals/providers/sec-edgar.ts, src/app/api/harness/data/route.ts; python/harness/{finance_tools,retrieval_tools,prompts}.py, python/harness/retrieval/structured.py, python/harness/research/escalation.py, python/harness/skills/learning.py, python/harness/eclipse_memory/{schema,store}.py. See docs/harness/decisions.md (ADR-H29..H32) and docs/DECISIONS.md ADR-031.

### ADR-033 — Aurora sentiment becomes an explainable Market Regime (2026)
**Decision:** The app-wide Aurora glow is driven by a transparent Market Regime model (src/lib/regime/) instead of a fixed 60% price / 40% coarse-RSS blend. A reading is a set of named components — price & breadth, volatility/options, macro/risk, news/narrative, positioning — each exposing score, weight actually used, availability, timestamp, source and confidence. Weights renormalize over the available set; missing inputs lower confidence (never a false neutral); stale inputs are excluded; a missing core price/breadth input makes the reading UNAVAILABLE. Taxonomy: Risk-on broadening / Fragile rally / Mixed / Risk-off / Narrative shock / Unavailable. Scope is explicit (selected market | US | global). All inputs come from JDDS only, through one new contract GET /api/regime. The glow stays decorative (pointer-events-none, aria-hidden); an accessible top-bar control opens the interactive frosted-glass breakdown, with actions to open Market Overview, filter news, ask Eclipse (be:ask-eclipse), and create a regime-change alert.
**Reason:** The old model was hardwired to US indices, exposed no confidence/data-age/provenance, used fixed weights, and could be influenced by cached/fallback data — attractive but not trustworthy. Financial data must never be fabricated; simulated/fallback/stale data must never read as live sentiment.
**Impact:** src/lib/regime/{types,scoring,client,store,visuals}.ts, src/app/api/regime/route.ts, src/components/layout/{aurora-glow,market-regime-control,top-bar}.tsx, src/components/ui/Aurora.tsx (added `dim`), src/components/screens/stock-chat.tsx (be:ask-eclipse), tests src/lib/regime/scoring.test.ts. See docs/DECISIONS.md ADR-033 and docs/ARCHITECTURE.md › Market Regime.


### ADR-034 — Autonomous-but-governed learning: provider layer, structured skill patches, hybrid namespaces, prompt segments (2026)
**Decision:** Four capabilities were added without relaxing any safety invariant. (1) External memory is a dedicated provider layer (python/harness/memory_providers/) implementing ONE MemoryProvider contract over the existing MCP boundary; external results are labelled provenance=external_provider / verified=false, fenced, native-first routed, never promoted, and external writes need an explicit user request AND approval (automatic external writes are absent by construction). (2) Self-improving skills are STRUCTURED patches (JSON-pointer replace/add/remove over an allowed-field set; identity/permission/tool/risk fields are not patchable) that always create a NEW SkillVersion; a stale base version is rejected, activation requires a passing evaluation, canary is limited to safe non-mutating skills, and rollback (manual or automatic on a regression/correction) is atomic and preserves both versions. (3) Memory retrieval uses logical namespaces and a bounded hybrid score whose semantic term can only nudge an already-authorized memory — never confirm, supersede, archive or authorize. (4) The prompt is split into immutable segments with an identity-only prompt_cache_key, so a new user message never invalidates the reusable prefix, and skills are injected as a bounded relevant-only manifest.
**Reason:** The product goal is autonomy in discovering, proposing, testing, versioning, activating, monitoring and rolling back improvements — but never autonomy in silently changing truth, permissions, financial-retrieval rules, or irreversible behavior. Each capability therefore ends at a reviewable, versioned, reversible or clearly-labelled boundary.
**Impact:** python/harness/memory_providers/* (new), python/harness/prompt_segments.py (new), python/harness/skills/{patches,versioning}.py (new), python/harness/eclipse_memory/hybrid.py (new); memory_tools.py, service.py, graph.py, main.py, agents.py; eclipse_memory/{policy,schema,store,skill_quality}.py; skills/{manifest,registry,evaluation}.py; migrations 12/13; routes /memory-providers/*, /skills/{id}/version-history + /skills/{id}/patch/**; TS skills-client/browser, eclipse-memory-client/browser/types, routes + a patch-version UI panel. Full decisions: ADR-H52—H55 in docs/harness/decisions.md.

### ADR-035 — Long-lived sessions, named profiles and reversible file editing (2026)
**Decision:** Sessions are first-class: a durable `ChatSession` (metadata + a bounded deterministic summary) in agent-memory.db, a run belongs to a session which belongs to one profile, session search ranks by the documented hybrid (exact phrase > BM25 > decision/correction/skill-failure/memory-action > recency > prose) with full filters, and a summary is searchable but never auto-injected. Named profiles are a metadata registry (eclipse_memory/profiles.py) over `.black-eclipse/profiles/` with per-namespace scoping, prompt profile, retention and settings, explicit READ-ONLY inheritance, and request-scoped isolation on every memory/session/skill/provider query. The Markdown memory projection is edited only through a targeted, atomic, versioned API (expectedVersion stale-edit guard, temp+fsync+rename, backup before regenerate, structured revision history + rollback + diff); SQLite remains canonical and archive/restore stay structured-memory actions.
**Reason:** Users need to continue long-lived sessions and keep work/personal/project memory isolated, and a hand-edited projection must not lose a concurrent edit, leave a partial file, or lack an audit trail — all without merging stores or adding infrastructure.
**Impact:** python/harness/persistence.py (ChatSession + run/session scope columns), session_index.py (build_summary), eclipse_memory/profiles.py, schemas.py (RunRequest ids), graph.py, main.py (/sessions*, /profiles*, filters); src/lib/harness/{memory-files,run-service,eclipse-memory-*,python-client}.ts, new /api/sessions, /api/profiles, /api/eclipse-memory/files/{edit,history}, and Memory Control Room tabs (Sessions/Profiles/Providers/Files/Skills + richer Overview). Full decisions: ADR-H56—H58 in docs/harness/decisions.md.

### ADR-036 - MCP Phase 6: trustworthy-before-capable hardening and resilience (2026)
**Decision:** The MCP layer is hardened in ordered phases. (1) The config contract is shareable and safe: `${VAR}`/`${env:VAR}` interpolation with a fail-closed remote rule, `selected_tools` include/exclude globs (legacy list unchanged), a static entry screen that REFUSES shell/interpreter/egress shapes at load, save and spawn, a fail-closed `trust` tier (`full`/`untrusted`) where an untrusted server needs a per-call approval for every non-read call and its own annotations can never activate a tool, and Eclipse tool names use a double-underscore boundary (`mcp__<server>__<tool>`) with the identity digest kept only as a genuine-collision last resort. The Python MCP read routes are operator-gated. (2) Lifecycle is resilient: separate jittered retry budgets, a call-time reconnect that re-checks authority, an honest session-expiry rule (a read replays once; a write returns `outcomeUncertain` and is never replayed), optional keepalive/recycle, POSIX orphan reaping via a stdlib-only supervisor (Windows relies on the SDK's Job Object), and lazy startup with a fingerprint-keyed on-disk schema cache. Every capability stays inert until an operator selects it; discovery never activates.
**Reason:** Everything users get wrong happens at the config boundary (a hostile entry that executes, a secret in plaintext, an unshareable file, an unusable filter, an undocumented authorization story), and one transient network blip permanently disabled a server until a manual reload while a crashed parent leaked children. Trust must be declared, not inferred from a server's own metadata, and a call that may have had an effect must say so rather than silently replay.
**Impact:** python/harness/mcp/{interpolation,screening,lifecycle,schema_cache,orphan_supervisor}.py (new); config.py, naming.py, client.py, manager.py, diagnostics.py, __init__.py; routers/mcp.py (read routes gated + save screening); python/requirements.txt (httpx2 pin); mcp-servers.example.json; tests python/tests/test_mcp_{config_hardening,lifecycle,orphan_reaping}.py. Full decisions: ADR-H67, ADR-H68 in docs/harness/decisions.md; progress docs/harness/mcp-phase-6-progress.md.

### ADR-037 - MCP data sources join native planning; sampling uses the run model; AC OAuth is a manual https step (2026)
**Decision:** An operator-declared MCP data tool (`data_source_tools: {tool: dataType}`) becomes a native `RetrievalSource` via `manager.mcp_sources()`, reaches `FinancialRetrievalLayer` through `ToolContext.mcp_manager`, and is considered by `plan_retrieval(additional_sources=...)` after native structured sources but before generic web; provenance stays `secondary`/`eclipseVerified=false` and JDdS is never silently upgraded. Server sampling (`sampling/createMessage`) runs on the runtime's own model adapter (`models.build_chat_model`) with the run's credentials and the policy token cap, still off by default. Eclipse-as-server is exercised by TWO offline clients (Streamable HTTP + SSE); the real Hermes interop is a documented manual step.
**Reason:** The deferred seams were real: without wiring, an MCP source could never be planned, a server sampling request could never be served, and "two clients" was asserted from one. The SDK's OAuth client refuses plain-http OAuth URLs (RFC 8252), so the interactive authorization-code leg is necessarily a manual https step — stated, not faked.
**Impact:** python/harness/mcp/{manager,mcp_retrieval,config,interactions}.py, python/harness/{tools,graph,service,retrieval_tools}.py, python/harness/retrieval/{layer,planner}.py, python/harness/routers/mcp.py; TS src/lib/harness/{mcp-types.ts}; tests test_mcp_source_planning.py, test_mcp_sampling.py, test_mcp_server_exposure.py, test_mcp_oauth.py. Full decision: ADR-H73 in docs/harness/decisions.md; progress docs/harness/mcp-phase-6-progress.md.

### ADR-038 - MCP config round-trip integrity: a save can never silently widen authority (2026)
**Decision:** `upsert_mcp_config` merges from the incoming model's `model_fields_set`, not `if value is not None`: absent means preserve, explicitly present means replace (including explicit `null`/`false`/`{}`/`[]` for clearing). An omitted field cannot be reset by a partial save; the UI can still clear a field because it always sends every field it owns. This makes a save provably unable to widen authority by omission (`trust`, `enabled_effects`, `tool_effects`, `data_source_tools`, `confirmed_catalog_digest`, `selected_tools`). `env` keeps its write-only merge; `install_mcp_integration` is create-if-absent; `public_dict()` emits the raw operator-set bounds so the wire is lossless. A pure TS module (`src/lib/harness/mcp-draft.ts`) owns view<->draft<->input mapping.
**Reason:** The Phase-6 field set lived only on the wire, not in the UI payload. Re-saving a server through Settings silently downgraded `untrusted`->`full`, disabled resources/prompts, dropped the data-source declaration and every tool effect, and invalidated the catalog confirmation - a silent authority widening.
**Impact:** python/harness/mcp/{config,runtime,introspection,oauth_flow,manager}.py, python/harness/service.py, src/lib/harness/{mcp-types,mcp-draft,mcp-client}.ts, src/components/screens/mcp-settings.tsx + src/components/screens/mcp/*, src/app/api/harness/mcp/{integrations/[id]/install,supply-chain}/route.ts; tests test_mcp_config_roundtrip.py, test_mcp_example_config.py, mcp-draft.test.ts. See docs/harness/decisions.md ADR-H74 and docs/harness/mcp-ui-parity-progress.md.

### ADR-039 - The release gate is local and runnable (2026)
**Decision:** Delete the unreachable `Black-Eclipse-site/.github/workflows/` (GitHub reads only root `/.github/workflows`; the repo git root is above the app dir) and replace it with ONE local gate: `npm run check:all` runs prisma generate, typecheck, lint, `npm test` and `npm run test:evals` in order, failing on the first error. The three eval gates (retrieval, contradiction, compaction) move into `npm test` via `scripts/test-evals.js` (offline, ~0.7s) using the explicit-`sys.path` launcher; the dead `semantic_cli` reference is removed. The CHANGELOG obligation is enforced by `.githooks/commit-msg` (non-doc change must stage the CHANGELOG; `SKIP_CHANGELOG_CHECK=1` bypass).
**Reason:** The release gate was fictional three ways: a CI job that could not pass, a workflow GitHub never read, and eval floors no local command ran.
**Impact:** .github/ removed; scripts/test-evals.js + scripts/check-all.js (new); package.json (`test:evals`, `check:all`, `test` includes evals); .githooks/commit-msg (new); scripts/install-git-hooks.js. See docs/harness/decisions.md ADR-H75 and docs/harness/engineering-hardening-progress.md.

### ADR-040 - A theme preset is holistic: it carries a partial ChatBot-backdrop snapshot (2026)
**Decision:** A preset is more than design tokens. `ThemePreset` and
`UserThemePreset` carry an optional **partial** `background` snapshot
(`Partial<BackgroundPrefs>`, validated by `normalizePresetBackground`). Selecting
a preset previews its backdrop (`useBackgroundStore.preview`, non-persisted) and
Apply commits it (`applyPreset(theme, background)`); the snapshot is **merged over
the user's live background prefs**, so a preset that only sets the wordmark keeps
the user's wallpaper and vice versa. `ChatWordmark` reads `previewPrefs ?? prefs`
so glyphs update live. The theme store calls the background store's adapter
(`applyBackgroundToDocument`) rather than subscribing to it — a store-to-store
subscription was a module **cycle** (`backgrounds.ts` imported `useThemeStore`);
`refreshBackground()` re-asserts the saved backdrop after every theme write.
**Reason:** Applying a preset changed colors/fonts/sidebar but left the ChatBot
landing (the most visible surface) untouched, because the two stores had no
coupling. The separation was intentional for *manual* backgrounds but wrong for a
unified "look".
**Impact:** src/lib/theme/{tokens,store,index}.ts, src/lib/backgrounds.ts,
src/components/screens/customization-settings.tsx (+ extracted
`customization-preview.tsx` to stay under the size ratchet),
src/components/chat/chat-wordmark.tsx, src/components/layout/{sidebar,top-bar}.tsx,
src/app/globals.css (`--font-heading`). Tests: tokens/store/backgrounds + new
preset-snapshot cases. See docs/ARCHITECTURE.md.

### ADR-019 — Provider-agnostic financial retrieval layer; web research is first-class (2026)
**Decision:** The agent requests an **objective + data type**, not a provider. A
dedicated `python/harness/retrieval/` package (`FinancialRetrievalLayer`) plans
the source(s) with a deterministic, explainable planner, retrieves them
(cache-aware, possibly in parallel), normalizes to one
`NormalizedFinancialObject`, validates, resolves source conflicts, and returns
provenance-carrying evidence. Tools call the layer; the layer is the only place
that knows JDdS, yfinance or the web exist. Web research
(`src/lib/web-research.ts`, the single shared search+fetch implementation) is a
first-class selectable path, not merely a fallback. A precise quantitative
request (historical prices, macro series, derived metrics) **never** uses web:
with no structured dataset the layer returns `UNAVAILABLE`.
**Why:** The Phase-2 objective is "what information do I need → what is the best
source → structured/cache/web/multiple → is the evidence fresh and reliable?" —
a decision that belongs in one explainable place, not scattered across tools.
It keeps the agent decoupled from every individual data source and keeps JDdS and
yfinance as *sources*, not mandates.
**Impact:** Adding a provider means implementing the `RetrievalSource` contract
and adding its name to the planner; source-selection policy is data. The
retrieval cache is in-process and shared across runs with in-flight
deduplication (no new database). See
[`harness/architecture.md`](./harness/architecture.md) and
[`harness/decisions.md`](./harness/decisions.md) (ADR-H10…H12).

---

### ADR-020 — A ticker is not an instrument identity; unhonoured qualifiers fail loudly (2026)
**Decision:** Instrument resolution requires a **canonical identity** —
`{ market, symbol, currency (ISO 4217), assetClass }` — not a bare ticker. Every
instrument resolution and payload carries it. A market qualifier (venue, country,
market id) is resolved through a single alias table (`resolveMarketId`); if it
cannot be honored the request **fails** (`market_unknown`). A bare ticker that
maps to more than one instrument is **`AMBIGUOUS`**, never a "best match". A
provider answer whose currency/market disagrees with the resolved identity is
**discarded**. `AMBIGUOUS` and `UNAVAILABLE` are correct, first-class outcomes.
**Why:** A live analysis of `FFC` (Fauji Fertilizer, PSX) returned a US
closed-end fund's price, stamped `realtime`, because the live path dropped the
`market` qualifier and fetched the bare ticker. Tickers are reused across venues,
asset classes and issuers (4160 colliding tickers in the committed master alone)
and carry no currency — a confidently wrong number is worse than no number.
**Impact:** `src/lib/instrument-identity.ts` owns resolution and returns a typed
failure union; the read-only bridge returns `AMBIGUOUS`/`UNAVAILABLE`; the Python
retrieval contract gains `AMBIGUOUS` + `InstrumentIdentity`; the fetchers refuse
an unresolvable market rather than emitting a bare US ticker. Every real
collision is a permanent regression fixture
(`src/lib/instrument-collisions.json`, generated from the master). Supersedes any
implicit "symbol is enough" assumption in the retrieval layer (ADR-019).

---

### ADR-021 — One failure contract, declared coverage, and terms-aware identity (2026)
**Decision:** Every tool and source reports failure through one taxonomy
(`src/lib/harness/failure.ts`): `ambiguous`, `market_unknown`, `not_found`,
`currency_mismatch`, `venue_mismatch`, `terms_missing`, `not_covered`,
`no_data`, `source_failure`, `incomplete`, `conflict`, `invalid_request`.
Message text is derived from the class, never hand-written per tool. Coverage is
**declared data** (`src/lib/harness/coverage.ts`), queryable via a `coverage`
capability — an unregistered market/asset class returns `not_covered` instead of
routing to a guessed backend. Derivatives and fixed income require contract
terms (`expiry`/`strike`/`right`, `maturity`/`coupon`/`seniority`) before a
value may be returned. Every numeric payload carries `currency` and `units`.
`get_news` falls through from the vault to the shared web transport and never
phrases a source failure as "no news". Source health is derived from recorded
runs (`healthy`/`degraded`/`failing`/`unknown`), so confidence is earned.
**Why:** The same underlying miss behaved differently across tools
(`get_market_snapshot` refused; `market_get_quote` returned a US fund;
`get_news` reported "no news" on an infrastructure miss). A false "no news" in a
financial tool reads as "nothing happened" — materially misleading. Uniformity
is the only way one tool's safety guarantee holds for all of them.
**Impact:** Extends ADR-020 (identity). Tools route through the shared classes;
a new asset class/venue is a data change in `coverage.ts`. `isCovered()` gates a
request before any source is attempted.

---

### ADR-022 — Deterministic retrieval, strict macro identity, and top-level degradation (2026)
**Decision:** Four rules complete the identity contract. (1) **D1 — parse once,
persist, replay.** A successful retrieval is written to `RetrievalRecord`
(`agent-memory.db`) keyed by request; an identical, non-realtime request replays
the stored result, so the same question yields identical figures and citations
instead of re-scraping (which drifts). (2) **A8 — macro series are identities.**
`python/harness/macro_identity.py` + `src/lib/harness/macro-identity.ts` resolve a
series to geography + institution + measure + adjustment + frequency. A generic
measure (`POLICY_RATE`) without a geography is AMBIGUOUS and never defaults to
US; non-US series route to non-US sources. (3) **D2 — truncation is withheld.**
A partial numeric parse (`1,23` from `1,234.56`) is detected and the result is
UNAVAILABLE + `incomplete`, never a figure. (4) **D3 — degradation is
top-level.** `degraded`/`degradation`/`incomplete`/`conflicts`/
`provenanceVerified` are fields on the result itself, not nested metadata, so a
value whose identity was not verified is structurally flagged.
**Why:** The same infrastructure produced drifting fundamentals (read-time
scrapes), conflicting policy-rate values (measure collision), a truncated figure,
and a wrong instrument — each invisible to the consumer because degradation was
either absent or buried.
**Impact:** Extends ADR-020/021. `RetrievalRecord` is idempotent (upsert on key).
Realtime data types are excluded from durable replay by design.

---

### ADR-023 — One shared resolution layer for every tool; no country/venue/asset-class default (2026)
**Decision:** Instrument resolution is unified in a single bridge resolver
(`resolveVaultInstrument` / `resolveAnyInstrument`), and **every** resolving
capability (`market_snapshot`, `fundamentals`, `live_quote`, `price_history`,
`financial_statements`) routes through it. A tool cannot bypass the gate; a test
enumerates the tool surface and fails if a resolving tool touches a non-gated
capability. There is **no** US (or any) default: an unqualified multi-listed
ticker is `AMBIGUOUS`; an unknown venue is `NOT_COVERED` (not `AMBIGUOUS`); FX
and crypto are first-class asset classes that never inherit an equity country
universe; a wrong-currency/venue answer is discarded. Instrument identity carries
the exchange and MIC where known.
**Why:** The previous round gated only the quote fallback path, so
`market_get_quote` was safe while `fundamentals_get_company_profile` silently
returned the US ADR of a multi-listed issuer and `market_snapshot` defaulted
unqualified lookups to the US — the original defect, reachable by omitting a
parameter.
**Impact:** Extends ADR-020/021/022. `market_unknown` now maps to
`not_covered`. New `financial_statements` capability serves the persisted
`FundamentalsCache` (parse-once, deterministic). Source health records from the
live path, keyed by source × market × asset class. Macro generic measures are
`AMBIGUOUS` even with a geography, and macro rows are newest-first with units.

---

### ADR-024 — Generic macro aliases never narrow silently; recognition ≠ coverage (2026)
**Decision:** A generic macro alias (`CPI`, `INFLATION`, `POLICY_RATE`, …) resolves
only when it names exactly ONE measure; a multi-meaning alias is AMBIGUOUS with
named candidates, and a recognised concept with no registered series (`PMI`) is
NOT_COVERED. An index series carries its `unitBase` (e.g. `1982-84=100`) so the
number is meaningful. Macro rows are newest-first with an explicit `asOf`. A
venue the system recognises but does not cover (`CME` → `DERIV`) is NOT_COVERED,
not "unknown venue" — recognition and coverage are distinct. FX/crypto payloads
state `served: live` and echo the requested symbol, since the vault holds
equities only. Warnings are de-duplicated, and a slow source cadence marks data
"delayed" (a transformation, not a quality warning).
**Why:** CPI silently resolved to the index level, `ES/CME` read as "unknown
venue", and the FX payload said "not in the universe" beside a valid quote — each
a confidently-wrong or self-contradictory answer.
**Impact:** Extends ADR-020…023. `macro_resolve` in TS is the single alias table
(the Python tool asks it before falling back locally). `get_source_health`
enumerates the declared source registry so a source with no runs is `unknown`,
not absent.

### ADR-027 — Evidence-grounded answering: a deterministic research flow over the existing retrieval layer (2026)
**Decision:** Add one shared Python package `python/harness/research/` that turns
an objective into a typed research mandate, retrieves each fact leg through the
*existing* Financial Retrieval Layer, produces deterministic per-leg coverage,
validates structured claims against actual evidence, computes named metrics in
pure Python, and persists a bounded research receipt. It adds no agent loop, no
model adapter, no second retrieval system and no second database. The model
interprets and explains; the harness owns coverage, calculation truth, structural
validation and the accept/partial/reject decision. Point-in-time `asOf` is
explicit and enters cache identity; `NormalizedFinancialObject` gains additive
`basis`/`publishedAt`. Receipts live in `agent-memory.db` (idempotent DDL), never
in `eclipse-memory.db` or JDdS.
**Why:** Phase 2 retrieval produced provenance and conflicts but nothing turned
an objective into required facts or gated the model's citations against evidence.
A shared flow is reusable by every financial agent (and future workflow nodes)
rather than each rebuilding citation logic.
**Impact:** New `/research/*` runtime endpoints and `ResearchReceipt` table; 61
new tests; general/unsupported chat is unchanged (`supported=false`). Full
decision set: ADR-H15…H19 in `docs/harness/decisions.md`; progress in
`docs/harness/phase-4-progress.md`.

---

### ADR-028 — Phase 4 hardening: deterministic identifiers, gated live chat, honest semantic evaluation (2026)
**Decision:** Evidence/calculation identifiers use SHA-256 content digests (not
the salted built-in `hash()`); a supported `financial_comparison` is routed into
the evidence gate before the agent graph and never silently downgraded; period
matching is canonical (no substring-of-digits); a versioned human-labeled
semantic-support evaluation reports false accepts/rejects per slice, and
production claim status stays `needs_review` where support is unproven.
Supersession links come only from explicit source revision metadata.
**Why:** The first Phase 4 cut had non-reproducible receipt hashes, a coverage
gate that let missing required metadata pass, no live wiring, and no way to
measure semantic support without over-claiming.
**Impact:** `python/harness/research/{periods,ids,synthesis,supersession,run}.py`
+ `eval/`; additive `research` on `HarnessRun`/`RunResult`; TS `ResearchRunInfo`
contracts + `/api/harness/runs/[id]/receipt` + `ResearchEvidencePanel`. Full
decisions: ADR-H20…H22 in `docs/harness/decisions.md`.

---

### ADR-029 — Bounded web escalation keeps answers evidence-grounded when structured data cannot (2026)
**Decision:** When structured retrieval is empty, stale, missing a requested
fiscal period, or lacks required metadata, the research flow escalates a leg
through a fixed hierarchy — structured → primary-source web (SEC/IR/regulators)
→ bounded reputable secondary web — using the existing web bridge and a single
`ResearchBudget`. Every numeric web fact must pass a deterministic validator
(fetched-page id, verbatim `sourceSpan`, value-in-span, no contradicting period,
metric/period/currency/basis match) before becoming evidence. Coverage gains
distinct statuses (`no_matching_period`, `stale`, `qualified`), and a missing
consolidation basis qualifies the value with disclosure rather than failing it.
**Why:** Stopping at an unrelated structured row produced unhelpful answers; but
"answer anyhow" must never mean invent. A snippet is not a number, a page
instruction is not a fact, and the model cannot decide a source is trustworthy.
**Impact:** `python/harness/research/{budget,web_facts,escalation}.py`;
`retrieval/web.py` serves `financial_statements`; receipt carries escalation +
budget; evidence panel shows tier/dates/period/span. Full decisions: ADR-H23/H24
in `docs/harness/decisions.md`.

---

## Decisions implied by the code (not yet formal ADRs)

These are real, load-bearing, and currently only documented in prose. Promote one
to an ADR if it gets challenged.

- **SQLite-only JDDS.** PostgreSQL was removed along with the old daemon.
  Reintroducing `DATABASE_URL`/pg tooling would be a new ADR.
- **Keep-alive tabs, single page.** Tabs mount on first visit and stay mounted;
  no URL routing for main screens.
- **In-process schedulers.** Cron runs inside the Next.js server (not Vercel-
  friendly by design; a `POST /api/cron/run` endpoint exists for hosted setups).
- **Synchronous-write discipline.** Bulk writers yield and sleep by design
  because better-sqlite3 blocks the event loop.
- **Separate market-workspace engine.** The Chart Workspace tab (tab id
  `market-workspace`) has its own panel engine rather than reusing the widget
  workspace.

---

### ADR-032 — Reliability & global-research hardening: capability-specific health, distinct route categories, a deterministic comparability gate (2026)
**Decision:** (1) The truthful-capability tools (`get_capability_matrix`,
`get_research_health`) are read-only and in the Eclipse agent allowlist. (2) A
tool's own `timeout_ms` is authoritative; the agent `toolTimeoutMs` is only a
ceiling, and optional news enrichment has its own bounded budget. (3) Source
health is keyed `(source × market × assetClass × capability × probeKind)`: a
healthy quote never makes statements/filings healthy, zero attempts stays
`unknown`, and fixture probes are labelled and excluded from live health. (4) The
route planner reports distinct categories (ineligible / not_implemented /
unconfigured / provider_failure / no_instrument / no_data / no_matching_period /
stale_data / invalid_document) so a skip is never an outage. (5) Documents
distinguish `too_large` / `empty_response` / `javascript_only` /
`unsupported_document` / `parse_failure`, preserve title/document-id/publication
metadata, stream to a bounded size, and refuse a value-less XBRL taxonomy. (6) A
comparison is permitted only when the legs are the same fact for the same entity
— `research/comparability.py` blocks a demerger/ticker reuse, unequal durations
(9M vs 12M), consolidation basis, metric definition, currency, unit, scale or an
unstated restatement, with the exact reason; each verified leg is still reported
and no misleading percentage is computed. The model is never the validator.
**Why:** The international runs recorded `TOOL_FORBIDDEN` for
`get_capability_matrix`, a 20s news timeout despite a declared 30s budget,
`unknown — 0 ok / 0 fail` health rows beside a `configured` coverage claim, SEC
attempted for non-US issuers, and correctness risks (Tata demerger, 9M vs 12M,
standalone vs consolidated, gross vs operations, issuer currency vs thousands,
EPS after a subdivision) that only the model understood.
**Impact:** `python/harness/{agents,finance_tools,retrieval_tools,tools,graph}.py`,
`python/harness/retrieval/{routing,layer}.py`,
`python/harness/research/{comparability,engine,run,receipt,citations}.py`,
`src/lib/harness/{capability,coverage,data-bridge,types}.ts`,
`src/lib/{web-research,retrieval-diagnostics}.ts`, `scripts/test-python.js`.
Progress: `docs/harness/phase-4-progress.md` (Phase 8).

---

### ADR-041 — Icon set is Phosphor behind a single aliased surface (2026)
**Decision:** The UI renders **Phosphor** (`@phosphor-icons/react` ^2.1.10) instead of lucide-react. Every icon is imported from ONE module, `src/lib/icons/index.ts`, which re-exports each Phosphor glyph under its original Lucide name (e.g. lucide `Search` → Phosphor `MagnifyingGlass`) plus `LucideIcon` (Phosphor `Icon`), so call sites and the `LucideIcon` map types stay unchanged and the set can be swapped in one file. Components never import an icon library directly. Selected/active states use Phosphor's `weight="fill"` prop — Phosphor outline glyphs are filled paths, so `fill="none"` would erase them; accent-colored fills use a className text color instead.
**Reason:** Lucide is ubiquitous and reads generic; Phosphor's weights (including duotone) and shape language are distinctive and suit the dark glass UI. Aliasing avoids a 165-file / 216-icon rename while confining the dependency to one module.
**Impact:** `src/lib/icons/index.ts` (new). Every icon-importing module imports from `@/lib/icons`. Three files that render icons gained a `'use client'` boundary (`components/chat/{chat-history,markdown}.tsx`, `components/screens/portfolio/fund-store.ts`) because Phosphor icons read `useContext`. `lucide-react` removed; `@phosphor-icons/react` added. `components.json` (`shadcn` CLI `iconLibrary`) still says `lucide` for generated components — reconcile when the CLI is next run. See `docs/NAVIGATION.md` (By task).

---

### ADR-043 — Structured responses ride the existing Markdown stream as a fenced block; the renderer is a registered component registry (2026)
**Decision:** Add the **Interactive Response Engine**: an assistant turn may include ONE fenced ```eclipse-response block whose JSON body a versioned zod contract (`src/lib/response/contract.ts`) validates, and which renders through a registry of components (`src/components/response/response-view.tsx`) — metric grids, sortable/filterable tables, ECharts charts, SVG diagrams, timelines, citations — inline in the existing chat. It reuses the **existing** Markdown renderer (`components/chat/markdown.tsx` intercepts the fence language), the **existing** ECharts dependency (lazily imported), and the **existing** message persistence (the fence is stored in `Message.content`; reopening re-parses it). No second chat engine, stream, transport, or artifact store is introduced.
**Reason:** The documented gap (`docs/chat-ui-audit.md` §16, Risk R7) was that the runtime's terminal output is plain Markdown with no structured wire contract, so structured blocks/charts were deliberately not built. A fenced block is the smallest real producer→renderer contract that needs no Python protocol rewrite, works in both chat surfaces unchanged, persists for free, and degrades safely (unknown/malformed blocks become diagnostics, never code execution, never a lost answer).
**Impact:** New `src/lib/response/**` (contract/parse/chart-option/diagram-layout + tests) and `src/components/response/**`; `components/chat/markdown.tsx` gains a `streaming` prop and the fence branch; `chat-history.tsx` passes it. `zod` promoted to a direct dependency (was transitive). `prompts.py` gains a "Structured answers" section so agents can author the block when it helps. No Python protocol, database schema, or persisted-conversation migration. Security posture: structured JSON only — never model-authored JS/HTML; per-block resource limits; per-block error boundaries.

**Amendment (2026-10-10) — full build-out.** The previously deferred capabilities are now shipped, each by reusing existing infrastructure: a deterministic **response planner** (`src/lib/response/planner.ts`; GFM tables → interactive tables, link-only source lists → citations, for every settled answer, no model call); **backend-driven controls** (`controls-block.tsx`) that compose a validated prompt and re-enter the existing agent loop via `onFollowup`; **export** JSON/CSV/PNG/PDF (`export.ts`; PNG via an ECharts instance registry, PDF via lazy `jspdf`+`jspdf-autotable`); **Artifact Center save** via the existing `registerArtifact`; a **`financial_chart`** block wrapping the existing `ChartWidget`; and a **`mermaid`** block (lazy, `securityLevel: 'strict'`; `mermaid` 12.1.0). Still out of scope, documented not faked: React-Flow *editable* graphs (diagrams are read-only by design), server-side pagination for tables over the 2000-row contract cap, and PDF embedding of unmounted/financial charts (those are listed in the PDF's export notes). See `docs/interactive-response-engine.md`.

**Amendment (2026-10-11) — rich block family + link hardening.** The catalogue grows to the full interactive vocabulary: **`stepper`** (numbered process with per-step status), **`infographic`** (icon cards — icons resolved through a semantic allowlist in `src/lib/response/icons.ts`; the model names a key, never a component or import path), **`report`** (a multi-section container whose section `blocks` are validated recursively like `tabs`), **`node_graph`** (a *selectable* graph that reuses the same deterministic `diagram-layout` engine — no second graph library; nodes are focusable/selectable and reveal their `detail`), **`action_group`** (PROPOSED next steps whose click sends a follow-up prompt into the existing agent loop — never a privileged backend call), and **`artifact`** (a reference to the existing Artifact Center, linked via its own download route). Spec-name synonyms are normalized to canonical blocks (`text`→`markdown`, `table`→`data_table`, `source_list`→`citations`, `interactive_form`→`controls`, `execution_timeline`→`timeline`). Every model-supplied citation/artifact link is now **scheme-restricted to http(s)** — bare `z.string().url()` accepts `javascript:`/`data:`, which becomes an XSS vector once rendered as an anchor. The agent guidance in `prompts.py` was rewritten to be more directive and to list the new blocks, **within the unchanged 26410-char ceiling**. All new blocks render through the same registry + `BlockBoundary`, and PDF export represents each. Tests: `src/lib/response/contract-blocks.test.ts`.

**Amendment (2026-10-14) — branching `flow` block + native-flow routing over Mermaid.** A new dependency-free **`flow`** block renders a branching process as frosted HTML/CSS cards: ordered `steps` joined by CSS connectors, each step optionally branching into parallel `paths`. It is distinct from `workflow` (strictly linear icon stages) and from `diagram`/`node_graph` (absolute-positioned SVG graphs) — the native, responsive answer for a flowchart. `mermaid` is now reserved for sequence/ER/class/gantt, and the prompt directs flow/flowchart answers to `diagram`/`flow`/`workflow`, so the heavy `mermaid` bundle stays unloaded for ordinary process answers. New aliases route producer vocabulary (`process`→`workflow`, `process_flow`/`pipeline`→`flow`, `flow_graph`→`diagram`). Rendering reuses the existing registry + `BlockBoundary`; the block is data-only (no model-authored HTML). Tests: `contract-blocks.test.ts`, `render.test.tsx`; `prompts.py` stays within its char ceiling. See `docs/interactive-response-engine.md`.

---

### ADR-044 — Private per-conversation long-term memory: a first-class `conversation` scope on the ONE Eclipse-Memory pipeline (2026)
**Decision:** A bot chat and each group-room member turn keep their own **private durable memory**, isolated from every other conversation/bot and from ordinary chat. This is implemented as an additive **`conversation` scope** on the existing `MemoryRecord.scope` column, keyed by `scopeId = conversation id` (the run's `sessionId`: `bot-<id>` or `group-<gid>-<botid>`). A `conversation` actor may read ONLY its own bucket, the signed-in user's own `user` rows (when present) and truly-global `general` knowledge — never another conversation. The scope is normalized at the ONE write gate (`EclipseMemoryService.commit_decision` via `decision.resolve_owner_scope`): in a conversation-scoped run with no signed-in user, a personal (`user`) fact is owned by that conversation. Opt-in is explicit: only the bot/group call sites set `memoryScope: 'conversation'`, which the front door maps to `RunRequest.conversationId`; ordinary chat is unchanged. Scheduled bot runs bind the SAME `bot-<id>` conversation so background learning is recalled in chat. `bot_delete` archives the bot's conversation memory (never hard-deletes). No new database, table, or column.
**Reason:** The bots UI documented "no separate memory" — every durable fact landed in shared `general`/unattributable `user` rows, so a bot could neither accumulate its own long-term context nor be isolated from other bots. A separate memory store per bot would violate the one-store/one-pipeline invariants (ADR-026/034); a new scope value reuses the authoritative scope-before-rank filter, the single decision gate and the single persistence path.
**Impact:** `eclipse_memory/{decision,retrieval,service}.py` (`resolve_owner_scope`, conversation candidate gathering + `_scope_authorized`, `forget_conversation`); `memory_tools.py` (default scope + `conversation` enum); `graph.py`/`graph_context.py`/`schemas.py`/`tools.py` (thread `conversationId`/`conversation_id`); `automation/dispatcher.py` (bind `bot-<id>`) and `automation/tools.py` (purge on delete); `routers/memory.py` (scopeId filter + `POST /eclipse-memory/conversations/{id}/forget`); TS `agent/types.ts`, `agent/registry.ts`, `harness/run-service.ts`, `harness/python-client.ts`, `harness/eclipse-memory-browser.ts`, both chat routes, `bot-chat.tsx`, `group-chat-room.tsx`, `bot-detail.tsx`, new `bot-memory-section.tsx`, i18n `en.ts`. Conversation ids remain local labels, not security identities. Tests: `python/tests/test_memory_conversation.py`. See `docs/harness/memory-skills-evolution-plan.md`, `docs/harness/automation-engine.md`.

---

### ADR-045 - Page targeting: a resolvable current-page default, distinguishable summaries, plain-string data sources, one owner policy (2026)
**Decision:** (1) `UiService.get_current_page` resolves the page by explicit `blueprintId`, else `path`, else `title`, else the owner's **most recently updated** page; it always returns `resolution` and, on a choice among candidates, the other `candidates` - it never silently returns row 1. Paths are normalised and `None` never matches; an unmatched lookup is `UI_BLUEPRINT_PATH_UNASSIGNED` when pages exist without a path, else `UI_BLUEPRINT_NOT_FOUND`. A new `GET /ui/current-page` exposes it (the `/ui/blueprints/{id}/current` route is unchanged). (2) `BlueprintSummary` gains `path` and `componentIds` so pages that share a title are distinguishable, and `q` searches path too. (3) `get_blueprint` fetches unscoped and applies the owner rule in the service, so `get_current_page`/`get_layout`/`preview` share ONE policy; a mismatch is the distinct `UI_BLUEPRINT_OWNER_MISMATCH` (HTTP 403), never `unknown blueprint`; read tools pass no owner scope. (4) The UI runtime advertises its valid structured data sources via `get_capabilities()["dataSourceRegistry"]`; the one family is `portfolio:<portfolio_id>:<view>` for the Ledger kinds.
**Reason:** Fifteen blueprint pages can share the title "Main", and there was no active-page notion, no path in the list summary, and path matching could never hit an unset path - so an agent could not unambiguously name a page, and the HTTP route and the agent tool disagreed on owner scope. A `data-table`/`metric-card` had a free-text source with no registry, so no one could know which strings resolve a portfolio. These are contract/data-shape fixes, not new subsystems.
**Impact:** `python/harness/ui/{service,store,model,catalog,tools}.py`, `python/harness/routers/ui.py`, `src/lib/harness/ui-client.ts`; tests in `python/tests/test_ui_{service,tools,api}.py`; docs `docs/ui-engineering.md`, `docs/portfolio-accounting.md`. No new store, table, migration, or agent loop. A page-level icon remains a scoped, unbuilt gap (needs a new model field + renderer; not the chat response-block icon enum).

**Amendment (2026-10-15) - the generic render path + portfolio surfaced end to end.** The page-targeting/data-source work above was contract-only: an agent could name a page and declare a `portfolio:<id>:<view>` source, but nothing rendered it (Pages/Builder render only host widget ids, and `source` config was inert). Two host primitives now close the loop: `data-table` and `metric-card` (`src/lib/workspace/generic-widgets.tsx`, registered in `src/lib/dashboard/registry.tsx` with ids matching the runtime catalog, so the workspace kernel does not drop them). `src/lib/workspace/data-source.ts` is the ONE allow-listed resolver (never an arbitrary URL): it parses the source, fetches through the existing portfolio front door (`src/lib/portfolio/engine-read.ts` -> `/api/portfolio/{id}/query|history|tax-lots`), caches it on a dynamic DataHub topic, and returns rows/columns or a value - or an explicit UNAVAILABLE with the reason. The six bespoke `portfolio-*` catalog kinds introduced earlier were removed: every advertised component is now renderable. Pages cache their arrangement by blueprint revision (`pageStorageKey(id, revision)`) so an agent edit seeds a fresh workspace instead of being shadowed, and Refresh reloads the open page. `ui_verify_page` / `UiService.verify_page` reads a page back and reports per-widget renderability. Remaining gaps (charts, other source families, live push) stay scoped: `docs/ui-engineering.md`.

**Amendment 2 (2026-10-15) - production surface: full renderable vocabulary, more source families, parity, page icons.** The generic render path was broadened so "compose anything" is real, not aspirational. The catalog seed (`builtin_specs`) is now EXACTLY `RENDERABLE_COMPONENTS`, the ids the host registry defines (metric-card, data-table, list, line/area/bar/pie-chart, markdown, status-indicator, empty-state, error-state); the nine never-renderable foundational kinds were removed, and parity is enforced on both sides (python/tests/test_ui_service.py + src/lib/workspace/generic-widgets.test.ts). The resolver gained allow-listed families: market:quote:<SYMBOLS>, market:indices:<MARKET>, market:price:<SYMBOL> (series), news:latest, agent:activity - each backed by an existing read route, cached on DataHub, still never fetching an arbitrary URL. Page-level icons (the previously scoped gap) are implemented: `UiBlueprint.icon`/`BlueprintSummary.icon` with an idempotent add-column migration, agent/HTTP/TS support, and Pages UI rendering. Pages refreshes the open page when its durable revision changes (focus + 20s poll), so an agent edit appears without disrupting a user's local arrangement. Docs: docs/ui-engineering.md.

**Amendment 3 (2026-10-15) - the remaining gaps closed: more source families, push updates, engineer loop proven.**
The resolver gained four more allow-listed families, each backed by an existing read route and tested: tasks:list / tasks:plans, artifacts:list, research:list, and jdds:search:<QUERY> (JDDS instrument search). Live page updates moved from a blind poll to a Server-Sent-Events stream (GET /api/ui/stream): the server watches the runtime's most-recently-updated blueprint (id:revision:updatedAt) and pushes a changed event; Pages reloads the open blueprint only when its durable revision moves (focus + a slow 60s poll remain as fallbacks). The Engineer -> Compose closure is now proven end to end in test_ui_api.py: register an engineered component, discover it via GET /ui/components, add it to a page, and GET /ui/blueprints/{id}/verify reports it renderable. Docs: docs/ui-engineering.md.

**Amendment 4 (2026-10-15) - production hardening: verify every family, no silent widget drops.** erify_page now validates a declared source against ALL families via catalog.validate_source (portfolio, market, news, agent, tasks, artifacts, research, jdds), not only portfolio. And the Pages surface no longer silently drops a blueprint widget whose id the host registry does not render yet: it passes the workspace a knownWidgetIds set = host registry + every id present in the blueprint, and 
enderDashboardWidget draws an explicit "Needs a renderer" card for it (the Engineer path can add the real renderer). Tests: python/tests/test_ui_service.py, src/app/api/ui/stream/route.test.ts. Docs: docs/ui-engineering.md.

---

### ADR-046 - The Eclipse theme is APP-WIDE: a server source-of-truth plus one canonical origin (2026)
**Decision:** The user's theme stops being a per-browser property. **(1) Server source of truth:** the JDDS `SystemConfig` table (key `eclipse_theme`) owns the single app-wide theme, exposed by `/api/theme` (GET/PUT/DELETE, operator-gated via the existing `guardMutation`). The client store (`src/lib/theme/store.ts`) demotes `localStorage` (`be_theme_v1`) to a per-origin first-paint cache: on boot it hydrates the cache then `hydrateFromServer()` adopts the saved app-wide theme; every edit pushes to the server; Reset issues DELETE. A *successful* null read means "shipped default": if the app-wide theme was never initialized the client MIGRATES its pre-existing per-origin theme up instead of wiping it; after an explicit reset it clears the local override (an unreachable server leaves the cache untouched). **(2) One canonical origin:** when `BE_CANONICAL_HOST` is set, `src/middleware.ts` redirects any other host to it (307, path + query preserved) before the auth gate; `scripts/local-host.js` sets it to the local hostname by default, while `npm run dev:next` leaves it unset so zero-config localhost is unchanged.
**Reason:** `localStorage` is scoped to the browser ORIGIN, so `http://localhost:3000`, `http://127.0.0.1:3000` and `https://blackeclipse` each held their own theme - three visibly different instances of one app. The repo had already solved this exact problem for chat transcripts (server copy + `localStorage` cache); the theme was the last major surface left per-origin. A DB property also makes the theme shared across devices, which is what "app-wide" means.
**Impact:** New `src/lib/theme/server.ts` + `src/app/api/theme/route.ts`; `src/lib/theme/store.ts` (`persistLocal`/`pushThemeToServer`/`clearServerTheme`, `hydrateFromServer`, server-aware `reset` + `useHydrateTheme`); `src/middleware.ts` (canonical redirect) + `scripts/local-host.js` (sets `BE_CANONICAL_HOST`) + `.env.example`. `localStorage` keys are unchanged; user-saved presets (`be_theme_presets_v1`) stay per-origin (a personal library, not the active look). Tests `src/app/api/theme/route.test.ts`, `src/lib/theme/store.test.ts`, `src/middleware.test.ts`. Docs `docs/ARCHITECTURE.md`, `docs/local-hostname.md`, `docs/NAVIGATION.md`. No schema change, no new table, no new agent loop.

### ADR-047 - The Anatomy isometric engine is a declarative response block over a vendored kit; the model authors a SCENE, never code (2026)
**Decision:** A new `anatomy` block type renders an interactive isometric mechanism figure inline in chat. It rides the ONE existing structured-response pipeline (ADR-043): a zod schema in `src/lib/response/blocks/anatomy.ts`, one entry in the renderer registry (`src/components/response/response-view.tsx`), the `finalize`/repair/degrade chokepoint, the Markdown projection, and the `response_block_schema`/`response_validate` read-back (so the model can query the exact field names before emitting). The model authors a **bounded, declarative scene** — parts (a `<kind, x, y, z, w, d, radius, height, corner, tone, lit, label>` slab/cylinder), wires (`from`/`to`/`via`/`tone`), a legend, and optional deterministic **stages** (`{ label, note, readout, lit[] }`) — and the geometry is compiled purely by `src/lib/anatomy/scene.ts` with the MIT-licensed Anatomy-derived kit vendored into `src/lib/anatomy/` (`NOTICE` + `LICENSE` record the MIT origin and the verified upstream commit `e9488296a44211c42701ff71705b7901470ec78f`; blob hashes prove `iso-kit.ts`/`live.tsx` byte-identical). The model never supplies SVG, HTML or code. The renderer is `'use client'` and renders the figure inside a frosted-glass card with a full-page overlay; the **stage is the sole source of live readout text**, animation is discrete (no rAF loop), autoplay is off under `prefers-reduced-motion`, and the interval is cleared on unmount — so the figure cannot invent a value and cannot leak a timer.
**Reason:** The Interactive Response Engine already covers flows, charts, diagrams, timelines, simulators and learning maps — but not a *physical-mechanism* illustration, which is what "explain how a CPU/cache/refinery works" needs. Anatomy (wheresryan22/anatomy, MIT) is exactly that toolkit; its bespoke figures are standalone HTML built by node scripts, which do not fit a chat artifact. Reusing only its geometry primitives and React drawing components, then driving them from a declared scene, keeps the Eclipse posture intact: a registered component registry, no model-authored code path (SEC-001), deterministic model-as-source-of-truth, and a text fallback (the Markdown projection) for surfaces that cannot render it.
**Impact:** New `src/lib/anatomy/` (vendored `iso-kit.ts`, `draw.tsx`, `live.tsx` + `NOTICE`; Eclipse-authored `scene.ts`), `src/lib/response/blocks/anatomy.ts` (schema extracted from `contract.ts`, whose size ratchet it helps satisfy), `src/components/response/anatomy-block.tsx`; `contract.ts` (import + union entry), `normalize.ts` (`anatomy` type aliases + status-tone normalization), `projection.ts` + `export.ts` (projection/PDF cases), `response-view.tsx` (registry). Tests `src/lib/anatomy/scene.test.ts`, `src/components/response/anatomy-render.test.tsx`, the matrix (`contract-matrix.test.tsx`). Docs `docs/interactive-response-engine.md`, `docs/NAVIGATION.md`. **Not wired:** WebGL/3D and the bespoke lathe/tube parts stay out of runtime (kept in `docs/figures/eclipse-refinery/` as a reference); only slab/cylinder parts render. No new dependency, no new agent loop, no wire-contract change.

### ADR-048 - True 3D mechanism models are a separate `model3d` block compiled to world meshes and rendered in an orbitable WebGL viewport (2026)
**Decision:** `/anatomy` now authors a `model3d` block, not the isometric `anatomy` scene. It keeps ADR-047's trust posture — the model supplies a bounded declarative SCENE (JSON), never meshes, shaders, SVG or code — but the scene is a real 3D graph: `parts[]` carry a `position`/`rotation`/`scale` transform, an optional `parent` (assemblies), material/tone/lit/label, and geometry as either a primitive (`box`, `cylinder`, `cone`, `sphere`, `torus`, `lathe`, `extrude`, `tube`, `gear`) or a **named mechanism recipe** with numeric `params` (`motor`, `gearbox`, `pump`, `valve`, `gauge`, `tank`, `bearing`, `gear`, `pulley`, `belt-drive`, `frame`, `bracket`, `fan`, `heat-exchanger`, `pcb`, `lever`, `spring`). The pure compiler (`src/lib/model3d/compile.ts` + `mesh.ts` + `recipes.ts`) expands recipes, composes parent transforms with a cycle guard, bakes every triangle to world space, grounds the scene, fits the camera and reports anything it cannot place as a diagnostic — it is total and server-safe. The renderer is split: a DOM half (`model3d-block.tsx`) owns the header, numbered key, legend, readout, stage controls and `.glb` download; a `next/dynamic(ssr:false)` WebGL half (`model3d-canvas.tsx`) uses the already-vendored `ogl` dependency for orbit/zoom/pan, lighting, a fading grid and projected hotspots. `glb.ts` is a dependency-free glTF 2.0 binary writer. The legacy `anatomy` block stays registered and renders unchanged for old payloads.
**Reason:** The isometric SVG compiler can only draw what it has primitives for and paints in producer order with no depth buffer; it cannot rotate, compose assemblies or show a machine from another angle, so `/anatomy` collapsed to "a base slab plus cylinders". The two real levers are (1) genuine 3D with depth/lighting and (2) a reusable parametric mechanism kit, so the model composes recognisable assemblies instead of guessing primitives. `ogl` is already a dependency (~15 KB, used by the Aurora background), so no new package and no bundle cost on the initial chat load (the viewport is lazy). Keeping the legacy block avoids a breaking wire-contract change; keeping compilation pure preserves determinism, tests and the no-model-code security posture (SEC-001).
**Impact:** New `src/lib/model3d/` (`names.ts`, `math.ts`, `mesh.ts`, `recipes.ts`, `compile.ts`, `glb.ts`), `src/lib/response/blocks/model3d.ts`, `src/components/response/model3d-block.tsx` + `model3d-canvas.tsx`; `contract.ts`, `normalize.ts` (type aliases), `projection.ts` + `export.ts` (text/PDF cases), `response-view.tsx` (registry), `command-agent.ts` (/anatomy directive), the `system.anatomy-figure` skill. Tests `src/lib/model3d/{mesh,compile}.test.ts`, `src/components/response/model3d-render.test.tsx`, `contract-matrix.test.tsx`. Docs `docs/interactive-response-engine.md`, `docs/NAVIGATION.md`, `docs/harness/slash-commands.md`, `.opencode/skills/black-eclipse-anatomy/SKILL.md`. No new dependency, no new agent loop, no schema change to existing blocks.

### ADR-049 - Eclipse Canvas is an editable document/code/visual workspace over the ONE Artifact Center; AI edits are typed operations re-applied under a revision guard (2026)
**Decision:** Eclipse Canvas is a persistent, editable workspace for documents, code and visual reports, built **on top of the existing Artifact Center with no second store**. A canvas document IS an ordinary `Artifact` whose bytes are canonical UTF-8 text and whose revisions are the artifact's immutable versions; the canvas kind (`document`/`code`/`visual`) and optional `language` live in the artifact's `metadata` under the key `canvas` (`python/harness/canvas/`, `CANVAS_METADATA_KEY`). `CanvasService` is the ONE canvas writer over `ArtifactService` (create/read/list/save/restore); each save is guarded by the `baseVersion` the editor loaded and serialized by a lock, so a stale save returns `409 CANVAS_CONFLICT` carrying `currentVersion` instead of overwriting. AI assistance is one orchestrator (`CanvasEditOrchestrator`): it builds a strict-JSON prompt, calls the model through the ONE model runtime (`vision.providers.call_model`, task `canvas_edit`), validates the reply with `model_runtime/structured.py`, and **re-validates every operation against the loaded revision**; modes are `ask`/`edit`/`review`/`build`, Ask mode never proposes edits, and a missing model configuration is the honest `CANVAS_MODEL_UNAVAILABLE`. Edits are a **closed vocabulary of typed operations** (`ops.py`: `append`, `replace_text_range`, `replace_section`, `insert_after_section`, `replace_document`, `apply_code_patch`) applied deterministically to current bytes — never a free-form patch, eval, template, or model-authored code. An AI-assisted save sends `operations`, and the server re-applies them to the current revision, so **client content is not trusted**; unknown, ambiguous or stale operations are rejected. Routes are `python/harness/routers/canvas.py` (`/canvas/artifacts*`, operator-gated, owner from `x-harness-user`), proxied by `src/app/api/canvas/[[...path]]/route.ts`, which also resolves the operator's model from the settings vault (`resolveRunModelSettings`) and injects `modelSettings` for `.../edits` (the runtime cannot read the vault). The UI (`src/components/screens/canvas/`) is a three-region workspace: a rail (list/search/create/import), a center editor (Tiptap document with Markdown as canonical content, Monaco code, JSON editor + live `ResponseView` preview for Eclipse response-envelope visuals) with debounced autosave, Ctrl/Cmd+S, version history + restore, export and a `409` conflict UI, and an AI panel with a propose → diff → accept flow.
**Reason:** The Artifact Center stores immutable, versioned, content-addressed files but has no editor, so "make this artifact editable" would otherwise require a second document store with its own versions, owner scope and model path — three new authorities over the same bytes. Reusing the catalog keeps one storage root, one version table and one owner scope. A typed-operation contract (rather than accepting model-authored content or patches) preserves the no-model-code security posture (SEC-001) and makes every AI change reviewable and reproducible; a revision guard is required because keep-alive editors and AI proposals can both hold stale bytes, and silently overwriting would be a data-loss path.
**Impact:** New `python/harness/canvas/` (`model.py`, `ops.py`, `service.py`, `edits.py`), route group `python/harness/routers/canvas.py` registered in `main.py`, tests `python/tests/test_canvas.py` + `test_canvas_api.py`; front door `src/lib/harness/canvas-client.ts` + `src/app/api/canvas/[[...path]]/route.ts`; UI `src/components/screens/canvas/` (`canvas-screen.tsx` + rail/center/workspace/ai-panel/editors); new `canvas` AppTab (`src/lib/app-tabs.ts`, sidebar `config.ts` icon `SquarePen` in the agentic group, `nav.canvas`, deep-link `APP_TABS`, mobile bottom nav, keep-alive block in `stock-chat.tsx`); integrations `/canvas [document|code|report|visual]` (`command-registry` + `native.canvas` returning an open link), Artifact Center "Open in Canvas" for artifacts whose metadata carries `canvas`, and response-actions "Canvas"; new editing dependencies (`@tiptap/*`, lowlight, `monaco-editor`, `@monaco-editor/react`); tests `src/app/api/canvas/route.test.ts` + `src/components/screens/canvas/canvas-screen.test.tsx`. Storage stays SQLite (`agent-memory.db` + artifact files under `.black-eclipse/artifacts`), never Postgres. Docs `docs/canvas.md`, `docs/harness/artifact-center.md`, `docs/NAVIGATION.md`, `docs/ARCHITECTURE.md`. No new agent loop, no new store, no new database table.

**Amendment (inline, 2026-10-17):** Canvas is **not a standalone surface**. The `canvas` AppTab, the sidebar entry/`nav.canvas`, the deep-link tab, the mobile bottom-nav item and the `/canvas` command were **removed**; there is no external Canvas page. Canvas renders as an **inline pane inside the ChatBot tab** (`stock-chat.tsx`): when `useCommandStore.canvasInlineOpen`/`pendingCanvasArtifactId` is set, the chat column and a right-hand `CanvasPane` (`CanvasScreen inline`) split the view, with a close button; the pane carries the editor and (collapsed by default) the same AI panel. It opens from a response's **Canvas** action (save → inline) and from the Artifact Center's **Edit in Canvas** (`dispatch({tab:'chat', params:{artifactId}})`), never a separate route. The backend, artifact model, typed-operation contract and revision guard are unchanged.

### ADR-050 - The chat's charts are drawn by the PYTHON engine (a composable SCENE of marks, so ANY chart can be drawn); the rest of the app stays on ECharts (2026)
**Decision:** The **chat's** chart blocks are drawn by the Python chart engine and by NOTHING else. A `chart` block in an answer is turned into a **SPEC** and drawn by the runtime with `matplotlib` (Agg, **transparent** background, the app's own palette), and the picture is recorded in the Artifact Center and fetched back as an artifact. The chat has no ECharts path: `chart-option.ts` (the old ECharts option builder) is deleted, and a chart in an answer is never a live canvas. The SPEC is DATA - a validated, `extra="forbid"` contract (`python/harness/charts/spec.py`: `kind`, title/footnote, `categories`, `series[]` with `null` for a missing reading, `points[]`, `matrix` + label axes, `candles[]`, `labels`/`values`, reference lines, highlight spans, a volume pane, value formats) - never a callback, a URL or a path, so a malformed chart is a refusal that names the missing part rather than a broken picture. **Any chart is expressible**: a chart may also arrive as a **SCENE** - `panels` of `marks` - which is compositional rather than a fixed list of kinds, so a violin, a box plot, a Gantt, a funnel, a ridgeline, an error-bar chart or a dual-axis chart is a composition of the primitives (`line`, `area`, `bar`, `scatter`, `band`, `rule`, `label`, `box`, `violin`, `histogram`, `errorbar`, `heatmap`, `pie`, `donut`, `candlestick`, `waterfall`, `ridge`, `stem`) rather than a new kind that must be invented for it. The model reaches the scene through the block schema (the bridge read-back now describes the mark vocabulary) and through the agent prompt. The engine is reached through the `python_chart` agent tool, the operator-gated route `POST /charts/render` (proxied by `src/app/api/harness/charts/route.ts`), and the browser client `src/lib/harness/charts.ts`. A figure is always `computed` and never evidence - `isEvidence: false` is part of the contract, not a caption (ADR-H123); the renderer draws what the spec says and nothing else; a missing `matplotlib` is a named `CHART_RENDER_UNAVAILABLE` refusal, never an empty picture; and the picture's identity is the digest of its spec. **The rest of the application is NOT part of this**: the journal, portfolio, dashboard, backtest, economics, fundamentals and workspace widgets, the price chart, the Investor Eye map and the relationship graphs stay on ECharts, because they draw from live data at interactive rates (a price chart that redraws on every tick, a map the user hovers) and a server-drawn picture cannot serve that. A lossy *mapping* is never silent: a block type the engine cannot draw exactly (`treemap`/`sunburst`/`funnel`/`gauge`) is drawn as the closest honest shape and the note appears under the picture.
**Reason:** ECharts could only draw the charts it already had: a fixed vocabulary of kinds, with a hover tooltip and a crosshair as the price of the interactivity. A chat answer is the one surface where the chart ITSELF is the payload - the model authored it, it travels in the response contract, and it is exported, saved and re-shared - so it is also the one surface where "the reader asked for a chart and got something else" is most visible. Making the chat's charts composable (a scene of marks) removes the ceiling: the system can now draw what the user asked for instead of the closest thing in a dropdown. The rest of the app keeps ECharts because there the interactivity IS the product, and a round trip per frame would be a downgrade, not an upgrade.
**Impact:** New `python/harness/charts/` (`spec.py`, `theme.py`, `render.py`, `service.py`, `tools.py`, `scene.py`, `scene_render.py`), route group `python/harness/routers/charts.py` registered in `main.py`, `register_chart_tools` wired into `HarnessService` and the chat agent's allowlist; front door `src/lib/harness/charts.ts` + `src/app/api/harness/charts/route.ts`; the chat's chart path `src/lib/response/chart-spec.ts` (block -> spec), `src/components/response/response-chart.tsx`, `src/lib/response/chart-registry.ts`, the export helpers in `src/lib/response/export.ts`, the `chart` block's scene fields in `src/lib/response/contract.ts`, and the schema bridge's deeper read-back (`src/lib/harness/response-bridge.ts`) so the model can see the mark vocabulary. Deleted: the chat's ECharts builder (`src/lib/response/chart-option.ts` + test) and the engine preference store. `matplotlib`/`numpy`/`pandas` are already installed in the runtime and the analysis sandbox - **no new dependency**, no new agent loop, no new store. Tests `python/tests/test_charts.py`, `python/tests/test_chart_scene.py`, `src/lib/harness/charts.test.ts`, `src/app/api/harness/charts/route.test.ts`, `src/lib/response/chart-spec.test.ts`, `src/lib/response/repair.test.ts`. Docs `docs/chart-engine.md`, `docs/interactive-response-engine.md`, `docs/NAVIGATION.md`, `docs/ARCHITECTURE.md`. ECharts stays a dependency of the app - for everything except the chat.

**Amendment (inline, 2026-10-21):** the chat's charts are **live figures, not
pictures**. The engine authors each chart as a **Plotly figure** in Python
(`python/harness/charts/plotly_figure.py`) and the browser renders it as
**vector SVG** with scroll-zoom, pan, a hover readout, a legend and a PNG/zoom
toolbar. The chart is still authored in Python from the same validated spec, so
the data, the palette and the honesty rules are unchanged; only the rendering
moved from "a PNG of a chart" to "a chart". The library is served by the app
from **its own route** (`/api/harness/charts/plotly`), out of the Python package
that ships it - never vendored into git, never fetched from a CDN, so the app
keeps working offline - and the figure has **no background**, so the chart sits
on the app's frosted surface without hiding the frost. The matplotlib picture is
still produced from the same spec and stays the export (PNG/PDF) and the honest
fallback when the library cannot load. The mark vocabulary grew the family
charts (`treemap`, `sunburst`, `funnel`, `sankey`), so a treemap of holdings is
a real treemap rather than a bar standing in for one. `plotly` is a new runtime
dependency, justified by exactly this: interactivity and vector output are
things a server-drawn PNG cannot provide.

**Amendment (inline, 2026-10-22, superseding the ECharts rework):** the chat's chart renderer stays **Plotly**. A chart block is mapped to the shared ChartSpec (src/lib/response/chart-spec.ts), a named dataSource is resolved through the app's own /api/price-history route (src/lib/response/chart-data.ts), and the Python engine returns both the live Plotly figure and the transparent matplotlib PNG. src/components/charts/plotly-chart.tsx paints the PNG immediately, then loads plotly.js only when the chart nears the viewport and on an idle frame, caches the library for the session, and cross-fades to the live chart; response-chart.tsx caches the rendered figure per spec. The chart registry maps the block id to the artifact id for PNG/PDF export. Drawability is validated with the engine's own list (src/lib/response/chart-capabilities.ts), candle shapes ([date,o,h,l,c], object rows) are normalised instead of dropped, and gauge/treemap/funnel/sunburst are drawn natively. The rest of the app keeps ECharts.
### ADR-051 - The scraper lane: a generated program collects what the vault does not have, through the host's fetch and a per-domain consent
**Decision:** Eclipse can now **build a scraper on demand**. When the answer is not in JDdS, the agent writes a Python program and runs it in the SAME analysis sandbox; the program fetches pages ONLY through a new host-mediated binding (`eclipse_tools.web_page`), and leaves its records in `result = {"rows": [...]}`. The lane is `python/harness/scraper/` (`tools.py` = the `scraper_run` tool, `program.py` = the answer contract + the wrapper that makes "the program never answered" a failure rather than an empty dataset, `authorizers.py` = the per-domain consent, `store.py` = where the rows and the program are kept), registered in `service.py` and advertised on the research agent (`WEB_RESEARCH_TOOLS`). The program cannot open a socket (`socket`/`ssl` stay denied by the runtime guard), no HTTP client is importable, and a URL literal is refused by the AST gate unless the HOST turned the web binding on for that run (`check_imports(..., allow_url_literals=True)`); the fetch itself is the existing `web_collect` capability � the same SSRF-hardened transport, size cap and timeout `web_scrape` uses � so the lane adds **no second transport, no second HTML parser and no second network path**. The consent is **per DOMAIN**: the approval kind `scraper_domain` is keyed by the host, the operator approves a site once, and the consent is then remembered as a revocable standing grant; a different site asks again. The collected rows are stored through ONE new front-door route (`POST /api/scraper/datasets` ? `CustomDatasetRecord`, provenance `SCRAPER`, each row keyed by its own content so a re-run updates in place), and the program is saved under `.black-eclipse/scrapers/` so it can be run again by id. Everything collected is **COLLECTED page data** � never a verified source, never a canonical market row.
**Reason:** "Eclipse cannot get data the vault does not have" was a design choice, not a physics limit, and it made the product fail the most ordinary user request ("pull these daily holdings and keep them"). The three gates that blocked it were all policy: the analysis runner hard-coded `network=False`, the runtime guard denied `socket`, and the AST gate refused URL literals. Flipping those in place would have handed the ANALYSIS sandbox a raw network � a second transport, a second parser, an SSRF and exfiltration surface, and a break of the "the sandbox computes, it does not fetch" invariant that the research engine depends on. The alternative was a HOST-MEDIATED fetch: the program asks, the host fetches through the transport that already carries every other web call, and the program keeps the logic (pagination, joining two pages, reshaping records) that no single declarative call can express. The consent is per DOMAIN rather than per program because the thing the operator is really approving is "let Eclipse read this site", and asking again for every scraper against one site would train the user to click through approvals without reading them. Storing the rows as a labelled dataset (not as canonical market rows) keeps the vault's provenance rules intact: a scraped row says where it came from and never claims to be verified.
**Impact:** New `python/harness/scraper/` (`__init__.py`, `program.py`, `authorizers.py`, `store.py`, `tools.py`), tests `python/tests/test_scraper_lane.py`; `python/harness/code/sandbox_tools.py` gains the `web_page` binding (the analysis sandbox's own set is UNCHANGED � `DATA_TOOL_NAMES` vs `WEB_TOOL_NAMES`), `python/harness/code/execution.py` gains `tool_allowlist`/`allow_url_literals`/`tool_call_limit`, `python/harness/code/policy.py` gains `SCRAPER_TOOLS`, `python/harness/code/authorizers.py` composes the scraper authorizer, `python/harness/routers/code.py` lists `scraper_domain` as reviewable, `python/harness/bridge.py` gains `post_json` (the ONE write seam to the front door � `/api/harness/data` stays read-only), `python/harness/agents.py` advertises `scraper_run`; front door `src/app/api/scraper/datasets/route.ts` (+ test) and `src/lib/harness/data-bridge-collect.ts` gains `links` extraction (the pagination handle a generated scraper needs). No new dependency, no new agent loop, no new database table (the rows land in `CustomDatasetRecord`), no change to the read bridge's contract. Docs `docs/harness/scraper-lane.md`, `docs/harness/web-research-engine.md`, `docs/NAVIGATION.md`.

### ADR-052 - Model-authored interactive apps are an `interactive` block in an opaque-origin sandbox; the host injects the market series (2026)
**Decision:** Eclipse can now render a **model-authored mini-app** inline in chat (a price knob, a VWAP explorer, a doji figure). It rides the ONE structured-response pipeline (ADR-043): a new `interactive` schema in `src/lib/response/blocks/interactive.ts`, one entry in the renderer registry, the finalize/repair/degrade chokepoint, the Markdown projection, the PDF export and the `response_block_schema` read-back. The block carries `html` (body markup, inline SVG), optional `css`/`js`, a height, optional static `data`, and an optional `dataset` (`{ kind:'series', symbol, market?, range?, interval? }`). `src/lib/interactive/document.ts` composes ONE document whose `<head>` carries an immutable meta CSP (`default-src 'none'; script-src/style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; frame-src/worker-src/object-src 'none'; base-uri/form-action 'none'`) and a tiny runtime (`Eclipse.data()/onData()/resize()`); a model-supplied full document is unwrapped to its body so the model can never replace the policy. The frame is `<iframe sandbox="allow-scripts">` with **no `allow-same-origin`**, i.e. an opaque origin with no app DOM, cookies, storage or network; messages are accepted only from that `contentWindow` with a per-render nonce. The model **never types prices**: `src/lib/interactive/dataset.ts` validates the request and calls the existing read-only `/api/price-history` route from the HOST, normalizes up to 5000 bars with `source`/`asOf`/`freshness`/`stale`, and posts the snapshot into the frame; an empty or failed series is an explicit `unavailable` envelope, never a fabricated line. `provenance:"illustrative"` labels example-only numbers.
**Reason:** Users need to BUILD and VISUALIZE things in the conversation, and the declarative blocks (including `simulation`/`gauge`/`model3d`) cannot express arbitrary interaction. Injecting model markup into the app DOM would be an XSS surface; the accepted posture is an opaque-origin sandbox with an immutable CSP and zero network, which closes host access and exfiltration for a frame that has nothing sensitive behind it. The two-origin/process-isolation shape (Claude Artifacts, MCP Apps) is stronger but needs a second deployable origin and token plumbing; it is deliberately deferred until interactive content is exposed to untrusted third parties. Keeping the data on the host is the fiduciary guard: a widget cannot show invented numbers under a "live" badge, and the real series still carries its observation/provenance envelope.
**Impact:** New `src/lib/response/blocks/interactive.ts`, `src/lib/interactive/{document,dataset}.ts` (+ tests), `src/components/response/{interactive-block,interactive-frame}.tsx`; `contract.ts` union/type, `normalize.ts` aliases, `projection.ts`, `export.ts`, `canvas/content.ts` (`REPORT_BLOCKS`), `prompts.py` guidance, `contract-matrix.test.tsx` sample. A settled ```` ```html ````/```` ```svg ````/```` ```eclipse-app ```` fence renders through the same sandbox (models often answer with code instead of the block), and a full document's own `<style>`/`<script>`/body are preserved while any model-supplied CSP `<meta>` is stripped. No new dependency, no new origin, no new backend route, no change to the read bridge. Docs `docs/interactive-response-engine.md`.

## ADR-053 - The crypto/on-chain/alt-data pack extends the ONE market layer with capabilities, not a second pipeline (2026)
**Decision:** Crypto/on-chain/alt-data coverage is added as **capabilities on the existing Market Data Layer** (ADR-023/024), not a parallel data path. `providers.py` gains `tvl`, `onchain`, `liquidations` and `long_short`; `config.DEFAULT_PRIORITY`/cache TTLs route them to the matching adapter. Five runnable TS adapters ship: `providers/defillama.ts` (keyless protocol/chain TVL), `providers/etherscan.ts` (keyed EVM account), `providers/solscan.ts` (keyed Solana account), `providers/coinglass.ts` (keyed liquidations/long-short) and `providers/hyperliquid.ts` (keyless perps funding/OI). `MarketDataRequest` carries optional `address`/`chain` fields, and `connectors/security.ts` gains ONE hardened `safePost` transport so Hyperliquid's POST-only `info` endpoint rides the same https-only/DNS-pinned/size-capped path as `safeGet` (request-body cap, no redirects, no signing/trading). Canonical records `TvlSnapshot`, `OnchainMetric`, `LiquidationStats` and `LongShortRatio` are normalized by the same gateway/quality/provenance path, and four agent tools (`get_tvl`, `get_onchain_metric`, `get_liquidations`, `get_long_short_ratio`) join the market tool set. The pack is reachable from the UI through ONE read-only route (`GET /api/crypto/alt-data`, same adapters), a Market Overview crypto panel and two catalog widgets.
**Reason:** The crypto pack touches quotes, supply, funding, TVL and on-chain facts, so a second pipeline would duplicate routing, quality, provenance, caching and secret handling — the exact drift the market layer exists to prevent. Adding capabilities keeps one router, one bridge transport, one secret store and one evidence path. Every shipped endpoint was verified against the provider's official docs first (DefiLlama, Etherscan V2, Solscan V2, CoinGlass V4, Hyperliquid `info`); keyed providers fail honestly with `AUTHENTICATION_FAILED` when no credential is present, and an absent/unknown symbol is `SYMBOL_NOT_FOUND` — never a zero or a guess. Hyperliquid deliberately serves only funding/open interest: the instrument master has no spot/perp stamp, so a perp quote/book offered as a spot fallback would be a silent category error.
**Impact:** `python/harness/market/{providers,config,models,gateway,tools,instruments}.py`, `src/lib/harness/market/{types,bridge}.ts`, `src/lib/harness/market/providers/{defillama,etherscan,solscan,coinglass,hyperliquid}.ts`, `src/lib/connectors/security.ts` (`safePost`), `src/lib/provider-keys.ts` (Etherscan/CoinGlass/Solscan entries), `src/app/api/crypto/alt-data/route.ts`, `src/components/screens/crypto-alt-data.tsx`, `src/lib/dashboard/{crypto-widgets.tsx,registry.tsx,layout.ts}`, `docs/harness/market-data.md`, `docs/harness/tool-registry.json`. No Prisma model change, no new agent loop, no second data pipeline, no new HTTP client (`safePost` is the same outbound layer), no change to `/api/harness/data`'s read-only contract.

## ADR-055 - Money movement is gated by a deterministic guardrail layer above the ONE approval store (2026)
**Decision:** Every money-shaped tool call (computer/browser/MCP/shell/native) is normalized into a `MoneyIntent` and evaluated by `python/harness/finance_guardrails/` for scam/coercion signals, impaired spending, crisis, aggregate spend/velocity limits and counterparty novelty. The layer is model-free and can only add restriction. An `allow` falls through to the existing capability authority; a `confirm`/`escalate` routes the exact intent (digest-bound, single-use) through the existing `McpApproval` store with `kind="money_guardrail"` and a cooling-off + typed-confirmation challenge; a `deny` is terminal and explained. Panic pause and crisis mode are human switches exposed only on operator-gated routes.
**Why:** The Security Floor, gateway and approval store decide *authority*, not *financial harm*. A single approved click can still be a coerced transfer, an impaired purchase or the tenth order in an hour. Keeping the new checks deterministic and augment-only means no prompt, skill, plugin, MCP server or model claim can loosen them, and every decision is explainable after the fact. Reusing the single-use approval store preserves one card and one audit trail.
**Consequences:** `finance_guardrails/` (`models`, `intent`, `scam`, `impaired`, `crisis`, `limits`, `store`, `service`, `tools`, `bootstrap`) plus `routers/finance_guardrails.py`; enforcement in `computer/tools.py`, `browser/tools.py` and `mcp/approvals.py`; two new tables on `agent-memory.db`; one Settings surface and one front-door proxy; a challenge on the existing approval card. No new database, no new approval mechanism, no model dependency, no broker integration. Known boundary: text signals are heuristics, so limits/novelty/confirm remain the backstop for unrecognized scams.

## ADR-056 - Live exits are always available; live entries stay operator-confirmed in chat (2026)
**Decision:** A live broker connection may close or modify its own positions without an arm, an approval or a mandate; the service only requires the account mode to match the connection (live/real, demo/demo). Entry stays gated: preview → single-use digest-bound approval → typed LIVE phrase → submit, and the chat surfaces that approval above the composer (`TradeApprovalBanner`) so the operator confirms from the conversation. Unattended mandates remain demo-only.
**Reason:** An exit that depends on an unexpired arm can trap an operator in a live position — a worse failure than the risk it prevents. Closing or modifying only reduces exposure, and the broker's own account mode still stops a demo connection from touching a real position. Entries are irreversible in the other direction, so the arm + typed-phrase contract stays; the chat card reuses the same server route, digest and approval store, not a parallel authority.
**Impact:** `python/harness/trade/positions.py` (mode-aware, arm-free exit; a live close-all is scoped to the connection magic), `trade/service.py` + `trade/authorizer.py` (`approval_view` exposes the exact order to the card), `trade/tools.py` and `trading_intent.py` (live turns preview, not submit), `src/components/chat/trade-approval-banner.tsx` mounted in `chat-input.tsx`, `src/components/screens/trading-settings.tsx` (edit/switch mode), tests `test_broker_orders.py`/`test_trading_intent.py`. No new approval store, no live mandate, no change to the arm or kill-switch contract.

## ADR-054 - Office edits are surgical and proven; financial models are deterministic and export live formulas (2026)
**Decision:** Editing an existing Office file is a **package operation**, not a library round-trip. `python/harness/documents/edit/` opens the OOXML ZIP, mutates only the parts named by a closed, validated `EditPlan`, copies every other part from its original bytes (same entry order and ZIP metadata), and then `verify_ooxml` refuses to publish unless every untouched part is byte-identical, every relationship resolves, every XML part parses and the expected edit is present. The result is a NEW artifact version; the original is never edited in place. PDF has no package-part contract, so the `pypdf` rewrite path reports `preservationMode: "rewrite"` and a missing backend is `PDF_LIBRARY_MISSING`. Financial modeling is a separate deterministic engine (`python/harness/modeling/`) whose `ModelSpec` is validated input-by-input, whose three-statement/DCF/LBO/comps results carry named checks, and whose Excel export writes the same arithmetic as live A1 formulas plus a `Values` sheet for non-calculating viewers. The engine composes `finance_facts.valuation`/`metrics` instead of duplicating DCF or ratio math.
**Reason:** `python-docx`/`openpyxl`/`python-pptx` are excellent creators but lossy editors: opening and re-saving an existing workbook drops charts, pivots, slicers, custom XML and VBA, which is exactly what a user notices when they ask Eclipse to "change one cell". A declarative, per-part edit plan plus a preservation report turns "we preserved it" from a claim into a gate. Modeling needs the same posture: the LLM must not invent a number or write XML, and a model that does not balance must say FAIL rather than look convincing in a deck.
**Impact:** New `python/harness/documents/edit/` (`package.py`, `plan.py`, `docx_ops.py`, `xlsx_ops.py`, `pptx_ops.py`, `pdf_ops.py`, `verify.py`, `apply.py`, `tools.py`) and `python/harness/modeling/` (`contracts.py`, `three_statement.py`, `dcf.py`, `lbo.py`, `comps.py`, `engine.py`, `xlsx_export.py`, `tools.py`); tools `document_edit`, `financial_model_build`, `financial_model_audit` registered on the ONE registry and advertised on the Eclipse agent (`DOCUMENT_EDIT_TOOLS`/`MODELING_TOOLS` in `agents.py`); `pypdf` added to `python/requirements.txt`; tests `python/tests/test_documents_edit.py` + `test_modeling.py`; docs `docs/harness/office-edit.md`, `docs/harness/financial-modeling.md`, `docs/NAVIGATION.md`. No new agent loop, no second model provider, no new store, no Office runtime, no macro execution, no new write surface outside the confined artifact store.

## ADR-057 - Inbound webhooks authenticate at the front door and are armed against a pinned version, never widened by the request (2026)
**Decision:** A workflow webhook is a zero-config `trigger.webhook` node plus a `WorkflowWebhook` row that owns the endpoint, an AES-256-GCM-encrypted secret (connector-secret store, shown once), method filter, response mode and mode. The public `POST|GET /api/webhooks/[slug]` route is exempt from the session gate and authenticates itself with an HMAC-SHA256 over `<timestamp>.<raw body>` (300 s replay window) or a token header; it is rate-limited and optionally idempotent via `X-Eclipse-Delivery`. Safe mode (default) starts the normal read-only TEST run. Armed mode is an explicit operator action that pins the workflow version, content hash and confirmed network grant; write/trading graphs are refused, and the digest/version is re-checked on every delivery (409 on mismatch). `trigger.webhook` is `sideEffects: none` with `webhook.receive`; it never counts as outbound network. Outbound HTTP rides ONE `graph_runtime/http_client.py` that re-checks SSRF on every redirect hop, caps hops, refuses HTTPS->HTTP, and redacts logs.
**Reason:** A webhook caller is untrusted and carries no authority. Letting the request choose a workflow, version or capability would turn an endpoint into a remote write surface; keeping the default as a test run makes an unauthenticated-by-policy trigger harmless, while arming gives an operator the integrations they actually need without a second approval mechanism. Pinning the version makes the grant auditable and immune to a later graph edit silently widening it.
**Impact:** New `python/harness/graph_runtime/http_client.py`; `trigger.webhook`/`integration.http@1.1.0`/`integration.webhook_send` in both registries; `run-server` -> Python `triggerContext` plumbing; `WorkflowWebhook` + `WorkflowWebhookReceipt` Prisma models (`db push`); `src/app/api/webhooks/[slug]` + workflow webhook CRUD/rotate routes; `webhook-store/crypto/client/summary` + inspector Webhook tab; middleware public prefix. No second workflow runtime, no caller-supplied grants, no network in a test run, no plaintext secret in JDDS or logs.

## ADR-058 - User personas layer an append-only, draft/active overlay on the authoritative prompt (2026)
**Decision:** Settings → Persona stores user-authored personas on the ONE `agent-memory.db` connection as a current row plus append-only versions. A persona is bounded at 2,000 characters, sanitized (role markers and code fences neutralized), and appended after the resolved `eclipse-agent` prompt as a labelled, lower-precedence "User persona" block. It never replaces the prompt and never touches the tool allowlist, permissions, data sources or the safety/evidence/financial rules — the allowlist is resolved before the persona is read. Edits stage a draft; activation promotes the draft into a new immutable version and deactivates the previous persona (one active globally); reset leaves no overlay; restore loads a version into the draft; archive keeps history. Every write carries a `revision` token and a stale save is refused with 409; version history is bounded to the last 50 rows (the active version is always kept). The overlay applies only to runs using the default chat agent (`eclipse-agent` — ordinary chat, bots, and any graph node that selects it), not to Graph Studio custom agent presets. The run records `persona id@version` alongside `surface`, so a later edit never rewrites what an earlier run saw.
**Reason:** Users asked to customize the assistant's behavior. A full replacement editor would let a one-line preference silently delete the finance/tool/response contract, and an editable shared base prompt would drift per user with no history. An append-only overlay is the lowest-risk pattern the industry has settled on (ChatGPT custom instructions, Claude Code preset + append), keeps one prompt authority, and makes the change reviewable and reversible through drafts and versions.
**Impact:** New `python/harness/personas.py` (store, sanitization, renderer, one-active rule, prompt helper) + `python/harness/routers/personas.py` (`/personas/*`, operator-gated) registered in `main.py`; boot wiring `install_persona_store` in `service.py`; `graph.py` appends the block for `eclipse-agent` and records provenance on `surface.persona`; front door `src/lib/personas/client.ts` + `src/app/api/personas/[[...path]]/route.ts`; UI Settings → Persona (`src/components/settings/personas-settings.tsx` + the `persona` section in `settings.tsx`); tests `python/tests/test_personas.py`. Tables are created on first use (no `jdds.db` migration, no second database). No second prompt authority, no new tool or permission surface.

## ADR-059 - Broker trades enter the journal as a rebuildable projection, never as a second source of truth (2026)
**Decision:** The journal connects to a broker through a `JournalBrokerLink` that names an existing Python `BrokerConnection`; no credential or second broker reader is introduced. A sync refreshes the harness-reconciled mirror (`BrokerFill`/`BrokerPositionSnapshot`), groups deals by `positionTicket`, and upserts one `Trade` per position keyed by `externalId = broker:<connectionId>:<positionTicket>`. Broker-owned fields (symbol, direction, prices, quantity, times, status, costs, P&L) are authoritative and rewritten on each sync; user-owned fields (notes, emotion, confidence, grade, checklist, screenshots, a self-assigned strategy/setup, added tags) are never overwritten. `pnlBasis = "broker"` stores the broker's own net arithmetic instead of recomputing it from price × quantity, which is wrong for FX/CFD contract sizes. Deleting a broker trade tombstones the externalId (`ignoredAt`) so the next sync cannot resurrect it. Attribution rules (magic / comment / symbol) fill missing strategy/setup/tags at projection time.
**Reason:** The broker is already authoritative and durable in the Python harness. Rebuilding a fetch loop or a second fill store in the journal would create two truths that drift, and recomputing CFD P&L from prices would silently corrupt every performance number. A one-way, idempotent projection keeps the journal rebuildable from the broker and leaves the user's analysis layer intact. Explicit field ownership is what makes "sync" safe to run repeatedly without destroying journal work.
**Impact:** New Prisma models `JournalBrokerLink`, `BrokerAttributionRule`, plus `externalId`/`brokerConnectionId`/`brokerPositionTicket`/`pnlBasis`/`ignoredAt` on `Trade` and broker identity fields on `TradingAccount` (`db push`); `src/lib/journal/broker/{project,source,links,sync}.ts`; `/api/journal/broker/{links,rules,preview,sync}`; Journal → Settings → Broker Connection UI. The sync accepts an explicit read-only `days` backfill window (bounded 1–3650; fills page 1,000 at a time by `(dealtAt, id)`, 20 pages max), and an opt-in per-link auto-sync timer (`JOURNAL_BROKER_SYNC_SECONDS`, default 300 s, failure backoff 5 min) reuses the same path. Every projected deal is also stored as an immutable `TradeFill` (unique by `(tradeId, dealTicket)`) so partials and scale-outs are visible; analytics gain `byAccount`/`byMagic`, and the journal agent gains `get_broker_status`/`get_broker_performance` which never mix broker and manual trades. Broker-owned execution fields are refused on PATCH (409); a link is verified against the runtime before creation; per-connection sync coalescing and unique-collision tolerance keep dev+prod dual-server safe; every journal trade lookup filters `ignoredAt` tombstones. `JournalProfile` is scoped per authenticated user (deterministic `jp_<userId>`, guest profile when auth is off) and fails closed when auth is required without a live session. Broker routes and the `/api/trading` proxy require an operator role when auth is active; adapter base URLs are validated and 429/5xx responses retried with bounded backoff. MT5, a read-only MT4 HTTP bridge, Alpaca and IBKR are the built-in adapters; Alpaca/IBKR use FIFO-normalized round-trips, so their journal P&L is marked computed. No new database, no new agent loop, no credential in `jdds.db`, no order-send path.

## ADR-060 - Graph files and images are local asset references; image readings are observations, never evidence (2026)
**Decision:** Graph Studio stores an uploaded file as a content-addressed reference in a bounded `HarnessGraphAsset` table on the ONE `agent-memory.db` (25 MB/file, 200 files, 512 MB/owner, optional TTL, owner-isolated reads) and an uploaded image in the existing vision `VisionAssetStore`. The Workflow IR carries only the `gas_`/`img_` ref plus metadata — never the bytes. `data.file.attach|parse|export` are thin adapters over that store; `image.attach|analyze|ocr|read_chart|compare|batch` are thin adapters over the EXISTING vision engine (`vision/orchestrate`). Image nodes are READ-ONLY: no chart rendering and no image generation. Every vision output is labelled `kind: "observation"`, `verified: false` with provider/model/warnings; `image.read_chart` may set `verified: true` only when the engine's own market-data check confirmed the reading. `agent.task` runs ONE bounded turn on the existing agent plane; instructions append to the preset prompt, a tool list may only narrow, and memory writes are refused in a test run. Memory nodes cover context/list/update/forget (archive, never hard-delete) with explicit `general|workflow|run` scopes.
**Reason:** A graph needs to consume a file a person picked without embedding megabytes in a document or inventing a second ingestion path. Reusing the runtime's own store keeps one owner check, one size cap and one lifetime. For images, the product requirement is reading reference candles/setups through the vision API, not drawing or generating them; the existing vision engine already owns routing, model credentials, OCR, chart analysis, safety scanning and provenance. Treating a model reading as evidence would be a fabricated fact, so the observation label is part of the contract.
**Impact:** `python/harness/graph_runtime/{assets,file_nodes,image_nodes}.py`, `routers/graph_assets.py` + `main.py`; `registry-files.ts`/`registry-media.ts` + `registry.ts`, `config-fields.ts`, `node-examples.ts`, `types.ts` (`file`/`image` port types, `media` category), `registry.manifest.json`; inspector file/image pickers and the `/api/graph-studio/{assets,images}` proxies; `image.read`/`model.vision`/`asset.*` test-run grants; memory service `list_memories`/`update_memory`/`archive_memory`. Vision assets are owner-scoped (the local operator keeps the legacy content id; any other owner gets a scoped id), the front door forwards the verified identity token, graph runs adopt the verified owner, and uploads are per-owner rate-limited. No render engine, no image-generation node, no second memory store, no second agent loop.

## ADR-061 - A company's books are a separate double-entry ledger; a posting balances exactly or does not happen (2026)
**Decision:** General accounting runs on a NEW `python/harness/ledger/` package and is NOT an extension of the Portfolio Accounting Engine. The portfolio engine keeps a securities book (positions, cost basis, valuation, performance); the ledger keeps a company book (accounts, entries, periods, statements). They share the Decimal money policy (`ledger/money.py` re-exports `portfolio/money.py` — reuse, not a fork) and the ONE `agent-memory.db` connection, and nothing else. A journal entry is written once and is never updated: `Σ base debit == Σ base credit` is checked exactly before persist, corrections are reversing entries linked to the original, and there is no update path in the store. Chart-of-accounts templates are DATA (`standard`/`sole_trader`/`minimal`); an account's TYPE alone decides its normal balance, `is_cash` marks cash rather than a name heuristic, and an account's `currency` is a restriction (blank = accepts any currency, translated through the line's `fx_rate`). Periods are monthly with a configurable fiscal-year start; `OPEN` accepts postings, `CLOSED` refuses until reopened, `LOCKED` refuses forever, and a close is refused (`PERIOD_NOT_READY`) while a blocker stands — starting with a non-zero suspense balance. Every statement (trial balance, balance sheet, income statement, cash flow) is recomputed from postings on each read and returns a `checks` list; the accounting equation is a check result, not a claim. Cash movement the engine cannot classify is reported in an explicit `unclassified` bucket with `complete: false`, never assigned a guessed category. Mutations declare effect `ledger.write` and are admitted only by a single-use, digest-bound operator approval (`ledger_write`) — admission alone is not authorization. A posting is a write to the book of record, not a payment, so it needs one approval and no money guardrail.
**Reason:** "Any accounting task" requires a defensible book of record, not a model doing arithmetic in prose. The failure modes that matter are silent ones — a rounded balance, an edited history, a cached total, an invented FX rate, a guessed cash-flow category — so each is a named refusal instead of a best effort. Keeping the securities book and the company book separate prevents one engine from acquiring two contradictory definitions of "balance". Reusing the portfolio store/Decimal/persistence/boot/authorizer patterns keeps the runtime at one database, one money type and one approval path while adding a genuinely new capability.
**Impact:** `python/harness/ledger/` (`money`, `models`, `errors`, `accounts`, `journal`, `periods`, `statements`, `store`, `service`, `tools`, `authorizers`, `bootstrap`); routes `python/harness/routers/ledger.py` (`/ledger/*`, operator-gated) included by `main.py`; boot wiring `service.py` (`install_ledger_layer`, `_ledger_bundle`), `ToolContext.ledger_service` + the `ledger` seam in `graph.py`, `fabric/builtin_workers.py`, `automation/dispatcher.py`; authorizer composition `code/authorizers.py`; agent allowlist `agents.py` (`LEDGER_TOOLS`); regenerated `docs/harness/tool-registry.json`; tests `python/tests/test_ledger_{core,statements,tools,authorizer,api}.py` (+ the background-parity expectation); doc `docs/ledger.md`. Phase 1 deliberately excludes bank import/reconciliation, subledgers (AR/AP/invoicing), jurisdiction tax, fixed assets/inventory/payroll, multi-entity consolidation, multi-currency revaluation and a statement exporter; they are listed as the next phases in `docs/ledger.md`.

## ADR-062 - Statement imports use a declared mapping; unmatched rows post to suspense and a reconciliation never repairs (2026)
**Decision:** Bank statements enter the ledger through `python/harness/ledger/importers.py` + `imports.py`. The format (`csv`/`ofx`/`qif`), the CSV column mapping and the date pattern are all DECLARED by the caller — never guessed from bytes or a filename — because `05/01/2026` is two different dates depending on locale; an unparseable date is `IMPORT_ROW_INVALID` naming its row. Every row carries a stable identity (the bank's OFX `FITID`, else a hash over date+amount+description+occurrence); the posted entry stores it as `external_id='stmt:<account>:<row-id>'` under `source='import'`, so re-import is a per-row duplicate check rather than a second posting. A row becomes `Dr <categorised> / Cr <bank>` when money left and the mirror when it arrived — the sign picks the bank side, so a liability target needs no special case. Categorisation (`rules.py`) is deterministic and versioned: conditions AND, needles OR, amount bounds compare magnitude, order is `(priority, name, version)`, first match wins, and an inapplicable rule is skipped AND reported. An unmatched row is POSTED to `9999 Suspense` (so the trial balance balances and the money is on the books) and blocks the close until categorised — it is never assigned to a guessed account. A foreign-currency row without an `fx_rate` is `REQUIRES_FX` and is not posted. Reconciliation (`reconcile.py`) is a report: it never mutates the journal, uses MATCHED/MISMATCH/MISSING/UNKNOWN (an ambiguous amount/date pair is UNKNOWN, never "the first one"), and only says `RECONCILED` when a supplied `statement_balance` agrees within tolerance AND no row is mismatched, missing or unknown; without a balance the run is `INCOMPLETE`.
**Reason:** The realistic failure of an import is not a crash — it is a shifted column, a silently re-imported file, a guessed category, or a "reconciled" tick that only compared the rows it could see. Each of those is made a named, visible state instead. Posting unmatched rows to suspense rather than dropping them keeps the books complete while making the problem impossible to miss at close. Making reconciliation read-only keeps the separation between "the bank says" and "the books say": the operator decides the correcting entry.
**Impact:** `python/harness/ledger/{importers,rules,imports,reconcile}.py`; `store.py` gains `LedgerRule` (append-only versions), `LedgerImportBatch`, `LedgerReconciliation` + items, and `postings()` now carries each entry's `externalId`/`memo`; `service.py` gains rule CRUD and import/reconcile delegates; `tools.py` adds `ledger_import_preview|commit`, `ledger_list_imports`, `ledger_rule_create|list|deactivate`, `ledger_reconcile_statement`, `ledger_list_reconciliations`, `ledger_get_reconciliation` (mutations stay behind `ledger_write`); `routers/ledger.py` adds `/import/*`, `/rules*`, `/reconcile`, `/reconciliations*`; `docs/harness/tool-registry.json` regenerated; tests `python/tests/test_ledger_{imports,reconcile}.py` plus the tools/API round-trips; doc `docs/ledger.md`. Deliberately not built: PDF-statement parsing, live bank feeds, automatic transfer pairing, splitting one row across accounts, and any repair posting from a reconciliation.

## ADR-063 - Period-end adjustments are derived and idempotent; a close is a checklist with severities (2026)
**Decision:** Phase 3 adds `python/harness/ledger/{schedules,revaluation,close}.py`. A depreciation/amortisation schedule derives EVERY period's entry up front from terms (cost, salvage, `periods`, method, two accounts, start date): `STRAIGHT_LINE` gives the last period the rounding remainder so `Σ entries == cost − salvage` exactly, `DECLINING_BALANCE` applies `factor/periods` to the opening book and never goes below salvage; a period that rounds to zero posts nothing but is still consumed. Posting is idempotent (`sched:<scheduleId>:<sequence>`), runs every period due on/before `through`, and stops at the first entry that cannot post (a closed period) rather than leaving a gap. Accruals are a one-off posting pair (`Dr debit / Cr credit`, optional automatic reversal) with an optional `reference` for idempotency. FX revaluation only considers accounts RESTRICTED to a foreign currency, requires a supplied rate per currency (`RATE_REQUIRED`, never invented), and computes `delta = target_base − current_base` on the debit-positive net so a positive delta is always a gain — no per-account-type branch. The adjustment is written as a `base_only` journal line: it changes the carrying amount without touching the account's transaction-currency balance, and the foreign balance is computed from transaction-currency lines only, so a re-run cannot compound. Idempotency is per account+date+rate, and the operation is convergent (re-running at the same rate is a no-op). The period close is `LedgerCloseRun`: a deterministic checklist whose failures carry a severity — `SUSPENSE_BALANCE` and `TRIAL_BALANCE_BROKEN` are blockers that refuse `close_period` (`PERIOD_NOT_READY` + the findings + the run id), while `NO_ENTRIES`, `IMPORT_INCOMPLETE`, `NEGATIVE_CASH`, `RECONCILIATION_NOT_CURRENT`, `SCHEDULE_DUE`, `FX_REVALUATION_MISSING` and `POSSIBLE_DUPLICATE` are warnings the operator may accept. The run is persisted with its findings and checks.
**Reason:** Month-end is where a ledger either earns trust or loses it, and the failure modes are quiet: a rounded depreciation schedule that leaves a residual, a revaluation that compounds each time it is run, a "close" that only means nothing crashed. Deriving the whole schedule up front makes the arithmetic inspectable before anything is posted; giving the last period the remainder makes the sum exact; excluding base-only lines from the foreign balance makes revaluation convergent. Severity-separated findings let the operator close a period with a known overdraft while refusing one with uncategorised money in suspense — a single "ok/not ok" would force a false choice. Persisting the run turns "we closed March" into evidence.
**Impact:** `python/harness/ledger/{schedules,revaluation,close}.py`; `JournalLine`/`LineSpec` gain `baseOnly` (with an additive `ALTER TABLE` migration for a DB created before the column) and the journal honours it as the one sanctioned base-currency line on a foreign-currency account; `store.py` gains `LedgerSchedule` and `LedgerCloseRun`; `service.py` gains `create_schedule`/`post_schedule`/`post_accrual`/`preview_revaluation`/`post_revaluation`/`close_report`/`list_close_runs`/`get_close_run`, and `close_period` now delegates to the checklist; `statements.close_check` was removed (one close checker, not two); `tools.py` adds `ledger_schedule_create|list|post`, `ledger_accrue`, `ledger_revalue_preview|post`, `ledger_close_preview`, `ledger_list_close_runs`, `ledger_get_close_run` (mutations behind `ledger_write`); `routers/ledger.py` adds `/schedules*`, `/accrue`, `/revalue*`, `/close/preview`, `/close-runs*`; `docs/harness/tool-registry.json` regenerated; tests `python/tests/test_ledger_{schedules,close}.py`; doc `docs/ledger.md`. Deliberately not built: recurring accrual schedules, inventory costing, payroll, open-item FX revaluation, year-end closing entries, and any UI surface.

## ADR-064 - An invoice is a validated batch of journal lines; tax rates are configured data and aging is derived (2026)
**Decision:** The AR/AP subledger (`python/harness/ledger/{parties,tax,documents,subledger_store}.py`) stores parties, tax codes, documents and payment allocations, and does NOT introduce a second journal. `create_document` computes its own lines — `INVOICE: Dr <AR control> total / Cr <revenue> net / Cr <tax account> tax`, `BILL` mirrored — and hands them to the ONE `LedgerService.post`, so an unbalanced document cannot exist. Control accounts are DISCOVERED from the chart by subtype (`RECEIVABLE` / `PAYABLE`) with a per-document override; a missing one is `CONTROL_ACCOUNT_MISSING`, never an invented account. `tax.py` ships NO rate table: a tax code is operator/partner-configured data naming jurisdiction, a fractional rate, the GL account and whether prices are inclusive; an unconfigured code is `TAX_CODE_NOT_FOUND` and an unconfigured jurisdiction is `UNSUPPORTED_TAX_JURISDICTION`. Tax is computed per line and quantized half-even, so the document total is the sum of its lines and inclusive pricing recovers `net + tax == amount` exactly. Documents are immutable once posted: a void reverses the journal entry and flips the status, and a document with payments cannot be voided (`DOCUMENT_HAS_PAYMENTS`). Payments post `Dr bank / Cr AR` or `Dr AP / Cr bank` and record an allocation; over-payment is refused and a repeated `reference` posts once. Aging is DERIVED at the report date from documents minus allocations-as-of-that-date (so an invoice paid in February is still open on 31 January), and its total is CHECKED against the control account's closing balance — a difference is reported, not hidden. Documents are base-currency only; a foreign-currency document is refused rather than silently translated.
**Reason:** Invoice software drifts from the ledger in exactly two ways: a document total that disagrees with its lines, and a "balance" cached on the party that no longer matches the control account. Posting the computed lines through the one journal path removes the first; deriving aging from allocations at the report date and checking it against the control account removes the second. Bundling tax rates would have been the single most tempting fabrication in the whole subsystem — a rate is a fact about a jurisdiction on a date — so the engine refuses instead, and the same is true of a missing control account. Requiring the right party kind and refusing over-payment keeps the subledger truthful at the edges where a UI usually lets bad data in.
**Impact:** `python/harness/ledger/{parties,tax,documents,subledger_store,document_tools}.py`; `store.py`/`service.py` wire `SubledgerStore` onto the SAME connection and add the party/tax-code/document/payment/aging facade; `tools.py` merges `DOCUMENT_TOOLS` into `LEDGER_TOOLS` so the agent allowlist and the `ledger_write` authorizer see one list; `routers/ledger.py` adds `/parties`, `/tax-codes`, `/documents*`, `/aging`; tests `python/tests/test_ledger_documents.py` plus tool/API round-trips; doc `docs/ledger.md`. Deliberately not built: credit notes, dunning, recurring invoices, purchase orders, credit limits, multi-currency documents, open-item FX revaluation, tax returns and any UI.

## ADR-065 - Consolidation is proportionate and eliminates only the matched intercompany amount; exports are text, and SAF-T is refused (2026)
**Decision:** Phase 5 adds `python/harness/ledger/{group_store,consolidation,exports,group_tools}.py`. A `LedgerGroup` is a consolidation SCOPE — it owns no journal and never posts to a member's books; `ledger_consolidate` recomputes a report from member postings. Members contribute PROPORTIONATELY (ownership % × every line); there is no non-controlling-interest line and the report states `ownershipMethod: "PROPORTIONATE"`. Accounts are matched BY CODE and must agree on TYPE — a code that is an asset in one member and a liability in another is `ACCOUNT_TYPE_CONFLICT` (blocker), never averaged. A member whose currency differs from the group's needs a supplied rate per currency (`RATE_REQUIRED`, member excluded, never an invented rate); one rate per currency applies to every account (no average-rate P&L split). Intercompany pairs are identified by account SUBTYPE (`INTERCOMPANY_RECEIVABLE`/`INTERCOMPANY_PAYABLE`, `INTERCOMPANY_EXPENSE`/`INTERCOMPANY_REVENUE`) and only the MATCHED amount is eliminated; the remainder stays in the consolidated books and is reported as `INTERCOMPANY_OUT_OF_BALANCE` (warning) — the consolidation never force-balances. The consolidated figures are fed to the SAME statement engine as a single ledger, so the accounting equation is checked once. Exports (`exports.py`) return `{format, filename, contentType, content}` for `journal_csv`, `trial_balance_csv`, `json` and an XBRL 2.1 INSTANCE document; the XBRL file declares a context/unit and primary-statement facts under a LOCAL namespace and states in its own comment that it is not taxonomy-conformant (no IFRS/GAAP linkbase). SAF-T is REFUSED with `UNSUPPORTED_EXPORT` and a reason: it is a jurisdiction-specific audit file with a mandated schema version and validation rules, and a look-alike would be indistinguishable from a compliant one.
**Reason:** Consolidation is where a group's books are most easily made to look right: average two charts together, plug the intercompany difference, and the numbers balance for the wrong reason. Matching by code AND type, eliminating only what actually matches, and reporting the remainder keeps every consolidated figure traceable to a member posting. Proportionate consolidation is a real method with a precise meaning — stating it beats implying full consolidation with an NCI line that was never computed. On exports, the same rule applies to formats as to numbers: an XBRL instance without a taxonomy and a home-made SAF-T are fabricated compliance, so the engine emits what it can defend, labels it, and refuses the rest.
**Impact:** `python/harness/ledger/{group_store,consolidation,exports,group_tools}.py`; `service.py` wires `GroupStore`/`ConsolidationService` onto the SAME connection; `tools.py` merges `GROUP_TOOLS` into `LEDGER_TOOLS`; `accounts.py` adds four `INTERCOMPANY_*` accounts to the standard chart; `store.py` gains `first_entry_date` (a statement export anchors at the first posting, not the current month) and raises the entry-list cap to 5000 with explicit `truncated` reporting; `routers/ledger.py` adds `/groups*`, `/groups/{id}/consolidate` and `/export` (declared before `/ledger/{ledger_id}` so the literal path wins); tests `python/tests/test_ledger_{consolidation,exports}.py` plus tool/API round-trips; doc `docs/ledger.md`. Deliberately not built: NCI/full consolidation, chart-of-accounts mapping across members, intercompany transaction matching, average-rate translation, SAF-T, taxonomy-mapped XBRL, an Excel/PDF statement pack, and any UI.

## ADR-066 - The ledger's delivery surface is a thin proxy, one tab over the same engine, and a closed bookkeeper preset (2026)
**Decision:** Phase 6 adds `src/app/api/ledger/[[...path]]/route.ts` (a pass-through to the runtime's `/ledger/*`, guarded for mutations exactly like the portfolio proxy, carrying NO accounting logic), `src/lib/ledger/client.ts` (typed browser client with one honest error type so a refusal reaches the UI as a message), the `Accounting` tab (`src/components/screens/accounting/{accounting-screen,use-accounting}.ts`), and a `bookkeeper` agent preset in `python/harness/agents.py`. The tab is one page over the SAME engine: ledger create/select, four headline figures, trial balance, P&L breakdown, journal, a close-check panel that renders blockers and warnings by severity, and AR aging with the control-account reconciliation badge. The preset's allowlist is EXACTLY the `ledger_*` family — no market data, no code, no browser, no MCP (`allowMcpTools` false) — and its prompt encodes the ledger's discipline (never invent a number; a posting balances or does not happen; corrections are reversals; unmatched rows go to suspense; a tax rate is configured data; a period is not "reconciled" unless the tool says so). Mutations still pass the `ledger_write` authorizer, so the allowlist is the surface and the authorizer is the gate. The tab is APPENDED to `DEFAULT_SIDEBAR_SECTIONS` rather than inserted, so the F1–F12 shortcut contract keeps pointing at the same sections; `app-tabs.ts`, the sidebar config, the shell's lazy import, search/deep-link and the i18n navigation slice are updated together, and the exhaustive `Record<AppTab, true>` in `deep-link.ts` makes a missed tab a compile error.
**Reason:** The engine is only usable if a person can see it without asking an agent to describe it, and the risk of a UI is that it becomes a second implementation — a screen that computes its own totals will eventually disagree with the book. A pure proxy plus one page over the same endpoints removes that class of bug. The preset matters for the same reason at the other end: an "accounting assistant" that can also browse the web, run code and reach MCP servers is not a bookkeeper, and a prompt that permits invented numbers undoes every invariant the engine enforces. Keeping the shortcut slots stable is a small courtesy that avoids silently rebinding a user's F-keys when a tab is added.
**Impact:** `src/app/api/ledger/[[...path]]/route.ts`, `src/lib/ledger/client.ts`, `src/components/screens/accounting/{accounting-screen.tsx,use-accounting.ts}`; tab wiring `src/lib/app-tabs.ts`, `src/lib/sidebar/config.ts`, `src/components/screens/stock-chat.tsx`, `src/lib/search/{registry,deep-link}.ts`, `src/lib/i18n/{nav.ts,dictionaries/navigation.ts}`; `python/harness/agents.py` (`BOOKKEEPER`, `BOOKKEEPER_SYSTEM_PROMPT`, `AGENT_PRESETS`); tests `python/tests/test_bookkeeper_preset.py`, `src/lib/sidebar/config.test.ts` (shortcut pin +1 section), `src/lib/app-tabs.test.ts`; doc `docs/ledger.md`. Deliberately not built: ledger graph nodes, close monitors in the automation catalogue, and UI for invoices/bills and consolidation.
**Amendment (2026-11-03):** the `Accounting` tab and the `/api/ledger` front-door client described above were REMOVED at the owner's direction — the ledger is a chat capability. The `bookkeeper` preset and the monitors remain. See ADR-068.

## ADR-068 - The ledger is chat-only: the dedicated tab and browser client were removed (2026)
**Decision:** The ledger has NO dedicated UI. The `Accounting` tab (`src/components/screens/accounting/`), the browser client (`src/lib/ledger/client.ts`) and the Next front-door proxy (`src/app/api/ledger/[[...path]]/route.ts`) were deleted, and every trace of the tab was reverted: the `AppTab` union, the sidebar section, the shell's lazy import and keep-alive block, search and deep-link registration, the i18n navigation key, the SDK `APP_NAV_SECTIONS` mirror, and the shortcut/size pins those changes had moved. Accounting is done in conversation with the `bookkeeper` agent (or any agent holding the `ledger_*` tools). The Python `/ledger/*` routes stay as an operator/API surface, like every other subsystem's; they are not a user-facing UI.
**Reason:** The product's interaction model is chat. A parallel screen is not a neutral addition: it doubles the surface that must stay in sync with the engine, it invites a second place where a total could be computed, and it teaches users that the book is something you browse rather than something you ask about. The owner's goal was for the user to do their accounting in chat, and the tools, the preset and the monitors already deliver that. Removing the tab returns the codebase to one entry point per capability.
**Impact:** Deleted `src/components/screens/accounting/`, `src/lib/ledger/client.ts`, `src/app/api/ledger/[[...path]]/route.ts`; reverted `src/lib/app-tabs.ts`, `src/lib/sidebar/config.ts`, `src/components/screens/stock-chat.tsx`, `src/lib/search/{registry,deep-link}.ts`, `src/lib/i18n/{nav.ts,dictionaries/navigation.ts}`, `src/lib/sdk/app-shell-types.ts`, `src/lib/sidebar/config.test.ts`, `src/lib/size-budget.test.ts`; `docs/ledger.md` now says "chat only" and lists the absence as deliberate. No Python change: the engine, tools, preset, monitors and `/ledger/*` routes are untouched. Deliberately not built: any tab, page, dashboard widget or browser client for accounting.

## ADR-067 - Ledger monitors are condition automations over a ledger fact provider; a missing fact never fires (2026)
**Decision:** The three ledger monitors (`ledger_suspense_watch`, `ledger_close_blocked`, `ledger_receivables_watch`) are ordinary `condition` automations in the CLOSED monitor catalogue (`advisory/monitors.py`), created switched off like every other monitor. Their variables are `ledger.<ledgerId>.<field>`, resolved by a NEW `LedgerFactProvider` in `automation/facts.py`, registered in `default_fact_provider` behind the existing `CompositeFactProvider`. The provider reads the SAME `LedgerService` the tools and the UI use: `suspense` (absolute suspense balance), `blockers`/`warnings`/`ready` (the current fiscal period's close evaluation), `open_ar`/`open_ap` (subledger aging totals) and `entries`. Field names that are not supported, a ledger that does not exist, and any exception inside a derivation all yield NO fact — so the condition evaluates UNAVAILABLE and does not fire. The monitor objectives instruct the agent to diagnose and PROPOSE, never post a category it cannot justify and never claim a period is closed.
**Reason:** Unattended accounting fails in two specific ways: money sits in suspense and nobody notices, or a period quietly cannot close. Those are exactly the facts the engine already computes, so exposing them as condition variables turns a monitoring system the runtime already has into ledger eyes without inventing a second scheduler or a second fact source. The UNAVAILABLE rule is the same discipline the rest of the ledger follows: a monitor that fires on a guessed balance is worse than a monitor that stays quiet.
**Impact:** `python/harness/automation/facts.py` (`LedgerFactProvider`, `default_fact_provider`), `python/harness/advisory/monitors.py` (three specs; `MONITORS` grows from 4 to 7), tests `python/tests/test_ledger_monitors.py`; no new tool, route, table or scheduler. Docs `docs/ledger.md` (Surfaces), ADR-067.

## ADR-068 - Lime is the shipped default theme; the theme stays a default, never a lock (2026)
**Decision:** `DEFAULT_ECLIPSE_THEME` (`src/lib/theme/tokens.ts`) is Lime, and that is what every installation renders without any customization. The spec: brand `#b4ff42`, accent `#c9ff77`, bullish `#b4ff42`, bearish `#ff3e3e`, pure-black page, `#0f0f0f` surface, comfortable density, `radius` 0.6rem, `dramatic` shadow, frosted glass 18px blur / 0.55 opacity / `#121214` tint, and the translucent Crystal sidebar (`#1a1a1e`, 0.55, 34px). The same palette is mirrored where the default is consumed before the store can run: the static `.dark` block in `src/app/globals.css` (first paint ships Lime, so there is no green flash while the theme hydrates), `DEFAULT_AURORA_PALETTE` in `src/lib/aurora-sentiment.ts`, the launcher splash mirror in `scripts/lib/app-theme.js`, and the Eclipse chart presets. Glass recipes in `globals.css` read `var(--glass-shadow, 0 24px 70px rgba(0, 0, 0, 0.60))` so an uncustomized session (no inline tokens at all) already matches the themed dramatic shadow instead of drifting from it. A saved theme always wins: the app-wide JDDS `SystemConfig['eclipse_theme']` row and saved presets are adopted as-is, and light-scheme presets deliberately keep `shadow: 'soft'`.
**Reason:** The brand is lime-on-black, and the default has to *be* the brand - a new user's first frame should not look like an older Eclipse. Making it the default rather than a forced value keeps the promise that customization is real: existing users who already saved a theme keep it, and anyone can switch away in Settings. Keeping the static CSS block, the splash mirror and the Aurora palette in sync avoids a two-speed identity where the app is Lime but the launcher or the glow still reads green.
**Impact:** `src/lib/theme/tokens.ts` (default spec + preset labels), `src/app/globals.css` (`.dark` brand/bullish/sidebar tokens + var-driven glass shadows), `src/lib/aurora-sentiment.ts`, `scripts/lib/app-theme.js`, `src/components/screens/chart-settings.tsx`, `src/components/screens/sdk/sdk-screen.tsx`, `src/lib/chart/drawings/manager.ts`, `src/components/chart/drawing-overlay.tsx`, `src/components/screens/market-overview{,-global-markets}.tsx`, `skills/IMPRINT.md`. Tests: `src/lib/theme/tokens.test.ts`, `src/lib/aurora-sentiment.test.ts`, `src/lib/desktop-launcher.test.ts`. Docs: `docs/CHANGELOG.md`, `docs/OVERVIEW.md`, `docs/GLOSSARY.md`.

## ADR-069 - Trade learning may tighten a pre-authorized playbook envelope and propose, never widen or trade (2026)
**Decision:** A trade-learning loop (`python/harness/trade_learning/`, graph nodes `trade.*`) reviews journal trades with DETERMINISTIC detectors, records findings as observations, counts recurrence in the ONE `LearningRecurrence` ledger keyed by trade id, and improves exactly one artefact: an append-only playbook envelope (`TradePlaybook` on `agent-memory.db`). The operator declares the envelope's **bounds**; the loop may move a knob only toward tighter risk and only one step per cycle, inside those bounds. `trade.commit` is an operator-run write and the runtime re-validates every transition, refusing `NOT_A_TIGHTENING` and `OUT_OF_BOUNDS` regardless of what a graph requested. Acceptance is out-of-sample: variants may be mutated from any measured state (including losers), but a candidate is accepted only when it beats the champion on a chronological holdout with a minimum sample. A model never decides that a mistake occurred. The loop has no broker code path, never places/modifies/cancels an order, and never edits a source file; a code or skill change is only an INERT candidate for the existing RSL/self-modification pipelines. Journal data is read through the authenticated, read-only `journal_trades` bridge capability and is never written from the runtime.
**Reason:** The request was a node that learns from a trade's mistakes and fixes itself. The failure modes of that request are a fabricated lesson (calling any loss a mistake), overfitting a filter to the trades that produced it, and an autonomous "fix" that increases risk. Deterministic detection plus one recurrence ledger removes the first; a holdout window and a minimum sample remove the second; tighten-only bounds plus an operator commit remove the third. Restricting the loop to a numeric envelope keeps it useful (risk really does change) without granting it the two authorities it must never have: money and code.
**Impact:** `python/harness/trade_learning/`, `python/harness/graph_runtime/trade_learning_nodes.py`, `src/lib/graph-studio/registry-learning.ts`, `src/lib/harness/data-bridge-journal.ts` + `journal_trades` capability, `TradeFinding`/`TradePlaybook` tables on `agent-memory.db`; tests `python/tests/test_trade_learning.py`, `python/tests/test_graph_trade_learning_nodes.py`; docs `docs/graph-studio.md`, `docs/graph-studio-trade-learning-nodes-plan.md`. Deliberately not built: auto-applied code changes, order modification, and a model-authored verdict.

## ADR-070 - Typed decision models are a separate provider layer, never a chat provider, and a low-confidence answer is never promoted (2026)
**Decision:** Typed decision models (System One-shaped, e.g. TypeSafe's Jev) are a first-class harness capability in `python/harness/decisions/`, deliberately SEPARATE from the chat Model Runtime. A request is a `state` plus a closed set of `choice` / `score` / `noul` questions; a response is typed answers with probabilities and, where the question type supports it, confidence. Providers implement one `DecisionProvider.ask` contract: the deterministic rule provider ships now; the TypeSafe adapter and an LLM-structured fallback are additive. A fail-closed `DecisionPolicy` maps certainty to exactly `answered` / `review` / `unavailable`; a partial answer set, a type mismatch, a provider error or no provider is always `unavailable`, never a partial or guessed result. Every request is an immutable `DecisionRecord` on the ONE `agent-memory.db` (question-template hash, state hash, answers, policy, usage, latency, tool), with outcomes appended later for calibration. Decision tools are read-only and an answer is never an authorization or a verified financial fact. Privacy is enforced in the registry before a provider is called, so `ECLIPSE_PRIVACY=local-models` can never reach a remote decision provider.
**Reason:** Jev is post-trained to return typed, calibrated decisions and has no messages, tools, streaming or context window. Forcing it through `PROVIDER_CATALOG` / `build_chat_model` / `ModelRouter` would break the `ChatModel` contract and corrupt routing and cost accounting. The value Eclipse needs is not "another chatbot": it is a bounded, reproducible judgment behind a gate (citation support, advisory admissibility, classification, routing) that code can branch on. That only works if confidence is honest, so the layer must fail closed rather than let a 0.55 answer read as a decision, and it must record the template and state that produced every answer so calibration can be measured later. Keeping it out of the chat picker also keeps zero-config honest: with no key, typed decisions are simply unavailable.
**Impact:** `python/harness/decisions/` (`contracts.py`, `policy.py`, `registry.py`, `providers/{base,rules}.py`, `service.py`, `store.py`, `tools.py`, `bootstrap.py`); wiring `python/harness/service.py` (`_run_kwargs()["decision"]`), `python/harness/graph.py` (`execute_run(decision=…)` → `ToolContext.decision_service`), `python/harness/agents.py` (`*DECISION_TOOLS`); tests `python/tests/test_decisions.py`; docs `docs/decision-models.md`, `docs/ARCHITECTURE.md`, `docs/NAVIGATION.md`. Deliberately not built yet: the TypeSafe/LLM adapters, Graph Studio decision nodes, and adoption inside existing gates (each is a separate, flagged change).

## ADR-071 - v0.3.0 ships on the measured live floor (0.8520); 0.9000 remains the target (2026)
**Decision:** The v0.3.0 production release accepts the measured live-eligible `task_success` of **0.8520** (167/196) as its release floor. The live gate's **0.9000** target and `DEFAULT_THRESHOLDS` stay unchanged; no threshold is edited. The release note and `docs/harness/benchmarks.md` publish the measured number, its run id/dataset checksum/commit and the 0.9 target beside it, and state that the live gate still reports FAIL. The exception is version-scoped: the next release must either reach 0.9 or record a new ADR. It covers a **single-operator self-hosted** deployment only — not a hosted multi-tenant service.
**Reason:** 0.8520 is the honest live-eligible score. The remaining 29 trials are real agent failures (the raw run including eval-only scripted tasks scores 0.6901; financial accuracy is 0.8706, hallucination 0.0). Producing a fresh ≥0.9 run needs a provider key that is not available in this environment. The alternatives are worse: raising the offline 1.0, editing the threshold, or omitting the number would all be a fabricated pass. Shipping with the failure published and the target explicit is the honest option, and the safety/financial gates that do not depend on the live lane were green on the release commit.
**Impact:** `docs/releases/v0.3.0.md`, `docs/harness/benchmarks.md` (annotation only; measured rows unchanged), `docs/PRODUCTION-READINESS.md` (C8 satisfied by this ADR), `docs/DECISIONS.md`. No code or threshold change. Follow-up: close the 29 live-eligible failures or re-justify at the next release.

## ADR-072 - First-run setup is a real gate, source toggles are enforced, and factory reset runs at boot (2026)
**Decision:** A genuine new install gets a boot-style setup gate (`OnboardingGate` in the root layout, backed by `GET/POST /api/setup`) that separates **Included** open data from **Connect your own** providers and **Advanced/community** sources. New installs default to open data plus SEC/FRED/BEA/BLS/World Bank/GDELT/USGS; TradingView, Yahoo, Binance, CoinGecko Demo, DuckDuckGo and scrapers are off unless the user acknowledges them. Choices persist in `onboarding_v1` and `data_source_prefs_v1`, and `src/lib/data-source-preferences.ts` is consulted by the scraper chain, crypto connector and economics route so a disabled source is actually skipped. Existing installs are migrated to `completed:true` on the first read and keep the legacy fetch chain until they re-run setup. Three reset scopes are explicit - re-run onboarding, reset setup/keys, factory reset - and a factory reset is **scheduled** by the web process and consumed by `scripts/doctor.js` on the next boot, before the DB guard opens `jdds.db`, because a live Next process cannot safely delete open SQLite files on Windows.
**Reason:** The old app had no first-run path, so a new user met a screen full of market data without knowing which sources were included, which required their own account, or which terms did not fit a paid product. The setup gate answers those questions in user language and makes the legal boundary visible. Toggles that only changed a checkbox would be a lie, so the fetch layer had to read the same preference store. The reset split protects the two very different intents ("ask me again" vs "erase me") and the scheduled factory reset avoids a half-deleted database caused by deleting files the running process still holds.
**Impact:** `src/lib/setup/` (catalog, capabilities, bundles, defaults, state, existing-install, check, capability-status), `src/components/setup/` (gate, shell, four steps, capability row/badges), `src/app/api/setup/{route,check,reset}`, `src/lib/data-source-preferences.ts`, enforcement in `src/lib/scraper/fetchers/index.ts`, `src/lib/connectors/providers/crypto.ts`, `src/app/api/economics/route.ts`, Settings General "Run setup again" + Data & Storage danger zone, `scripts/lib/reset.js`, `scripts/reset.js`, `bin/black.js` (`black onboarding`, `black reset`), `scripts/doctor.js` boot hook, `package.json` reset aliases; tests `src/lib/setup/setup.test.ts`; docs `docs/onboarding-plan.md`, `docs/CHANGELOG.md`.

## ADR-073 - Every AI answer carries a point-of-use risk notice, and setup records which version the user accepted (2026)
**Decision:** The product ships one canonical disclosure (`src/lib/legal/disclosure.ts`, version `2026-11-09`) in three placements: a non-dismissible label + short sentence under every settled assistant answer (expandable to the full notice), a permanent one-liner in the shared composer and the floating dock, and a required checkbox on the first-run wizard's first step. `POST /api/setup` rejects a missing or stale `riskAck.version`; a successful finish writes `{version, acceptedAt, mode}` to `risk_ack_v1`. Canvas reports opt in through `ResponseView.showNotice`. The text is specific (AI can be wrong, not advice, not the broker, verify before acting, you own the decision, losses are possible) and matches the product's existing provenance labels. This is evidence and risk allocation, not immunity: the enforceable limitation of liability belongs in Terms reviewed by counsel.
**Reason:** A vague "use at your own risk" footer is weak evidence and is defeated by a product that behaves otherwise. The legal blueprint's layered model (global docs, first-run acknowledgment, point-of-use labels, provenance, action confirmation) requires the acceptance to be conspicuous, specific, timely and consistent. Recording the exact version makes the acceptance provable and forces a re-ask after any material wording change; keeping the short form non-dismissible means a user who never opens a notice still sees it. The product already refuses to fabricate data and keeps trades proposal-only, so the notice states what is actually true rather than inventing a shield.
**Impact:** `src/lib/legal/{disclosure,acceptance}.ts`, `src/components/legal/ai-output-notice.tsx` (+ test), `src/components/chat/{chat-history,chat-input,chat-dock}.tsx`, `src/components/response/response-view.tsx`, `src/components/screens/canvas/canvas-viewer.tsx`, `src/components/setup/{onboarding-shell,types}.ts` + `steps/{welcome,review}-step.tsx`, `src/lib/setup/{state,types}.ts`, `src/app/api/setup/route.ts`; docs `docs/legal/disclosures.md` (new), `docs/legal/README.md`, `docs/CHANGELOG.md`, `docs/DECISIONS.md`, `skills/ARCHITECT.md`.

## ADR-074 - Existing installs accept the notice once, and an order requires an explicit ownership checkbox (2026)
**Decision:** ADR-073's notice has two additional placements. (1) An install that was migrated before the notice existed is not forced back through setup: `RiskAckGate` (mounted in the root layout) reads `/api/setup`, appears only when setup is complete and the CURRENT notice version is unaccepted, and records the acceptance through the standalone `GET/POST /api/legal/risk-ack` (same `risk_ack_v1` record, same version check). (2) The trade approval card requires an explicit "this order is my decision and my responsibility, and I verified it with my broker" checkbox before Confirm & send, in addition to the existing typed live phrase. Both are blocking: the gate hides the app until accepted, and the send button stays disabled until owned.
**Reason:** A first-run-only acceptance would leave every migrated install unrecorded, which is the population most likely to already rely on the product. Re-running the whole wizard for a copy change is disproportionate; a one-time versioned gate is the smallest honest control. The order is the highest-risk action the product can take, so ownership must be stated at the moment of action, not only in a footer the user scrolled past. The typed phrase proves intent; the checkbox records responsibility. Neither replaces the Terms' limitation of liability.
**Impact:** `src/components/legal/risk-ack-gate.tsx`, `src/app/api/legal/risk-ack/route.ts`, `src/app/layout.tsx`, `src/components/chat/trade-approval-banner.tsx`; docs `docs/legal/disclosures.md`, `docs/CHANGELOG.md`, `docs/DECISIONS.md`, `skills/ARCHITECT.md`.
## ADR-075 - Notice intensity follows deployment; per-answer copy is one clause (2026)
**Decision:** ADR-073/074 are refined, not reversed. The per-answer notice keeps the label and the expander but repeats only ONE responsibility clause ("Verify with your broker; decisions are yours"), never the full paragraph. The upgrade notice's intensity follows the deployment: a dismissible bottom banner on local self-hosted (auth off on loopback, dismissal per session, records nothing) and a blocking modal on hosted/remote requests. `GET /api/setup` reports `hosted` from the auth config and the request Host, so the client never guesses. Canvas and the composer are unchanged.
**Reason:** Repeating the whole paragraph every turn produces banner blindness, which weakens the conspicuousness the notice exists to create; a short clause plus an always-available full text is more likely to be read. A hard modal on a single-user local install is disproportionate friction, while a multi-user hosted service genuinely needs explicit acceptance from each user. The deployment signal is server-side because the browser cannot know how it is reached.
**Impact:** `src/lib/legal/disclosure.ts` (`AI_OUTPUT_INLINE`), `src/components/legal/ai-output-notice.tsx` (+ test), `src/components/legal/risk-ack-gate.tsx`, `src/app/api/setup/route.ts` (`hosted`), `src/components/setup/types.ts`; docs `docs/legal/disclosures.md`, `docs/CHANGELOG.md`, `docs/DECISIONS.md`, `skills/ARCHITECT.md`.

## ADR-076 - Quant mode is figure-first: a deterministic Quant Figure Kit over stored artifacts, with a `quant` chart source (2026)
**Decision:** A standard/deep quantitative turn that produces a series or distribution is presented figure-first. The figures are built by ONE deterministic kit (`python/harness/quant/visuals.py`) from a stored experiment artifact — equity, underwater, monthly returns, exposure, trade distribution, trades vs holding time, rolling risk, cost waterfall, Monte-Carlo, regime attribution, walk-forward, and a 3D parameter sensitivity surface. The agent receives compact `chart` blocks from `render_quant_figures` that name their source (`quant: {experimentId, figure}`); the browser resolves the spec through `GET /quant/experiments/{id}/figures/{figure}`. The block never carries a series, so a long curve cannot be truncated by a message. 3D is restricted to a measured third dimension: a complete optimization grid becomes a `surface`, an incomplete grid becomes a `scatter3d` point cloud and says so; time series and compositions stay 2D. `quant_intent_block` carries the visual contract and figure budget, `evaluate_quant_outcome` reports a missing figure when a series-producing tool ran, and the response mode is upgraded to `visualization` for quant turns. The same kit and the same `ResponseChart` surface render on the Backtest bench (`QuantFigurePanel`), so chat and screen cannot drift. A figure that cannot be computed is an honest `available: false` with the reason.
**Reason:** The engine already stored everything needed to draw a trustworthy picture, but the protocol made a chart optional ("experiment id, model id, ... chart or report") and the model had to type the series to draw one — a long series risks truncation and a model-authored chart is not a computed figure. A deterministic kit keeps the arithmetic in `harness/quant/`, makes the same experiment always produce the same vocabulary, and lets a five-figure answer stay a short message. The 3D boundary is deliberate: a surface is a diagnostic of a real search, not a way to make a two-variable result look impressive.
**Impact:** `python/harness/quant/visuals.py` (new), `python/harness/quant/visual_tools.py` (new, `render_quant_figures`), `python/harness/quant/tools.py` (registration), `python/harness/routers/quant_reports.py` (figure routes), `python/harness/quant_intent.py` (visual contract + outcome check), `python/harness/graph.py` (quant response-mode upgrade), `python/harness/prompts.py` (quant source guidance), `python/harness/charts/render.py` + `scene_render.py` (3D PNG axes), `src/lib/response/contract.ts` (`quant` source), `src/lib/response/chart-data.ts` (`fetchQuantFigure`), `src/components/response/response-chart.tsx`, `src/components/screens/backtest/shared/quant-figure-panel.tsx` + `edge-report-panel.tsx`; tests `python/tests/test_quant_visuals.py`, `test_quant_intent.py`, `test_quant_routes.py`, `src/lib/response/chart-render.test.ts`; docs `docs/quant-engine.md`, `docs/chart-engine.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.
## ADR-077 - Any-size sources are opened in place: catalog first, bounded reads, routed queries (2026)
**Decision:** A folder or drive is registered as a source *root* and catalogued in place in `.black-eclipse/sources/sources.db`; bytes are never copied, uploaded or base64-encoded. Opening returns a bounded card (magic/format, schema or page count, preview, index state) built from at most 4 KB head + 1 KB tail. Reading is a windowed seek (<= 1 MiB) or a bounded line range. Text is indexed lazily per file into FTS5; structured data is queried through read-only DuckDB with a view named `t`, an allowlist of one SELECT/WITH statement, a memory limit and a spill directory; when nothing is indexed, a parallel scan runs under explicit byte/time/result/cancellation budgets. A router derives candidates from catalog predicates and picks metadata, FTS, DuckDB or scan, then reports `engines`, `scannedFiles`, `scannedBytes`, `truncated` and warnings. Embeddings are opt-in, applied only to a filtered candidate subset and scored over a bounded vector window; a terabyte corpus is never embedded. Roots are added only by an operator route; agent tools are read-only.
**Reason:** The request was to open files of any size -- terabytes included -- and still search and solve fast. Copying is the wrong primitive at that scale (minutes to hours per copy), whole-corpus embedding is mathematically and economically impossible (1 TB of text is ~500M chunks and terabytes of float32 vectors), and a single mega-index makes every query wait for the slowest region. In-place cataloguing makes open independent of file size; pushdown makes tabular queries exact and fast; lazy indexing makes the second touch instant without paying for the first terabyte; explicit budgets make a cold corpus useful and honest instead of a hang. Keeping the agent read-only and root registration operator-only preserves the existing filesystem-authorization boundary.
**Impact:** `python/harness/sources/` (`config.py`, `errors.py`, `contracts.py`, `catalog.py`, `scanner.py`, `probe.py`, `extract.py`, `indexer.py`, `semantic.py`, `query.py`, `rawscan.py`, `planner.py`, `service.py`, `tools.py`, `bootstrap.py`); `python/harness/routers/sources.py` (`/sources/*`, operator-gated); wiring in `python/harness/service.py` + `python/harness/main.py`; agent allowlist `SOURCE_TOOLS` in `agents.py`; front door `src/app/api/sources/[...path]/route.ts` + `src/lib/sources/client.ts`; Sources tab in `src/components/screens/sources/sources-screen.tsx` via `APP_TABS`, sidebar config, deep links and the palette; tests `python/tests/test_sources_core.py` + `test_sources_query_tools.py`; docs `docs/harness/large-sources.md` (new), `docs/NAVIGATION.md`, `docs/CHANGELOG.md`. Deliberately not built: corpus upload/copy, whole-corpus embeddings, automatic OCR of scanned PDFs, media transcription, and agent-initiated root registration.

## ADR-078 - Quant mode is method-first: discovery produces candidates, evidence promotes them (2026)
**Decision:** The centre of quant mode is the discovery loop, not the backtest button. A new `python/harness/quant/discovery/` package owns: a 14-family method taxonomy (thesis, edge source, signals, failure modes, regime priors, capacity, validation plan); method cards and a deterministic, explained fit ranking; market-structure diagnostics (variance ratio, Hurst, AR(1) half-life, volatility clustering, calendar buckets, IS/OOS stability); falsifiable hypotheses with acceptance/invalidation rules; method?valid `StrategySpec` synthesis plus two-method composition; and a bounded IS/OOS miner that records the REAL trial count and reports a Deflated Sharpe. The chain is `library ? scan ? hypothesis ? synthesize/mine ? validate ? playbook`. Discovery may recommend, synthesize, mine, persist and scan for current setups; it may NOT promote a method to validated, and a setup is a condition, not an order. Persisted `QuantMethod` rows and `generate_trading_playbook` turn a surviving spec into reusable instructions, always paper-first.
**Reason:** Users do not need another indicator or a one-off backtest; they need methods they can understand and trade, and a credible path to invent new ones. A recommendation without the thesis/failure map is a signal to copy; a mined candidate without the trial count is data mining. Keeping discovery descriptive and the engine deterministic preserves the honesty contract while making the product useful before anything has an edge. Methods that are not single-asset rule strategies refuse with their `requires` instead of being forced into the DSL.
**Impact:** `python/harness/quant/discovery/` (taxonomy, library, diagnostics, hypotheses, synthesize, miner), `quant/playbook.py`, `quant/discovery_tools.py`, `quant/store.py` (`QuantMethod`), `routers/quant_desk.py` (`/quant/methods/*`, `/quant/discovered-methods/*`), `quant_intent.py` (discovery kind + protocol), `src/components/screens/strategy-studio/method-discovery-panel.tsx`; tests `test_quant_discovery.py`, `test_quant_desk.py`, QuantBench discovery/math cases; docs `docs/quant-method-discovery.md` (new), `docs/quant-engine.md`, `docs/strategy-studio.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.

## ADR-079 - The front door is a resident process, not a launcher child (2026)
**Decision:** `scripts/local-proxy.js` (the `https://blackeclipse` front door) outlives the app. Launchers ATTACH to a live door (`scripts/lib/proxy-state.js` → `.black-eclipse/proxy-state.json`) instead of spawning a second one; a per-user sign-in task re-raises it after a reboot (`scripts/lib/proxy-task.js`, `black proxy on|off|status`); only `proxy:stop` / `black proxy off` ends it. While the app is down the door answers PAGE NAVIGATIONS with the branded `/offline` page (cached from the app, with a built-in fallback) and every other path with a plain 503 — so health probes and the page's reconnect check can never read the card as "the app is up". `POST /__eclipse/start` boots the app with `BE_NO_PROXY=1`. The service worker (`public/sw.js`) remains the second layer, covering the door itself being down (fresh profile, hard reload, cleared site data).
**Reason:** A stopped app means nothing is listening, and no code inside the app can answer then — the browser's own `ERR_CONNECTION_REFUSED` page is the only possible output, which reads as a broken website, cannot carry the product's name or theme, and made a deliberately stopped Eclipse look crashed. Only the two always-available layers (the resident front door, and the browser worker) can own that state. Making the door responsible also fixes the mismatch where the app was kept alive by its own task while the door was not.
**Impact:** `scripts/local-proxy.js`, `scripts/local-host.js` (attach + `BE_NO_PROXY`), `scripts/local-proxy-stop.js`, `scripts/start-desktop.js` (no longer stops the door), `scripts/up.js`, `bin/black.js` (`proxy` command), `package.json` (`proxy:on|off|status`), `scripts/lib/{proxy-state,proxy-offline,proxy-task,front-door}.js`, `public/sw.js`, `src/lib/offline/page.ts`, `src/app/offline/route.ts`, `src/components/layout/offline-ready.tsx`; tests `src/lib/offline/page.test.ts`; docs `docs/ARCHITECTURE.md`, `docs/local-hostname.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.

## ADR-080 - A trading bot is an automation bound to a strategy version, with standing consent (2026)
**Decision:** Trading bots are not a new engine. A strategy bot is an ordinary `Automation` with `action.type = "run_strategy"`, a saved quant strategy id + version, a broker connection, and an `ExecutionAuthority` (L0 proposes, L1 fills demo, L2 fills live). One automation execution = one decision on the last closed bar; the runner (`python/harness/automation/trading_runner.py`) checks standing consent, the session window, data freshness and the symbol universe, then calls the EXISTING `LiveStrategyService.run_once` -> `BrokerService.submit_order` path. Every run persists a `DecisionRecord` (including no-signal and every refusal) on `AutomationExecution.decision`. Market authority is granted ONCE: `POST /automations/{id}/trading/prepare` creates a bot-pinned bounded trial/mandate and stores `action.config.consent` (modes, symbols, orders/day, margin, positions, expiry). `prepare` accepts an empty phrase when that stored grant still covers the request, so a scheduled bot renews its own mandate without nagging; widening any limit, expiry or revocation requires the phrase again. `check_live_order` moved inside `submit_order` whenever `automation_id` is present, so no caller can skip the ladder. Pausing/disabling/archiving/deleting a bot revokes its mandates and stops its trial; positions are never closed automatically.
**Reason:** The broker layer already had the only correct order path and the only honest refusal vocabulary; duplicating either would create a second way to lose money. Re-typing a phrase per mandate made scheduled live trading unusable, while removing the phrase entirely would have turned a one-time operator decision into standing silence. Binding consent to the bot's definition and version keeps the two properties that matter: the operator answers once, and an edit invalidates the grant. A persisted decision per run is what makes "why didn't it trade?" answerable instead of a mystery.
**Impact:** `python/harness/automation/{model,validation,service,dispatcher,store,engine}.py`, new `automation/trading_policy.py` + `automation/trading_runner.py`, new `routers/bot_trading.py`, `quant/live.py` (mode-aware `prepare`, bot pin, `run_once` attribution + dry run, `stop_by_bot`), `trade/{authority,service}.py` (demo L1 mandates, central fence, revoke-by-bot), `src/lib/harness/clients/{automation-client,automation-browser}.ts`, `src/app/api/automations/[...path]/route.ts`, `src/lib/bots/{state.ts,trading/}`, `src/components/bots/{create-bot-dialog,bot-detail,bot-trading-section,run-history}.tsx` + `components/bots/trading/trading-setup-panel.tsx`; tests `src/lib/bots/trading/*.test.ts`, `python/tests/{check_trading_policy.py,test_trading_policy.py,test_trading_bot_runner.py,test_trading_validation.py,test_trading_visuals.py,test_agent_trading_prepare.py}` + bot-fence cases in `test_broker_orders.py`; docs `docs/trading-bots-plan.md`, `docs/bot-mode.md`, `docs/trading.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.
## ADR-081 - Alternative intelligence is a measurement engine, not a model that looks at things (2026)
**Decision:** Investment leads from public records ship as one lane (`python/harness/intel/`) built on a hard split: collectors produce `Observation`s (a numeric value plus the window, source, reproducible method, confidence as evidence quality, and full provenance) and a pure scorer assembles cards. No model produces a measurement; a model may only read the cards. Signals are gated individually (attention |z| >= 2, filing heat ratio >= 2), weights are renormalised over the signals that fired, direction comes only from directional signals, and conviction is corroboration across independent publishers rather than score magnitude. A signal that could not be measured is `UNAVAILABLE` with a named reason and is never zero. Four signals over two keyless sources (SEC EDGAR Form 4/13D/submissions; Wikimedia pageviews) ship first; paid satellite imagery is deliberately excluded because free 10 m data cannot support car counting or tank-fill shadow geometry, and claiming otherwise would fabricate data. Tools are read/low and advertised on `ECLIPSE_AGENT`, so scheduled bots inherit the surface without parity work.
**Reason:** The commercially valuable alt-data products are calibrated deterministic measurements validated against ground truth, not vision-model impressions; building the impression version first would produce confident nonsense. EDGAR also punishes the naive screen: netting planned 10b5-1 sales, grants and option exercises against open-market purchases measures compensation mechanics rather than conviction, so those are excluded and the exclusion is reported. Separating `value` (the comparable series) from `detail` (the evidence) lets a time series stay uniform while a card quotes the actual numbers, and persisting cards as immutable snapshots is the only way to grade a signal honestly later.
**Impact:** new `python/harness/intel/` (`contracts.py`, `sec.py`, `attention.py`, `engine.py`, `store.py`, `service.py`, `tools.py`, `bootstrap.py`); boot wiring `service.py` (`install_intel_layer`); allowlist `agents.py` (`INTEL_TOOLS`); env `BE_EDGAR_USER_AGENT`; tables on the ONE `agent-memory.db` (`IntelObservation`/`IntelWatch`/`IntelScan`/`IntelEntityRef`); tests `python/tests/test_intel_{sec,attention,engine,service}.py`; docs `docs/harness/intel-signals.md`, `docs/NAVIGATION.md`, `docs/CHANGELOG.md`.
## ADR-082 - An update promotes a signed release artifact, not a rebuild of the tag (2026)
**Decision:** The update path moves a verified ARTIFACT into place; it never checks out the release in the user's repository and compiles it. A signed manifest may advertise `builds` (the release's compiled `.next`, uploaded by the release workflow as `BlackEclipse-Build-<version>.zip`) with `url`, `sha256`, `size`, `buildId` and `lockHash`. `scripts/update.js` downloads it, refuses unless the bytes match the signed digest, extracts it into `.runtime/updates` with absolute-path/`..` entries rejected, requires a serviceable build (`BUILD_ID` == signed `buildId`, routes manifest, server, `static/<buildId>`), installs dependencies only when `lockHash` differs from the checkout's `package-lock.json`, and promotes exactly that directory via `desktop-promote.js --from`. `git` is never read. The checkout+`npm ci`+`npm run build` path survives only as a fallback for manifests without `builds` and behind `--git`; it keeps its clean-tree, tag==signed-commit and rollback rules.
**Reason:** The checkout path refused whenever the working tree was dirty, and every release edits the changelog (a date, some prose), so the maintainer's own machine could not take an update — the recurring "it did not update" report. It also moved HEAD backward on an ahead-of-release checkout, spent 3–5 minutes and a 12 GB heap compiling on the user's machine, and trusted a source hash that could mark an out-of-date `.next` as current. Shipping the compiled build is how desktop software updates in practice (the Tauri updater this shell already uses, VS Code, electron-updater, OpenCode): compile once in CI, sign the digest, verify and swap on the client. Signing the digest (not just the URL) keeps the existing trust model: a tampered asset is refused and deleted.
**Impact:** new `scripts/lib/build-artifact.js` (download/hash, safe extraction, build validation, lock hash) + `scripts/release-build-zip.js` (`npm run release:build`); `scripts/update-verify.js` + `src/lib/updates/core.ts` (`validateBuilds`, `buildForPlatform`, safe URL re-check mirrored); `scripts/update-sign.js` (`--build-*`); `scripts/update.js` (prebuilt path + `--git`); `scripts/update-apply.js` (mode `build`, `stagedDir`, promotes with `--from`); `src/lib/updates/types.ts` + `src/components/layout/update-button.tsx` (mode/phases/copy); `.github/workflows/desktop-release.yml` (`release-build` job + attached zip); tests `src/lib/updates/core.test.ts`; docs `docs/UPDATES.md`, `docs/environments.md`, `docs/CHANGELOG.md`.

## ADR-083 - Dependency installs are staged and swapped by rename; the live tree is never emptied (2026)
**Decision:** An update that needs new dependencies never runs `npm ci` in `<app>/node_modules`. It fetches them into `.runtime/deps-stage` (a copy of `package.json` with the project-only lifecycle scripts removed, plus `package-lock.json` and `.npmrc`), validates the result (`.package-lock.json` present, `next`/`react`/`react-dom` present), then moves the live tree to `.runtime/deps-previous` and renames the staged tree into place. The two renames are the only destructive step; if the second fails the first is undone. Root lifecycle scripts (`postinstall` = prisma generate + python stack + hooks) run against the real tree immediately after the swap; if one fails, the previous tree is renamed back. npm runs with `NODE_ENV` cleared and `--include=dev`, because the app runs with `NODE_ENV=production` and a checkout still needs its dev tree for `npm run build`, `tsc` and the tests. A replaced tree that the running app still maps is parked under a timestamped name and removed by the next install. There is no file-lock probe: `scripts/lib/update-locks.js` is deleted, and `scripts/update-apply.js` no longer closes Eclipse to "release" its native modules.
**Reason:** On 2026-11-22 an in-app update (v0.4.24 -> v0.4.25) emptied `node_modules` and then failed with `EPERM: unlink ...\@next\swc-win32-x64-msvc\next-swc.win32-x64-msvc.node`. The safety module in place tested "is this file locked?" by renaming it; verified on the same machine, Windows *allows* renaming a loaded `.node` file and only refuses deletion, so the probe reported "free", npm deleted the tree, and the delete of the mapped binary failed - 67 of 600+ packages left, the running window alive only on modules already in memory. The premise was wrong, not the arithmetic: no non-destructive lock test exists on Windows, while renaming a *directory* that contains mapped files is allowed. Staging exploits that asymmetry, so the class of failure disappears instead of being detected better.
**Impact:** new `scripts/lib/deps-install.js` (`installDependencies`, `stageDependencies`, `swapIntoPlace`, `restorePrevious`; npm runs with `NODE_ENV` cleared and `--include=dev`) + tests `src/lib/updates/deps-install.test.ts`; `scripts/update.js` (prebuilt and git paths call `installDependencies`; rollback restores the parked tree by rename instead of re-running `npm ci`); `scripts/update-apply.js` (no lock probe, no stop/revive of the app, `mode: build` recorded when the prebuilt path is chosen, promotion always `--no-restart`); deleted `scripts/lib/update-locks.js`; docs `docs/UPDATES.md`, `docs/CHANGELOG.md`.

## ADR-084 - Every declared broker kind executes through one contract; capabilities are named per venue (2026)
**Decision:** A broker kind that says it can trade returns a `TradingAdapter` and the registry declares `execution=True` with the modes it may execute; a read-only adapter may not be declared executable and an executable adapter may not be declared read-only (a drift test enforces both directions). All 19 registered kinds now execute through the same `order_preview -> approval/mandate -> guardrails -> single send` path. The contract stops being MetaTrader-shaped: an order id is `str | int` (`ticket_ref` preserves a UUID, an exchange id and a leading zero, and converts a plain decimal to an int); the account vocabulary is `demo | real` (a paper/sandbox/practice/testnet host is `demo`); REST ownership uses a client order id `eclipse-<magic>-<digest>` in place of MT5's magic, and `cancel_all` refuses with `OWNERSHIP_UNAVAILABLE` when the venue has no tag rather than cancelling the account; a spot balance is closed by an opposite market order after resolving the asset to the venue's own pair (explicit `symbolMap`, then `quoteAsset`, then the venue's listed pairs — never a guessed pair). A venue that cannot attach a stop/target says `STOP_UNSUPPORTED`; it never drops one. **Reason:** seventeen adapters were read-only by declaration, which the product owner correctly read as "cannot trade"; adding a flag alone would have been a lie, because the service, store and UI carried MT5 assumptions (integer tickets, magic, `trade_mode`, `login` in the arm phrase) that would have failed or mislabelled a REST order. Making each venue implement the same contract — with its own signing, payload and close semantics — is the difference between "the flag is on" and "an order is sent exactly once and its result is reported honestly". **Impact:** `python/harness/trade/adapters/orders.py` (shared preview/result/size/ownership/pair helpers), `model.py` (`Ticket`, `ticket_ref`, `quoteAsset`, venue selector keys), `adapter.py` (identifier + optional-volume signatures), `service.py` (string tickets, placed-vs-filled status, venue-neutral live arm with `armRef`), `positions.py`, `store.py` (text position snapshots + one-time rebuild), `reconcile.py` (venue state vocabulary), `proof.py`, `supervisor.py`, `tools.py`, `routers/trading.py`, `graph_runtime/{broker,trading_ops,trading_lifecycle}_nodes.py`, `PortType.TICKET` in `graph_runtime/model.py` + `registry.py`, `quant/live.py`, all 19 adapter modules, `registry.py`; `src/lib/graph-studio/{types,validation,port-style}.ts` (`ticket` port), `src/lib/harness/clients/trading-client.ts` (`BrokerTicket`), `src/lib/harness/trading-routes.ts` (opaque ticket ids), `src/components/layout/broker-connection-form.tsx` + `sidebar-broker-dialog.tsx` (every registered kind, not four); tests `python/tests/test_*_execution.py` (one per venue), `test_ticket_ref.py`, `test_broker_execution_registry.py`, `test_broker_store_migration.py`, `test_graph_model.py`; docs `docs/trading.md` (execution section, MT4 write contract, regenerated matrix), `docs/graph-studio.md`, `docs/CHANGELOG.md`.
