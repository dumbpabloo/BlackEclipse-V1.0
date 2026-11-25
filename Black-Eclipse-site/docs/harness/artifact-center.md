---
title: Artifact Center
status: Current
last-verified: 2026-11-25
---

# Artifact Center

**Who this is for:** anyone asking "where did the file Eclipse produced go, and
how do I find/preview/download it again?" â€” and anyone extending the catalog.

The Artifact Center is the **one durable catalog of everything Eclipse and the
user produce**: reports, datasets, charts, images, code, backtest outputs, uploaded
documents. It is reachable from the Agentic sidebar (**Artifacts**), from the agent
(`artifact_*` tools), and from every producer subsystem through one service.

## The three layers (and what NOT to confuse them with)

| Layer | Where | Owns |
|---|---|---|
| Catalog rows | `agent-memory.db` tables `HarnessArtifact*` | metadata, provenance, lifecycle, tags, collections |
| File bytes | `.black-eclipse/artifacts/<owner>/<YYYY>/<MM>/<sha16>-<name>` | the content-addressed originals + thumbnails |
| Surfaces | `/artifacts/*` (FastAPI) â†’ `/api/artifacts/**` (Next proxy) â†’ Artifact Center UI | list/search/preview/download |

The catalog is **NOT JDdS** (`jdds.db`, market data) and **NOT** another
subsystem's tables. It adds its own tables through `artifacts/store.py`'s
`_ensure_schema`, on the ONE shared `agent-memory.db` connection â€” the same
pattern as `tasks/store.py` and `execution/store.py`.

## Code map

```
python/harness/artifacts/
  model.py       contracts: Artifact, ArtifactVersion, ArtifactCollection,
                 ArtifactPage, categories/statuses/preview states, input models,
                 the closed SORT_COLUMNS table
  content.py     the ONE MIME/category/preview-kind derivation (magic-byte sniff
                 where bytes exist; extension is a hint, never proof)
  storage.py     LocalArtifactStorage â€” confined, content-addressed, atomic,
                 size-bounded; OWNS storage keys (the service never sees a path)
  store.py       ArtifactStore â€” the tables + parameterized list/search; sort
                 keys are looked up, never interpolated
  preview.py     bounded text/CSV/JSON/code extracts + Pillow image/PDF thumbs;
                 office files read as ZIP+XML (never executed); VIDEO posters
                 come from a registered extractor seam
                 (`set_video_poster_extractor` â€” the video layer installs one
                 over the renderer's FFmpeg tree; absent/failed â‡’ UNSUPPORTED,
                 never a fabricated poster)
  service.py     ArtifactService â€” register/version/preview/tag/collect/archive/
                 restore/delete/purge/reconcile; owner scope + idempotency
  compare.py     compare_versions / compare_revisions / to_markdown â€” a
                 deterministic diff of two stored revisions: checksum + size,
                 provenance, change note and metadata (added/removed/changed).
                 Bytes are compared by checksum, never line-diffed
  producers.py   the hook other subsystems call (analysis, task scheduler)
  tools.py       the `artifact_*` agent tools
  bootstrap.py   install_artifact_layer + the bounded reconciler singleton
python/harness/routers/artifacts.py   the /artifacts/* route group (operator-gated)
src/app/api/artifacts/[[...path]]/route.ts   the thin proxy (streams downloads)
src/lib/harness/artifacts-browser.ts         the client-safe UI client
src/components/screens/artifacts/artifacts-screen.tsx  the Artifact Center screen
```

## Registration contract (how files get in)

A producer calls `ArtifactService.register(...)` (or `register_bytes` /
`register_path`) with a **source** â€” a path inside an authorized workspace root, or
inline bytes. The caller **may never supply a storage key**. The service:

1. resolves + confines the source (refused if outside every authorized root),
2. copies the bytes into the content-addressed store (atomic temp + rename),
3. computes the sha256, MIME (with magic-byte sniff) and category,
4. inserts the row, the v1 version row, tags/collection links, and a lifecycle
   event, **then** generates the preview. A preview failure never invalidates the
   original â€” it is recorded as `previewStatus: FAILED`/`UNSUPPORTED`. A
   `.deck.json` artifact previews as a readable outline (deck title + slide list)
   plus a first-slide thumbnail written by the presentation layer
   (`preview.py::_deck_outline`, `ArtifactService.set_preview_thumbnail`).

Idempotency: pass an `idempotencyKey`; a repeat returns the existing artifact
rather than creating a duplicate (this is how a run retry or a job replay stays
clean).

Producers wired today:

- `python/harness/analysis_tools.py` â€” every `ArtifactRef` an `analysis_run`
  produced (`register_run_artifacts`, idempotent per run+digest).
- `python/harness/tasks/scheduler.py` â€” a completed task's outcome artifacts
  (`register_reference_dicts`), mirrored beside the existing `HarnessTaskArtifact`.
- `python/harness/execution/orchestrator.py` â€” every file the Code Execution &
  Verification Layer collected from `dist/build/out/coverage/reports/â€¦`
  (`_register_produced`, absolute paths from the workspace binding).
- `python/harness/research/web_engine.py` â€” a completed web-research run, recorded
  as an evidence-REFERENCING report artifact (`register_research_report`,
  best-effort, idempotent per `(researchId, snapshotId)`). The Markdown report
  names claim verdicts, conflicts and an evidence appendix by id, and its metadata
  carries the `researchId`/`snapshotId`/evidence ids, so an artifact points back at
  the durable evidence records instead of duplicating them (ADR-H162).
- Agents directly: `artifact_register`, `artifact_add_version`, `artifact_list`,
  `artifact_get`, `artifact_tag`, `artifact_create_collection`.
- `python/harness/documents/` â€” the Document Authoring `document_create` tool builds
  Word/Excel/PowerPoint/Markdown/CSV/JSON files and registers the bytes here
  (`register_bytes`, explicit MIME so the category/preview resolve correctly). See
  `docs/harness/document-intelligence.md` â€º *Authoring*, ADR-H192.
- `python/harness/documents/edit/` â€” `document_edit` publishes a targeted edit as a
  NEW version through `add_version`. It passes `expectedChecksum` (the source's
  sha256); `ArtifactService.add_version` refuses with `ARTIFACT_CONFLICT` when the
  current artifact checksum no longer matches, so a concurrent edit cannot silently
  overwrite a newer version. Its idempotency key is
  `edit:<artifactId>:<planHash>:<sourceSha>`.

Terminal/filesystem discovery is the backstop: the bounded reconciler plus
explicit `artifact_register` cover files a command wrote, without ever scanning
arbitrary machine directories.

## Lifecycle

`ACTIVE â†’ ARCHIVED â†’ ACTIVE` (archive/restore are distinct and reversible) and
`â†’ DELETED â†’ PURGED`. Delete is soft (bytes retained, recoverable); purge removes
every version's bytes and the rows, and **refuses while another artifact shares the
content**. State never changes without a recorded `HarnessArtifactEvent`.

## Canvas (editable artifacts over this catalog)

Eclipse Canvas gives the catalog an editor without adding a second store. A
canvas document **is** an ordinary catalog entry: its bytes are canonical UTF-8
text and its revisions are the artifact's immutable versions. Its canvas kind
(`document`/`code`/`visual`) and optional `language` are stored in the artifact's
`metadata` under the key `canvas`, and an artifact detail whose metadata carries
that key shows an **"Edit in Canvas"** action that opens the artifact **inline in
the ChatBot tab** (there is no separate Canvas surface). `CanvasService`
(`python/harness/canvas/service.py`) is the ONE canvas writer over
`ArtifactService`; saves are revision-guarded (a stale save is `409
CANVAS_CONFLICT`), and AI edits are typed operations the server re-applies to
current bytes so client content is never trusted. Because canvas artifacts are
ordinary entries, they inherit owner scope, preview, download, versions, tags,
archive and reconciliation unchanged.

Full design, routes and the operation contract: `docs/canvas.md`; ADR-049.

## Authorization

Every `/artifacts/*` route (reads AND mutations) carries `require_operator`
(loopback or the runtime secret), and every query is scoped by `ownerId` (the
front door passes `x-harness-user`, defaulting to `local`). A caller can never
read, preview or download another owner's artifact â€” not by id, not by search, not
by thumbnail. Storage keys are validated against traversal, absolute paths and
symlink escape. Untrusted content is never executed; HTML/SVG is served as a
download and never rendered in the app origin.

## Media playback (video/audio)

A video artifact's thumbnail is a REAL frame: the video layer registers an
FFmpeg poster extractor on the preview engine at boot (the artifacts layer
holds no video import), and a missing extractor or frame is an honest
`UNSUPPORTED`, never a placeholder image. For inline playback the download
route additionally supports:

- `Range` requests â†’ `206` + `Content-Range` + `Accept-Ranges: bytes` (so a
  player can seek; an unsatisfiable range is `416` with `bytes */<size>`), and
- `?inline=1` â†’ `Content-Disposition: inline` instead of `attachment`.

The bytes are identical and the authorization is unchanged â€” the Next proxy
forwards the client's `Range` header and the upstream range/stream metadata.
Artifacts registered by the video pipeline also carry a `conversationId`
(when the requesting conversation supplied one), so the catalog can be
filtered by the conversation a render came from.

## Reconciliation

`ArtifactService.reconcile()` is bounded and idempotent: it flags rows whose
storage object is gone as `MISSING`, recovers rows whose object reappeared, and
(opt-in) verifies checksums. With `discover`, it registers new files found under
the bounded workspace artifact directories. With `purge_expired`, it permanently
removes soft-deleted artifacts whose retention deadline (`retentionUntil`, else
`deletedAt + HARNESS_ARTIFACT_RETENTION_DAYS`) has passed â€” never ACTIVE/ARCHIVED
rows. A background reconciler runs reconcile+discover+purge every
`HARNESS_ARTIFACT_RECONCILE_SECONDS` (default 900, disable with
`HARNESS_ARTIFACT_RECONCILE=0`; disable purge with `HARNESS_ARTIFACT_AUTO_PURGE=0`).
On-demand: `POST /artifacts/reconcile`.

## Configuration

| Env | Meaning |
|---|---|
| `HARNESS_ARTIFACT_ROOT` | the storage root (default `<repo>/.black-eclipse/artifacts`) |
| `HARNESS_ARTIFACT_RECONCILE` | `0` disables the background reconciler |
| `HARNESS_ARTIFACT_RECONCILE_SECONDS` | reconciler interval (floor 60s) |
| `HARNESS_ARTIFACT_RETENTION_DAYS` | grace period before a soft-deleted artifact is auto-purged (default 30) |
| `HARNESS_ARTIFACT_AUTO_PURGE` | `0` disables automatic purge of expired soft-deleted artifacts |
| `AGENT_MEMORY_DB_PATH` | the catalog database (the ONE shared harness DB) |

## Tests

```
scripts/vendor/python/python.exe -m pytest python/tests/test_artifacts_store.py \
  python/tests/test_artifacts_security.py python/tests/test_artifacts_tools.py \
  python/tests/test_artifacts_api.py -v
npx vitest run src/lib/harness/artifacts-browser.test.ts src/app/api/artifacts/route.test.ts
```

## Realtime

Artifact lifecycle mutations publish canonical `artifact.*` events on the ONE
Event System bus (`event_system/registry.py`, `artifact` namespace) so an operator
console or a future live subscriber can react; the Artifact Center UI refreshes on
a bounded poll and recovers authoritative state by re-querying the API (so a
dropped realtime channel never leaves a stale view).
