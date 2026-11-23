# The chart engine — Eclipse draws its charts with Python

**Status:** Active. **Last verified:** 2026-11-19.

Eclipse renders every chart with the Python runtime's chart engine: a validated
**chart spec** goes in, `matplotlib` draws a transparent PNG, and the picture is
recorded in the Artifact Center. There is no browser charting library in the
render path.

This document is the canonical description of that engine **and** of the plan
that retires ECharts.

---

## 1. What the engine is for

The **chat's** charts are drawn by this engine, and **only** by this engine.
A `chart` block in an answer becomes a **real chart**: the runtime authors it
in Python (a Plotly *figure*), and the browser renders it as **vector SVG** with
zoom, pan, a hover readout, a legend and a PNG/zoom toolbar. It is a chart, not
a picture of one.

The chat has no ECharts path: a chart in a chat answer is never a live canvas
of the old kind — it is a live canvas of the new kind.

Two outputs come from one spec:

| Output | What it is | Used for |
|---|---|---|
| the **figure** | a Plotly figure (JSON): vector, interactive | the chat |
| the **picture** | a PNG (matplotlib), recorded as an artifact | the PNG/PDF export, and the fallback when the chart library cannot load |

Both are produced from the SAME data, so they can never disagree.

**The rest of the application is NOT part of this.** The journal, the
portfolio, the dashboard, the backtest screen's interactive equity/drawdown
charts, the economics and fundamentals panels, the workspace widgets, the price
chart, the Investor Eye map and the relationship graphs all stay on ECharts.
They draw from live data at interactive rates (a price chart that redraws on
every tick, a map the user hovers), which a server-drawn picture cannot serve.
The one exception is the Backtest bench's computed **Quant figures** panel: it
shows the same `ChartSpec` figures the chat shows, through the same chat chart
surface (`ResponseChart`), because those are analysis deliverables rather than
live canvases.

## 2. The contract

### 2.1 Two forms, one vocabulary

A chat chart arrives in one of two forms, and the engine draws both:

| Form | The block carries | The engine draws |
|---|---|---|
| **simple** | `chartType` + `series` | that kind of chart (line, bar, pie, heatmap, candlestick, …) |
| **scene** | `panels` → `marks` | exactly the marks you wrote |

The scene is what makes **any chart** possible. A fixed list of kinds can only
draw the charts someone thought of; a scene is compositional, so a violin, a
box plot, a Gantt, a funnel, a ridgeline, an error-bar chart or a dual-axis
chart is a **composition of the primitives** — not a new kind that has to be
invented for it.

**The mark vocabulary** (each mark carries its own data and styling):

`line` · `area` · `bar` · `scatter` · `band` · `rule` · `label` · `box` ·
`violin` · `histogram` · `errorbar` · `heatmap` · `pie` · `donut` ·
`candlestick` · `waterfall` · `ridge` · `stem` · `treemap` · `sunburst` ·
`funnel` · `sankey` · `scatter3d` · `surface` · `regression`

**A mark may name its series instead of carrying it.** A mark (or a whole
chart block) may set a `dataSource` — `{symbol, market?, interval?, start?,
end?, field?, name?}` — and the ENGINE fetches that series through the same
market gateway every price comes from, then draws it. This is what keeps a
250-point chart out of the model's mouth: a chart that spells out every price
is a long message, and a long message gets truncated — a truncated chart block
is a lost chart, with no diagnostic and no picture. With a named source the
model writes ~200 characters and the reader gets the real 251-bar series,
fitted. A source that cannot be fetched is a refusal, never a guess.

**A quant figure names a stored experiment instead of a market symbol.** A
`chart` block may carry
`quant: { experimentId, figure }` (or a `caption`). The Python Quant Figure Kit
builds the spec from the stored artifact (`python/harness/quant/visuals.py`), and
the browser fetches it from
`GET /quant/experiments/{id}/figures/{figure}`. This is the second named source
family: the block stays tiny, the numbers never pass through the model, and the
figure can only show what the engine measured. An unavailable figure is an
honest empty state with the runtime's reason, never a guessed chart.

**A mark's values may be written the way a person writes them.** `y` / `data`
accept a scalar, an `[x, y]` pair (the pair carries its own x), or an object
with named fields — `{i: 1, close: 188.7}` (a field named like an x is the x,
one named like a y is the y, and when the names say nothing the first two
numeric fields are the point, in order). A model writes points; a chart that
refuses a point over its FORMAT is a chart the reader never sees.

**A mark's `x` may be labels, not just numbers.** `"Q1"` / `"Jan"` / a product
name is drawn at its index on the panel's shared category axis (every mark in
the panel uses the same first-seen order, so a bar and a line line up), and the
axis is ticked with the labels. A value that is neither a number nor a label is
a **gap**, never a crash: the renderer used to fail the whole picture with
`could not convert string to float`.

A mark may set: `name` (the legend), `y` / `x` / `z` / `values` / `lower` /
`upper` / `sizes` / `matrix` / `labels` / `rowLabels` / `colLabels`, `color`,
`alpha`, `width`, `style` (`solid` / `dashed` / `dotted`), `fill`, `label`,
`yAxis` (`left` / `right` — a dual-axis chart), `stack` and `bins`.

**A regression is a mark, and the engine fits it.** `regression` takes the
points (`x` and `y`) and draws them, plus ONE ordinary least-squares line
through them, with the R-squared in the legend. The fit is computed by the
engine from the same numbers it draws - it is never smoothed, weighted or
robustly re-fitted behind the reader's back, so the picture and the R-squared
can never disagree. A fit that cannot be computed (fewer than two points, no
variation in x, no variation in y) is NOT drawn: the points are, and the trace
says why. A missing reading drops its own point and never shifts the x axis
under the points that remain. The same arithmetic is in
`python/harness/charts/regression.py`, and it is plain on purpose: a reader
must be able to check `R2 = 0.87` from the picture.

**3D is a mark, not a separate chart.** `scatter3d` (a point cloud) and
`surface` (a grid) carry a third dimension (`z`) and live in the figure's
`scene` — a camera, three axes and a cube — so the reader can orbit, zoom and
pick points. The flat axes step aside for a 3D chart. A 3D panel also gets a
real matplotlib 3D axes for the PNG fallback, so the picture is not a blank
flat box under the live scene. A surface grid may be written either as a
`matrix` (rows of z) or, the way a parameter sweep is usually written, as
`x` + `y` + a FLAT row-major `z` (`len(z) == len(x) * len(y)`); both become the
same grid. A 3D mark with no grid/three vectors is refused by name, never drawn
as an empty box. The Quant Figure Kit uses 3D only where the third dimension is
measured (a parameter sensitivity surface, for example) and falls back to a 3D
point cloud when the grid is incomplete.

**An unsupported mark is named, never silently substituted.** The wire contract
preserves a mark `type` the renderer does not know (and `(missing)` when the
field is absent), so `response_validate` and the chart surface can report
exactly which primitive was refused. A mark's primitive is always `type`; the
parser also accepts `kind`/`markType` as synonyms so a well-meant mark is
recovered instead of being lost.

A panel may set: `title`, `xLabel`, `yLabel`, `valueFormat`, `logScale` and
`height` (its share of the figure). A scene is up to 6 panels.

### 2.2 The spec

`python/harness/charts/spec.py` — `ChartSpec` (pydantic, `extra="forbid"`).

| Field | Meaning |
|---|---|
| `kind` | `line`, `area`, `bar`, `stackedBar`, `pie`, `donut`, `scatter`, `histogram`, `heatmap`, `candlestick`, `waterfall`, `radar`, `gauge`, `treemap`, `scene` |
| `panels` | The scene: panels of marks (see above) |
| `title` / `subtitle` / `footnote` / `source` | The text around the picture |
| `xLabel` / `yLabel` | The axis labels |
| `categories` | The x-axis labels (dates, tickers, periods) |
| `series[]` | `{ name, data[], color?, style?, band?, colors? }` — `data` holds `null` for a **missing reading** (never `0`) |
| `labels` / `values` / `colors` | A pie/donut's slices and their colours |
| `points[]` | A scatter's `{ x, y, label?, size?, color? }` |
| `matrix` / `rowLabels` / `colLabels` | A heatmap's cells and its two label axes |
| `candles[]` | A candlestick's `{ open, high, low, close }` |
| `referenceLines[]` / `highlights[]` | Named horizontal lines; shaded category spans |
| `volumeData[]` | An optional volume pane under the chart |
| `valueFormat` | How the value axis is written (`percent`, `currency`, `compact`, `r`, `number`) |
| `theme` | `eclipse-dark` (default) or `eclipse-light` |

A per-kind validator states what each kind needs: a pie needs one label per
value, a heatmap needs rectangular rows, a line needs a series, a scene needs
at least one mark. A spec that does not satisfy its kind is refused with the
missing part named.

The wire shape is **camelCase** (like every other harness model); the
snake_case field names stay valid too.

### 2.3 The theme

`python/harness/charts/theme.py`. The background is **transparent** so the
figure sits on the frosted glass card without hiding the frost; the ink is
light because the surface behind it is dark. The default series palette is a
**categorical colour set** (ocean blue → amber → violet → rose → teal →
fuchsia), matching the browser default, because a chart the reader has to
decode by shade is a chart that hides its own data. Market direction keeps the
conventional green / red pair, and heatmaps use the same diverging pair. The
light theme is a real theme, not an inversion, because the app has a light
mode.

Greyscale is still a real choice, not a missing colour: the "Black & white"
presets in Settings › General › Charts replace the default for the live figure
and the drawn picture alike. Nothing forces a hue on a chart whose author
wanted none.

Colours are the user's, not the engine's: the browser applies the palette from
Settings › General › Charts to the live figure (`components/charts/apply-palette.ts`),
so a colour change repaints instantly without asking the runtime to redraw, and
"Save as PNG" exports exactly what is on screen. The Python palette is what a
static export (the PNG fallback and the Artifact Center copy) is drawn with, so
the two start from the same coloured default.

### 2.4 The honesty rules

* A figure is always `computed`, never evidence. `RenderedChart.to_dict()`
  emits `isEvidence: false` and there is no field that can change it
  (ADR-H123).
* The renderer draws what the spec says and **nothing else** — it never
  invents a point, a mean, an axis or a scale.
* A missing `matplotlib` is a named refusal (`CHART_RENDER_UNAVAILABLE`), not
  an empty image.
* The picture's identity is the digest of its **spec** (`spec_digest`), so the
  same spec always draws the same picture.

### The chart block IS the drawing

A chart block carries the DATA, and the engine draws it. The agent does not
call a drawing tool to read the numbers back afterwards: the figure carries
them (a `regression` mark fits the line and shows the R-squared in its
legend), and a block costs ONE step instead of a round trip. The same rule
governs the data behind it: **one attempt per route**. A route that refuses is
a routing problem, not a reason to keep asking; after the second refusal for
the same series, the agent uses the data it holds (or the numbers the user
gave) and says plainly what the data is and where it came from. The reader
wants the analysis, not a provenance audit.

## 3. The surfaces

| Surface | Entry point |
|---|---|
| The agent | the `python_chart` tool (`python/harness/charts/tools.py`) — registered on the one tool registry and on the chat agent's allowlist |
| The front door | `POST /api/harness/charts` → `python/harness/routers/charts.py` → `POST /charts/render` (operator-gated) |
| The browser | `src/lib/harness/charts.ts` (`renderChart`) + `src/components/charts/plotly-chart.tsx` (the live chart) |
| The chart library | `src/app/api/harness/charts/plotly/route.ts` (serves plotly.js from the Python package, cached) |
| The **chat's chart blocks** | `src/lib/response/chart-spec.ts` (block -> spec) + `src/components/response/response-chart.tsx` (the picture) |
| The **quant figure source** | `src/lib/response/chart-data.ts` (`fetchQuantFigure`) -> `GET /quant/experiments/{id}/figures/{figure}` -> `python/harness/quant/visuals.py` |
| The **backtest figure panel** | `src/components/screens/backtest/shared/quant-figure-panel.tsx` (reuses `ResponseChart`) |
| The drawn-chart registry | `src/lib/response/chart-registry.ts` (block id -> artifact id, for PNG/PDF export) |

The picture is returned as an **artifact**, not as bytes: the browser fetches
it back through `/api/artifacts/<id>/download`, so a chart is discoverable,
downloadable and versioned.

**Artifact registration is best-effort, and never hides a drawn chart.** If the
Artifact Center cannot record the picture (a locked or absent database on a
fresh install), the render response carries the PNG inline (`image:
{mimeType, data}`) and the browser shows it from a data URL. The chart is still
real; only its durable copy is missing. A "could not be stored" box is never
shown in place of bytes that exist. `chartImageSrc()` in
`src/lib/harness/charts.ts` is the one place that chooses artifact URL vs inline
image.

```
chart block ──▶ chart-spec.ts ──▶ spec ──▶ python_chart tool / /charts/render
                                                        │
                                                        ▼
                                            matplotlib ──▶ PNG ──▶ Artifact Center
                                                        │
                                                        ▼
                                      browser ◀── /api/artifacts/<id>/download
```

### 3.1 The live chart

The engine's figure is rendered by the browser with **plotly.js**, which the
app serves from its OWN route (`/api/harness/charts/plotly`): the library ships
inside the Python package, so it is never vendored into git and never fetched
from a CDN — the app keeps working offline. The browser loads it once and
reuses it for every chart.

The route **asks the running interpreter where its plotly lives** (falling back
to the vendored/managed site-packages paths), so a system Python, a venv or the
Windows vendored install all resolve. It is deliberately `force-dynamic`: a
static build-time answer was cached, which meant a package installed after
`next build` was invisible and a build that raced `postinstall` cached a
permanent 503.

The figure has **no background** (`paper_bgcolor` and `plot_bgcolor` are
transparent), so the chart sits on the app's frosted surface without hiding
the frost. The chart's ink, grid and hover label are the app's own tokens.

If the library cannot load, the chat says so and falls back to the drawn
picture — an honest degradation, never a broken chart.

**Loading is staged so a chart is visible immediately.** The 4.7 MB library is
never on the critical path of an answer:

1. the drawn PNG (an artifact fetch) paints the chart as soon as it arrives;
2. plotly.js is loaded only when the chart is about to be seen (an
   `IntersectionObserver` with a 400 px margin) and on an idle frame
   (`requestIdleCallback`), then cached for the rest of the session;
3. the live chart cross-fades in over the PNG when it is ready.
The rendered figure is also cached per spec (the `RENDER_CACHE` in
`src/components/response/response-chart.tsx`), so scrolling chat history never
asks the engine to draw the same chart twice. The chart registry is keyed by
the block id and points at the artifact, which is what remains the export
source.

Every live chart carries its own view controls in a compact row directly
**under the plot** (not floating over it): **zoom in**, **zoom out** and
**reset**. Zoom scales the current 2-D axis ranges around their centre (or the
3-D camera eye) instead of jumping to a preset, and reset restores the figure
the engine authored (axes or camera). A 3-D scene is **orbitable by drag**
(`scene.dragmode: "turntable"`; the 2-D `dragmode: "pan"` must never be
inherited by the scene). Plotly's own modebar stays available for pan/box-zoom
and PNG download; double-clicking the chart also resets it.

### 3.2 The picture is hoverable

A PNG cannot be hovered. The engine therefore returns, with the picture, an
**interaction map**: the picture's own pixel geometry (the axes' box) and, per
category, its x position and each series' value there
(`python/harness/charts/interaction.py`).

The browser draws the pointer experience over the picture from that map: a
crosshair at the nearest category and a tooltip with the values
(`src/components/charts/chart-hover.tsx`). Because the map is in the picture's
pixels and the browser converts it to percentages, the interaction survives any
display size — the picture is never stretched, and the crosshair still lands on
the right column.

The map is a convenience, never a claim: the values it carries are the same
values the picture was drawn from, and a gap stays a gap (no value at all).

### 3.2 The picture's size and its legibility

The chat draws at **1000×560** and shows it at its natural aspect, left
aligned, capped at 860 px wide (`object-contain`, no fixed height), inside a
card that **shrinks to the picture** (`w-fit`) rather than spanning the
column. The old `w-full` + a fixed `maxHeight` stretched the picture; now it
is never distorted, and the focus view simply shows the same picture larger.

Two rules keep the picture readable:

* **The text is a fraction of the picture, not of the screen.** The engine
  draws in fewer inches at a higher resolution (`DRAW_DPI = 150`), so the
  labels and strokes scale with the picture — a chart shown at 860 px has
  legible labels whether it was drawn at 720 px or at 1400 px.
* **Crowded axes are thinned, never faked.** When a category axis carries
  more than ~12 labels, the engine keeps every Nth label (and turns long
  ones, because a row of `2026-07-01` strings cannot share a line). The DATA is
  never thinned — only the labels are.

### 3.3 First-run install

Charts must work on a fresh install without a manual pip command. `plotly`
therefore lives in `python/analysis-requirements.txt` alongside
`numpy` / `pandas` / `matplotlib`, and both `scripts/setup-python.js` (postinstall)
and `scripts/doctor.js` probe **all four**. This matters for an install that
already had `langgraph`: the runtime marker used to skip the requirements file
forever, so the chart stack was never completed. A missing stack is repaired with
`npm run setup:python`.

**`langgraph` alone is not "the runtime is installed".** A partially installed
interpreter can import the graph engine while every provider package is absent.
`PYTHON_RUNTIME_DEPS` in `runtime-config.ts` (and the equivalent probes in
`setup-python.js` / `doctor.js`) therefore requires `langchain_openai` as well —
otherwise the launcher selected that interpreter over the complete vendored one
and every model call failed with `No module named 'langchain_openai'`.

**"Installed" is not "can draw".** The agent stack (`langgraph`) can be healthy
while `matplotlib`/`plotly` are absent — those imports are lazy, inside the
renderer, so a boot probe never touches them. The contract that makes this
visible:

* `src/lib/harness/runtime-config.ts` — `PYTHON_CHART_PACKAGES` names every
  third-party module `python/harness/charts/*.py` imports (`matplotlib`,
  `numpy`, `plotly`, `pydantic`), and `PYTHON_RUNTIME_PACKAGES` installs each
  one. `chartEngineReadiness()` is the single "can draw" decision, shared by the
  front door, the doctor and the tests. `chartEngineReadinessFromHealth()` turns
  the runtime's own `/charts/health` document into that decision.
* `probeChartEngine()` in `src/lib/harness/python-runtime.ts` asks the **running
  runtime** first (`GET /charts/health`, the same process that will draw, so
  `python` is its own `sys.executable`). Only when the runtime does not answer
  does it fall back to probing a local interpreter. This distinction matters:
  a separately resolved interpreter can disagree with the running runtime — a
  pinned release whose build resolved a path the runtime never used reported
  "missing matplotlib, numpy, plotly, pydantic" and refused every chart while
  the live runtime could draw. The drawing process is the authority on drawing.
* `findPython()` only prefers the vendored embedded interpreter when that file
  actually exists; a path computed at build/load time is never reported as a
  usable interpreter it is not.
* The front door's `POST /api/harness/charts` returns **three different
  503s**: `RUNTIME_STARTING` (the runtime is installed and still booting or
  restarting — retryable, `Retry-After: 2`, never a repair instruction),
  `RUNTIME_UNAVAILABLE` (no interpreter is installed — a repair instruction),
  and `CHART_RENDER_UNAVAILABLE` (the runtime is up but a drawing library is
  missing — with the interpreter path and module names). The UI shows that
  cause instead of generic italic text.
* **A booting runtime is waited out, never reported as an error.** One page
  request may wait only a few seconds on a cold start (long waits starve the
  browser's per-origin sockets), so the chart surface keeps a calm
  "Starting the chart engine…" state and retries the render with backoff for
  several minutes (`isRetryableChartError`). "Run `npm run setup:python`" can
  only appear when the interpreter is genuinely absent, and "Try again" is
  offered as a manual fallback.
* The runtime's `GET /charts/health` reports `{ matplotlib, plotly, numpy,
  python }` from inside the drawing process.
* **Never again:** `src/lib/harness/runtime-config.test.ts` scans the charts
  package for third-party imports and fails when one is absent from
  `PYTHON_RUNTIME_PACKAGES`, and asserts the selected interpreter can import
  the whole chart stack. `scripts/setup-python.js` and `scripts/doctor.js` also
  verify by *importing* after an install (retrying with `--force-reinstall` when
  a distribution exists but is unimportable), and `scripts/test-python.js`
  refuses to run the Python suite without the stack — so a pip that exits 0 but
  leaves the library unusable is a loud failure rather than a silent skip.

## 4. What the user gets

| | The chat's chart |
|---|---|
| Engine | the Python chart engine: a Plotly figure authored in the runtime, rendered as SVG |
| Interaction | zoom, pan, a hover readout, a legend, a PNG/zoom toolbar — it is a chart, not a picture |
| Any chart | yes: the SCENE form draws any composition of the marks (and the family charts — treemap, sunburst, funnel, sankey — are marks now) |
| Export | the artifact's bytes (PNG and PDF both embed the picture); the chart's own toolbar also exports a PNG at 2× |
| Background | none — the chart is transparent, so the frosted surface shows through |
| Provenance | always `computed`, never evidence |

What the rest of the app does is unchanged: ECharts, with its live hover,
crosshair and zoom.

## 5. Where the code lives

| Piece | Path |
|---|---|
| The spec | `python/harness/charts/spec.py` |
| The theme | `python/harness/charts/theme.py` |
| The renderer | `python/harness/charts/render.py` |
| The service (artifact) | `python/harness/charts/service.py` |
| The agent tool | `python/harness/charts/tools.py` |
| The route | `python/harness/routers/charts.py` |
| The browser client | `src/lib/harness/charts.ts` |
| The component | `src/components/charts/python-chart.tsx` |
| The front-door route | `src/app/api/harness/charts/route.ts` |
| The response-engine translation | `src/lib/response/chart-spec.ts` (a chart block → the engine's spec) |
| The response chart surface | `src/components/response/response-chart.tsx` |
| The chart registry | `src/lib/response/chart-registry.ts` |
| The scene (panels of marks) | `python/harness/charts/scene.py` + `scene_render.py` |
| The live figure | `python/harness/charts/plotly_figure.py` |
| The chart library route | `src/app/api/harness/charts/plotly/route.ts` |
| The live chart component | `src/components/charts/plotly-chart.tsx` |
| Tests | `python/tests/test_charts.py`, `python/tests/test_chart_scene.py`, `python/tests/test_chart_figure.py`, `src/lib/harness/charts.test.ts`, `src/lib/harness/runtime-config.test.ts`, `src/app/api/harness/charts/route.test.ts`, `src/lib/response/chart-spec.test.ts` |

The decision itself is **ADR-050** (`skills/ARCHITECT.md`, mirrored in
`docs/DECISIONS.md`).
