---
title: Overview & Mental Model
status: Current
last-verified: 2026-11-26
---

# Overview & Mental Model

**Who this is for:** anyone new to the project, including non-developers who just
want to understand what the thing does.

## What BlackEclipse is

**Black Eclipse is an open-source agentic harness designed to help you automate
finance.** It pairs a Bloomberg-inspired terminal UI with a Python agent runtime:
you screen stocks across 25+ world markets, watch a portfolio, follow news,
explore market bubbles, keep a trading journal, and talk to a research agent —
all in one dark, dense, glassmorphic SPA. The same harness runs scheduled agents,
backtests strategies and gates trading behind explicit approvals.

The defining constraint: **it must run with zero configuration.** Clone it, run
it, and there is market data on screen. Everything paid or keyed is an opt-in.

## The mental model (this is the part worth remembering)

Think of the system as four horizontal layers. Almost every question about the
codebase resolves to "which layer am I in?"

```
  ┌──────────────────────────────────────────────────────────────┐
  │ 1. SHELL & SCREENS      one page, many tabs, never URL-routed │
  │    stock-chat.tsx · sidebar · top-bar · Aurora               │
  ├──────────────────────────────────────────────────────────────┤
  │ 2. CLIENT DATA LAYER    pub/sub topics + Zustand stores       │
  │    workspace/data-hub.ts · hooks/use-portfolio-engine.ts      │
  ├──────────────────────────────────────────────────────────────┤
  │ 3. API ROUTES           thin, server-only, guarded            │
  │    src/app/api/**/route.ts                                    │
  ├──────────────────────────────────────────────────────────────┤
  │ 4. JDDS                 one SQLite file, the single vault     │
  │    jdds.db · prisma/schema.prisma · scraper/scheduler        │
  └──────────────────────────────────────────────────────────────┘
```

Three ideas carry most of the weight:

1. **One page, many tabs.** There is no client-side router for the main screens.
   `stock-chat.tsx` holds an `activeTab` state machine; tabs mount on first visit
   and then stay mounted hidden. So "which route is this?" is almost always the
   wrong question — ask "which tab, which screen component".
2. **JDDS-first.** Data is scraped or imported into the local SQLite vault
   **once**, then every screen reads from it. Screens do not talk to data
   providers directly. This is why page loads are fast and why the app works
   offline-ish once warmed.
3. **Frosted glass is the design system.** Surfaces are not styled ad hoc. See
   [`../.opencode/skills/black-eclipse-ui/SKILL.md`](../../.opencode/skills/black-eclipse-ui/SKILL.md).

## What a new user actually sees

The sidebar is user-reorderable with configurable function-key navigation.
Defaults follow its shipped order: F1 ChatBot, F2 Dashboard, F3 Command Center,
F4 Investor Eye, F5 Chart Workspace, F6 Market Screener, F7 Portfolio, F8 Journal,
F9 Market News, F10 Market Bubbles, F11 Economics, and F12 Price Alerts. Later
sections (Skills, Eclipse Memory, Artifacts, Graph Studio) ship
without a default function key. A menu that has never been customized is
re-numbered on load, after a reorder and after an app-wide nav update, so its
first twelve entries always read F1–F12 in order — this repairs older saved
orders and the Dashboard insertion. Once you assign or clear a key in
Settings › Sidebar, those explicit assignments are preserved instead; some
keyboards need Fn, and browser/OS interception may require choosing a different
key in Settings.

Press **Ctrl+/ (Cmd+/ on Mac)** outside text fields and other dialogs for
searchable shortcut help. Page groups appear as screens are visited. Common
contextual actions include **Shift+E** customization, **Shift+W** widget/panel
library, **Shift+N** creation/new layout, **S** local search and **R** refresh
where those actions exist. On the ChatBot tab, **Ctrl+N (Cmd+N on Mac)** opens a
new chat page. Help marks the current scopes; hidden screens and text entry do
not receive page commands.

The sidebar ships with these tabs, split across two top-level modes —
**Terminal** (market tools) and **Agentic** (the AI runtime). A segmented
switcher at the top of the menu (Alt+T / Alt+A) flips between them; **ChatBot**
appears in both. The switcher is hidden while the sidebar is collapsed, but the
selected mode still applies — use Alt+T / Alt+A to switch.

| Terminal tab | What it is |
|---|---|
| Dashboard | The daily decision surface: market weather, leadership, portfolio trend health, review queue, watchlist radar, concentration and alerts. Global-first, with a market selector. |
| Command Center | A customizable widget dashboard (see the workspace concept below) |
| Chart Workspace | A customizable multi-panel chart/market grid (independent layout engine) |
| Market Screener | The main stock screen across global markets |
| Portfolio | Holdings, performance, allocation, transactions — plus per-fund heatmap, correlation matrix and risk decomposition |
| Journal | SQLite-backed trading journal with analytics (ships labelled demo trades you can remove) |
| Market News | Sentiment-tagged RSS feed, ingested into JDDS |
| Market Bubbles | Physics-based turnover map — real JDDS instruments sized by 24h traded value, plus whole-market Volume Analytics |
| Economics | Macro series + economic calendar |
| Price Alerts | Condition-based alert watches with a server-side poll loop (plus Investor Eye) |

| Agentic tab | What it is |
|---|---|
| ChatBot | Multi-agent research chat; research-worded questions hit the real research engine. Shared with Terminal |
| Skills | The skill control room — library, learning queue, evaluation, execution activity |
| Eclipse Memory | Dedicated `eclipse-memory.db` control store: memories, corrections, mistakes, policies, health |

Plus an embedded **Chart** tab reached from other screens (TradingView-backed).

The remaining agentic capabilities — Code Tasks, Editor, Scheduled Jobs, Tasks,
Plugins, Model Control Center, Search Intelligence, Research and Backtest — are
**not** sidebar tabs. They remain fully available from the ChatBot, which drives
them through its tools (code/filesystem tools, `research_*`, quant backtest
tools). Setup that used to live on those screens is reached elsewhere: provider
credentials under **Settings › AI**, MCP servers under **Settings › MCP**, and
retrieval under **Settings › Market**. The former screens' files are retained but
unmounted.

## The one concept that confuses newcomers: two workspaces

There are **two** drag-and-drop, user-customizable grid systems in this repo, and
they are not the same thing:

- **`src/lib/workspace/`** — the *widget* workspace behind the **Command Center**
  dashboard. Sections → columns → widget cards, plus optional `left`/`right` side
  zones (Watchlist and Market).
- **`src/lib/market-workspace/`** — a separate *panel* workspace behind the
  **Chart Workspace** tab (chart panels and market panels). The folder and tab
  id (`market-workspace`) keep the old name; only the user-facing label changed.

The older `src/lib/customize/` kernel is **superseded**. Do not build on it.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for how the two relate, and
[`DECISIONS.md`](./DECISIONS.md) ADR-005 for why.

## Honesty invariants (never violate these)

These are product promises, not style preferences:

- **Never fabricate financial data.** If a source is unavailable, show an
  `UNAVAILABLE` state. Widgets that need brokers or feeds they do not have must
  say so plainly — the broker-backed widgets and the Home Trading section
  render an honest setup note when no broker is connected.
- **Label simulated and stale data.** Imported/stale universes are
  distinguishable from live scrapes (`Price.source`); demo data sits behind a
  visible `DEMO` banner.
- **Zero-config must keep working.** Any change that makes `npm run dev` require
  setup breaks the core promise.
- **The Aurora glow is the market-state indicator.** It reflects an explainable
  **Market Regime** reading (risk-on broadening … risk-off, plus an honest
  UNAVAILABLE state), lime-on-black. The glow is decorative; the top-bar
  **Market Regime** control opens the interactive breakdown. See
  `src/lib/regime/`.

## Where to go next

- Want it running? → [`QUICKSTART.md`](./QUICKSTART.md)
- Want the layers in detail? → [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Want to find a file fast? → [`NAVIGATION.md`](./NAVIGATION.md)
- Hit an unfamiliar word? → [`GLOSSARY.md`](./GLOSSARY.md)
