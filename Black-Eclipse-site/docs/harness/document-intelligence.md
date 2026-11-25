---
title: Document Intelligence Subsystem
status: Current
last-verified: 2026-11-25
---

# Document Intelligence Subsystem

**Code:** `python/harness/document_intelligence/`
**Tools:** `document_open`, `document_inspect`, `document_search`,
`document_extract`, `document_analyze`, `document_query`, `document_compare`,
`document_render`, `document_capabilities`
(registered by `document_intelligence/tools.py:register_document_tools`).
`document_open`/`inspect`/`extract`/`analyze`/`render` also accept an
`artifactId`, so a generated workbook or deck can be read back without knowing
its confined filesystem path.
**Config:** `document_intelligence/config.py` (`document.json` override +
`HARNESS_DOCUMENT_*` / `DOCUMENT_*` / `OCR_*` env)
**ADR:** `docs/harness/decisions.md` ADR-H147

## What it is

A single, provider-agnostic document-understanding capability integrated with the
existing agent runtime. It turns a file of any supported format into one unified
`DocumentIR`, then indexes, searches, compares and cites it. It never builds a
second provider stack, OCR engine, evidence store, database or query engine â€”
every one of those is reused.

```
agent tool (document_open, document_search, ...)
        â”‚
        â–¼
process_document(...)                          document_intelligence/orchestrator.py
        â”‚
        â”œâ”€ validate          security.py        (size, magic, macro, SSRF, injection)
        â”œâ”€ cache             store.py           (content-hash â†’ reuse extraction)
        â”œâ”€ detect + parse    adapters/registry  (one adapter per format â†’ DocumentIR)
        â”œâ”€ OCR routing       ocr.py             (deterministic first â†’ vision fallback)
        â”œâ”€ structure         base.py / entities (sections, entities)
        â”œâ”€ chunk + index     chunking.py / store.py
        â”œâ”€ evidence          evidence.py        (â†’ research EvidenceStore, UNTRUSTED)
        â””â”€ assess            processing.py      (READY | PARTIALLY_READY | FAILED)
```

## The DocumentIR (the backbone)

`ir.py` is the vocabulary every adapter emits and every downstream stage reads:
`DocumentIR`, `Page` (a PDF page / PPTX slide / HTML doc), `Sheet` (a worksheet
as data), `Block`, `Table`/`TableCell`, `DocumentImage`, `Section`, `BBox`,
`Provenance`, `DateFacts`. Rules:

- Coordinates are never lost; `BBox.unit` records points / px / emu / percent.
- Every block carries `Provenance` (method + status + confidence + engine).
- `compute_overall_text_method` reports the *weakest* essential method, so a
  partly-OCR'd document is never called fully native.
- The IR blob on disk is metadata only; image binaries live in the vision asset
  store and are re-hydrated from there.

## Adapters (one per format)

| Adapter | Kinds | Notes |
|---|---|---|
| `pdf` | pdf | native text layer, scans via OCR routing, whitespace tables, links; raster via vision backend |
| `docx` | docx | headings, lists, tables with cells, hyperlinks, headers/footers, footnotes, comments, embedded images |
| `pptx` | pptx | slides, titles, text boxes, speaker notes, tables, charts, images; each slide is a Page |
| `xlsx` | xlsx | worksheets (incl. hidden), sparse cells with **formulas separate from values**, merges, named ranges, comments, conditional formats, charts, used-range table |
| `odf` | odt, odp, ods | ODF text/presentation/spreadsheet |
| `tabular` | csv, tsv | streaming rows, column schema + type profiles, bounded preview |
| `text` | markdown, mdx, txt, html, json, jsonl, xml, yaml, toml | structure-preserving; HTML sanitized; data formats get schema inference |
| `image` | png/jpeg/webp/gif/tiff/bmp/svg | single-page doc; OCR then vision caption/layout |
| `epub` | epub | spine order â†’ pages, sanitized XHTML |
| `legacy` | rtf, doc, xls, ppt | RTF native; legacy OLE converted via LibreOffice when present, else honest refusal |
| `archive` | zip, tar, gz, 7z | **lazy inspection only**; bomb guards before any extraction |

OOXML/ODF are read with the stdlib (`zipfile` + `ElementTree`), with a bounded
`lxml` recovery parser for a malformed part. The READ path adds no `openpyxl` /
`python-docx` / `python-pptx` dependency. (The WRITE path â€” see *Authoring* below â€”
is a separate subsystem and does use those three writers.)

## Authoring (the write side)

Document Intelligence only READS. Producing new documents is a separate, small
subsystem: **Document Authoring** (`python/harness/documents/`, tools
`document_create` and `eclipse_report`, ADR-H192). It builds REAL Word (`.docx`),
Excel (`.xlsx`) and PowerPoint (`.pptx`) files â€” plus Markdown, CSV, JSON and
`.txt` â€” from a neutral content vocabulary (`blocks` for docx/markdown/txt,
`sheets` for xlsx, `slides` for pptx, `rows` for csv/json) and records the bytes
in the **Artifact Center** (`artifacts/`, ADR-H160) for preview and download.
`eclipse_report` adds paginated PDF reports via the vendored Matplotlib PDF
backend (`documents/reporting.py`), using the same block vocabulary. See
`docs/harness/reports.md`.

Every produced document also gets a **readable copy** in a real folder â€”
`Documents/Eclipse` by default, or the `saveTo` the user named (`downloads`,
`desktop`, an absolute folder) â€” and the result carries its absolute path, so an
answer can point at the file instead of describing it. `documents/outputs.py`
owns that write plus `locate()`, and the `file_locate` tool searches the person's
own folders (export folder, Downloads, Documents, Desktop, the bound workspace)
by name, extension and age, newest first. A miss names the folders it searched.

- Writers are `python-docx`, `openpyxl`, `python-pptx` (pure-Python OOXML; see
  `python/requirements.txt`). They are imported lazily, so a partial install makes
  ONE format report `DOCUMENT_LIBRARY_MISSING` instead of emitting a broken file.
- Bytes go straight into the confined artifact store through the ONE
  `ArtifactService`; a missing store reports `ARTIFACT_UNAVAILABLE`. The only
  other write is the export copy above â€” the export folder, or an absolute folder
  the user explicitly named; a relative path is refused.
- It is the answer to "Word/Excel/PowerPoint cannot be created" â€” the read path
  stays stdlib-only.
- Editing an EXISTING file is a separate, preservation-first subsystem:
  `python/harness/documents/edit/` and the `document_edit` tool. See
  `docs/harness/office-edit.md`.
- A **dedicated presentation/deck lane** sits beside this one:
  `presentation_create` builds a versioned `DeckSpec` artifact and exports a
  `.pptx` from it, with themes, 13 layouts, speaker notes, tables, native charts,
  in-app preview/present and editing. `document_create(format="pptx")` now
  delegates to the same deck exporter (one layout writer), so the one-shot path
  and the editable path cannot drift. See `docs/harness/presentations.md`.

## Reuse (no forks)

- **OCR / rendering / models** â€” `harness.vision` (`ocr.run_ocr`,
  `documents.open_pdf`/`render_page`/`parse_table`, `providers.call_model`).
- **Evidence** â€” `research.web_contracts.Evidence` + `research.web_store.EvidenceStore`.
- **Query** â€” read-only SQL over DuckDB (preferred) or a pandas `df.query`
  fallback; only a single `SELECT`/`WITH` statement is accepted.
- **Storage** â€” the ONE `agent-memory.db` connection (`DiDocument`, `DiVersion`,
  `DiJob`, `DiChunk`, `DiTable`, `DiImage`, `DiEntity`, `DiOcrRun`, `DiCache`).
- **Security** â€” `mcp.ssrf.check_outbound_url`, `research.web_security` boundary.

## OCR routing

```
image â†’ deterministic OCR (winsdk/tesseract) â†’ confidence â‰¥ threshold? â†’ accept
                                              â””â”€ no â†’ vision-model transcription
```
Method + confidence are recorded per page. No engine and low confidence â‡’
`UNAVAILABLE`, never a fabricated transcript.

## Security (documents are untrusted input)

- extension/MIME/magic detection + byte ceilings before any parser runs;
- macro-enabled Office documents refused (`MACRO_ENABLED`) unless allowed;
- archive bombs detected by declared size, ratio, entry count, nesting â€” before
  extraction;
- document URLs SSRF-guarded through `mcp.ssrf`;
- HTML active content stripped; scripts never execute;
- extracted text is DATA: scanned and delimited through the shared boundary, so
  "ignore previous instructions" inside a document can never override the prompt.

## States, retry, caching

`DISCOVERED â†’ VALIDATING â†’ INGESTING â†’ PARSING â†’ OCR â†’ VISION â†’ STRUCTURING â†’
INDEXING â†’ READY | PARTIALLY_READY | FAILED | CANCELLED`. A stage that cannot run
is `UNAVAILABLE`/`SKIPPED`. Parsing retries transient failures (bounded, backoff)
and is bounded by `processing_timeout_seconds`. The same content hash reuses a
fresh cached extraction unless `force` is set.

## Agent context + right sidebar

`collector.py` collects each `document_*` result into a bounded run blob,
persisted on the `HarnessRun.documents` column and returned on
`RunResult.documents`/`client.final`. TS `document-view.ts` coerces it and
`src/components/chat/document-panel.tsx` renders it in the transcript â€” document,
type, pages, status, the per-capability processing checklist (âœ“/âœ—), current
page/section, evidence/entity counts.

## Configuration

Env vars: `DOCUMENT_MAX_FILE_SIZE`, `DOCUMENT_PROCESSING_TIMEOUT`, `OCR_PROVIDER`,
`OCR_CONFIDENCE_THRESHOLD`, `MAX_PAGES`, `MAX_ARCHIVE_SIZE`, `MAX_ARCHIVE_FILES`,
`DOCUMENT_CACHE_ENABLED`, `DOCUMENT_INDEX_ENABLED`, `HARNESS_DOCUMENT_QUERY_ENGINE`,
`HARNESS_DOCUMENT_*`, plus a `.black-eclipse/document.json` override
(`HARNESS_DOCUMENT_CONFIG` for the path). Hard ceilings cannot be raised by config.

## Tests

`test_document_intelligence_unit.py` (IR, security, every adapter, OCR, chunking,
compare, entities, store, cache), `test_document_intelligence_tools.py` (registry,
tools, evidence bridge, injection, E2E CSV question, partial state),
`test_document_intelligence_security.py` (bombs, macro, oversize, SSRF, HTML,
injection), `test_document_intelligence_graph.py` (a tool result reaches the run
blob) and `src/lib/harness/document-view.test.ts`.

## Known limitations (external)

- Legacy binary `.doc`/`.xls`/`.ppt` need LibreOffice (`soffice`) to convert;
  without it the request is refused with `UNSUPPORTED_FORMAT` and guidance.
- Rendering Office formats also needs LibreOffice; PDF rendering needs
  `pypdfium2` (or Pillow for the stdlib PDF backend). Absent, `document_render`
  reports `UNAVAILABLE` rather than a fake page.
- OCR depends on an OS engine (Windows-native `winsdk` or Tesseract). When neither
  is installed, OCR pages report `UNAVAILABLE` honestly.
- 7z archives are recognized but not extracted (no safe in-process reader);
  `.gz`/`.tar`/`.zip` are supported.
- Vision captioning/analysis of embedded images or document pages needs a
  vision-capable model; without one those capabilities are `UNAVAILABLE`.
