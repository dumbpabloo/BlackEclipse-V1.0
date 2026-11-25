---
title: Video Pipeline — fframes Render Worker
status: Current
last-verified: 2026-11-24
---

# Video Pipeline — fframes Render Worker

**Who this is for:** anyone building the native video capability, or a developer
whose first `cargo build` of the worker fails on Windows.

## What exists today (Phases 1–8 — worker, job layer, tools, storyboards, evidence, preview/repair, chat surface, hardening)

Two pieces, cleanly separated:

1. **The Rust worker** (`rust/fframes-worker/`) — renders the built-in
   10-second title card AND data-driven storyboards (`--project spec.json`):
   seven scene types composed from typed spec parameters on a shared dark
   theme. [fframes](https://github.com/dmtrKovalenko/fframes) is pinned exactly
   to `1.2.0` (committed `Cargo.lock`) with the `compile-time-svgtree` feature
   (required for scene overlaps — see Phase 4). Phase 1 proved the hard part:
   fframes builds and renders on this Windows/MSVC machine against a
   checksum-verified FFmpeg build, deterministically and fast (the 32 s demo
   storyboard renders in ~6.5 s on the CPU backend). Its CLI also provides the
   review commands the job layer drives: `strip` (contact sheet), `frame`
   (PNGs), `onion`, `svg`, `inspect`.

2. **The job layer** (`python/harness/video/`) — projects, previews,
   draft/final renders, frame inspection and artifact registration, executed as
   queued **Execution Fabric** jobs (job type `video` on its own `video` queue,
   on a dedicated worker). Renders are never inline; the Fabric owns durability,
   retries, cancellation and lease recovery. Phase 3 exposed it to the agent
   (the `video_*` tools + the bundled `system.video-production` skill);
   Phase 4 added the versioned storyboard schema, the scene library and
   project revisions; Phase 5 made figures evidence-bound; Phase 6 added PNG
   previews and the bounded repair loop; Phase 7 added the HTTP surface
   (`/video/*`), the chat job card and the Artifact Center's video
   thumbnail + inline player; Phase 8 added the resource ceilings (queue caps,
   workspace GC), the isolation pins, the worker SBOM and the scheduled-recap
   template.

## Phase 2 — the Fabric job layer

The integration plan's worker operations map onto the service like this:

| Operation | Where | Notes |
|---|---|---|
| `create_project` / `validate_project` | `VideoService` + `VideoProjectStore` | Filesystem-backed minimal projects (`<video-root>/projects/<id>.json`): stable id, name, kind (`title_card` today), free-form spec. Validation = spec problems + environment readiness (binary built, FFmpeg tree present). The versioned storyboard schema is Phase 4. |
| `render_draft` / `render_final` | `VideoService` → Fabric job | Submits a `video` job (`action=render`, `mode=draft\|final`). |
| `preview_project` | `VideoService` → Fabric job | Submits a `video` job (`action=preview`): PNG contact sheet + one frame per scene, registered as image artifacts (Phase 6). |
| `render_repair` | `VideoService` → Fabric job | One bounded repair of a failed render (Phase 6): optional spec revision, linked via `repairOf`, capped per failure line. |
| `inspect_video` | `VideoService` → Fabric job | Inspects a completed render by `job_id`, or by `project_id` for its latest successful render. |
| `get_job_status` / `list_jobs` / `cancel_job` | `VideoService` | Reads/operates on the Fabric job records. |
| `get_artifact` | `VideoService` → Artifact Center | The final render's MP4, registered with `source_type=job`, tags `["video","fframes"]` and video metadata (dimensions, frames, duration, inspection summary). |

**What one render job does** (`VideoService.run_render_job`):

1. Resolve and validate the project — an unknown id is a structured failure,
   never a guessed render.
2. Render with the Rust worker as a fixed-argv child process
   (`fframes-worker render --json [--draft] -o <path>`), streaming the
   worker's JSON progress events into Fabric progress (`frame N/M`) and
   killing the child on cancellation.
3. **Inspection gate** — `fframes-worker inspect --json` runs on the produced
   file. Findings (missing fonts, clipped text, invalid SVG) fail the job with
   `INSPECTION_FAILED`; the MP4 stays on disk and referenced in `report.json`.
4. **Final renders only**: register the MP4 into the Artifact Center — a
   catalog failure fails the job (`ARTIFACT_REGISTRATION_FAILED`), because the
   user-visible promise is the artifact, not just a file on disk.

Every job writes `report.json` next to its output
(`<video-root>/jobs/<jobId>/`): request, structured result, raw
render/inspection details. A worker exit of 0 is never trusted on its own — a
missing or empty output file is `RENDER_OUTPUT_MISSING`.

**Lifecycle**: `QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED | TIMED_OUT`
(mid-flight cancel requests pass through `CANCEL_REQUESTED`). A worker that
dies mid-render leaves an expired lease; Fabric recovery re-queues the job
(within `max_attempts=2`) and a fresh boot re-drives it — renders are
deterministic, so a retry is safe. The worker registers in
`fabric/builtin_workers.py`; its policy lives in `fabric/policies.py`.

**Using it** (Python — the seam the `video_*` tools wrap):

```python
project = service.video.create_project(name="BTC recap", kind="title_card")
handle = await service.video.render_draft(project["id"])   # {"jobId", ...}
job = service.video.get_job(handle["jobId"])               # status/progress
final = await service.video.render_final(project["id"])    # registers the artifact
await service.video.cancel_job(final["jobId"])
```

**Environment** (all optional): `HARNESS_VIDEO_ENABLED`,
`HARNESS_VIDEO_WORKER_BIN`, `HARNESS_VIDEO_FFMPEG_DIR` (falls back to
`FFMPEG_DIR`), `HARNESS_VIDEO_ROOT` (default `<site>/.black-eclipse/video`),
`HARNESS_VIDEO_DRAFT_TIMEOUT_MS` (10 min), `HARNESS_VIDEO_FINAL_TIMEOUT_MS`
(30 min), `HARNESS_VIDEO_INSPECT_TIMEOUT_MS` (5 min),
`HARNESS_VIDEO_PREVIEW_TIMEOUT_MS` (3 min),
`HARNESS_VIDEO_TRANSIENT_RETRIES` (1 in-job retry of transient failures),
`HARNESS_VIDEO_MAX_CONCURRENT` (1),
`HARNESS_VIDEO_MAX_PENDING_PER_PROJECT` (4 queued+running jobs per project),
`HARNESS_VIDEO_GC_ENABLED` (1), `HARNESS_VIDEO_GC_KEEP_JOBS` (20),
`HARNESS_VIDEO_GC_MAX_TOTAL_MB` (2048).

**Tests**: offline runner + service/Fabric suites with a fake worker holding
the same machine contract (`python/tests/test_video_{runner,service,tools}.py`,
`video_fakes.py`) and an opt-in real end-to-end
(`HARNESS_VIDEO_IT=1` → `tests/test_video_real_smoke.py`: real draft, real
final, artifact registered, real inspection, and the full tool workflow).

## Phase 3 — agent tools + the video-production skill

Fourteen typed tools on the ONE shared `ToolRegistry` (so schema validation,
timeouts, events and the audit trail apply unchanged), registered by
`video/bootstrap.py` and allowlisted for `eclipse-agent` in `agents.py`:

| Tool | What it does |
|---|---|
| `video_capabilities` | Readiness (worker binary, FFmpeg tree) + supported project kinds, schema version and limits — the honest first call for any video request. |
| `video_create_project` / `video_update_project` / `video_list_projects` | Create, revise (revision bumped) and list projects (`title_card`, or `storyboard` with a schemaVersion 2/3 spec). |
| `video_validate_project` | Spec problems + environment readiness before any render. |
| `video_resolve_data` | Resolve bindings through the gateway WITHOUT rendering; stale flagged (Phase 5). |
| `video_preview` | Queue the PNG preview: contact sheet + per-scene frames as image artifacts, with frame findings (Phase 6). |
| `video_render_draft` / `video_render_final` | Queue the draft / final render jobs; the final registers the MP4 in the Artifact Center. |
| `video_render_repair` | One bounded repair of a failed render: revised spec, `repairOf` link, capped per failure line (Phase 6). |
| `video_inspect` | Queue a frame inspection of a completed render (by job, or a project's latest render). |
| `video_job_status` / `video_list_jobs` / `video_cancel_job` | Track, list and cancel the queued jobs. |

All fourteen are `permission="read"` / `risk="low"`: they queue Execution-Fabric
jobs and write only into the confined video workspace and the Artifact Center
— the same class as `ml_*` and `document_create`, and deliberately NOT part of
any effectful tuple in `code/policy.py` (an autonomy profile that narrows
those tools never silently widens video access elsewhere).

The bundled skill `system.video-production`
(`python/harness/skills/library/system/video-production/`) is the procedure an
agent follows: capabilities → create (or update) → validate → draft → poll →
inspect → final → hand over the artifact id, with the honesty rules (a video
exists only when a job SUCCEEDED; `INSPECTION_FAILED` / `TIMED_OUT` /
`CANCELLED` are reported as such) and the capability boundary (spec values are
display strings the agent must source from the conversation or real tools —
never inventing a number; evidence binding is Phase 5).

**Acceptance (Phase 3)**: the opt-in real smoke `test_real_tool_workflow` runs
the whole workflow through the registered tools against the real worker —
capabilities → create → draft → status → inspect — and passes on this machine.

## Phase 4 — the storyboard schema + scene library

A video project can now be a **storyboard**: a versioned (`schemaVersion` 2),
validated spec describing an ordered scene list. One authority for the schema
is `python/harness/video/spec.py` (precise problems with paths; unknown keys
are rejected, never silently dropped); the Rust worker re-checks load-bearing
bounds so a hand-edited spec fails cleanly instead of panicking a render.

| Scene type | Parameters |
|---|---|
| `intro` / `outro` | `title`, optional `subtitle` — full-frame titles; the outro fades to black over its last 0.8 s |
| `metric` | `title` + 1-4 cards `{label, value, delta?}` — staggered entrance, `+`/`-` deltas colored |
| `candlestick` | `title`, `symbol`, 2-60 `{o,h,l,c}` — real value mapping, grid, last-price line, left-to-right reveal |
| `comparison` | `title`, `left`/`right` panes `{label, value, detail?}`, optional `winner` — the winner gets the accent frame |
| `timeline` | `title`, 2-6 events `{label, detail?}` on a revealed axis |
| `quote` | `text` (wrapped), optional `attribution` |

Common: `durationSeconds` 1-30, `transition` `cut` (default) | `fade`
(0.4 s cross-fade). 1-12 scenes, total ≤ 300 s, `format: "landscape"`
(1920x1080@30 — the only format today), `theme: "dark"`, optional
`style.accent` `#RRGGBB`.

**Revisions, not rebuilds.** `video_update_project` replaces a project's spec
and bumps its `revision`; the next render is a queued job against the new
spec, and the job workspace keeps a copy of the exact spec it rendered
(`jobs/<jobId>/spec.json`) — so `video_inspect` re-renders the SAME video
(the inspector is invoked with `--project`, never against a different one).

**One non-obvious requirement**: the worker enables fframes'
`compile-time-svgtree` feature. Without it `Svgr` is a raw string and
overlapping scenes merge by **string concatenation**, producing a two-root
document the SVG parser rejects — every `fade` transition would fail at the
overlap frames. With it, scene trees compose as nodes; static-subtree caching
comes along for free (the 32 s demo renders in ~6.5 s).

**Acceptance (Phase 4)**: the opt-in `test_real_storyboard_render_and_inspect`
renders a real 3-scene storyboard (with fades) through the Fabric, inspects it
with the same spec, and passes on this machine; a 7-scene / 32 s demo (intro,
metrics, 26 candles, comparison, timeline, quote, outro) renders, inspects
clean and was reviewed frame-by-frame.

## Phase 5 — evidence-bound financial data

A `schemaVersion` 3 storyboard can DECLARE where its figures come from instead
of hand-writing them; the render job resolves the bindings through the
EXISTING Market Data Gateway (no second provider, client or cache) before the
worker runs:

- candlestick scenes: `data: {symbol, market?, interval: 1h|4h|1d, count}`
  replaces static `candles` (never both).
- metric items and comparison panes: `source: {symbol, market?, field:
  price|changePct|volume}` replaces `value` (never both).

The resolver (`video/data.py` + `video/market_provider.py`, over a gateway
built on the service's read-only bridge) formats values with units
(`$68,240`, `+2.41%`, `38.20B`), enforces candle parity after rounding, and
produces the **EvidenceManifest** — per figure: provider, source, retrieval
time, data timestamp, units, freshness, quality, warnings. The manifest
travels with the result: job output, `report.json`, and the Artifact Center
metadata on a final render.

Rules, enforced end-to-end:

- **Missing data fails the render** (`DATA_UNAVAILABLE`, naming the binding):
  no fabricated fallback, ever.
- **Stale data is flagged three times**: the gateway's `data_status: STALE`
  verdict becomes an amber STALE chip in the video, `evidence.stale: true` in
  the manifest, and a visible `stale` in `video_resolve_data`.
- Every bound scene renders a source line into the video itself
  (`Source: yahoo · delayed · retrieved 2026-10-18 03:12 UTC`).
- `video_resolve_data` (tool #12; read) previews every bound figure — what it
  will show, provider, retrieval time, freshness — WITHOUT rendering.

**Acceptance (Phase 5)**: every figure in a bound video traces to a gateway
record or fails; the opt-in `test_real_bound_storyboard_render` renders a
bound storyboard with stale data end-to-end (chip + footer + manifest) and
passes on this machine, and the footer/STALE rendering was reviewed
frame-by-frame.

## Phase 6 — previews, inspection & bounded repair

Two additions, one principle: failures are detected early and retried in a
CLOSED, RECORDED, BOUNDED way — never as an endless loop, and never presented
as a completed video.

### Previews (`action=preview`, `video_preview`)

A preview job renders REVIEW MATERIAL instead of a video: the worker's
`strip` command (a labelled contact sheet of evenly spaced frames) plus one
`frame` PNG per scene, addressed as `#0@50%`, `#1@50%`, … so fades can never
shift the target. Storyboards preview the SAME resolved spec a render would
use (bindings resolved first; `spec.json` in the job workspace). Every PNG is
registered in the Artifact Center as a `image`/PNG artifact (`source_type=job`,
idempotency `video-preview:<jobId>:<name>`) — a missing catalog is recorded
per file, never a false failure; the file on disk is real either way.

The worker's own frame diagnostics (structured: `severity`, `key`, `kind`,
message) are deduplicated and reported in the job output
(`findings`/`findingsTotal`/`findingsErrors`/`findingsWarnings`). Findings do
NOT fail a preview — its job is to make them VISIBLE before anyone pays for a
render: a preview job never produces an MP4, and its output carries no
artifact.

### The bounded repair loop (`video/repair.py`)

Failure classes get deliberately different, closed-set responses:

| Failure | Response |
|---|---|
| `DATA_UNAVAILABLE`, `WORKER_UNAVAILABLE` (transient) | retried INSIDE the render job, at most `HARNESS_VIDEO_TRANSIENT_RETRIES` extra attempts (default 1), each recorded in the output as `retries` — then the job fails for real with the true class |
| `INSPECTION_FAILED`, `RENDER_FAILED`, content/validation failures | not retried blindly; the failure goes back to the author, repaired through `video_render_repair` with a revised spec (revision bump) |
| `TIMEOUT`, `CANCELLED` | terminal; never auto-retried |

`video_render_repair(projectId, ofJobId, spec?)` validates that `ofJobId` is a
FAILED/TIMED_OUT render of the same project, refuses a content failure with no
change (`REPAIR_REQUIRES_CHANGE` — repeating the same request would repeat the
failure), bumps the revision when a spec is given, and submits a render whose
input links the failure (`repairOf` + the diagnostics it responds to, read from
the failed job's `report.json`). The loop is capped: **at most 2 repairs per
failure line** (the connected `repairOf` component, counting in BOTH directions
so re-referencing the original failure cannot evade the cap). Past the cap the
service refuses with `REPAIR_LIMIT_REACHED` — the explicit terminal state; the
diagnostics are what the user gets, not another retry.

A completed FINAL render records `review`: the latest SUCCEEDED draft/preview
of the SAME `projectRevision`, or `null` — the honest answer to "did anyone
see this cut before the expensive render?".

**Acceptance (Phase 6)**: the opt-in `test_real_preview_catches_a_clipped_title`
renders a real contact sheet + per-scene frames, registers them as image
artifacts and DETECTS a real clipped-text warning before any render;
`test_real_transient_retry_recovers_a_bound_render` proves the in-job retry
against the real worker; the offline suites pin override ordering, artifact
registration, diagnostics, the transient budget (0/1 retries), the
`repairOf` link, the requires-change refusal and the cap (including the
evasive re-reference). The contact sheet was reviewed visually.

## Phase 7 — the HTTP surface, the chat job card and video artifacts

### The HTTP door (`python/harness/routers/video.py` → `/api/video/[[...path]]`)

`VideoService` now has an operator-gated HTTP surface — a thin shell with no
video logic of its own, so HTTP callers get the SAME queued jobs, bounded
repair and confined workspaces as the agent tools: `GET /video/capabilities`,
project CRUD + `validate`/`resolve-data`/`preview`/`render`/`repair`, and
`GET /video/jobs` / `GET /video/jobs/{id}` / `cancel` / `inspect`. Refusals map
by cause (`*_NOT_FOUND` → 404, `REPAIR_*`/`INVALID_STATE` → 409,
`DATA_UNAVAILABLE`/`RUNTIME_UNAVAILABLE` → 503, validation → 400) with the
stable code in the detail body.

The Next.js front door mirrors the established proxy pattern:
`src/lib/harness/clients/video-client.ts` (browser-safe fetch client, typed
`VideoApiError` carrying the refusal code) → `src/app/api/video/[[...path]]/route.ts`
(guard → runtime health → encoded upstream path; operator token + identity JWT
forwarded). Render/preview/repair requests accept a `conversationId`: produced
artifacts are registered with **conversation + run + agent** association (the
agent tools read it from the run context — conversation id when the run opted
in, else the chat session id), so the Artifact Center can answer "what came
out of this conversation".

### The chat job card (`video` run blob → `VideoPanel`)

A `video_*` tool hands the job it QUEUED to the run's collector
(`video/collector.py` — the same idiom as the image/vision collectors), and the
run persists a bounded `video` blob (job + project ids; max 8, deduped).
`client.final` carries it to the chat, which renders `VideoPanel`: it polls the
REAL job records through `/api/video/jobs/{id}` (progress, attempts), then
shows only what exists — a final render's inline player + download once its
artifact is registered, a preview's image count, an inspection's findings, or
the honest failure (`errorClass` + message). Actions are the real service ops:
cancel a running job, regenerate a draft/final, or hand a revise instruction
back to the agent. Nothing on this card claims a video that is not
`SUCCEEDED` with an artifact.

### Video artifacts: real poster thumbnails + inline playback

The artifact preview engine stays free of video imports: `artifacts/preview.py`
exposes one seam (`set_video_poster_extractor`), and the video layer registers
an FFmpeg poster extractor (`video/poster.py`) over the SAME FFmpeg tree the
renderer links — when `ffprobe` can report the duration, a real content frame
just over a third in (past the intro, before the outro); otherwise the proven
1.5 s / 0.5 s fallbacks. 512 px wide, stored as the artifact's `.thumb.png`. No
extractor / no extractable frame ⇒ `UNSUPPORTED`, never a fabricated poster. The
Artifact Center shows the poster in grid cards when a thumbnail exists and plays
the MP4 inline in the detail view; the download route now honors `Range`
requests (`206` + `Content-Range` + `Accept-Ranges`) and `?inline=1` (inline
disposition), with the Next proxy forwarding range headers and byte-stream
metadata — so seeking works, and the bytes and
authorization are unchanged.

**Acceptance (Phase 7)**: the opt-in
`test_real_final_render_gets_a_poster_thumbnail` renders a real final video and
asserts a REAL PNG poster (extracted by FFmpeg from the rendered MP4) becomes
the artifact's thumbnail; the offline suites pin the router contract (incl.
conversation association end-to-end), the collector, the preview seam
(extractor present/absent/failing), range/inline serving, the proxy + client,
and the panel's honest states.

### The player contract (one component, one transport)

Every produced MP4 the app shows goes through ONE component,
`src/components/media/video-player.tsx`: the chat `VideoPanel`, the Artifact
Center detail view and the inline run artifact. It is domain-free (a same-origin
`src`, an optional poster, a download href) and owns the concerns the old bare
`<video controls>` elements did not:

- **phases**: `loading → ready → playing → ended | error`. A failed stream says
  why (`MediaError` code → plain sentence) and offers Retry (cache-busted),
  Download and Open-in-browser — never a dead black box and never a fake
  "ready".
- **one player at a time**, pause when scrolled off-screen, keyboard control
  (`space`/`k`, `←`/`→` 5 s, `m`, `f`), mute/volume, speed, fullscreen on the
  container, a poster frame and a buffered/seek rail.
- **desktop downloads stay shell-owned**: the download control calls
  `downloadFromUrlInShell`, which writes through `be-save-download`.

The transport underneath is part of the contract:

| Invariant | Where |
|---|---|
| A range request answers `206` + `Content-Range` + `Content-Length`; a bad range is `416` with `bytes */size` | `routers/artifacts.py::download_artifact` |
| A plain `200` still sets `Content-Length` — without a known length Chromium treats media as unseekable and can refuse to start | same |
| `If-Range` mismatch serves the WHOLE object, never a fragment of another version | same |
| The Next artifact proxy forwards `Range`/`If-Range`, passes `206` + byte headers through, sets `Vary: Range`, and retries a dropped media GET once | `src/app/api/artifacts/[[...path]]/route.ts` |
| Final renders are remuxed `-movflags +faststart` (`-c copy`, atomic replace), so the index leads the file; the outcome is reported on the render result and can never fail the render | `video/transcode.py` + `video/runner.py` |

The chat card's status blob also carries `artifact.thumbnailKey`, so the player
only requests a poster that actually exists (`VideoJobStatus.hasThumbnail`).

## Phase 8 — hardening: ceilings, isolation, SBOM, recaps, deployment

### Resource ceilings (per-project queue caps + disk quotas)

- **Queue depth**: `video/limits.py` refuses a submit when a project already
  has `HARNESS_VIDEO_MAX_PENDING_PER_PROJECT` (default 4) non-terminal jobs —
  `TOO_MANY_PENDING` names the cap. It is per project (another project never
  starves) and counts QUEUED+RUNNING jobs only; terminal jobs free their slots.
- **Disk quotas**: `video/gc.py` prunes `<video-root>/jobs/<id>/` to
  `HARNESS_VIDEO_GC_KEEP_JOBS` (20) newest directories and
  `HARNESS_VIDEO_GC_MAX_TOTAL_MB` (2048) total bytes, oldest-first, never the
  single newest (it may be a render in flight) and NEVER `projects/`. It runs
  at boot (`boot.py` lifespan, off the event loop) like the code-workspace
  sweep; this is safe because a final MP4 is COPIED into the Artifact Center
  at registration — the job workspace is an audit aid, not the deliverable.
- **Timeouts/cancellation** were already bounded (draft/final/inspect/preview
  budgets; Fabric cancel + lease recovery); the caps are advertised in
  `video_capabilities().limits`.

### Isolation — pinned, not claimed

The worker already executes only its own compiled code with a fixed argv. The
Phase-8 pins make "nothing arbitrary" a TESTED contract instead of prose:

- storyboard specs have NO path-bearing keys: scene/item/top-level media keys
  (`image`, `video`, `audio`, `asset`, `src`, `file`, `path`, `media`, …) are
  refused as unknown fields, pinned by test;
- the invoked worker's argv is asserted INSIDE the job workspace
  (`--project` → `jobs/<id>/spec.json`, `-o` → `jobs/<id>/…`), for renders AND
  previews (the fake worker now records strip/frame argv too);
- a worker that REPORTS an output outside the workspace is refused before
  inspection or registration: renders → `INVALID_STATE`, previews →
  `INVALID_INPUT`, and the Artifact Center stays empty (new confinement check
  in `_attempt_render` + the existing preview `_confined`).
- There is still **no OS-level sandbox** (no container/job-object isolation);
  the honest boundary is: separate process, compiled-in inputs, confined
  workspace I/O, bounded time/queue/disk.

### Pinned deps + SBOM

`python/harness/video/sbom.py` (CLI: `python -m harness.video.sbom`) turns the
committed `Cargo.lock` into `rust/fframes-worker/SBOM.json`: every locked
crate's name/version/source/checksum, the EXACT fframes pin (parsed from
`Cargo.toml`; a loose pin is refused), the FFmpeg build identity and the
licensing note (BtbN GPL variant). License strings are deliberately NOT
auto-extracted (`Cargo.lock` has none; honesty over a fake field) — the file
says so and names `cargo metadata`/`cargo-about` as the way to extend it. A
test fails when the committed SBOM drifts from the lock, so a dependency bump
cannot ship without a fresh SBOM.

### Deployment (Windows) + zero-config behavior

- **Zero-config is unchanged**: `npm install && npm run dev` never requires the
  Rust toolchain. `doctor.js` REPORTS the video worker (binary present?
  FFMPEG_DIR set?) and never fails boot or builds anything (a cargo build
  needs the FFmpeg dev tree + libclang and minutes of CPU).
- **When the worker is absent** the pipeline degrades honestly: capabilities
  report `ready:false` with the exact missing piece, the validate/render paths
  refuse with the environment problem named, and no fake render is ever
  produced (503-style at the HTTP layer when the LAYER itself is missing).
- **Packaging story**: ship a prebuilt `fframes-worker.exe` next to the
  FFmpeg runtime DLLs (`avcodec`, `avformat`, `avutil`, `swscale`,
  `swresample`, encoder DLLs) under `rust/fframes-worker/target/release/` (the
  binary loads DLLs from `FFMPEG_DIR/bin` on PATH), plus `SBOM.json`. The
  BtbN build is the **GPL** variant (x264/x265) — redistribution must account
  for FFmpeg/encoder licenses; an LGPL-compatible build without x264 would
  need a re-render of the acceptance proofs.

### Scheduled market recaps

`video/recaps.py` builds a market recap as a `schemaVersion 3` BOUND storyboard
— `intro` → per symbol a `metric` scene (last price + volume, both `source`
bindings) → one daily `candlestick` for the first symbol → `outro` — 1–5
symbols, validated against the canonical schema, never an authored figure.
`POST /video/recaps` (`createVideoRecap` in the TS client) creates the project;
rendering is the ordinary queued job. Scheduling adds NO new timer: the
`daily-market-recap-video` job template (cron `30 21 * * 1-5`) runs the
ordinary agent runtime, which uses the video tools.

### Graph Studio node

`video.render` is DECLARED (Python `graph_runtime/video_nodes.py` + the TS
registry mirror, in lockstep) with `executor=None` and a `since_phase` naming
exactly what it waits for: the graph runtime executes synchronously today, a
render is an async queued Fabric job, and the test-run envelope refuses
effectful nodes. The compiler reports `executor_unavailable` with that
sentence instead of a node that pretends; until then the video pipeline is
reachable through the tools, `/video/*`, and automation `call_tool` actions.

**Acceptance (Phase 8)**: `test_real_recap_draft_renders` renders a data-bound
recap through the REAL worker (every figure resolved, no authored numbers);
the offline suites pin the GC budgets (count/bytes/newest-protected, projects
untouched, disable switch), the queue cap (including per-project and
slot-freedom), serialization at `max_concurrent=1`, the isolation contract
(spec keys, argv confinement, escaping outputs refused), the SBOM drift check
(281 locked crates), the recap spec/template, and the honest graph-node
diagnostic. `doctor.js` reports the worker on this machine ("present",
"FFmpeg configured").

## Windows build prerequisites (the non-obvious part)

fframes links FFmpeg's libav libraries and generates its FFI bindings with
libclang. On Windows (MSVC target) you need:

1. **Rust**, MSVC toolchain (built and verified with 1.99.0). The machine needs
   a Visual Studio Build Tools installation for the MSVC linker.
2. **FFmpeg 9.0 shared dev build** — BtbN's
   `ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip` from
   [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest)
   (the `latest` tag; its `include/` + `lib/*.lib` form the MSVC-importable dev
   tree, which is exactly what the fframes README prescribes). Point
   `FFMPEG_DIR` at the extracted folder and put `%FFMPEG_DIR%\bin` on `PATH` —
   the DLLs load at **build time** (proc macros) and at **run time**.
3. **libclang** for bindgen (`ffmpeg-sys-fframes` → `bindgen 0.72`). Any
   libclang ≥ 9 works; set `LIBCLANG_PATH` to the folder containing
   `libclang.dll`. Upstream suggests `winget install LLVM.LLVM` (machine-wide,
   needs elevation). On a non-elevated dev box the lighter route is the PyPI
   wheel into a tools dir: `pip install libclang==18.1.1 --target <dir>`, then
   `LIBCLANG_PATH=<dir>\clang\native`. libclang is **build-time only** —
   machines that merely run the worker never need it.

Do **not** enable codec cargo features (`h264`, `aac`, …) on Windows: they
request a from-source FFmpeg build, which the crate does not support on
Windows. Encoders come from the prebuilt FFmpeg (the BtbN build carries
x264/x265/AAC). This dev machine keeps the two tool trees under
`%USERPROFILE%\.black-eclipse-tools\`.

```powershell
$env:FFMPEG_DIR    = "C:\path\to\ffmpeg-n9.0-latest-win64-gpl-shared-9.0"
$env:LIBCLANG_PATH = "C:\path\to\libclang\native"
$env:PATH          = "$env:FFMPEG_DIR\bin;$env:PATH"
```

## Build, render, inspect

```powershell
# from rust/fframes-worker/ — the first build takes minutes, later builds seconds
cargo build --release

# draft (half-res, fastest preset), then inspect before paying for the full render
cargo run --release -- render --draft -o out/draft.mp4
cargo run --release -- inspect                    # exit code 2 when problems are found
cargo run --release -- render -o out/title-card.mp4

# self-review helpers: a labelled contact sheet and individual frames as PNG
cargo run --release -- strip 0s..10s -n 6         # writes strip.png
cargo run --release -- frame 5s                   # writes frames/5s.png
```

`render` / `inspect` / `strip` / `frame` are fframes' own CLI (cargo feature
`cli`); time specs accept frames (`120`), times (`3.2s`), percentages (`50%`)
and ranges (`a..b`). These commands are what an agent will drive in later
phases: a draft, a machine-readable report, then a final render — never a
claim of success without a validated output.

**Acceptance proof (2026-10-17, this machine):** draft: 300 frames in 1.2 s;
full render: 1920x1080@30 in 2.5 s; `inspect`: "no problems found";
ffprobe: `h264 / yuv420p / 1920x1080 / 30 fps / duration 10.000 s` — a valid
MP4, verified visually via `strip.png` and single frames.

## Invariants and honest limits

- **fframes is pinned `=1.2.0`** in `Cargo.toml` and locked in `Cargo.lock`,
  with the `compile-time-svgtree` feature (scene overlaps + static caching —
  see Phase 4). Bump deliberately and re-run the acceptance proof.
- **Storyboard specs are contracts.** `schemaVersion` is meaningful: a new
  shape is a new version (Python validates, Rust re-checks), never a silent
  mutation of an existing version. v2 = static display strings; v3 adds data
  bindings.
- **CPU backend only, for now.** The Skia/Vulkan GPU backend (~10x faster) is a
  separate crate; GPU verification is deferred — the CPU backend is already far
  faster than needed (300 frames in ~2.5 s). `vulkan-1.dll` is present on this
  machine, so the fallback question is open, not blocked.
- **Licensing:** fframes is MIT, but the BtbN build linked here is the **GPL**
  variant (x264/x265). Redistribution of the worker binary must account for
  FFmpeg/encoder licenses — revisit when packaging lands (Phase 8).
- **Renders are always queued Fabric jobs** (type `video`, dedicated worker),
  never inline — the job layer is the narrow interface. Isolation is pinned
  (Phase 8): the renderer executes only its own compiled code, argv and
  reported outputs are confined to the job workspace, and specs cannot name
  arbitrary paths. There is still **no OS-level sandbox**, and workspace
  growth is bounded by the GC budgets rather than by a filesystem quota.
- **Retries are bounded everywhere.** In-job transient retries are capped
  (`HARNESS_VIDEO_TRANSIENT_RETRIES`) and recorded; repairs are capped per
  failure line (`REPAIR_LIMIT_REACHED`); queue depth and disk usage are capped
  per project (`TOO_MANY_PENDING`, GC). There is no configuration that turns a
  failure — or a submit loop — into unbounded work.
- **A preview is images, never a video.** `action=preview` produces PNGs and
  findings; the only path to an MP4 artifact is a SUCCEEDED render (draft
  files stay in the job workspace; finals register in the Artifact Center).
- **Deliberately deferred** (honest scope, not gaps in the dark):
  - the Skia/Vulkan GPU backend (CPU: 300 frames ≈ 2.5 s — verification is a
    separate crate's job);
  - formats other than landscape 1920x1080@30 (the spec's closed set reports
    anything else as unsupported);
  - audio tracks and captions scenes (the spec fields are intentionally absent
    until implemented, so a spec can never request unrendered audio);
  - indicator/backtest-backed bindings (Phase 5 binds quotes + candles only);
  - OS sandboxing / per-process resource ceilings (see above);
  - the Graph Studio `video.render` executor (declared with `since_phase`).
- **MCP note (AGENTS.md).** MCP results are user-directed and never
  Eclipse-verified: they are NOT routed through JDdS or the native research
  verifier, so video figures stay on the gateway/binding path — a recap or a
  bound scene NEVER sources a number from an MCP tool.

## Where this goes next

Phase roadmap (from the integration plan): **2) done** — the Fabric job layer
is live; **3) done** — the `video_*` tools + `system.video-production` skill
are live; **4) done** — the storyboard schema + scene library are live;
**5) done** — evidence-bound market data (bindings, manifest, stale flags) is
live; **6) done** — previews, diagnostics and the bounded repair loop are
live; **7) done** — the `/video` HTTP surface, the chat job card and video
artifact thumbnails/playback are live; **8) done** — ceilings, isolation pins,
SBOM, the recap template and the deployment story are live.
Each phase must leave the app working, and none may present a failed render
as a completed video.
