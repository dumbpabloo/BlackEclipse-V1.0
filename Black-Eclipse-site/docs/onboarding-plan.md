---
title: First-Run Onboarding — Implementation Plan
status: Implemented (v1)
last-verified: 2026-11-21
---

# First-Run Onboarding — Implementation Plan

> **Implemented 2026-11-09.** The catalog/capabilities/bundles/state layer, the
> `/api/setup` routes, the boot-style gate and six splash-style steps, the source-preference
> enforcement in the scraper/crypto/economics paths, the Settings entries, and
> the three reset scopes (`black onboarding`, `black reset --onboarding|--setup|
> --factory`) are in the tree. See ADR-072. `/api/setup/check` now runs a real,
> bounded reachability probe for every included open source (see
> `src/lib/setup/included-probes.ts`) and reports rate-limited/failed honestly.
> Enforcement now covers the scraper chain, crypto connector, economics,
> Investor Eye, news ingestion + `/api/news`, and the web-search provider chain.
> Remaining future scope: per-user onboarding if multi-tenant use is ever added.

**Goal:** A boot-style, full-screen setup experience shown once, on a brand-new
installation, before the user lands in the app. It asks how the user will use
the product (personal vs commercial), which **built-in data APIs** to enable,
and how the AI should be powered (built-in/local vs the user's own provider key).
Every choice is honest, reversible, and enforced — no cosmetic toggles.

**Non-goals (v1):** billing, hosted Eclipse keys, per-user (multi-tenant)
onboarding, account creation. This is instance setup, not sign-up.

---

## 1. What already exists (reuse, do not rebuild)

| Need | Existing thing | Reuse |
|---|---|---|
| Boot visual language | `src/components/ui/global-loader.tsx` + `src/components/ui/ascii-loader.tsx` (`DotGrid`, `AsciiProgress`) | Same background, grid, `%`/label treatment |
| First-run pattern | `src/components/screens/dashboard/onboarding.tsx` (`DashboardOnboarding` dialog) | Non-dismissible dialog pattern, persona cards, glass styling |
| AI provider list | `src/lib/agent/presets.ts` (`LLM_PRESETS`), `src/components/screens/ai-provider-settings.tsx` | Provider catalog, key status, model picker |
| Key vault | `src/lib/provider-keys.ts`, `GET/POST/DELETE /api/provider-keys` | Save/test keys encrypted at rest; never return values |
| LLM config | `src/lib/settings.ts` (`getLLMConfig` / `setLLMConfig`), `POST /api/settings` | Persist provider/baseUrl/model |
| Built-in market data | `src/lib/scraper/fetchers/index.ts` chain; `src/lib/connectors/catalog.ts`; `src/lib/economics/sources.ts`; `src/lib/investor-eye/*` | Enforce source prefs here |
| Settings storage | `SystemConfig` key/value (`prisma/schema.prisma`), `getConfig/setConfig` in `src/lib/settings.ts` | One new key namespace |
| Mutation guard | `guardMutation` in `src/lib/api-guard.ts` | Protect the completion route |
| Boot mount | `src/app/layout.tsx` | Mount the gate next to `<GlobalLoader />` |

**Do not** reuse `GET /api/onboarding` — that route already belongs to the
Skills cold-start content (`src/app/api/onboarding/route.ts`, used by
`skills-browser.ts`). New routes live under **`/api/setup`**.

---

## 2. Onboarding experience (organized for a working first session)

The goal is not "collect API keys." The goal is: **a new user reaches a useful,
honest app in under two minutes, understands exactly what is and is not
connected, and can fix anything later.**

### 2.1 Seven principles

1. **Organize by capability, not by vendor.** A new user thinks "I want charts
   and news," not "I want Polygon.io." Vendors appear only after a capability is
   chosen.
2. **Separate Included from Connected, visibly.** Every data item is either
   *Included* (open data, no account) or *Connect your own* (user's key/account).
   This is also the legal boundary, so it must be obvious on screen.
3. **One decision per screen.** Do not show 30 toggles at once. Expand only the
   capability the user is working on.
4. **Never dead-end.** Every capability can be skipped, and the skip explains
   exactly what will be unavailable (`UNAVAILABLE`, never fake data).
5. **Chat first, then data.** Chat is the home surface; configure AI before the
   data tour so the first message works.
6. **Test before finish.** A readiness check exercises the chosen AI and enabled
   sources and reports real failures, not optimistic labels.
7. **Advanced/legal choices are collapsed.** Community/scraper sources live in
   an "Advanced" accordion, off by default, with an explicit acknowledgement.

### 2.2 Screen map

```
Boot loader (existing, z-40)
  └─ Onboarding (z-200) ─ 6 splash-style steps + handoff
       1. Welcome & mode          (one choice + bundle)
       2. Look & feel             (theme presets + saved themes, applied live)
       3. AI & Chat               (one choice + test)
       4. Included data           (open sources, flat toggles, no accordions)
       5. Connect providers       (live prices choice + provider cards)
       6. Review & readiness      (status table + Run check + Finish)
  └─ App (loader finishes behind the overlay)
```

Shared frame: **a two-column setup card** (max 1060px, `max-h` of the viewport
with a `100vh` fallback and a `100dvh` refinement). The left rail (300px, hidden
below `md`) holds the wordmark and a numbered/iconed vertical stepper (active
step filled with `--primary`, completed steps checked, future steps muted). Once
the risk notice is acknowledged **every step is clickable across the whole row**
(disc + title), so the rail is a second way to navigate; the current step is
disabled and marked `aria-current="step"`. The right panel holds the step title
(22px serif), one-line description (hidden on very short windows), the scrolling
step body, and a pinned `shrink-0` footer with `Skip for now` / `Back` on the left
and `Continue` / `Finish setup` on the right. The rail and the step body are
independent scroll areas (`min-h-0`), so a short window scrolls instead of
clipping the footer. Below `md` the rail collapses into a brand bar plus a 3px
progress line. The app shell (sidebar included) is fully covered; `z-[200]`
clears the shell's `z-50`. Escape does not close it. Focus-trapped, keyboard
accessible, reduced-motion aware. Only step 1 has `Skip for now`.

### Step 1 — Welcome & how you'll use Eclipse

**Purpose:** set the legal default and the bundle without asking a legal
question in legal language.

- Title: **Welcome to BlackEclipse**
- Body: "Let's connect your AI and data. This takes about a minute. Everything
  here can be changed later in Settings."
- Two mode cards:
  - **Personal / research** — "For your own analysis. You can enable free
    community sources, with their terms shown."
  - **Commercial / product** — "For work you charge for. We'll only enable
    open-licensed data and ask you to connect your own licensed providers."
- A third, quieter choice: **Not sure yet — use the safe defaults** (Personal
  mode, open data only, no community sources).
- Fine print: "Data-source labels are an informational snapshot, not legal
  advice. Verify each provider's current terms."
- `Skip for now` → safe defaults (open data only, AI `mock`), then a
  confirmation screen that says: "Chat is off and live prices are off. You can
  turn them on in Settings or run setup again."

**Why this order:** the mode changes every default that follows. Asking it once
avoids re-asking inside each capability.

### Step 2 — Look & feel (theme)

**Purpose:** let the user choose the app's appearance before they ever see the
shell, so the first screenshot of Eclipse is theirs, not the shipped default.

- Shows `THEME_PRESETS` (all shipped dark and light looks) as preview tiles, plus
  any themes the user already saved on this install.
- Selecting a tile applies the theme **immediately** — the wizard itself
  recolors — and persists it app-wide through the theme store
  (`applyPreset` / `applyUserPreset`), the same path Settings › Customization
  uses. The preset's ChatBot backdrop and Aurora glow are applied with it.
- A line states the current scheme, and points to Settings › Customization for
  fine-tuning. The choice is persistent even if the user later picks `Skip for
  now` on step 1's footer.
- Nothing is required here: the current theme stays active if the user does not
  touch a tile, so the step cannot dead-end.

### Step 3 — AI & Chat

**Purpose:** make the first chat message work, or say honestly that it won't.

- Detect existing config first: if an LLM key/base URL is already present, show
  **"Connected — {provider} / {model}"** with the real endpoint, a model picker
  (the saved provider's discovered models), **Test connection** (a real
  `/api/agent/test` chat probe with latency/failure shown honestly) and
  **Change**.
- Three cards:
  1. **Local (recommended if installed)** — Ollama / LM Studio. Detects local
     servers and models; if none, shows install steps and a base-URL field.
  2. **Use my API key** — provider grid from `LLM_PRESETS`. Reveals masked key,
     `Test key`, and model picker. Saves via the existing vault.
  3. **Custom OpenAI-compatible endpoint** — base URL, optional key, model,
     optional context window.
- After Connect succeeds, the success view **stays open** until **Use this
  connection** is pressed. That view is where the operator picks the model, so
  it is never collapsed before they have seen it. **Set up later** is only
  offered when nothing is already connected, so it cannot wipe a working key.
- Live status line: **"Chat will use: Ollama · llama3.1"** or **"Chat will be
  unavailable (mock mode)."**
- `Set this up later` is allowed; the app then runs `mock` and the final review
  says so. Never claim a provider is connected without a real test or saved key.

### Step 4 — Included data (flat, no accordions)

**Purpose:** show exactly what ships with Eclipse and let the user turn any
included source off. There are no expand/collapse rows and no dropdowns.

- Bundle chips at the top (Research / Personal trader / Commercial / Crypto /
  Custom) apply a starting point only.
- Four flat cards: **Company research**, **Macro & economics**, **World events**,
  **News headlines**. Each card lists its source rows with a label, commercial
  badge, note and an accessible switch.
- A count line ("N included sources enabled") and a note that live prices,
  crypto and search are configured on the next step.
- A short community-sources note explains that those live on the next step and
  stay off unless accepted. It does **not** hide a checkbox in an accordion.

| Capability | Included (no account) | Default |
|---|---|---|
| **Company research** | SEC EDGAR | ON |
| **Macro & economics** | FRED public CSV, BEA, BLS, World Bank, Eurostat/ECB | ON |
| **World events** | GDELT, USGS, NASA FIRMS (verify) | ON |
| **News headlines** | Official regulator/central-bank RSS | ON |

### Step 5 — Connect providers (flat, sectioned)

**Purpose:** make the one legally-sensitive decision — live prices — explicit,
then offer optional providers in plain sections. No accordions.

- Header: "Eclipse does not include exchange price data."
- **Live prices** — three radio cards:
  1. **Licensed provider** — provider cards (Polygon, Twelve Data, Alpha
     Vantage, Marketstack, Finnhub) with a key field and Save.
  2. **Community sources** — one acknowledgement checkbox, then flat toggles for
     TradingView / Yahoo / HTTP / Browser / Scrapling. Personal use only.
  3. **Skip for now** — charts show `UNAVAILABLE`; everything else works.
- **Crypto (optional)** — CoinMarketCap and CoinGecko paid provider cards; a
  separate acknowledgement + flat toggles for Binance/Kraken/CoinGecko Demo.
- **Research search (optional)** — Tavily/Brave/Exa/Serper cards; a separate
  acknowledgement + toggle for the keyless DuckDuckGo source.
- **More macro & company** — Trading Economics, FMP, Finnhub cards.
- Every provider card has `Connected` status, a masked key field, Save and a
  Terms link. Keys are saved through the existing encrypted vault.

Acknowledgements are **local to their section** (prices, crypto, search), never
a hidden cross-step flag.

### Step 6 — Review & readiness

**Purpose:** prove the app will work before the user lands in it.

- A capability checklist, not a vendor list:

| Capability | Status | Action |
|---|---|---|
| Chat | Ready · `openai/gpt-4o-mini` | `Change` |
| Company research | Ready · SEC EDGAR | `Change` |
| Macro | Ready · FRED/BEA/BLS/World Bank | `Change` |
| News & search | Ready · keyless fallback | `Add key` |
| Live prices | Skipped — will show UNAVAILABLE | `Connect` |
| Crypto | Off | `Connect` |
| World events | Ready · GDELT/USGS | `Change` |

- **`Run a quick check`** calls `POST /api/setup/check` and tests the chosen AI
  and enabled sources. It reports the real result per item: `ok`, `failed`,
  `rate-limited`, `unavailable`, with the provider's message and latency.
- Warnings are specific:
  - "Commercial mode with no licensed price provider. Charts will be
    UNAVAILABLE."
  - "AI test failed: 401 from the provider. Chat will not work until this is
    fixed."
- `Finish setup` is enabled unless the config write itself fails. Warnings never
  block the user; they are recorded and shown again in Settings.
- `Back` returns to the relevant step.

### Step 7 — Handoff

- POST the choices, write `onboarding_v1` + `data_source_prefs_v1`, invalidate
  the settings cache, then fade out. A short `Saved. Warming up…` state uses the
  same `DotGrid` so it blends into the boot loader.
- Seed the starter dashboard from step 1's bundle/profile (reuse
  `DASHBOARD_TEMPLATES`) so the first screen is not empty.
- Optional first-run tips card (dismissible, only once): "Try: ask about a
  ticker · open Macro · connect prices later." No marketing carousel.
- If the user skipped AI: land in Chat with an honest banner and a
  `Connect a model` button, not a fake assistant.

### 2.3 What works with each setup (set expectations up front)

| Capability | Safe defaults (no keys) | + AI key/local | + licensed market key | + community (personal) |
|---|---|---|---|---|
| Chat / research agent | ✗ (mock) | ✓ | ✓ | ✓ |
| Company filings / fundamentals | ✓ (SEC) | ✓ | ✓ | ✓ |
| US / global macro | ✓ | ✓ | ✓ | ✓ |
| World events | ✓ | ✓ | ✓ | ✓ |
| News headlines | ✓ (keyless fallback) | ✓ | ✓ | ✓ |
| Research web search | ✓ (keyless, best effort) | ✓ | ✓ | ✓ with search key |
| Live quotes / charts / screener | ✗ UNAVAILABLE | ✗ UNAVAILABLE | ✓ | ✓ |
| Crypto prices | ✗ UNAVAILABLE | ✗ UNAVAILABLE | ✓ (if crypto provider) | ✓ |
| Portfolio / journal | local only | ✓ | ✓ | ✓ |

This table (in friendlier words) belongs in the UI, not just the docs. It is how
a new user understands why a page says `UNAVAILABLE`.

### 2.4 Bundles (defaults, not locks)

- **Research (no keys, recommended default):** included open data ON; live
  prices skipped; community OFF; AI chosen in step 2.
- **Personal trader:** included open data ON; community sources ON only after
  acknowledgement; AI chosen in step 2.
- **Commercial:** included open data ON; community OFF; live prices require a
  licensed key.
- **Crypto:** open data + CoinMarketCap/CoinGecko paid; community OFF.
- **Custom:** nothing pre-selected.

A bundle writes ordinary source prefs. It is never a hidden second config.

### 2.5 Definition of "starts without issues"

The first session passes when all of these are true:

1. The app renders the shell with no console errors.
2. Chat either answers, or shows a clear "model not configured" banner.
3. Every market page shows data or a labeled `UNAVAILABLE` state (no infinite
   spinner, no blank panel, no fabricated value).
4. Settings shows the same sources that onboarding enabled (no drift).
5. `Run a quick check` reports the same statuses the user saw in Review.
6. Re-running setup from Settings reproduces the same configuration.
7. The onboarding overlay never reappears on a later launch unless requested.

---

## 3. Commercial-use matrix (informational snapshot, 2026-11-09)

> Not legal advice. Each row links to the upstream terms; the onboarding UI
> must display the link and the "verify" language. If terms change, update
> `src/lib/setup/source-catalog.ts` only.

### Keyless / built-in sources

| Source | Powers | Commercial status | Included default | Commercial default | Note |
|---|---|---|---|---|---|
| TradingView scanner | Whole-market scans, covered-market quotes | **Not allowed** (no public API; redistribution/scraping restricted) | OFF — Advanced opt-in | OFF | https://www.tradingview.com/policies |
| Yahoo Finance via yfinance | US quotes/history | **Not allowed** without Yahoo's written permission | OFF — Advanced opt-in | OFF | https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html |
| HTTP / browser scraper | Fallback quotes | **Depends on the target site** | OFF — Advanced opt-in | OFF | Target-site ToS governs each fetch |
| SEC EDGAR | Filings, company facts | **Allowed** (public domain; fair-access policy) | ON | ON | https://www.sec.gov/os/webmaster-faq |
| Binance public ticker | Crypto prices | **Not allowed** for redistribution/profit without written consent | OFF — Advanced opt-in | OFF | Binance Terms, market-data clause |
| Kraken public API | Crypto prices | **Verify terms** (public data; commercial use subject to Kraken terms) | OFF — Advanced opt-in | OFF | https://www.kraken.com/legal |
| CoinGecko Demo / keyless | Crypto prices | **Attribution required; Demo is not a commercial license** | OFF — Advanced opt-in | OFF; suggest paid Basic+ | https://www.coingecko.com/en/api_terms |
| GDELT | Geopolitical events | **Allowed** (open, attribution) | ON | ON | https://www.gdeltproject.org/about.html |
| USGS | Earthquakes | **Allowed** (public domain) | ON | ON | https://www.usgs.gov/information-policies |
| NASA FIRMS | Fire/thermal events | **Verify terms** (free with attribution) | ON (verify) | ON with attribution | https://firms.modaps.eosdis.nasa.gov/ |
| FRED (keyless CSV) | Macro series | **Allowed** (attribution: "Courtesy of FRED") | ON | ON | https://fred.stlouisfed.org/legal/ |
| World Bank Open Data | Macro | **Allowed** (CC BY 4.0) | ON | ON | https://datacatalog.worldbank.org/public-licenses |
| BEA / BLS public data | Macro | **Allowed** (US government public domain) | ON | ON | https://www.bea.gov/help/faq/ |
| DuckDuckGo HTML search | Research fallback | **Not for commercial redistribution** | OFF — Advanced opt-in | OFF; suggest Tavily/Brave/Exa/Serper | Provider ToS |

### Keyed providers to recommend in commercial mode

| Provider | Good for | Commercial note |
|---|---|---|
| Polygon.io | US equities, FX, aggregates | Commercial on paid plans |
| Twelve Data | Global equities/FX/crypto | Commercial on paid plans |
| Finnhub | News, earnings, quotes | Free tier personal; paid for commercial |
| Alpha Vantage | Quotes, FX, technicals | Premium tier for commercial |
| Marketstack | EOD/intraday | Paid plans for commercial |
| Financial Modeling Prep | Fundamentals | Paid plans for commercial |
| CoinMarketCap | Crypto | Basic tier advertises commercial-use rights (verify) |
| CoinGecko Basic+ | Crypto | Commercial with attribution on paid plans |

**Recommended "first" stack for a commercial user:** SEC EDGAR + open macro
(FRED/BEA/BLS/World Bank) + one licensed price provider (Polygon.io for US,
Twelve Data for global). Do **not** ship TradingView/Yahoo/Binance as
commercial defaults.

**Legacy convenience stack (existing installs only):** TradingView + yfinance +
crypto + SEC + open macro. This is what the app does today, but it is **not**
what onboarding enables by default. Onboarding uses the safe set, and the
legacy sources are an off-by-default "Advanced / community" opt-in. Existing
installs keep their behavior until the user re-runs setup or changes a toggle,
so the migration never silently breaks a working instance.

### 3.1 Legal model: what "built-in" can safely mean

There are two different things people call "built-in API":

1. **Shipping integration code** (a client for a provider's documented API).
   The code is MIT-licensed; this is generally fine.
2. **Shipping or proxying the data itself** (a bundled key, a hosted backend, a
   scraper). This is governed by the provider's data license/ToS.

Risk goes up sharply when any of these is true:
- you bundle **one key shared by all users**,
- **your** server fetches and re-serves the data,
- the product is **paid** or ad-supported,
- the source is **scraped** and its ToS forbids automated access/redistribution.

For a **local, self-hosted** app each user's machine calls the provider
directly, so the project distributes software rather than data. That reduces
redistribution exposure, but it does **not** legalize a scraper that the
software ships enabled. For a **hosted/SaaS** version you are re-serving data to
customers and need commercial redistribution rights (or each customer must use
their own provider account, subject to that provider's multi-user terms).

**Safe built-in (out-of-box, no key, redistributable with the stated condition):**

| Source | Condition |
|---|---|
| SEC EDGAR | Public domain; set a real `User-Agent`, respect the fair-access rate limit |
| FRED | Free; display "Courtesy of FRED" |
| BEA, BLS, Census, US Treasury, EIA | US government public domain (check each agency's attribution request) |
| World Bank Open Data | CC BY 4.0 attribution |
| Eurostat / ECB data | Free reuse with attribution |
| GDELT | Open; attribution |
| USGS | Public domain |
| NASA FIRMS | Free with attribution; verify current commercial terms |
| Wikidata | CC0: no attribution required, safe to reuse |
| Wikipedia article text | CC BY-SA: attribution **and share-alike** — do not blend into a proprietary database without review |

**Never ship enabled as a "built-in" for a product you charge for:**
- TradingView scanner (no public API; scraping/redistribution restricted)
- Yahoo Finance / yfinance (commercial use requires Yahoo's permission)
- Binance and other exchanges' market data (commercial redistribution needs
  written consent)
- CoinGecko Demo/keyless (attribution only; **not** a commercial license)
- DuckDuckGo / Google / Bing HTML scraping
- Any "unknown" site scraper

**Bring-your-own-key only (never bundle your key):**
Polygon.io, Finnhub, Twelve Data, Alpha Vantage, Marketstack, FMP,
CoinMarketCap, CoinGecko paid, Tavily/Brave/Exa/Serper, and every LLM provider.
Free tiers are usually personal/evaluation; commercial use and redistribution
need a paid or enterprise agreement.

**Product rule:** *built-in = open data*. Everything else is "Connect your own
account", shown as `Requires your key` with a signup link. No shared key, no
server-side proxy, no silent scraping. If you later sell a hosted version,
obtain commercial redistribution rights **before** enabling any non-open
source.

---

## 4. Architecture

### 4.1 New modules

```
src/lib/setup/
  source-catalog.ts      # static catalog: id, label, category, powers, badge,
                         # license status, terms URL, attribution text
  capabilities.ts        # maps source ids -> user-facing capabilities
                         # (chat/company/macro/events/prices/crypto/search)
  bundles.ts             # Research / Personal trader / Commercial / Crypto
                         # bundle presets (apply ordinary source prefs)
  defaults.ts            # safe included defaults vs advanced opt-in
  state.ts               # read/write onboarding state + source prefs (server)
  types.ts               # shared types (client-safe)
  existing-install.ts    # upgrade heuristic (see 4.3)
  check.ts               # readiness checks (AI + enabled sources), server-only
src/lib/data-source-preferences.ts   # SERVER enforcement helper (no React)
src/components/setup/
  onboarding-gate.tsx    # mount point; fetches state, renders overlay or null
  onboarding-shell.tsx   # full-screen frame, step machine, footer, focus trap
  steps/welcome-step.tsx  # mode + bundle
  steps/theme-step.tsx    # shipped + saved theme presets, applied live
  steps/ai-step.tsx       # local / own key / custom / later
  steps/included-step.tsx # flat open-data cards + switches (no accordions)
  steps/providers-step.tsx# live-prices choice + section-local provider cards
  steps/review-step.tsx   # capability checklist + Run check + finish
  parts/capability-row.tsx
  parts/source-row.tsx
  parts/license-badge.tsx
  parts/provider-card.tsx
  parts/summary-strip.tsx
  parts/ready-table.tsx
src/app/api/setup/
  route.ts               # GET state, POST complete
  check/route.ts         # POST readiness check (rate-limited, real requests)
```

Draft state (uncommitted choices) lives in `sessionStorage` so a refresh does
not lose the wizard; keys are saved to the vault immediately when tested, not
held in the wizard until Finish. `onboarding_v1` is written only on Finish or
Skip.

Client-safe split: `source-catalog.ts`, `defaults.ts`, `types.ts` must not
import Prisma. `state.ts` is server-only and imports `@/lib/settings` +
`@/lib/provider-keys`.

### 4.2 Persistence (SystemConfig)

| Key | Shape | Purpose |
|---|---|---|
| `onboarding_v1` | `{ completed, completedAt, mode, version, aiSkipped, acceptedRestrictedSources }` | New-user gate flag + audit of explicit choices |
| `data_source_prefs_v1` | `{ [sourceId]: { enabled: boolean, reason: 'default'\|'user'\|'commercial-blocked' } }` | Enforced source toggles |
| Existing `llm_config` | unchanged | AI provider choice |
| Existing `provider_keys_v1` | unchanged | Keys saved via vault |

`onboarding_v1` is instance-level. If per-user onboarding is ever wanted, add
`userId` later; do not silently scope it now.

### 4.3 New-user detection (critical)

- `GET /api/setup` returns `{ completed: boolean, isNewInstall: boolean, ... }`.
- If `onboarding_v1` exists → `completed` from it.
- If absent → decide **new install vs upgraded install**:
  - New install: no prior operator data — `systemConfig` has 0 rows (or only
    the DB was just created), no provider keys, no auth users, no domains/
    dashboards.
  - Existing install: any of those exist. Then the first GET writes
    `onboarding_v1 = { completed: true, migrated: true }` and never shows the
    gate. This prevents every current user from being ambushed by onboarding
    after an update.
- Add a "Run setup again" button in Settings → General (and a `/setup` deep link)
  that forces the gate even when completed.

### 4.4 API contract

`GET /api/setup`

```ts
{
  completed: boolean;
  isNewInstall: boolean;
  mode: "personal" | "commercial";
  bundles: Array<{ id: "research"|"personal-trader"|"commercial"|"crypto"|"custom";
                   label: string; description: string; sourceDefaults: Record<string, boolean> }>;
  capabilities: Array<{
    id: "chat"|"company"|"macro"|"events"|"prices"|"news"|"search"|"crypto";
    label: string; status: "ready"|"needs-key"|"off"|"unavailable";
    includedSources: string[]; connectedSources: string[]; advancedSources: string[];
  }>;
  sources: Array<{
    id: string; label: string; category: "market"|"crypto"|"news"|"macro"|"research"|"events";
    powers: string[]; commercial: "ok"|"attribution"|"personal-only"|"verify";
    termsUrl?: string; enabled: boolean; editable: boolean;
    tier: "included"|"connected"|"advanced";
  }>;
  ai: { provider: string; model: string; hasKey: boolean; configured: boolean };
  warnings: string[];
}
```

`POST /api/setup`

```ts
{
  mode: "personal" | "commercial";
  bundleId?: "research" | "personal-trader" | "commercial" | "crypto" | "custom";
  sources: Record<string, boolean>;
  acceptedRestrictedSources?: string[];
  ai?:
    | { kind: "local"; presetId: "ollama" | "lmstudio"; baseUrl?: string; model?: string }
    | { kind: "key"; presetId: string; apiKey: string; model?: string }
    | { kind: "custom"; baseUrl: string; apiKey?: string; model: string; contextWindow?: number }
    | { kind: "skip" };
  extraKeys?: Array<{ envVar: string; value: string }>;
}
```

Server behavior:
1. `guardMutation(request, "setup")`.
2. Validate mode/source ids against the static catalog; a source in the
   `advanced` tier can only be enabled when it is also in
   `acceptedRestrictedSources`.
3. In commercial mode, reject enabling `personal-only` sources unless listed in
   `acceptedRestrictedSources`.
4. Persist `data_source_prefs_v1`; write `onboarding_v1.completed = true`.
5. Save AI config with the existing functions (`setLLMConfig`), keys with
   `saveProviderKey` (never log values).
6. Return the effective state so the UI can show real warnings.
7. Invalidate the settings cache (`settingsCache` in `src/app/api/settings/route.ts`)
   so the app reads the new config immediately.

`POST /api/setup/check`

```ts
// request
{ ai?: boolean; sources?: string[]; keys?: string[] }
// response
{
  ok: boolean;
  items: Array<{
    id: string; label: string; kind: "ai"|"source"|"key";
    status: "ok"|"failed"|"rate-limited"|"unavailable"|"skipped";
    message: string; latencyMs?: number; httpStatus?: number;
  }>;
}
```

The check reuses the real test endpoints (`testProviderKey` behavior) and the
source health probes. It is rate-limited, times out each item, and never reports
`ok` because a key merely exists.

### 4.5 Enforcement (the part that makes toggles real)

Add `src/lib/data-source-preferences.ts`:

```ts
export type SourceId = "tradingview" | "yfinance" | "http" | "browser"
  | "scrapling" | "crypto" | "sec" | "coingecko" | "kraken" | "binance"
  | "gdelt" | "usgs" | "firms" | "fred" | "worldbank" | "bea" | "bls";

export async function isSourceEnabled(id: SourceId): Promise<boolean>;
export async function getEnabledSources(): Promise<Record<SourceId, boolean>>;
```

Cached in-process for ~30 s (same pattern as settings cache), defaulting to the
current behavior if the key is absent (so zero-config is unchanged). Wire it:

- `src/lib/scraper/fetchers/index.ts`: gate `tradingview`, `yfinance`, `http`,
  `browser`, `scrapling`, `crypto` before each strategy. If disabled, skip to
  the next; if all are disabled, return an honest `blocked`/`empty` result.
- `src/lib/connectors/providers/crypto.ts` / `registerBuiltinConnectors()`:
  skip registration when `binance` is disabled.
- `src/lib/economics/sources.ts`: gate `fred`/`worldbank`/`bea`/`bls` fetches,
  or filter the source list returned to the UI.
- `src/lib/investor-eye/*`: gate `gdelt`, `usgs`, `firms`.
- `src/lib/scraper/fetchers/sec.ts` (or the SEC fetcher): gate `sec`.
- CoinGecko/Kraken adapters: gate their ids.

**Honesty rule:** a disabled source must produce the existing `UNAVAILABLE`
state or be omitted from the source list — never a fabricated value, and never
a silent fallback to a source the user disabled.

### 4.6 Mounting / boot interaction

```tsx
// src/app/layout.tsx
<GlobalLoader />
<OnboardingGate />   // client component; renders null unless needed
{children}

// src/app/page.tsx (the app shell)
if ((await onboardingGateState()) === "incomplete") return <SetupPlaceholder />;
<StockChat />
```

- `OnboardingGate` fetches `/api/setup` on mount.
- While `completed` is unknown, render nothing (the normal loader shows).
- If incomplete, render the overlay at `z-50` with `pointer-events-auto`; it
  covers the loader and blocks interaction with the shell.
- **A brand-new install does NOT mount the app shell behind the wizard.**
  `page.tsx` checks the first-run flag server-side (`lib/setup/gate.ts`) and
  renders a placeholder instead of `StockChat`, so the wizard owns the whole
  connection budget. This is not cosmetic: the shell opens long-lived SSE
  streams (`/api/ui/stream`, `/api/sdk/app/stream`) that hold one of the
  browser's six HTTP/1.1 connections per origin for the life of the page, plus
  a boot burst of API calls. With the shell mounted behind the wizard, the
  wizard's own `POST /api/setup` queued behind those streams and was never
  sent — the "Saving…" that never resolved for new users. A warm install has
  far less boot traffic, which is why it only reproduced on first run.
- The decision is conservative: `onboardingGateState()` answers `incomplete`
  only for a definite new install, `complete` for a recorded/migrated one, and
  `unknown` (render the app) when the read fails. A DB problem must never hide
  the product behind a wizard.
- The layout asks the same `onboardingGateState()` (memoized per request with
  React `cache`) and passes the answer to both `OnboardingGate` (it force-shows
  the wizard for that response, even if the follow-up read reports a completed
  install) and `AppActivity`. Server-side, so SSR and hydration agree.
- Every app-wide poller/stream/update-check is inside `AppActivity`, which
  renders nothing while setup is pending: notifications, the desktop session
  heartbeat, update checks, the global loader's warm-ups, sound/tts providers
  and the shell guards issue no request during setup. The wizard is the only
  client traffic (verified: `GET theme`, `GET setup`, `GET provider-keys`).
- A forced re-run still has the shell mounted, so the wizard also announces
  itself (`lib/setup/overlay.ts`). Stream owners (`useAppShellSync`,
  `SavedPagesNav`, dashboard and pages `/api/ui/stream` consumers) close their
  SSE and stop polling while it is open, then reconnect. Same headroom, both
  paths.
- The native "Where your data lives" notice is kept clear of the wizard: the
  shell source now shows it top-right, never over the footer's primary button,
  and skips it while the setup dialog is present; `OnboardingShell` also
  contains a shim (move to top-right + `pointer-events: none` except the
  button) so installs built before that change cannot lose the Finish click.
- Every finish failure is human text (`describeSetupWriteError` maps a busy or
  read-only database to an actionable sentence), logged, and accompanied by a
  "Copy diagnostics" action with no secrets in it.
- If the state read fails while the shell is withheld, the gate shows a small
  "Setup state unavailable / Reload" card instead of a blank screen. When the
  shell is mounted, a failed read still renders nothing (never block the app).
- The forced re-run path (`?setup=1`, Settings → Run setup again) is unchanged:
  onboarding is already complete, so the shell mounts and the gate overlays it.
- On finish, the wizard reloads `/`; this time the flag is complete, so the
  shell mounts and warms normally.
- Do **not** make the loader wait for onboarding; they are independent.

---

## 5. UI specification (matches the boot page)

- Background: brand grid + soft top glow (unchanged).
- Panel: `.glass-card-strong`, `rounded-2xl`, `border-white/12`, two columns
  (`300px | 1fr`), internal scroll in the content column only, pinned footer.
- Type: `font-serif` 22px step title, 13px description, 11.5–13px body, 11px
  metadata. No 8–9px copy — the previous dense card was hard to read.
- Icons: `@/lib/icons` only (no new icon dependency).
- Toggles: a real accessible switch (`role="switch"`, `aria-checked`) built
  from existing primitives; no native checkbox styling.
- Badges: pill, 9–10px uppercase, colors from the existing tone vocabulary
  (emerald = ok, amber = attribution, red = personal-only, muted = verify).
- Motion: 160–220ms fade/slide between steps; honor
  `prefers-reduced-motion: reduce` (instant).
- Size: panel scrolls internally; footer stays pinned; works at 360px width.
- No confetti, no marketing hero, no fake progress. The `DotGrid` is the only
  animation.

---

## 6. Implementation phases

### Phase 1 — Catalog, capabilities, state (no UI)
1. Add `src/lib/setup/types.ts`, `source-catalog.ts`, `capabilities.ts`,
   `bundles.ts`, `defaults.ts`.
2. Add `src/lib/setup/state.ts` (`readOnboardingState`, `completeOnboarding`,
   `getSourcePreferences`, `setSourcePreferences`).
3. Add `src/lib/setup/existing-install.ts` heuristic.
4. Add `src/app/api/setup/route.ts` (GET/POST) with `guardMutation`.
5. Add `src/lib/setup/check.ts` + `src/app/api/setup/check/route.ts`
   (rate-limited readiness tests).
6. Unit tests: capability mapping covers every source; bundle defaults are
   legal-safe; commercial filtering; existing-install migration; POST
   validation; check items never report `ok` without a real probe.

**Exit:** `GET /api/setup` on a fresh DB returns `completed:false` with
capabilities/bundles; on a DB with data returns `completed:true, migrated:true`;
POST round-trips; check reports real statuses.

### Phase 2 — Enforcement
7. Add `src/lib/data-source-preferences.ts` (cached; when the key is absent,
   preserve legacy behavior so existing installs do not change).
8. Gate the scraper chain, crypto connector, economics sources, investor-eye
   sources, SEC. Add tests: disabled source is skipped; all-disabled returns an
   honest failure; an advanced source stays off unless accepted; unset prefs
   preserve legacy behavior.

**Exit:** toggling a source in `data_source_prefs_v1` demonstrably changes which
fetcher runs, with no fabricated data.

### Phase 3 — Onboarding UI
9. Build `OnboardingGate` + `OnboardingShell` + the six splash-style steps:
   welcome/mode, look & feel (theme), AI, data/capabilities (with the three-way
   live-prices decision and the collapsed Advanced section), review/readiness.
10. Build the capability-row and readiness-table components; reuse
    `ProviderKeyEditor`, `ModelPicker`, and the key-status logic (extract a
    shared hook if needed).
11. Persist wizard draft in `sessionStorage`; save keys immediately on test;
    commit only on Finish/Skip.
12. Mount in `src/app/layout.tsx`; seed the starter dashboard from the bundle.
13. Add "Run setup again" in `general-settings.tsx` and a `/setup` entry point.

**Exit:** a new user can finish with zero keys and reach a shell where Chat and
every capability show an honest ready/off/unavailable state; the Review table
matches what Run check reports; existing install never sees the gate.

### Phase 4 — Polish + tests
14. Keyboard/focus trap, reduced motion, 360px layout, screen-reader labels.
15. Error states for every API failure (save failed, test failed, network,
    skipped key, commercial-without-price-provider).
16. Verify the section 2.5 first-session criteria end-to-end.
17. Run `npm run test` and `npx tsc --noEmit`.

---

## 7. Failure and edge cases

| Case | Behavior |
|---|---|
| `/api/setup` fails | Gate renders nothing; app loads normally. Setup is retryable from Settings. Never block the app on a failed setup read. When the shell was withheld for a first run (`page.tsx` saw an incomplete flag) and the read then fails, the gate shows a "Setup state unavailable / Reload" card instead of leaving the placeholder alone. |
| Finish request stalls (locked DB, server restart, queued connection) | The POST has a 30 s deadline instead of waiting forever. On timeout the wizard re-reads `/api/setup`: a recorded `completed:true` enters the app (the save landed; only the reply was lost), otherwise it shows an error and stays retryable. The button can never sit on "Saving…" indefinitely. |
| Database busy or read-only while finishing | `completeOnboarding` retries each idempotent upsert (250/500 ms) and the SQLite adapter waits up to 5 s for the write lock; a persistent failure becomes a plain-language sentence (close another Eclipse window / check the data folder is writable) plus "Copy diagnostics", never a raw code. |
| First run on a cold/slow machine | Market warm-up, ingests, schedules and backups are deferred until setup completes; app-wide pollers and update checks do not mount. The Python runtime warm-up still runs during setup on purpose: its first import from a cold payload measured over three minutes, so warming it while the user reads the wizard is what keeps the first chat after setup responsive. |
| The native first-run data notice appears during setup | It is shown top-right, skips while the setup dialog is present, and (for older builds) the wizard moves it to the top-right with `pointer-events: none` except its button — it can never sit over "Finish setup". |
| AI/key step fails while finishing | Setup is recorded first and the AI failure is returned as a warning: the Review step explains it, the user can go Back to fix it, or enter Eclipse and fix it in Settings. A provider-vault error must not undo (or block) the completed setup. |
| DB locked during boot | GET retries once; on failure, skip onboarding this session (log once). |
| Key save succeeds, test fails | Keep the saved key, show the provider's real error, allow `Continue` with a warning. Do not claim success. |
| User skips AI | Run `mock`; review says "Chat unavailable until a provider is configured." |
| Commercial with no price source | Allow finish, but show a persistent warning + empty states become `UNAVAILABLE`. |
| User enables a restricted source in commercial mode | Require the explicit acknowledgement checkbox; record it in `acceptedRestrictedSources`. |
| Daily driver already configured | `existing-install` migration marks complete before first paint. |
| Multiple operators (`HARNESS_AUTH=required`) | Setup is instance-level; document that it runs once per instance, not per account. |
| User clears config | Re-run from Settings; do not auto-show again. |

---

## 8. Test plan

- `gate.test.ts`: the server-side first-run decision (recorded, unfinished,
  corrupt flag, new vs upgraded probe, unreadable DB → render the app).
- `route.test.ts`: finishing records the setup even when the AI/key step fails
  (warning), a skipped AI needs no provider, and a stale risk ack or an
  unacknowledged advanced source is still refused before any write.
- `state.test.ts`: busy-lock retries are idempotent and bounded; a busy or
  read-only database becomes actionable text, never a raw Prisma code.
- `npm run smoke:onboarding` walks the wizard without writing. With
  `SMOKE_FINISH=1` (throwaway `BE_DATA_DIR` only) it clicks Finish and asserts
  the wizard unmounts and the app mounts.
- A first-run check (by hand, or in CI) asserts the wizard holds **zero** SSE
  streams and that no app-wide poller runs while it is open.
- `source-catalog.test.ts`: every source has a tier, badge, terms URL where
  required, and a default; no duplicates.
- `capabilities.test.ts`: every source maps to exactly one capability; every
  capability has at least one Included or Connect path; no advanced source is a
  default.
- `bundles.test.ts`: Research/Commercial bundles never enable advanced or
  `personal-only` sources; Personal-trader only enables them through the
  acknowledgement flag; Crypto bundle never enables an exchange source.
- `defaults.test.ts`: safe defaults = open data only; commercial never enables
  `personal-only` without acknowledgement.
- `state.test.ts`: unset prefs = legacy behavior; round-trip; malformed JSON
  falls back safely (no crash).
- `existing-install.test.ts`: empty DB = new; any prior config = migrated.
- `api/setup/route.test.ts`: guard, validation, advanced/commercial rejection,
  success payload, cache invalidation.
- `api/setup/check/route.test.ts`: rate limit; per-item timeout; a stored key
  with a failing probe is `failed`, never `ok`; skipped items stay `skipped`.
- `data-source-preferences.test.ts`: disabling `tradingview` skips it; disabling
  all returns an honest failure; unset keeps the legacy chain; an advanced
  source stays off until accepted.
- Component smoke: renders step 1, advances, and blocks finish until required
  fields are valid (if a DOM test harness exists; otherwise extract the step
  machine to a pure reducer and test that).
- First-session test (manual + one automated smoke where possible): section 2.5
  criteria — no console errors, Chat ready or honest banner, every market page
  data or `UNAVAILABLE`, Settings matches onboarding, re-run reproduces.

---

## 9. Docs / changelog obligations

- This plan: `docs/onboarding-plan.md`.
- After implementation: add an "Onboarding" row to `docs/NAVIGATION.md`; update
  `docs/QUICKSTART.md` ("first run asks these questions"); update
  `docs/ARCHITECTURE.md` / `docs/DATA-FLOW.md` only if the source-preferences
  enforcement changes the data path (it does — document it).
- Append to `docs/CHANGELOG.md` on every phase.
- If a source-preference contract is added, note it in `docs/DECISIONS.md` (app
  ADR) because it changes how the scraper chain selects sources.

---

## 10. Open decisions (recommended answers)

1. **Instance vs per-user onboarding?** → Instance-level for v1. Local installs
   are effectively single-operator; per-user adds a user id to the config and a
   second gate after login.
2. **Should commercial mode hard-block restricted sources?** → No. Soft-block
   with an explicit acknowledgement. Hard-blocking would make the UI unusable
   for users who already hold a license, and we cannot verify licenses.
3. **Do we add a new dependency for toggles/stepper?** → No. Build from
   `Button`, `Label`, `Input`, and `cn`; the repo already avoids UI deps.
4. **Should "Skip setup" exist?** → Yes, only on step 1 and only in personal
   mode, so a user can get to the app; it still writes `completed:true` with
   defaults and is re-runnable from Settings.
5. **Where does the "Run setup again" button live?** → Settings → General,
   next to the existing reset/advanced actions; plus a `/setup` deep link.

---

## 11. Effort estimate

| Phase | Scope | Rough effort |
|---|---|---|
| 1 | Catalog, capabilities, bundles, state, routes, check, migration, tests | 1–1.5 days |
| 2 | Enforcement across scraper/connectors/econ/investor-eye + tests | 1–1.5 days |
| 3 | Gate, splash shell, 6 flat steps, provider/key reuse, mounting | 2-2.5 days |
| 4 | Polish, error states, accessibility, full test pass | 0.5–1 day |

Total: ~4.5–6.5 engineering days. Phase 1+2 must land before the UI so the
toggles are real on day one.

---

## 12. Final recommendation

Ship in this order:
1. **Make the liability-safe set the true built-in set**: SEC EDGAR, open macro
   (FRED/BEA/BLS/World Bank), and open event feeds (GDELT/USGS, FIRMS after
   verification). These are the ones you can ship enabled to every user.
2. **Move TradingView, Yahoo, Binance, CoinGecko Demo, and scrapers out of the
   default built-in set.** Either remove them or make them an explicit opt-in
   "community source (use at your own risk)" that is off by default. Do not
   describe them as features you provide.
3. **Make every market-price source bring-your-own-key.** The project ships the
   connector, the user owns the provider account and accepts its terms. Never
   bundle a shared key and never proxy vendor data through the project's server.
4. **Let the user bring their own AI provider** (key or custom endpoint) on the
   same screen where they choose local vs hosted.
5. **Organize the screen by capability, not vendor**: Chat, Company research,
   Macro, Events, Prices, News & search, Crypto. Vendors appear only inside the
   capability the user expands.
6. **Always end with a readiness check** so the user knows what will work before
   they leave onboarding. A skipped capability is fine; a surprise failure is
   not.
7. **Enforce every toggle** in the fetch layer before shipping the UI, so a
   disabled source cannot be used silently.
8. **Ask a lawyer before charging money** for a build that enables non-open
   sources. The labels in this plan are engineering guidance, not a legal
   opinion.

---

## 13. Restarting onboarding and starting over

Three different user intents must not share one destructive button. Each scope
is explicit about what it removes and what it keeps.

| Scope | Use when | Removes | Keeps |
|---|---|---|---|
| **Re-run onboarding** | "I want to redo the setup questions" | `onboarding_v1` only (the wizard opens prefilled from current prefs) | Keys, AI config, chats, portfolio, journal, research, everything else |
| **Reset setup & connections** | "I want a clean configuration but keep my data" | `onboarding_v1`, `data_source_prefs_v1`, `llm_config`, provider-key vault, MCP server config | Chats, portfolio, journal, research, artifacts, skills |
| **Factory reset (start from scratch)** | "Clear Eclipse and begin as a new user" | All three databases (`jdds.db`, `agent-memory.db`, `eclipse-memory.db`) + sidecars, `.black-eclipse/` runtime state, backups, connector key, MCP config, provider keys, configs, chats, portfolio, journal, research, skills, artifacts, recordings. Optionally `.env`. | Source code only |

The existing `scripts/wipe-personal-data.js` is already the low-level primitive
for the factory scope (it refuses paths outside the repo/vault roots and never
touches source). Do **not** write a second deletion implementation.

### 13.1 Settings UI

**General → Onboarding (new card)**

- Title: **Setup**
- `Run setup again` — calls the API with `scope:"onboarding"`, then opens
  `/?setup=1` (the gate treats that query as "show even if completed").
- Copy: "Opens the welcome questions again. Your API keys, chats and portfolio
  are not changed."

**Data & Storage → Danger zone (extend the existing section)**

- **Reset setup & API keys** — moderate.
  - Lists exactly what is removed/kept (the table above).
  - Requires typing `RESET`.
  - `Cancel` / `Reset setup`.
- **Factory reset — start from scratch** — destructive.
  - Lists exactly what is removed.
  - Requires typing `WIPE`.
  - Checkbox `Create a backup first` (default ON). Backup goes to
    `backups/reset-<timestamp>/` and the path is shown after the reset.
  - Checkbox `Also delete .env` (default OFF, with a warning that it removes
    host configuration and secrets).
  - Because the running Next process cannot safely delete live SQLite files on
    Windows, this does **not** delete in-process. It writes a reset request
    marker and shows:
    - "Reset scheduled. Restart Eclipse to finish."
    - the exact command: `black reset --factory` (or simply restarting with
      `black eclipse` / `npm run dev`, which consumes the marker at boot).
  - If the user is already in a terminal-only environment, the CLI path is the
    primary path; the UI is the discoverable path.

Styling follows the existing frosted-glass system; the destructive cards use
the existing rose tone and the `confirmDestructive` preference from
`general-prefs.ts`.

### 13.2 Shared reset module

```
scripts/lib/reset.js
  listScopes()                 # dry-run summary of what each scope touches
  resetOnboarding({ dataDir }) # remove onboarding_v1 only
  resetSetup({ dataDir })      # onboarding + prefs + llm_config + key vault
  requestFactoryReset(opts)    # write .black-eclipse/reset-request.json
  consumeFactoryReset(opts)    # boot-time: backup (optional) -> wipe -> remove marker
  runFactoryReset(opts)        # CLI: stop servers -> backup -> wipe -> optional start
```

Marker shape (`.black-eclipse/reset-request.json`):
```json
{ "scope": "factory", "requestedAt": "...", "backup": true,
  "includeEnv": false, "requestedBy": "settings" }
```

**Boot consumption:** `scripts/doctor.js` runs before every `black eclipse`,
`npm run dev` (`predev`), and `npm run start` (`prestart`). Add
`consumeFactoryReset()` near the top, **before** the DB guard creates or opens
`jdds.db`, so a pending reset never leaves an empty DB behind. It:
1. Reads the marker.
2. If `backup`, copies the DBs + config to `backups/reset-<timestamp>/` (the app
   is not running at this point, so the copy is consistent).
3. Runs the existing wipe script with `--confirm=WIPE` (+ `--include-env`).
4. Deletes the marker and logs one line per removed root.
5. Continues boot; the app sees no data and shows onboarding.

If the marker is malformed or outside the allowed roots, doctor logs and
ignores it — never guesses.

### 13.3 CLI

Add to `bin/black.js`:

```
black onboarding                 # re-run onboarding on next start (non-destructive)
black reset --onboarding         # remove onboarding_v1 only
black reset --setup              # onboarding + prefs + AI config + provider keys
black reset --factory            # stop servers, backup, wipe, then start
black reset --factory --no-backup
black reset --factory --include-env
black reset --factory --no-start
black reset --list               # dry run: show every path that would change
```

Rules:
- `--factory` without `--confirm=WIPE` prompts for the literal word `WIPE` in an
  interactive terminal; non-interactive shells require `--confirm=WIPE`.
- `--factory` stops production and development first (`black stop --all`),
  otherwise the delete can fail on Windows file locks.
- `black onboarding` is intentionally different from `black reset --factory`:
  it only re-asks the questions. It prints the URL and starts the app if it is
  not running.
- Exit codes: `0` success, `1` refused/failed, `2` bad usage.
- Help text and `black help` updated.

npm aliases (so users who never link the CLI still have one-liners):

```json
"reset:onboarding": "node scripts/reset.js --onboarding",
"reset:setup":      "node scripts/reset.js --setup",
"reset:factory":    "node scripts/reset.js --factory",
"reset:list":       "node scripts/reset.js --list"
```

`data:wipe` stays as the low-level primitive and is documented as the same
operation as `--factory` without the server stop/start wrapper.

### 13.4 API

`POST /api/setup/reset`

```ts
{
  scope: "onboarding" | "setup" | "factory";
  confirm: "RESET" | "WIPE";      // required for setup/factory
  backup?: boolean;                // factory, default true
  includeEnv?: boolean;            // factory, default false
}
```

Behavior:
- `guardMutation` + rate limit.
- `onboarding`: delete `onboarding_v1`; return
  `{ ok: true, reload: "/?setup=1" }`.
- `setup`: delete `onboarding_v1`, `data_source_prefs_v1`, `llm_config`, all
  provider-key vault entries; invalidate the settings cache; return
  `{ ok: true, reload: "/?setup=1" }`.
- `factory`: do **not** delete live files. Write the marker with
  `requestedBy:"settings"`; return
  `{ ok: true, scheduled: true, command: "black reset --factory" }`.
- Never echo key values or file contents. Log only scope + timestamp.
- If `HARNESS_AUTH=required`, require a valid session; a future enhancement can
  re-ask for the password.

### 13.5 Safety rules

1. **No silent reset.** Every scope names exactly what is removed and requires a
   typed confirmation (UI) or `--confirm=WIPE` (CLI).
2. **Backup before factory by default.** Print the backup path. `--no-backup`
   must be explicit.
3. **Never delete source, `node_modules`, or files outside the repo/vault
   roots.** The existing wipe guard stays authoritative.
4. **Do not delete `.env` by default.** It holds host configuration; require
   `--include-env` / the checkbox.
5. **Do not delete live SQLite files from the web process.** Schedule + consume
   at boot, or run the CLI with the server stopped.
6. **Idempotent.** Re-running a scope on a clean install is a no-op with a clear
   message.
7. **Audit line.** Append `{scope, at, backupPath}` to
   `.black-eclipse/reset.log` (no secrets). The log itself is removed by a
   later factory reset.
8. **Auth-aware.** If auth is enabled, the API requires a session; the CLI
   refuses to run `--factory` while the app is serving unless it can stop it.

### 13.6 Tests

- `reset.test.ts`: scope boundaries (onboarding keeps keys/chats; setup keeps
  chats but removes keys; factory marker contains the right flags).
- `api/setup/reset/route.test.ts`: confirmation required; factory never deletes
  in-process; onboarding/setup return the reload path; cache invalidation.
- `scripts/lib/reset.test.js`: allowed-root guard; dry run lists only owned
  paths; malformed marker ignored; backup path created; no source path matched.
- CLI smoke: `black reset --list` on a temp fixture; `--factory --no-start`
  removes only fixtures; `--confirm` required in non-interactive mode.
- Boot test: doctor consumes a marker before DB creation, wipes, and leaves the
  app in a new-install state (onboarding gate appears).

### 13.7 Docs

- `docs/QUICKSTART.md` → "Starting over" with the three scopes and commands.
- `docs/data-and-privacy.md` → reset vs wipe, backup, `.env` caveat, unencrypted
  disk caveat (already documented for wipe).
- `docs/NAVIGATION.md` → a row for reset/onboarding: `bin/black.js`,
  `scripts/lib/reset.js`, `scripts/wipe-personal-data.js`,
  `src/app/api/setup/reset/route.ts`, Settings "Danger zone".
- `docs/CHANGELOG.md` on implementation.
- `docs/DECISIONS.md` (app ADR) for the three-scope reset contract and the
  "schedule + consume at boot" decision, because it changes how the app handles
  destructive data operations.
