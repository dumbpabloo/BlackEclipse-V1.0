---
title: The Public README
status: Current
last-verified: 2026-11-26
---

# The Public README

**Who this is for:** anyone changing the public claim, the screenshots, the install
instructions, the benchmark block or the social preview — and anyone who has to
defend a sentence on them.

The repository root `README.md` is the **primary** public front door. There is also
a single-file static landing page in `site/index.html`, published to GitHub Pages by
`.github/workflows/pages.yml`; it must carry the same positioning, the same three
sections and the same repository URLs as the README. Every social post, release
note and link points at one of those two surfaces.

That makes both surfaces a marketing surface with a **factual obligation**. This
project's whole argument is that its claims are auditable, so a sentence there is
held to the same standard as a number in a report.

---

## 1. The order, and why it is that order

A stranger needs to know *what this is* before they can care *why it is
different*, and needs to see it before they trust either. So:

1. **Hero** — the banner image with the exact positioning sentence (*"Black Eclipse
   is an open-source agentic harness designed to help you automate finance."*), a
   one-line explanation, the download button and badges, all centred.
2. **What it is** — the fragmentation problem in three short paragraphs, ending on
   the honesty invariant (`UNAVAILABLE`, never a guessed figure).
3. **Features** — six groups, each one line and linked to its package or doc.
4. **See it** — three real captures side by side (Agent, Bots, Workflows). The hero
   banner is **authored brand art**, not a screenshot; the gallery is the capture
   surface.
5. **Use** — six concrete recipes (value a company, find setups, test an idea, run a
   morning brief, reconcile a book, paper-trade a mandate), plus the persona line.
6. **Needs** — requirements and "what it does not need" in one table.
7. **Install** — download, one-command script, source build, checksum; troubleshooting
   and the embed example are collapsed `<details>` so they do not bloat the page.
8. **How it works** — the plan → act → compute → verify → record loop and the three
   separations (model/Python, research/execution, declared effects).
9. **Proof** — two benchmark tiers, the "what it does not measure" note collapsed, and
   three refusals. This replaces the old "problems" and "different" tables.
10. **Data sources** — the three tiers compressed; the full table is collapsed and
    lives in docs.
11. **Documentation · roadmap and limits · open source · licensing · disclaimer** —
    the long legal and limitation blocks collapse, the operational links do not.

Two rules of thumb: a section that repeats an earlier one gets cut (that is why
there is one "How it works", not three philosophy sections), and a claim that
cannot be checked belongs in a blog post, not here. The page stays under ~380
lines (it was 444 before the rewrite); positioning, explanation, download and install
must all be visible in the first two folds.

## 2. Every claim and its evidence

If you change a claim, change this row in the same commit. If the evidence no
longer exists, the claim comes off the README.

| Claim | Evidence in the tree |
|---|---|
| "Open-source agentic harness designed to help you automate finance" | The category claim: `python/harness/` is the runtime (agents, tools, model providers, events, run persistence) — see `docs/harness/architecture.md`; automation is native (`python/harness/automation/`, `docs/harness/automation-engine.md`); the six feature groups below each map to a package. |
| The six feature groups (research, finance engine, strategy/backtesting, portfolio/journal, agents/automation, gated trading) | `python/harness/{research,market,finance_facts,quant,portfolio,automation,team,trade}/`; docs `docs/harness/search-retrieval.md`, `docs/harness/financial-analysis-engine.md`, `docs/backtesting-engine.md`, `docs/portfolio-accounting.md`, `docs/harness/automation-engine.md`, `docs/trading.md` |
| "Needs no admin, Node, Python or database" / "~141 MB core" | `scripts/desktop-package.js` (payload: `runtime/node.exe` + `scripts/vendor/python` + `app/`); NSIS manifest is `RequestExecutionLevel user`; slim installer excludes lazy packs (`scripts/desktop-packs.js`) |
| "The model never invents a figure; unsourceable data is `UNAVAILABLE`" | `docs/harness/financial-analysis-engine.md`, `python/harness/research/calculations.py`; `python/harness/market/verification.py`; `UNKNOWN`/`UNAVAILABLE` outcomes in `docs/harness/benchmarks.md` |
| "Persistent memory and reviewed skills" | `python/harness/eclipse_memory/` (corrections, contradiction detection; `contradictions_cli` gate recall 1.0 / false-conflict 0.0), `python/harness/skills/learning.py` + `harness/learning_review.py` (learning candidates arrive **inert** and are promoted by review, not automatically) |
| Data-source tiers, commercial status and the "personal use only" set | `docs/data-sources.md` (generated from `src/lib/setup/source-catalog.ts`; the `commercial` field is an informational snapshot, not a licence grant); enforced at the fetch path by `src/lib/data-source-preferences.ts` |
| One download, no Node/Python/database prerequisite | `scripts/desktop-package.js` (payload: `runtime/node.exe` + `scripts/vendor/python` + `app/`), `scripts/package-python.js`; the NSIS manifest is `RequestExecutionLevel user` with `InstallDir $LOCALAPPDATA\Programs\BlackEclipse` |
| Optional feature packs are lazy, not required | `scripts/desktop-packs.js` header: "the harness imports them lazily, so the app runs without them and gains the capability when the folder appears" |
| The offline `-full` installer bundles the packs | `scripts/desktop-installer.js` (`--full` adds `File /r packs/<p>/site-packages/*` and defines `FULL`); the stable alias always mirrors the slim build |
| MetaTrader 5 integration for trading operations | `python/harness/trade/` (`terminal.py`, `tools.py`), `python/harness/computer/applications/adapters.py` (`MT5Adapter`); doc `docs/trading.md` |
| Provider-agnostic models (OpenAI, Anthropic, Google, Mistral, Groq, OpenAI-compatible, Ollama) | `python/harness/models.py`, `python/harness/model_runtime/`, extras in `Black-Eclipse-site/pyproject.toml` |
| The model never computes a metric; formula, inputs, assumptions and seed are recorded | `docs/harness/financial-analysis-engine.md`, `python/harness/research/calculations.py` |
| Backtesting is anti-lookahead; walk-forward, Deflated Sharpe / PBO | `docs/quant-engine.md`, `python/harness/quant/` (`walkforward`, `multiple_testing`, `montecarlo`, `verdict`) |
| Leases, fencing, at-most-once effects, `unknown` as a first-class outcome | `docs/harness/failure-recovery.md`, `python/harness/run_effects.py` |
| One authorization path, hash-chained audit, undeclared tools unpublished | `docs/harness/security.md`, `docs/harness/tool-registry.json`, `python/harness/security/` |
| Authority scores; conflicts disclosed, never averaged | `docs/harness/search-retrieval.md`, `python/harness/retrieval/authority.py`, `market/verification.py` |
| Sandbox flags (`python -I`, scrubbed env, `--network none`) | `docs/harness/quant-sdk-sandbox.md`, `python/harness/quant/sdk_runtime.py` |
| Live trial gates, mandate, kill switch, named refusals | `docs/quant-live-strategy.md`, `python/harness/trade/`, `python/tests/test_quant_live.py` |
| **Benchmark numbers** (0.9970 / 0.9748 / 0.9963 / 1.0000) | `docs/harness/benchmarks.md` — generated, never hand-edited |
| **Corpus size** (347 tasks, 15 datasets, split counts, oracle coverage 78.1%) | `python/harness/evaluation/baselines/corpus_manifest.json` |
| `financial_accuracy` is graded deterministically, not by a model | `docs/harness/agent-evaluation.md` §5 (`FinancialGrader` recomputes the oracle) |
| Installers are unsigned; releases publish `SHA256SUMS.txt` | `.github/workflows/desktop-release.yml` (three-OS matrix), `scripts/desktop-installer.js` (writes the sums, prints the unsigned warning) |
| Linux `.deb` and macOS `.dmg` artifacts | `.github/workflows/desktop-release.yml` (build + smoke-test steps per OS) |
| `npm run desktop:doctor` exists | `Black-Eclipse-site/package.json` scripts |
| Pine engine is a separate AGPL-3.0-only program | `Black-Eclipse-site/pine-engine/NOTICE.md`, root `LICENSE` |

## 3. Screenshots

- Captures live in `Black-Eclipse-site/docs/figures/readme/`, 1600px wide,
  PNG, ≤ 200 KB each.
- **The hero banner is not a capture.** `.github/assets/readme-hero.svg` +
  `readme-hero.png` is authored brand art: the positioning sentence in
  four-tone lime-on-black, deliberately **without a logo lockup** (the repository
  name already sits above the README). Edit the SVG and re-export the PNG with
  `sharp`; never hand-edit the bitmap.
- **The gallery images are real captures of the running app.** No mockups, no stock
  imagery, no invented dashboards. A screenshot that shows a state the product
  cannot reach is a fabricated benchmark with better lighting. The Bots and
  Workflows figures were taken from the **installed** desktop app
  (`%LOCALAPPDATA%\Programs\BlackEclipse`), not the dev server — that is the only
  lane that serves a warm harness and a live workflow catalogue.
- **Crop the bottom of the app window.** On some routes the composer's status strip
  renders a machine-local path (`C:\Users\<name>\AppData\Local\Temp\...`), which
  leaks a user profile into a public repo. The committed figures were checked for
  this. See `docs/OPEN-SOURCE-READINESS.md` O3.
- Screenshots that show demo data must keep the `DEMO` badge visible.
- **The README ships three gallery figures** (Agent, Bots, Workflows) under
  "See it". **Dashboard and Journal are still uncaptured** and are not referenced
  from the README. They need fresh prices: a stale vault correctly renders
  `LIVE PRICES UNAVAILABLE`, which is honest and useless as a screenshot. Do not
  substitute a competitor's image or a stock chart; leave the gap.

Regenerate from the launch assets with `sharp` (run from `Black-Eclipse-site/`,
where it is installed): resize to 1600 wide, `extract` to height 868, then
`png({ compressionLevel: 9 })`.

### The four that need a warm, fresh app

Dashboard, Journal, Graph Studio and Bots cannot be captured from a cold checkout,
and a stale vault is not enough either. What happens when you try (all four
verified on 2026-11-15, against a copy of a real vault):

| Surface | What blocks a good capture |
|---|---|
| **Dashboard** | Needs fresh prices. A vault whose last price write is days old makes the app render `LIVE PRICES UNAVAILABLE` and leave widgets on `Loading…` — which is correct behaviour and a terrible screenshot. |
| **Journal** | Needs seeded trades. The demo seeder supplies 76 labelled trades, but the tab also needs the price bridge, so it renders `LOADING` while the bridge is cold. |
| **Bots** | The list arrives (13 automations seed from a used profile), but the conversation pane fetches threads from the harness and sits on `LOADING` for a long time on a large `agent-memory.db`. |
| **Graph Studio** | Deep-linking `?tab=graph-studio` renders the Workflows gallery; the canvas needs a workflow *opened* from the catalog, and the catalog needs the harness warm. It is not in the default sidebar, so it also cannot be reached by clicking F-keys. |

Capture procedure:

```bash
cd Black-Eclipse-site
npm run desktop:refresh                      # seeds the vault, boots the runtime
# let the app refresh prices once (Market Discover -> Refresh Prices), then:
node scripts/capture-readme-shots.js
```

The script waits for real content, refuses to write a figure that is still
`LOADING`, and crops to 1600 wide. Two hard rules:

- **Do not screenshot a stale vault.** `LIVE PRICES UNAVAILABLE` is the product
  being honest; shipping it in the README makes the product look broken instead.
- **Do not screenshot a personal vault.** The dev vault carries real broker
  trades. Scrubbing for a capture means deleting `Trade`, `TradeFill`,
  `TradeExecution`, `JournalDay` and `JournalProfile` from a *copy*, then seeding
  demo trades — never pointing the app at the real file.

A headless capture against a dev server with no seeded harness produces `BOTS · 0`,
a blank canvas and a spinner. That was tried twice and rejected both times.

## 4. Copy rules

1. **No unverifiable superlative.** "Deterministic", "durable", "auditable" are
   fine because each maps to a row above. "Best", "fastest", "most accurate" are
   not.
2. **No slogan as a section heading, and no metaphor where a mechanism would do.**
   Learned the hard way: "a door that doesn't open" and "survives Tuesday" read as
   generic because they could describe any product. "What it refuses to do",
   followed by six refusal codes, could not.
3. **Concrete over clever.** Prefer the flag, route, command or error string:
   `maxConcurrency` 4, `python -I`, `--network none`, `APPROVAL_REQUIRED`.
4. **Say what is unmeasured.** `UNKNOWN` appears in the benchmark block on
   purpose. Never round it to zero or to a pass.
5. **Never claim a distribution that does not exist.** The install block leads
   with the source install and labels PyPI as pending (§6).
6. **The positioning sentence is fixed, and the category arrives first.** *Black
   Eclipse is an open-source agentic harness designed to help you automate
   finance.* Use it verbatim in the README hero, the Pages landing page, the
   social card and the package description. It puts the category ("agentic
   harness"), the licence ("open-source") and the outcome ("automate finance")
   in one sentence, so a stranger learns what this is before any promise
   arrives. Do not replace it with a slogan, and do not let the four verbs
   ("Research. Analyze. Build. Automate. Trade.") stand in for it — they are
   scope, not identity. If someone asks *what* is automated, the answer is:
   research, analysis, strategy evaluation, reporting and — only behind
   explicit approvals — trading.
7. **No vendor trademark in the positioning.** Not a "Bloomberg replacement" — a
   terminal is a data product, this is infrastructure. `docs/OPEN-SOURCE-READINESS.md`
   O6 flags vendor wording for a trademark read.

## 5. The social preview

`.github/assets/social-card.svg` is the source of truth for the 1280×640 preview
(Repository → Settings → Social preview). Export the PNG rather than editing the
bitmap:

```bash
cd Black-Eclipse-site
node -e "require('sharp')('../.github/assets/social-card.svg',{density:144}).resize(1280,640).png().toFile('../.github/assets/social-card.png')"
```

`.github/assets/download-windows.svg` is the README's only download button.
`download-linux.svg` and `download-macos.svg` still exist, but they are **not
linked**: macOS and Linux builds are produced on request and not published yet,
and a button that 404s is worse than no button. When one ships, restore its
button on the same line (the row is measured: 249 + 220 + 231 px plus gaps = 716
px inside an ~880 px column), with the download glyph on the **left** of the
label. Shrink the existing buttons rather than letting the row wrap — a wrapped
row reads as one big button and two strays.

If macOS and Linux are restored, they are secondaries and carry a **1px** border
(`#2b2f33`) so they hold an edge on a dark page without out-shouting the primary.

The button carries **a glyph and a label, nothing else**: no size, no "no admin",
no architecture. That detail lives in the Install table, where someone who cares
about signing and SDK requirements will look. It is a filled lime surface with no
border.

Widths are **measured, not estimated**: render the label with `sharp`, `trim()` it,
and add `PAD_L + ICON_W + GAP + textW + PAD_R`. Two earlier attempts were rejected on
a render because the label clipped in a wider fallback font and a chevron collided
with the text — measure before shipping a width change.

Each is pinned to a **version-independent** release alias,
because `releases/latest/download/<name>` is the only link that survives a version
bump:

| Platform | Stable name | Produced by | Published |
|---|---|---|---|
| Windows | `BlackEclipse-Setup.exe` | `scripts/desktop-installer.js` | yes |
| Linux (amd64) | `BlackEclipse-linux-amd64.deb` | `scripts/desktop-bundle.js` (`buildDeb`) | no — built on request |
| macOS (Apple silicon) | `BlackEclipse-mac-arm64.dmg` | `scripts/desktop-bundle.js` (`buildMac`) | no — built on request |

Never point a button at a versioned filename. macOS keeps the architecture in the
name because only one arch is produced per runner and an Intel Mac cannot launch an
arm64 build. `.github/workflows/desktop-release.yml` asserts each alias exists and
fails the release rather than publishing a button that 404s; if a runner's
architecture ever changes, update the SVG **and** the README link in the same
commit.

## 6. What is not finished, and who can do it

| # | Open item | Who can do it | Effect on the README |
|---|---|---|---|
| L1 | Social preview image | **A person, in the web UI.** GitHub exposes no API for it: Repository → Settings → Social preview → upload `.github/assets/social-card.png` (1280×640, already on `main`). | None on the text; without it, link previews show a generic avatar. |
| L2 | Dashboard and Journal figures | **A person, with the app open** (one command). `node scripts/capture-readme-shots.js` against a running, seeded install. | Neither is referenced by the README; add a row only when a fresh, warm capture exists. The agent, Bots and Graph Studio figures already ship. |
| L3 | `black-eclipse-harness` is **not on PyPI** (`docs/harness/roadmap/round-4-gap-closure.md` O6: "declarable, not installable") | **A person, with a PyPI token.** `.github/workflows/publish-harness.yml` publishes on a version tag once `PYPI_API_TOKEN` exists. | The install block shows the source install and says "PyPI release pending". When it publishes, replace the source line with `pip install black-eclipse-harness` and delete the note. |
| L4 | The public repository URL does not exist yet; `origin` is private, and `pyproject.toml` points at `github.com/black-eclipse/black-eclipse` | **A product decision + a history rewrite.** `docs/OPEN-SOURCE-READINESS.md` explains why the private repo must not simply be flipped public. | Every link uses the current `origin`. Rewrite them in one pass when the public repository is created — never mix the two. |

### Why the four screenshots keep failing (do not retry blindly)

Five capture attempts on 2026-11-15, each against a **copy** of a real vault
(55,714 prices, 20 workflows, 13 automations), all rejected. What happens:

1. A vault whose prices are days old is correct to render
   `LIVE PRICES UNAVAILABLE`; the widgets stay on `Loading…`.
2. Forcing a real refresh through `POST /api/refresh` works but takes **~290
   seconds** for the US market and blocks the Node event loop hard enough that the
   dev server stops answering for ~100 seconds afterwards (node-cron logs missed
   executions).
3. `?tab=<id>` deep links are unreliable on a cold dev load — `?tab=dashboard`
   rendered the Eclipse Agent tab instead, twice.
4. Bots lists 13 automations, but the conversation pane fetches threads from a
   1.8 GB `agent-memory.db` and can sit on `LOADING` for minutes.
5. Graph Studio never reaches a canvas; only the Workflows gallery renders.

So the constraint is not the screenshot tooling, it is that **the dev lane cannot
serve a warm, freshly-priced app**. The installed desktop app can, which is why the
capture script targets a running app rather than starting one.

## 7. Where this sits

- [`OPEN-SOURCE-READINESS.md`](./OPEN-SOURCE-READINESS.md) — what makes the
  repository safe to publish. The README is the *argument*; that doc is the *gate*.
- [`PRODUCTION-READINESS.md`](./PRODUCTION-READINESS.md) — what is still open in
  the product. The README's roadmap section points here.
- [`NAVIGATION.md`](./NAVIGATION.md) — the task row that sends you back here.

## 8. The landing page (`site/index.html`)

The Pages page is a **second surface for the same claims**, not a redesign with
different copy:

| Rule | Why |
|---|---|
| The hero is the exact positioning sentence, then one line on what the harness does | Same first impression as the repo |
| Sections are **Features → Use → Needs**, then How it works / Install | A visitor who lands from a search gets the same three answers |
| Every repository URL is `dumbpabloo/BlackEclipse-V1.0` | The older `dumbpabloo/black-eclipse` path 404s; the Pages canonical URL is `https://dumbpabloo.github.io/BlackEclipse-V1.0/` |
| The page stays a single static HTML file — no build, no Node, no server | It must not reintroduce the deleted browser lane |
| It links the README and Quickstart rather than duplicating the benchmark table | One canonical copy of each number |

Publish path: `site/**` or `pages.yml` on `main` → Actions → Pages. Enable once in
**Settings → Pages → Source: GitHub Actions**. If the repository is renamed again,
update the README links, the Pages canonical/OG URLs, the JSON-LD `codeRepository`
and `downloadUrl`, and `site/index.html` in the same commit.
