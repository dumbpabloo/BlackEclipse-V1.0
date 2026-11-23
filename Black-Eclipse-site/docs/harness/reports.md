---
title: Reports (PDF, Word, Excel and computed deliverables)
status: Current
last-verified: 2026-11-23
---

# Reports

**Who this is for:** anyone changing how Eclipse turns a computed result into a
downloadable report, or debugging a report that failed to render.

## What it does

`eclipse_report` assembles a report from neutral content blocks, saves the real
file in the Artifact Center, and writes a readable copy into a folder the person
can open — `Documents/Eclipse` by default — returning its **absolute path**.
It is the reporting surface for a computed result: the agent writes the values it
actually computed, labels them computed, and never substitutes a retrieved figure
for a derived one.

Supported formats:

| Format | Writer |
|---|---|
| `pdf` | `documents/reporting.py::build_pdf` — paginated Matplotlib PDF backend (vendored, no new dependency). |
| `docx` | `documents/authoring.py::build_docx` (`python-docx`). |
| `xlsx` | `documents/authoring.py::build_xlsx` (`openpyxl`). |
| `pptx` | `documents/authoring.py::build_pptx` (`python-pptx`). |
| `markdown` / `md` / `txt` | `documents/authoring.py` text builders. |

## Flow

```
analysis_run (compute; returns outputs + provenance)
  -> eclipse_report(format="pdf", title=..., blocks=[...], saveTo="documents")
  -> Artifact Center record (real bytes, preview/download)
  -> readable copy at C:\Users\<you>\Documents\Eclipse\<name>.pdf
  -> result carries {"path": "<absolute path>", "artifact": {...}}
```

`saveTo` accepts `documents` (default), `downloads`, `desktop`, or an absolute
folder path; a relative path is refused, because it would depend on a working
directory the person never chose. `document_create` follows the same rule.

The copy is written by `documents/outputs.py::publish` (atomic temp file +
`os.replace`, a name collision becomes `name (2).ext`, no separator or `..` can
survive in a file name). If that copy fails the tool still succeeds — the
Artifact Center bytes are durable — and the result says plainly which folder it
could not write.

For a reusable report, build a `report_template` capability through the
Capability Builder; the recipe computes the values, then `eclipse_report`
renders them.

## Block vocabulary

- `heading` (`text`, `level` 1–3)
- `paragraph` (`text`)
- `bullets` / `numbers` (`items`)
- `table` (`headers`, `rows`)
- `page_break`

PDF pages are paginated automatically; tables are bounded and long cell text is
truncated. Word/Excel/PowerPoint use the existing authoring builders, so their
content rules are unchanged.

## Finding a file again (`file_locate`)

The chat filesystem tools are confined to the bound project, so "where is the
PDF you just made?" had no honest route once the file lived in Documents or
Downloads. `file_locate` searches the person's own folders — the Eclipse export
folder, Downloads, Documents, Desktop — plus the bound workspace, and answers
with **absolute paths, newest first**:

- inputs: `query` (name fragment or glob), `extensions`, `sinceDays`, `limit`,
  optional `folders`;
- bounds: depth 4, 60 000 scanned entries, a skip list (`node_modules`, `.git`,
  `AppData`, `Library`, …), hidden folders skipped;
- a miss names the folders it searched — it never guesses a path.

`documents/outputs.py` holds both halves: `publish()` (write + absolute path) and
`locate()` (bounded search). `outputs.destination()` resolves `documents`/
`downloads`/`desktop` through the shell's *known folders* on Windows
(`SHGetKnownFolderPath`), so a OneDrive-redirected Documents folder still works.

Code-tool writes (`write_file`, `create_file`, `code_edit`, patches) also return
`absolutePath` and append `Absolute path: …` to the tool text (Hermes does the
same), so "where did that land?" is answerable for project files too.

## Artifact and honesty rules

- Bytes are registered through the ONE `ArtifactService.register_bytes` with
  `source_tool="eclipse_report"`; the file appears in the Artifact Center.
- A missing writer library, an unrenderable format, or an unavailable artifact
  store returns a coded error (`DOCUMENT_LIBRARY_MISSING`,
  `REPORT_RENDER_UNAVAILABLE`, `REPORT_RENDER_FAILED`, `ARTIFACT_UNAVAILABLE`) — never a fake file.
- A computed value in a report is labelled computed; the tool cannot verify the
  provenance of a number it is given, so the agent must supply the computed
  result and its method.
- No network, no arbitrary filesystem path (the export folder or an explicit
  absolute folder the user named), no macro execution.

## Tests

`python/tests/test_reports.py` covers real PDF bytes, docx/xlsx dispatch,
artifact registration, empty-report refusal and unsupported-format refusal.
`python/tests/test_file_outputs.py` covers the export copy, the absolute path in
the result, collision-safe names, `saveTo`, and `file_locate` (including an
honest miss). `python/tests/test_documents_authoring.py` covers the existing
builders.

`scripts/verify_file_outputs.py` is the machine-checkable end-to-end proof: run
it with the vendored interpreter to print this machine's export/search folders,
create a real PDF through the `eclipse_report` handler, print the absolute path
the agent receives, and find the same file again with `file_locate` by name:

```bash
scripts/vendor/python/python.exe scripts/verify_file_outputs.py
```

It exits non-zero unless the path is absolute, the bytes are a real `%PDF-`, and
the locate result is the same file.

## Not yet implemented

- Embedded report figures (the analysis figure is already an Artifact Center
  image; a future block can place it directly on a PDF page).
- HTML/PDF styling templates and PDF/A.
- A report-template UI (the capability appears in Capability Studio › Generated).
