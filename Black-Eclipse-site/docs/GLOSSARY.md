---
title: Glossary
status: Current
last-verified: 2026-11-25
---

# Glossary

**Who this is for:** anyone hitting a term that is not obvious from the code.

Terms are grouped, not alphabetical, so related ideas stay together.

## Data

**JDDS** â€” "Joint Data Distribution System." The project's own name for the
SQLite vault (`jdds.db`) plus the rule that goes with it: data is stored once and
distributed to every screen. It is not a service or a daemon â€” just the DB and
the architecture around it. (`prisma/schema.prisma` header)

**DataHub** â€” the client-side pub/sub layer (`src/lib/workspace/data-hub.ts`).
Components subscribe to *topics*; the hub fetches each topic once, caches it with
a TTL, refreshes on an interval, and fans the result out. Hidden widgets
unsubscribe so they stop burning rate limits.

**Topic** â€” a named data feed in the DataHub, e.g. `market:indices:US`,
`market:overview`, or `journal:analytics`. Registered once with
`registerTopic(jsonTopic(...))`.

**Provenance / `DataSource`** â€” the label on every DataHub entry saying where the
data came from: `LIVE | CACHED | HISTORICAL | DEMO | ESTIMATED | UNAVAILABLE`.
A source of truth for "is this real and current".

**`Price.source`** â€” the database-level provenance: `'scraper'` (live scrape) vs
`'csv'` (imported historical/API data).

**Warm-up** â€” the boot-time bulk job that fills prices for open markets so page
loads are instant. Production-only by default; opt in during dev with
`BE_WARMUP=1`.

**Universe** â€” the set of assets belonging to a market (`Asset` rows). "Syncing
the universe" means discovering new listings and deactivating dead symbols.

**`yfinance` bridge** â€” the optional Python path (`scripts/yfinance_bridge.py`)
used for markets TradingView covers poorly. Requires Python 3.9+.

**Stale** â€” data older than its refresh cycle, or imported data. It must be
labeled, never presented as live.

## Workspaces & UI

**Widget** â€” a single card on the Command Center dashboard, defined by a
`WidgetDefinition` (id, category, label, icon, sizes, `dataDeps`, â€¦).

**Widget workspace** â€” the engine behind the Command Center dashboard:
`sections â†’ columns â†’ widget cards`, with optional **side zones** (`left` and
`right`) flanking the centre `main` zone. Lives in `src/lib/workspace/`.

**Market workspace** â€” a *separate* engine behind the **Chart Workspace** tab,
based on panels rather than widget cards. Lives in `src/lib/market-workspace/`.
Not the same as the widget workspace. The label was renamed Chart Workspace while
the folder and tab id (`market-workspace`) stayed put.

**Zone** â€” a canvas column a widget workspace section belongs to: `main`
(centre), `left`, or `right`. All render under one `DndContext`, so cards drag
freely between them. The legacy name for the right zone was **rail**; stored
layouts using `rail` are migrated to `right` on read. A zone can be **removed**
(hidden) from the Customize edit bar â€” its sections are kept in the layout, so
re-showing restores the exact arrangement.

**Left / right panel** â€” the narrow side columns of a widget workspace (the
Command Center's Watchlist on the left, Market on the right). Cards drag freely
between the centre and the side panels.

**Rail** â€” *legacy* name for the workspace's right side zone (the Market panel).
Now just the `right` zone; stored layouts still using `rail` are migrated on read.

**Preset** â€” a curated starting layout offered to users.

**Persona / template** â€” a curated starting layout for the Command Center
(Trader, Investor, Macro, Crypto, Forex, Quant), authored as **widget-id specs**
in `src/lib/dashboard/templates.ts` â€” never registry indices, so adding a widget
cannot silently repoint a template. A template describes only the **centre**;
the side rails are added by `buildMarketViewLayout`.

**Band** â€” a horizontal grouping in a persona template: a flat widget list that
`composeBoard` arranges into a section (a `full` band = one track; a `board` band
= N balanced tracks). `composeBoard` enforces global first-wins dedupe, a
4-track cap clamped to the card count, and content sizing. See
`src/lib/dashboard/layout.ts`.

**Track / track board** â€” the current layout model (`WorkspaceLayout.version: 2`).
A section's **columns are vertical tracks**; the pure packer
(`src/lib/workspace/pack.ts`) distributes each section's cards into the shortest
track by estimated height, so a short widget never leaves dead space beside a
tall one. Packing runs at compose time only (defaults/templates/imports), never
on user edits. The centre defaults to three balanced tracks (`CENTRE_TRACKS`).

**Stacked rail** â€” a side zone built as ONE section with a single column holding
every tile, rather than one section per tile. This removes the per-tile section
padding/gap that made the dashboard look gappy. See `buildLeftSections` /
`buildRailSections`.

**Frame vs grow sizing** â€” every default card sizes to its widget
(`height: null`) in one of two modes. A **frame** (`placement.defaultHeight`,
from `WIDGET_FRAME_HEIGHT` / `WidgetDefinition.defaultHeight`) is a definite box
whose body stretches to fill it â€” charts, maps, live video, scrollable
tables/feeds. A **grow floor** (`placement.minHeight`) is a floor under content
that sizes itself â€” compact stat tiles. The renderer keys its body class off
this so a frame widget never collapses to a `flex-basis:0` body. A user can
still pin an exact height by dragging the card's bottom edge; auto is the
default and the old "Fit height" control is gone.

**Zone width** â€” a side panel's user-resized width in px (`zoneWidths.left` /
`zoneWidths.right` in the layout), set by dragging the vertical handle beside
the panel and clamped to 180â€“520px. Presentation-only; never affects the tree.

**Compact mode** â€” *removed*. It forced a fixed 40px card height and crushed
widget content; there is no row-height mode any more.

**Placement / two-click contract** â€” the interaction model for moving widgets:
grab to *preview* a slot, click again to *commit*. Escape or right-click cancels.
See ADR-008.

**Keep-alive tab** â€” a tab that mounts on first visit and then stays mounted
(hidden with `display:none`) so revisits are instant.

**Frosted glass / glass tiers** â€” the shared surface styling: `.glass-card`
(default), `.glass-card-strong` (overlays), `.glass-card-subtle` (small tiles),
`.glass-drawer` (real-frost drawers), `.glass-button`.

**Aurora** â€” the WebGL glow at the top of the viewport that reflects combined
market + news sentiment in lime-on-black. The visual USP.

**Backdrop root** â€” a CSS condition (transform, filter, persisting animation) on
an ancestor that silently breaks `backdrop-filter` on descendants. This is the
reason drawers are portaled to `document.body`.

## Agent & AI

**Agent provider** â€” the abstraction (`src/lib/agent/types.ts`) behind chat.
Default is a keyword-based **mock provider**; real providers (OpenAI/Anthropic/
Ollama) are pluggable and selected by `getAgentProvider()`.

**Research engine** â€” the multi-agent pipeline that produces real, sourced
answers instead of canned ones, and persists its work in JDDS
(`ResearchReport` / `AgentRun` / `ResearchFinding`).

**Research memory** â€” the persisted record of what agents did, so they can cite
past findings, avoid redoing work, and retry failed tool calls.

**Tool call** â€” one action an agent takes (query data, scrape, search), recorded
with its outcome.

## Trading

**Portfolio engine** â€” the pure FIFO/LIFO cost-basis + stats logic in
`src/hooks/use-portfolio-engine.ts`, shared by the portfolio screens and the
dashboard portfolio widgets. It is read-only analysis over user-entered
transactions.

**Broker integration** â€” *not implemented.* There is no paper broker and no
broker adapter in the codebase today. The `trading`-category dashboard widgets
are `UNAVAILABLE` stubs (`UnavailableStub` in `src/lib/dashboard/registry.tsx`).
This is a planned area, not existing functionality.

**Watch** â€” one alert rule (a condition tree over market data), evaluated by a
server-side poll loop. Fires into `PriceAlertEvent` history, which survives
deleting the watch.

**R-multiple** â€” profit measured in units of the initial risk. A core journal
metric.

**MFE / MAE** â€” max favorable / adverse excursion: how far a trade went in your
favor or against you, in R. Used for behavioral analytics, not prediction.

## Process & repo

**ADR** â€” Architecture Decision Record. A short note on *why* a decision was
made. See [`DECISIONS.md`](./DECISIONS.md).

**Skill** â€” a persistent instruction file for AI agents. Two sets exist:
`skills/*.md` (the human-readable engineering OS) and `.opencode/skills/*/SKILL.md`
(the same knowledge registered so opencode auto-loads it on matching tasks).

**Guard / `api-guard`** â€” the module (`src/lib/api-guard.ts`) that decides
whether a mutating request is allowed, plus per-IP rate limiting and the SSRF
allowlist for outbound feed URLs.

**`doctor`** â€” the boot preflight script (`scripts/doctor.js`) that checks the
environment before `dev`/`up` runs.

**`db-guard`** â€” the script (`scripts/db-guard.js`) that sets SQLite durability
PRAGMAs and takes rolling backups.

**Seed** â€” the database a fresh clone or install starts from. It is **generated, not
committed**: `scripts/doctor.js` / the installer create the schema with
`prisma db push`, then the demo journal seeder (`src/lib/journal/demo.ts`) fills
it with rows tagged `source:'demo'`. `scripts/lib/seed-db.js` bakes the same
schema + demo rows into a packaged installer's `app/seed/jdds.db`. `jdds.db`
itself is untracked runtime data.

**Capability matrix** â€” the truthful, per-market breakdown of what the system can
actually do (`src/lib/harness/capability.ts`): recognition, searchable universe,
quote, history, fundamentals, official filings, web/documents, and observed
health. Separate from *coverage* (recognition): a market can be recognized while a
capability is unimplemented or unconfigured there.

**Provider eligibility** â€” whether a provider may be attempted for a given
request (by market/issuer type/data type). An ineligible provider is SKIPPED, not
attempted-and-failed; e.g. SEC is ineligible for a non-US issuer. Missing
credentials disable only that route.

**Exchange suffix** â€” the Yahoo-style suffix on a global ticker (`.TW`, `.KS`,
`.L`, `.T`, `.NS`). Mapped by `EXCHANGE_SUFFIX_ALIASES` to an implied market; an
unknown suffix is never guessed.

**Unsupported document** â€” a fetched document with no usable text (a scanned or
image-only PDF, or a JavaScript-only shell). Reported as its own diagnostic
category; OCR is not available, so it is never passed through as garbage.

## Presentations

**Deck / DeckSpec** -- a presentation stored as a versioned JSON artifact (a
Canvas artifact of kind `presentation`), not as a `.pptx`. The spec carries
meta, size, theme, named data sources and slides; the PowerPoint file is a
rendered derivative (ADR-085).

**Slide layout** -- the named shape of a slide (`title`, `bullets`,
`two_column`, `statement`, `quote`, `metric_row`, `table`, `chart`,
`image_full`, `image_right`, `closing`, ...). Layouts place content; a deck
never carries absolute coordinates, which keeps preview and export identical.

**Theme preset** -- one of `eclipse` / `paper` / `midnight` / `slate`:
palette, type scale and chart colours. Python (`presentations/themes.py`) is
canonical for export; the TS mirror is pinned by a parity test.

**Overflow** -- a slide whose text cannot fit at the smallest readable size. It is
reported (`overflow:<region>`, `overflowSlides` in an export), never silently
clipped.

**Deck artifact vs deck export** -- the deck is the editable artifact; an export
is a NEW artifact tagged `pptx` whose metadata names the source
`derivedFrom {artifactId, version}`. Exports never modify the deck.
