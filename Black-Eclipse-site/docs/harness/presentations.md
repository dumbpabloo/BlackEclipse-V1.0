---
title: Presentations (Decks)
status: Current
last-verified: 2026-11-25
---

# Presentations (Decks)

**Who this is for:** anyone asking "how do decks work, and how do I change one?"
and anyone extending the authoring/export path.

A presentation is a **versioned deck artifact**, not a PowerPoint file. The deck's
canonical bytes are a JSON `DeckSpec`; the app renders it with the slide engine,
and `.pptx` is a **derivative** produced by `python-pptx`. That is the one
invariant (ADR-085): there is no deck database, no deck storage root and no deck
version table.

Why not store the `.pptx`? Because previewing it would require LibreOffice,
AI/manual edits would become byte surgery with no visual feedback, and "what the
app shows" would stop matching "what you export." Keeping the spec canonical makes
preview and export agree by construction and keeps zero-config working.

## What you can do

| Surface | Action |
|---|---|
| Chat | ask for a deck; the agent calls `presentation_create` and returns the artifact + the absolute `.pptx` path |
| `/deck <subject> [--theme paper] [--slides 8]` | an agent-turn command that composes a deck and reports what the tool returned |
| Canvas (inline in chat) | open the deck artifact, edit slides, reorder/duplicate/delete, change theme/layout, add notes, present fullscreen |
| Canvas AI panel | ask for a change; the copilot proposes typed **deck operations** (add/move/set_*), previewed and applied under the revision guard |
| Canvas Export menu | **PowerPoint (.pptx)**, **PDF**, or **PNG slides (.zip)** — each registers a new artifact and writes the file to `Documents/Eclipse` |
| Chat import | `presentation_import` turns a `.pptx` already in the Artifact Center into an editable deck outline (design/charts/images are named as dropped) |
| Artifact Center | deck preview is a readable outline plus a rendered first-slide thumbnail; the JSON is downloadable |

## Code map

```
python/harness/presentations/
  model.py        DeckSpec/Slide/Theme/Chart/Meta + limits + DeckError
  themes.py       four presets (palette, type scale) -- canonical for export
  fit.py          deterministic fit ladder (the same numbers as fit.ts)
  layout.py       fractional regions per layout -> inches + font sizes + warnings
  render.py       deck -> neutral render model; resolves charts/images honestly
  export_pptx.py  render model -> real .pptx (notes, tables, native charts, images)
  ops.py          closed deck op vocabulary (add/delete/move/replace/set_*)
  service.py      PresentationService over CanvasService + ArtifactService
  tools.py        presentation_create / _edit / _export / _outline / _import
  import_pptx.py  .pptx -> deck outline (titles, text, notes; not a round-trip)
  export_pdf.py   PDF + per-slide PNG (Matplotlib), first-slide thumbnail
python/harness/routers/presentations.py   GET /presentations/themes, POST /presentations/{id}/export
src/lib/presentation/        schema.ts (zod mirror) · theme.ts · fit.ts · layout.ts
src/components/screens/canvas/render-slide.tsx      ONE slide renderer
src/components/screens/canvas/presentation-editor.tsx  rail + stage + inspector + present
src/lib/harness/clients/presentations-client.ts     export client
src/app/api/presentations/[[...path]]/route.ts      front-door proxy
```

Deck CRUD is not a new API: it uses `/canvas/artifacts*` with
`kind: "presentation"`. Only deck-specific actions (themes, export) get their own
route.

## The contract

`DeckSpec` (schema version 1) has `meta`, `size` (`16:9`/`4:3`), `theme`
(`eclipse`/`paper`/`midnight`/`slate`, optional `accent`), `dataSources` and
`slides`.

Thirteen layouts: `title`, `section`, `agenda`, `bullets`, `two_column`,
`statement`, `quote`, `metric_row`, `table`, `chart`, `image_full`, `image_right`,
`closing`.

Limits: 150 slides, 7 bullets/slide, 180 chars/bullet, 700 chars of body prose,
4 metrics, 12 table rows, 8 columns, 4,000 chars of notes, 4 MB deck bytes. There
are **no absolute coordinates** and no HTML/SVG on a slide — a layout is chosen
by name, which is what makes preview and export agree and keeps a model from
producing an off-canvas slide.

Types live in `python/harness/presentations/model.py` (canonical) and
`src/lib/presentation/schema.ts` (zod mirror). The shared fixture deck
(`src/lib/presentation/__fixtures__/full.deck.json`) is validated by both, and
the committed `golden-layout.json` pins the TS layout engine to the Python one;
`theme.test.ts` pins the palettes.

## Rendering parity

Regions are **fractions of the slide** (0..1). Python converts them to inches for
`python-pptx`; the browser converts them to percentages and sizes type in `cqw`
container units. A 160px thumbnail and the full stage are the same slide.

`fit.py`/`fit.ts` use one arithmetic model (no DOM measurement): a conservative
average glyph width, integer wrapped lines, and a fixed line-height multiple. If
no readable size fits, the slide carries `overflow:<region>`, the editor shows a
**Text overflow** chip, and an export returns `overflowSlides` instead of
claiming a clean deck. The contract's own limits are chosen so a legal deck
normally fits; the overflow path is the honest fallback, not the common case.

## Data honesty

- A chart slide carries explicit `categories`/`series`, or names a `dataSource`
  (a `price` source with `symbol`/`range`/`interval`).
- `presentation_create` resolves named sources **once** through the read-only
  market bridge and stores the resolved series in the deck. The artifact is then
  self-contained; re-exporting it does not silently refetch different numbers.
- An unresolved chart is `available: false` with a reason: the preview shows
  **UNAVAILABLE**, the exporter draws a labelled placeholder, and the result
  carries `chart_unavailable`. No fallback series, no invented number.
- An image must be an Artifact Center id; a missing/unreadable image is a
  labelled placeholder with its alt text, never a stock photo.
- Metric/table values come from the caller; a computed figure must say so, and a
  sourced one carries a `source`.

## Export

`export_pptx` creates a blank presentation, applies the slide size, then draws
exactly the render model: background, title/subtitle, bullets, columns, statement/
quote, metric cards, tables, charts, images, source/footer/page and speaker notes.

- Standard charts (`line`, `area`, `bar`, `stackedBar`, `pie`, `donut`) become
  **native, editable PowerPoint charts**; unsupported kinds are unavailable in
  decks rather than silently redrawn as another chart type.
- Images honour `cover`/`contain`.
- The export registers a NEW artifact tagged `deck`/`pptx`/`export` with
  provenance `derivedFrom: {artifactId, version}` and writes a human copy through
  `documents/outputs.publish_copy` (Documents/Eclipse or `saveTo`). The source
  deck is never modified.
- `format: "pdf"` renders each slide as a PDF page with the vendored Matplotlib
  backend; `format: "png"` returns a ZIP of per-slide PNGs (`slide-01.png` ...).
  Both work on a fresh clone without LibreOffice and carry the same
  `overflowSlides`/warnings.
- Every deck write (create, editor save, AI save) refreshes the artifact's
  first-slide thumbnail via `ArtifactService.set_preview_thumbnail`; a rendering
  failure leaves the outline preview in place.
- A missing `python-pptx` is `PRESENTATION_LIBRARY_MISSING` (503), never a fake
  file; a missing Matplotlib is the same code for PDF/PNG.

## Tools and command

| Tool | Purpose |
|---|---|
| `presentation_create` | deck artifact + exported `.pptx` (the `export:false` flag skips the file) |
| `presentation_edit` | revision-guarded typed ops; a stale `baseVersion` is a 409 |
| `presentation_export` | render an existing deck to a new `.pptx`, PDF or PNG ZIP |
| `presentation_outline` | read slide ids/layouts/titles before editing |
| `presentation_import` | turn an existing `.pptx` artifact into an editable deck outline |

All five are in `agents.DOCUMENT_TOOLS`. `/deck` rewrites the request into a deck
directive and runs as a normal agent turn (`command-agent.ts`). The one-shot
`document_create(format="pptx")` now delegates to the same exporter, so there is
one layout writer.

## Editing

The Canvas kind is `presentation` (MIME
`application/vnd.eclipse.deck+json`, `.deck.json`). The editor's changes are
serialized back to the artifact content and saved by the existing Canvas
revision guard (a 409 is surfaced, never overwritten). Undo/redo is local history.

`presentation_edit`/the editor use only the closed op vocabulary in `ops.py`.
Canvas **text** ops are refused for decks (`apply_operations(kind="presentation")`
raises), and a save with a text op is rejected by kind. The Canvas AI panel is
available for decks: `CanvasEditOrchestrator` branches on
`CanvasKind.PRESENTATION`, asks the model for a strict `DeckOp` proposal, applies
those ops to the current JSON under the revision guard, and returns them in
`deckOperations` (never as a full replacement body). Accepting the proposal sends
those ops back through the same canvas save path.

## Importing an existing deck

`presentation_import` reads a `.pptx` already in the Artifact Center and builds a
new editable deck: slide titles, text lines as bullets (bounded to the deck
limits), table rows as text, and speaker notes. It is an **outline**, not a
lossless round-trip: masters, free positioning, animations, charts and images are
not represented in `DeckSpec`, and the tool names what it dropped in `warnings`.

## Not implemented yet

- A template gallery and a brand kit (logo/palette persistence beyond a theme).
- Animations, transitions, SmartArt and arbitrary positioning.
- Lossless `.pptx` round-trip (the importer is an outline by design).

The roadmap and the remaining phases are in `docs/presentations-plan.md`.

## Tests

- `python/tests/test_presentations.py` (33) + `test_presentations_api.py` (6) —
  contract, layout/fit, ops, AI deck proposals (fake model), export (`.pptx`,
  PDF pages, PNG ZIP, thumbnails), import, service revision guard + provenance,
  deck preview outline, the tools, and the theme/export HTTP routes.
- `src/lib/presentation/{schema,layout,theme}.test.ts` — validator fixtures,
  Python layout parity (golden), palette parity.
- `src/components/screens/canvas/render-slide.test.tsx` — every layout renders,
  UNAVAILABLE chart/image states.
- `src/components/screens/canvas/proposal-operations.test.ts` — deck ops win the
  proposal path.
- `src/lib/harness/commands/deck-command.test.ts` — `/deck` resolution and
  directive.

### Known deviations from the plan

- `timeline`/`comparison` layouts from the plan are not in v1; `two_column`
  covers the comparison shape, and a timeline can be expressed as bullets.
- Import is an outline, not a design-preserving conversion (the plan's stated
  position).
