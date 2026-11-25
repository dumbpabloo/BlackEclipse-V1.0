---
title: Presentations (Decks) -- dedicated subsystem plan
status: Proposed (not implemented)
last-verified: 2026-11-24
---

# Presentations (Decks) -- dedicated subsystem plan

**Who this is for:** whoever builds the next step. It is the design, the file
map, the phase order, the acceptance tests and the honest trade-offs.

**What this is not:** an implemented feature. Nothing in this document exists in
the tree yet unless a file is named as *existing*. Items under
[Decisions](#decisions-needed-before-phase-1) still need an answer; the
recommendation is stated for each.

---

## 1. What Eclipse can do today

| Capability | Where | What is missing |
|---|---|---|
| One-shot `.pptx` from chat | `python/harness/documents/authoring.py` -> `build_pptx` (`document_create`, `eclipse_report`, `financial_model_build`) | Only `{title, bullets, notes}` per slide, one default layout, no theme, no tables/charts/images on a slide, no in-app preview, no editing |
| Edit an existing `.pptx` | `python/harness/documents/edit/pptx_ops.py` (`document_edit`) | Preservation-first byte edits for files the user brings -- not an authoring surface |
| Read/understand a `.pptx` | `python/harness/document_intelligence/` (slides as Pages) | Read-only; there is no writer that consumes that structure |
| Editable artifacts | Canvas (`python/harness/canvas/`, kinds `document` / `code` / `visual`) | No deck kind; Canvas content is UTF-8 text and its edit ops are text-range ops, not slide ops |
| Slide-like chat view | the `carousel` response block (`src/lib/response/contract.ts`) | A presentation *inside the transcript*; not exportable, not editable, no artifact |
| PDF/Word/PowerPoint reports | `python/harness/documents/reporting.py` | Report layout, not slide layout |

So the gap is not "make a pptx" -- that exists. The gap is a **deck as a
first-class, editable, versioned object with a real preview and a faithful
export**.

## 2. Design options (and why one wins)

### Option A -- deck JSON is the artifact; `.pptx` is a rendered derivative (recommended)

A deck is a versioned JSON document (`DeckSpec`) stored as an Artifact Center
artifact. The app renders it with a React slide renderer; export renders the same
neutral layout model with `python-pptx`.

- Pro: real in-app editing and preview with no Office runtime; deterministic
  preview == export; versions, AI edits, owner scope, tags, collections for free;
  works on a fresh clone with zero config.
- Con: two renderers (React + python-pptx) must be kept in step; a `.pptx` the
  user edits in PowerPoint does not round-trip back into the deck (we do not
  claim it does).
- Mitigation for the con: one **shared layout model** (normalized boxes) computed
  by one engine, golden fixtures that pin both renderers to it (Section 6).

### Option B -- the `.pptx` file is the artifact; preview via LibreOffice

- Pro: OOXML is the single truth; perfect PowerPoint fidelity; imports edit
  cleanly.
- Con: requires LibreOffice (`soffice`) to preview anything, which breaks the
  zero-config rule; AI editing becomes byte surgery with no visual feedback;
  preview is slow and can fail on a machine without the converter.
- Verdict: rejected as the core. Kept as an *assist*: "Open in PowerPoint" for
  exported files, and `document_edit` for decks the user supplies.

### Option C -- present decks as a `visual` canvas artifact (response envelope + carousel)

- Pro: almost no new machinery; reuses the existing envelope writer.
- Con: the response envelope is not a deck contract (no speaker notes, theme,
  per-slide layouts, asset references); the exported deck stays as limited as
  today; the editor would be a JSON textarea.
- Verdict: acceptable as a *v0 stopgap* only if Phase 1 slips. Not the plan.

**Recommendation: Option A**, delivered in phases so the useful half (a better
writer + export) lands before the editor.

## 3. Architecture

```
                          DeckSpec (JSON, schemaVersion 1)
                                   |
             +---------------------+----------------------+
             |                                            |
   app (React, authoritative view)              python (authoritative export)
   src/lib/presentation/schema.ts               python/harness/presentations/model.py
   src/lib/presentation/layout.ts   <-- parity --> render.py  (neutral layout model)
   src/components/screens/canvas/presentation-*  export_pptx.py / export_pdf.py
             |                                            |
             +----------------------+----------------------+
                                    |
                        ONE Artifact Center (ArtifactService)
                        - artifact bytes = canonical DeckSpec JSON
                        - versions = immutable artifact versions
                        - revision guard = canvas base_version
                        - previews = outline text (Phase 1), thumbnails (Phase 4)
```

Key invariants:

1. **One writer.** `PresentationService` is the only thing that creates or saves a
   deck. It composes `CanvasService` (which already owns revision-guarded saves
   over `ArtifactService`). No deck database, no deck storage root, no deck
   version table.
2. **One layout model.** `render.py` produces `RenderDeck`/`RenderSlide` in
   normalized units (0..1). React multiplies by the stage size; python-pptx
   multiplies by the slide size. Neither side invents geometry.
3. **Export never edits.** `presentation_export` registers a NEW artifact
   (`sourceTool: presentation_export`) with provenance
   `derivedFrom = deck artifact id @ version`. The deck artifact is untouched.
4. **Data honesty is structural.** A chart slide names a `dataSource`; if it
   cannot be resolved the render model carries `unavailable` + reason and the
   slide renders an UNAVAILABLE state. No fallback series, no invented number.
   Metric/table cells carry `source` or `computed` when they are not user prose.

## 4. The data contract -- `DeckSpec` v1

Canonical file: `python/harness/presentations/model.py` (pydantic, strict enums).
Mirror: `src/lib/presentation/schema.ts` (zod). Parity is enforced by fixtures
(Section 10), not by trust.

```jsonc
{
  "schemaVersion": 1,
  "meta": {
    "title": "Q3 Investor Update",
    "subtitle": "BlackEclipse Capital",
    "author": "Eclipse",
    "date": "2026-11-24",
    "footer": "Confidential -- not investment advice",
    "language": "en"
  },
  "size": "16:9",                       // "16:9" | "4:3"
  "theme": {
    "preset": "eclipse",                // eclipse | paper | midnight | slate
    "accent": "#7C9CFF",                // optional override
    "logoArtifactId": null              // optional; resolved server-side to bytes
  },
  "dataSources": {
    "aapl_1y": { "kind": "price", "symbol": "AAPL", "range": "1y", "interval": "1d" }
  },
  "slides": [
    { "id": "s1", "layout": "title", "title": "...", "subtitle": "...", "notes": "..." },
    { "id": "s2", "layout": "agenda", "title": "Agenda", "bullets": ["..."] },
    { "id": "s3", "layout": "bullets", "title": "Highlights", "bullets": ["..."], "source": "10-Q 2026-09-30" },
    { "id": "s4", "layout": "metric_row", "title": "By the numbers", "metrics": [
      { "label": "Revenue", "value": "$12.4M", "delta": "+18% YoY", "source": "10-Q 2026-09-30" }
    ]},
    { "id": "s5", "layout": "chart", "title": "Revenue trend",
      "chart": { "type": "line", "dataSource": "aapl_1y", "x": "date", "y": "close" },
      "source": "JDdS daily close" },
    { "id": "s6", "layout": "table", "title": "Comps",
      "table": { "headers": ["Ticker", "P/E"], "rows": [["AAPL", "31.2"]], "caption": "" } },
    { "id": "s7", "layout": "two_column", "title": "Risks / Mitigants",
      "left":  { "heading": "Risks",     "bullets": ["..."] },
      "right": { "heading": "Mitigants", "bullets": ["..."] } },
    { "id": "s8", "layout": "statement", "statement": "One sentence, set large." },
    { "id": "s9", "layout": "image_right", "title": "Product",
      "image": { "artifactId": "art_...", "alt": "...", "fit": "cover" } },
    { "id": "s10", "layout": "closing", "title": "Thank you", "subtitle": "..." }
  ]
}
```

Closed vocabulary (v1):

| Group | Values |
|---|---|
| `layout` | `title`, `section`, `agenda`, `bullets`, `two_column`, `statement`, `quote`, `metric_row`, `table`, `chart`, `image_full`, `image_right`, `timeline`, `comparison`, `closing` |
| `size` | `16:9`, `4:3` |
| `theme.preset` | `eclipse`, `paper`, `midnight`, `slate` |
| `fit` | `cover`, `contain` |

Limits (enforced in both validators): 150 slides, 7 bullets/slide, 180 chars per
bullet, 700 chars of body prose/slide, 4 metrics, 12 table rows, 8 columns,
4000 chars of speaker notes/slide, 4 MB deck bytes. `id` is a short local slug
(`s1`, `s2`, ...); it is stable across edits and is what ops address.

**No absolute coordinates, no free-form HTML, no SVG on a slide.** Layout is
chosen by name; that is what lets preview and export agree, and it is why a model
cannot produce an unreadable deck by placing a text box off-canvas.

## 5. Editing model (ops, AI, revision guard)

Canvas text ops (`replace_text_range`, `append`, ...) do not fit structured
JSON. Add a deck op vocabulary in `python/harness/presentations/ops.py`:

```
deck.add_slide      { at, slide }
deck.delete_slide   { slideId }
deck.move_slide     { slideId, to }
deck.replace_slide  { slideId, slide }
deck.set_meta       { field, value }           // closed field list
deck.set_theme      { preset?, accent? }
deck.set_notes      { slideId, text }
deck.set_title      { slideId, text }
deck.set_bullets    { slideId, bullets }
deck.set_chart      { slideId, chart }
deck.set_table      { slideId, table }
deck.set_metrics    { slideId, metrics }
```

Properties, matching the Canvas contract: typed, bounded, no JSON-patch, no
arbitrary path, applied to the *current* bytes under `base_version`, rejected
with a typed error (`DECK_SLIDE_NOT_FOUND`, `DECK_LIMIT`, `DECK_INVALID`). The
Canvas AI orchestrator (`canvas/edits.py`) gets a `PRESENTATION` rule and a
deck-specific `_WireProposal` schema; it still returns propose -> review ->
accept, never a direct write.

## 6. Deterministic layout and fit (the hard part)

Slides fail by text overflow, not by syntax. The plan pins geometry:

- `render.py` defines, per layout, named regions in normalized units
  (title band, body, aside, chart frame, table frame, footer). Margins 0.6in on a
  13.333x7.5in 16:9 canvas.
- A shared **fit calculator** (`fit.py` / `fit.ts`, same numbers) steps type down
  the ladder `[28, 24, 20, 18, 16, 14, 12]` pt using a conservative per-font
  average glyph width table (Calibri/Segoe UI/Arial), line height and region
  capacity. The first step that fits wins.
- If nothing fits, the slide carries `warnings: ["overflow"]`; the editor shows a
  warning chip and the exporter (a) uses the smallest step, (b) reports
  `overflowSlides` in its result, and (c) never claims the deck fits. A tool that
  produced an overflowing deck says so.
- Images are `cover`/`contain` in the region with a focal point; a missing image
  renders an honest placeholder with the alt text, never a stock photo.

This is deliberately *not* a browser text-measurement pipeline: measurement must
be identical on the server (export) and the client (preview), so it is arithmetic,
not `getBoundingClientRect`.

## 7. Charts, tables and the no-invention rule

- A chart slide reuses the shared chart contract (`src/lib/response/chart-spec.ts`
  is the camelCase mirror of `python/harness/charts/spec.py`) and resolves its
  named `dataSource` through the same read-only path the chat uses
  (`chart-data.ts` -> `/api/price-history`, or the python market bridge on the
  export side).
- Export mapping:
  - bar / line / area / pie / scatter -> a **native editable PowerPoint chart**
    (python-pptx chart part) when the spec maps 1:1.
  - anything native charts cannot express (candlestick, waterfall, custom marks)
    -> PNG from the existing Python chart engine
    (`python/harness/charts/render.py::render_chart`), embedded with a caption.
    Same engine as the transcript, so the deck matches what the user saw.
- Unresolved data: the render model carries
  `unavailable: { reason, source }`; the slide shows an UNAVAILABLE block with
  the reason and the slide keeps its provenance line. Budgets, deltas and
  multiples stay text the model wrote -- the tool never computes a financial
  value, and a computed value must be labelled `computed`.
- Tables are real tables in both renderers (React `<table>`; python-pptx table
  with theme header fill).

## 8. Surfaces

### 8.1 Agent tools (Phase 0)

Registered on the ONE `ToolRegistry`, added to `DOCUMENT_TOOLS` in
`python/harness/agents.py` (so every assembled agent sees them) and to
`CHAT_CODE_TOOLS` only if they are safe there (they are: artifact-only writes).

| Tool | Input | Output |
|---|---|---|
| `presentation_create` | `title`, `theme`, `slides[]` (the DeckSpec subset) or `outline[]` | deck artifact id + absolute path of an exported `.pptx` |
| `presentation_edit` | `artifactId`, `baseVersion`, `operations[]` | new version number + warnings |
| `presentation_export` | `artifactId`, `format: pptx\|pdf\|png`, `saveTo?` | new artifact + absolute path; honest error codes |
| `presentation_outline` | `artifactId` | slide titles/layouts (read-only; for "what's on slide 7?") |

`document_create(format="pptx", slides=[...])` stays -- it becomes a compatibility
shim that converts its `{title, bullets, notes}` slides into a minimal `DeckSpec`
and calls the new exporter, so the two writers cannot drift.

### 8.2 Slash commands

- `/deck <topic or file>` -- compose a deck (prompt carries the deck instructions
  and the schema, then the run calls the tools) and open it in Canvas.
- `/deck from-chat` -- turn the latest answer / carousel into a deck (same
  conversion as the response action).
- Registry: `src/lib/harness/commands/command-registry.ts` (spec + handler) and
  the python command catalog if mirrored. No new command category.

### 8.3 Chat

- A finished answer with headings gets a response action **Turn into deck**
  (`response-actions.tsx`): one slide per heading, bullets from the section,
  sources preserved. For a `carousel`, each slide maps 1:1.
- A deck artifact renders in chat as a compact card: title, slide count, theme
  chip, and **Open deck** / **Export .pptx** / **Slideshow**. No new response
  block type in v1 -- the card is an artifact attachment view, which avoids
  touching `contract.ts`, `normalize.ts`, `finalize.ts`, `projection.ts`,
  `export.ts` and the size budget. (A dedicated `deck` block is a Phase 5 option
  if inline decks become the primary surface.)

### 8.4 Canvas (the editor)

- New `CanvasKind.PRESENTATION = "presentation"`; MIME
  `application/vnd.eclipse.deck+json`; category `document`; extension
  `.deck.json`; metadata key `canvas.kind`.
- `canvas-editors.tsx` dispatches to a new `presentation-editor.tsx`:
  - left rail: slide thumbnails (rendered at 1/6 scale by the same renderer),
    add / duplicate / reorder (dnd-kit is already a dependency) / delete;
  - center: the slide stage at 16:9 (or 4:3) with inline text editing;
  - right: inspector (layout, theme, notes, source, image picker from artifacts)
    and the existing Canvas AI panel;
  - header: save status, undo/redo, versions, export, present.
- `present-mode.tsx`: fullscreen stage, arrow keys/Space, `N` notes drawer,
  `Esc`, slide counter. No transitions/animations in v1.
- `canvas-viewer.tsx` (read-only, used from chat / Artifact Center) renders the
  deck with the same `SlideStage` plus an outline.

### 8.5 Artifact Center

- Preview kind for `.deck.json`: outline text (title + up to 20 slide titles) via
  `artifacts/preview.py`; downloadable JSON.
- Phase 4 adds real slide thumbnails: the editor renders slide 1 client-side and
  uploads it through a small preview-bytes endpoint (new route, owner-scoped), or
  the server draws a schematic with PIL. Decide in Phase 4; until then
  `UNSUPPORTED` is the honest state.

## 9. File-by-file change map

### Python (new package `python/harness/presentations/`)

| File | Responsibility | Approx. lines |
|---|---|---|
| `model.py` | `DeckSpec`/`Slide`/`Theme`/`ChartRef`/`DataSource`, enums, limits, `DeckError` codes, `parse_deck`, `dump_deck` | 320 |
| `ops.py` | closed `DeckOp` models + `apply_deck_ops` + `describe`; reuses `DeckError` | 300 |
| `layout.py` | region maps per layout + normalized `RenderSlide`/`RenderDeck` | 300 |
| `fit.py` | shared fit ladder + glyph width table + overflow detection | 140 |
| `themes.py` | four presets (palette, type scale, table/ chart colors) + `theme_tokens()` | 180 |
| `render.py` | `DeckSpec` -> `RenderDeck` (resolves images/data sources through injected resolvers) | 220 |
| `export_pptx.py` | `RenderDeck` -> `.pptx` via python-pptx (slides, notes, tables, native charts, PNG fallback) | 380 (split tables/charts if it grows) |
| `export_pdf.py` | `RenderDeck` -> PDF via the vendored Matplotlib backend (same approach as `documents/reporting.py`) | 240 |
| `service.py` | `PresentationService` over `CanvasService` + `ArtifactService`: create / save / export / outline | 260 |
| `tools.py` | the four tools + `register_presentation_tools` | 260 |
| `golden.py` | writes the golden render fixtures (developer script, `python -m`) | 90 |

Modified:

- `python/harness/canvas/model.py` -- `CanvasKind.PRESENTATION`, MIME, constants.
- `python/harness/canvas/service.py` -- `_MIME`, `_CATEGORY`, `_with_extension`.
- `python/harness/canvas/ops.py` -- dispatch by kind to `apply_deck_ops`.
- `python/harness/canvas/edits.py` -- `_KIND_RULES[PRESENTATION]`, deck proposal schema.
- `python/harness/agents.py` -- `DOCUMENT_TOOLS` += the four names; new
  `PRESENTATION_TOOLS` if a separate allowlist is preferred.
- `python/harness/service.py` -- construct `PresentationService`, register tools
  (mirrors `documents` wiring).
- `python/harness/routers/presentations.py` (new) -- `POST /presentations/{id}/export`
  and `GET /presentations/themes`; operator-gated; mounted in `main.py`. CRUD stays
  on `/canvas/artifacts*`.
- `python/harness/documents/tools.py` / `reporting.py` -- the pptx branch delegates
  to the new exporter through the shim.
- `python/harness/artifacts/preview.py` -- deck outline extractor.
- `python/requirements.txt` -- no change (python-pptx, matplotlib already pinned).

### TypeScript

New `src/lib/presentation/`:

| File | Responsibility |
|---|---|
| `schema.ts` | zod `DeckSpec` mirror + `parseDeck`, `emptyDeck`, `DECK_LIMITS` |
| `theme.ts` | preset tokens (fallback mirror of `themes.py`) + CSS var builder |
| `layout.ts` | mirrors `layout.py`; `layoutSlide(slide, size)` -> regions |
| `fit.ts` | mirrors `fit.py` (same ladder, same glyph table) |
| `render-slide.tsx` | the ONE slide renderer (stage + thumbnail scales) |
| `present-mode.tsx` | fullscreen presenter |
| `deck-actions.ts` | create/save/export through the canvas + presentation clients |
| `fixtures.ts` | loads the shared fixture corpus in tests |

New `src/components/screens/canvas/presentation-editor.tsx` (+ `slide-rail.tsx`,
`slide-stage.tsx`, `deck-inspector.tsx`, `theme-picker.tsx`, `present-mode.tsx`).

Modified: `src/lib/harness/clients/canvas-client.ts` (`CanvasKind` union),
`src/components/screens/canvas/canvas-editors.tsx` (dispatch),
`canvas-viewer.tsx` (deck view), `src/app/api/presentations/[[...path]]/route.ts`
(new proxy), `src/lib/harness/commands/command-registry.ts` (+ handler),
`src/components/response/response-actions.tsx` ("Turn into deck"), the artifact
card (`src/components/chat/artifact-view.tsx` or the artifact screen card) for the
deck card, `src/lib/agent/response-model.ts` for tool labels.

### Docs

- `docs/harness/presentations.md` -- canonical doc, written in Phase 1.
- `docs/presentations-plan.md` -- this file (stays as the rationale).
- `docs/NAVIGATION.md` -- a row for the subsystem (Phase 1).
- `docs/canvas.md` -- kinds/editors table.
- `docs/harness/document-intelligence.md` (Authoring) -- link to the new writer.
- `docs/harness/artifact-center.md` -- deck preview.
- `docs/GLOSSARY.md` -- Deck, DeckSpec, slide layout, theme preset.
- `skills/ARCHITECT.md` + `docs/DECISIONS.md` -- ADR-053 (identical text).
- `docs/CHANGELOG.md` -- one entry per phase.

## 10. Testing strategy

**Contract and parity (the drift guard).**

- Shared fixtures in `docs/harness/presentations/fixtures/`:
  `minimal.json`, `full.json`, `overflow.json`, `invalid/*.json`.
- Python: `test_presentations_contract.py` validates every fixture and asserts
  each `invalid/*` fails with the expected code.
- TS: `src/lib/presentation/schema.test.ts` does the same with zod.
- TS `layout.test.ts` compares `layoutSlide()` output against committed golden
  JSON produced by `python -m harness.presentations.golden`; a mismatch fails.
  (A parity test, not a proof -- stated as such.)

**Export.**

- `test_presentations_export.py`: build a deck, export, reopen with python-pptx,
  assert slide count, titles, notes, table cells, chart presence, and that
  untouched shapes are sane; assert a missing image is a placeholder and an
  unresolved data source is `unavailable`; assert overflow is reported, not
  hidden.
- `test_presentations_ops.py`: bounds, unknown slide id, idempotent ops,
  empty-bullets rejection, move/delete/reorder.
- `test_presentations_service.py`: revision guard (409 carries `currentVersion`),
  versions increment, owner scope, export provenance points at the source
  version.
- `test_presentations_api.py` + `test_presentations_tools.py`.

**UI.**

- `render-slide.test.tsx` -- every layout, the UNAVAILABLE state, the overflow
  chip.
- `presentation-editor.test.tsx` -- add/duplicate/reorder/delete, title edit,
  undo, save conflict banner.
- `present-mode.test.tsx` -- keyboard, notes drawer, escape.
- Canvas kind dispatch test; command registry/handler test; "Turn into deck"
  conversion test (headings -> slides).
- `src/lib/size-budget.test.ts` may need a budget review if a shared file grows;
  keep modules small instead of raising the budget.

**End-to-end.**

- `test_presentations_e2e.py` (scripted model): "make a 6-slide deck" -> tool
  calls -> artifact -> export exists on disk -> outline read-back matches.
- The repo checks still apply every change: `npm run test`, `npx tsc --noEmit`,
  and `npm run eval:registry` after tool registration (regenerates
  `docs/harness/tool-registry.json`).

## 11. Phases, deliverables, acceptance

Effort is engineering-days for one person familiar with the codebase; treat it as
a shape, not a promise.

### Phase 0 -- Contract + writer (3--4 days)

Ship: `DeckSpec` + validators + fixtures, layout/fit engine, themes, python-pptx
exporter, `presentation_create` / `presentation_export` / `presentation_outline`
tools, `document_create(format=pptx)` shim.

**Done when:** from chat, "make a 10-slide deck with a title, agenda, bullets, a
metrics slide, a table and a sourced chart" produces a deck artifact plus a
`.pptx` that opens in PowerPoint with notes, a native editable chart, and the
UNAVAILABLE state when the series cannot be resolved. Overflow is reported.

### Phase 1 -- Deck as a Canvas artifact (3 days)

Ship: `CanvasKind.PRESENTATION`, deck ops, `PresentationService`,
`presentation_edit`, Canvas kind dispatch to a **read-only deck view** + outline,
Artifact Center preview, `/canvas` open exactly like other artifacts,
`/presentations/{id}/export` route + TS client, "Open deck" from chat.

**Done when:** a deck survives restart, opens in Canvas from chat and from the
Artifact Center, shows its outline, and exporting from the UI produces the same
bytes as the tool.

### Phase 2 -- The editor (5--7 days)

Ship: slide rail with thumbnails, stage with inline text editing, inspector
(layout/theme/notes/source/image), add/duplicate/reorder/delete, fit warnings,
undo/redo, revision-guarded autosave, present mode, AI panel deck rules.

**Done when:** a user can create a deck from zero, reorder slides, change theme,
add a notes page, present it fullscreen, and watch a second window receive the
new version (or a 409 with a clear banner). No layout can place content outside
the slide.

### Phase 3 -- Data-backed slides (3 days)

Ship: chart slides with named sources + native/PNG mapping, table-from-source,
computed-value labels, provenance lines, UNAVAILABLE everywhere it applies.

**Done when:** every number on every slide traces to a source or is labelled
computed, and removing the source turns the slide into an honest UNAVAILABLE
instead of a stale chart.

### Phase 4 -- Export breadth and polish (3 days)

Ship: PDF export (Matplotlib path; LibreOffice used when present for fidelity),
PNG-per-slide, deck thumbnails, import (Markdown outline -> deck; existing
`.pptx` -> outline + notes via Document Intelligence, explicitly not a lossless
round-trip), template gallery, brand kit (logo + palette as a theme preset).

**Done when:** PDF works on a fresh clone with no LibreOffice; a user's `.pptx`
can seed a new editable deck without pretending to preserve its design.

### Phase 5 -- Optional

`/deck` command polish, inline `deck` response block, live presenting with a
second window, timing/rehearsal, MCP PowerPoint bridge, animation support.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| React and python renderers drift | one normalized layout model + golden fixtures in CI + theme tokens emitted by one module |
| Text overflow / unreadable slides | deterministic fit ladder, per-layout capacity, explicit `overflow` warning, export reports it |
| Two schema definitions (pydantic + zod) | shared fixture corpus; every fixture must pass both; failures name the field |
| Font metrics differ between browser and PowerPoint | conservative width table, desktop-only app, documented fallback fonts (Calibri/Segoe UI/Arial) |
| python-pptx chart limits | native charts only where 1:1; otherwise PNG from the same Python chart engine |
| Scope creep (animation, SmartArt, arbitrary positioning) | explicit non-goals below; the closed layout vocabulary is the guard |
| Deck JSON as an HTML/JS vector | it is data only; never rendered as HTML; links follow the existing http(s) policy; images resolve to artifact bytes server-side |
| Existing `document_create(pptx)` behaviour changes | compatibility shim + existing tests kept green; output may improve, contract does not change |
| Large files / memory | split modules, lazy-import python-pptx, bounded content (4 MB deck), stream export to a temp file then register bytes |

## 13. Non-goals for v1

Animations and transitions, SmartArt authoring, equations, embedded audio/video,
macros, collaborative multi-cursor editing, mobile editing, arbitrary
pixel-positioned elements, a template marketplace, legacy `.ppt` authoring,
Keynote/Google Slides export, and a lossless `.pptx` -> deck round-trip.

## 14. Decisions needed before Phase 1

1. **Source of truth.** Deck JSON + rendered export (recommended), or `.pptx` +
   LibreOffice preview? Recommendation: JSON.
2. **Where decks live.** A fourth Canvas kind (recommended: reuse versions,
   owner scope, AI panel, inline chat pane) or a separate `/decks` screen with its
   own rail? Recommendation: Canvas kind; add a filtered "Decks" view in the
   Artifact Center later if the list gets noisy.
3. **PDF now or later.** Recommendation: later (Phase 4), and native Matplotlib
   so zero-config holds; LibreOffice only improves fidelity.
4. **Present mode in Phase 2 or 5.** Recommendation: Phase 2, because "dedicated
   presentation tool" implies presenting; it is a small renderer variant.
5. **Chart authoring depth in v1.** Recommendation: line/bar/area/pie/scatter +
   candlestick via PNG; everything else deferred.

## 15. Rollout and observability

- Feature flag `HARNESS_PRESENTATIONS=on|off` (default on once Phase 1 ships) for
  the tools and the canvas kind, so a bad export can be disabled without a
  release.
- Counters: decks created, versions saved, exports by format, export failures by
  code, slides per deck, overflow rate, percent of decks opened in Canvas.
- Failure codes are user-visible and stable: `DECK_INVALID`, `DECK_LIMIT`,
  `DECK_SLIDE_NOT_FOUND`, `DECK_CONFLICT`, `PRESENTATION_LIBRARY_MISSING`,
  `PRESENTATION_EXPORT_UNAVAILABLE`.
- Every export result names the artifact id, the absolute path (same contract as
  `document_create`), and `overflowSlides[]`.
