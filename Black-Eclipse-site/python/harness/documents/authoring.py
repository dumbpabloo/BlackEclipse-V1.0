"""Document Authoring — build real Word / Excel / PowerPoint / text files in-process.

Document Intelligence READS office formats; this module WRITES them. Every builder
is a pure function: neutral content in, file bytes out. No builder picks a model,
touches the network, runs an Office runtime or executes a macro — each assembles
the OOXML ZIP container (Word .docx, Excel .xlsx, PowerPoint .pptx) or a plain
text format (Markdown, CSV, JSON, .txt) directly through a small library.

Libraries are imported lazily so a partial install degrades ONE format at a time:
a missing writer raises `DocumentError("DOCUMENT_LIBRARY_MISSING", …)`, which the
tool surfaces honestly instead of emitting a broken file.

The neutral vocabulary (mirrored by the `document_create` tool schema):

    Block   — heading | paragraph | bullets | numbers | table | page_break
    Sheet   — { name, rows }        rows: list of rows (first row = header)
    Slide   — { title, bullets, notes }
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Iterable, Optional, Sequence

#: The formats this module can author, mapped to their file extension.
FORMAT_EXTENSION: dict[str, str] = {
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "markdown": "md",
    "md": "md",
    "csv": "csv",
    "json": "json",
    "txt": "txt",
    "text": "txt",
}

#: MIME type per canonical format (used when registering the artifact).
FORMAT_MIME: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "txt": "text/plain",
}

MAX_BLOCKS = 2_000
MAX_SHEETS = 100
MAX_SLIDES = 300
MAX_ROWS = 20_000
MAX_CELL_CHARS = 32_767  # Excel's hard cell limit


class DocumentError(Exception):
    """A document-authoring failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_format(fmt: str) -> str:
    key = (fmt or "").strip().casefold()
    if key == "md":
        key = "markdown"
    if key == "text":
        key = "txt"
    if key not in FORMAT_MIME:
        raise DocumentError(
            "DOCUMENT_FORMAT_UNSUPPORTED",
            f"unsupported format {fmt!r}; expected one of "
            f"{sorted(set(FORMAT_MIME) | {'md', 'text'})}",
        )
    return key


def slug_filename(title: str, extension: str) -> str:
    base = re.sub(r"[^\w.\- ]+", " ", (title or "").strip(), flags=re.UNICODE).strip(" .")
    base = re.sub(r"[\s_]+", "-", base).strip("-")[:120] or "document"
    return f"{base}.{extension}"


# ── shared coercion helpers ───────────────────────────────────────────────────


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _rows_of(value: Any) -> list[list[Any]]:
    """Coerce a rows payload into a list of row-lists.

    Accepts a list of lists (verbatim) or a list of objects (header = union of
    keys in first-seen order). Anything else is a validation error.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise DocumentError("DOCUMENT_INVALID_CONTENT", "rows must be an array")
    if not value:
        return []
    if isinstance(value[0], dict):
        headers: list[str] = []
        seen: set[str] = set()
        for row in value:
            if isinstance(row, dict):
                for key in row:
                    if key not in seen:
                        seen.add(key)
                        headers.append(str(key))
        rows: list[list[Any]] = [headers]
        for row in value:
            mapping = row if isinstance(row, dict) else {}
            rows.append([mapping.get(h) for h in headers])
        return rows
    out: list[list[Any]] = []
    for row in value:
        if isinstance(row, (list, tuple)):
            out.append(list(row))
        else:
            out.append([row])
    return out


def _cell(value: Any) -> Any:
    """Cell values must be scalar; long text is truncated to Excel's limit."""
    if value is None or isinstance(value, (int, float, bool, str)):
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS]
        return value
    return _as_text(value)[:MAX_CELL_CHARS]


# ── Word (.docx) ──────────────────────────────────────────────────────────────


def build_docx(blocks: Sequence[dict[str, Any]], *, title: str = "") -> bytes:
    try:
        from docx import Document  # noqa: PLC0415
        from docx.shared import Pt  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            "DOCUMENT_LIBRARY_MISSING", "python-docx is not installed (Word authoring unavailable)"
        ) from exc

    document = Document()
    if title:
        document.core_properties.title = title
        document.add_heading(title, level=0)

    for block in list(blocks or [])[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph").casefold()
        if kind == "heading":
            level = int(block.get("level") or 1)
            document.add_heading(_as_text(block.get("text")), level=max(1, min(9, level)))
        elif kind in ("bullets", "bullet", "unordered_list"):
            for item in block.get("items") or []:
                document.add_paragraph(_as_text(item), style="List Bullet")
        elif kind in ("numbers", "number", "ordered_list"):
            for item in block.get("items") or []:
                document.add_paragraph(_as_text(item), style="List Number")
        elif kind == "table":
            headers = [_as_text(h) for h in (block.get("headers") or [])]
            rows = block.get("rows") or []
            columns = len(headers) or (len(rows[0]) if rows and isinstance(rows[0], (list, tuple)) else 0)
            if columns == 0:
                continue
            table = document.add_table(rows=1, cols=columns)
            table.style = "Light Grid Accent 1"
            header_cells = table.rows[0].cells
            for i in range(columns):
                header_cells[i].text = headers[i] if i < len(headers) else ""
            for row in rows:
                cells = table.add_row().cells
                values = list(row) if isinstance(row, (list, tuple)) else [row]
                for i in range(columns):
                    cells[i].text = _as_text(values[i]) if i < len(values) else ""
        elif kind == "page_break":
            document.add_page_break()
        else:  # paragraph / text / anything unrecognized defaults to prose
            text = _as_text(block.get("text"))
            paragraph = document.add_paragraph()
            run = paragraph.add_run(text)
            if block.get("bold"):
                run.bold = True
            if block.get("italic"):
                run.italic = True
            size = block.get("size")
            if isinstance(size, (int, float)):
                run.font.size = Pt(float(size))

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ── Excel (.xlsx) ─────────────────────────────────────────────────────────────


def _sheet_title(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", (name or "Sheet").strip())[:31].strip() or "Sheet"
    candidate = cleaned
    suffix = 1
    while candidate.casefold() in used:
        tail = f"_{suffix}"
        candidate = cleaned[: 31 - len(tail)] + tail
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def build_xlsx(sheets: Sequence[dict[str, Any]], *, title: str = "") -> bytes:
    try:
        from openpyxl import Workbook  # noqa: PLC0415
        from openpyxl.styles import Font  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            "DOCUMENT_LIBRARY_MISSING", "openpyxl is not installed (Excel authoring unavailable)"
        ) from exc

    workbook = Workbook()
    # The default worksheet is reused for the first sheet, removed otherwise.
    default = workbook.active
    used: set[str] = set()
    entries = list(sheets or [])[:MAX_SHEETS] or [{"name": title or "Sheet1", "rows": []}]
    for index, sheet in enumerate(entries):
        if not isinstance(sheet, dict):
            continue
        rows = _rows_of(sheet.get("rows"))[:MAX_ROWS]
        name = _sheet_title(str(sheet.get("name") or (title if index == 0 else f"Sheet{index + 1}")), used)
        worksheet = default if index == 0 else workbook.create_sheet()
        worksheet.title = name
        for r_index, row in enumerate(rows, start=1):
            for c_index, value in enumerate(row, start=1):
                worksheet.cell(row=r_index, column=c_index, value=_cell(value))
            if r_index == 1:
                for c_index in range(1, len(row) + 1):
                    worksheet.cell(row=1, column=c_index).font = Font(bold=True)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ── PowerPoint (.pptx) ────────────────────────────────────────────────────────


def _deck_bullets(bullets: Sequence[Any]) -> list[list[str]]:
    """Split bullets into deck-legal groups (<=180 chars, <=7 per slide).

    The one-shot writer used to keep every line, so trimming here would lose
    text silently. Long lines are split at word boundaries and extra lines become
    continuation slides instead.
    """
    flat: list[str] = []
    for bullet in bullets:
        text = str(bullet).strip()
        while len(text) > 180:
            cut = text.rfind(" ", 0, 180)
            if cut <= 0:
                cut = 180
            flat.append(text[:cut].strip())
            text = text[cut:].strip()
        if text:
            flat.append(text)
    if not flat:
        return [[]]
    return [flat[index:index + 7] for index in range(0, len(flat), 7)]


def build_pptx(slides: Sequence[dict[str, Any]], *, title: str = "") -> bytes:
    """Build a real .pptx through the deck writer (one layout engine).

    The simple `{title, bullets, notes}` vocabulary is converted into a minimal
    DeckSpec and exported by `presentations/export_pptx.py`, so the one-shot
    document tool and the dedicated deck tool share a writer. Bullets beyond the
    deck's 7-per-slide limit become continuation slides (nothing is dropped). A
    missing python-pptx still reports `DOCUMENT_LIBRARY_MISSING`.
    """
    from ..presentations.model import MAX_SLIDES as DECK_MAX_SLIDES  # noqa: PLC0415
    from ..presentations.model import DeckError, parse_deck  # noqa: PLC0415
    from ..presentations.render import render_deck  # noqa: PLC0415
    from ..presentations.export_pptx import export_pptx  # noqa: PLC0415

    deck_slides: list[dict[str, Any]] = []
    if title:
        deck_slides.append({"layout": "title", "title": title})
    for slide in list(slides or []):
        if not isinstance(slide, dict):
            continue
        slide_title = str(slide.get("title") or "Slide")
        notes = slide.get("notes")
        groups = _deck_bullets(slide.get("bullets") or [])
        for index, group in enumerate(groups):
            deck_slides.append({
                "layout": "bullets" if group else "section",
                "title": slide_title if index == 0 else f"{slide_title} (cont.)",
                "bullets": group,
                **({"notes": notes} if index == 0 and notes else {}),
            })
    if not deck_slides:
        deck_slides = [{"layout": "title", "title": title or "Presentation"}]
    if len(deck_slides) > DECK_MAX_SLIDES:
        raise DocumentError(
            "DOCUMENT_INVALID_CONTENT",
            f"the deck needs {len(deck_slides)} slides; a presentation carries at most {DECK_MAX_SLIDES}",
        )
    try:
        deck = parse_deck({"meta": {"title": title or "Presentation"}, "slides": deck_slides})
        data, _summary = export_pptx(deck, render_deck(deck))
        return data
    except DeckError as exc:
        if exc.code == "PRESENTATION_LIBRARY_MISSING":
            raise DocumentError(
                "DOCUMENT_LIBRARY_MISSING", "python-pptx is not installed (PowerPoint authoring unavailable)"
            ) from exc
        raise DocumentError("DOCUMENT_INVALID_CONTENT", exc.message) from exc


# ── text formats ──────────────────────────────────────────────────────────────


def blocks_to_markdown(blocks: Iterable[dict[str, Any]], *, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for block in list(blocks or [])[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph").casefold()
        if kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1)))
            lines.append(f"{'#' * level} {_as_text(block.get('text'))}")
            lines.append("")
        elif kind in ("bullets", "bullet", "unordered_list"):
            for item in block.get("items") or []:
                lines.append(f"- {_as_text(item)}")
            lines.append("")
        elif kind in ("numbers", "number", "ordered_list"):
            for i, item in enumerate(block.get("items") or [], start=1):
                lines.append(f"{i}. {_as_text(item)}")
            lines.append("")
        elif kind == "table":
            headers = [_as_text(h) for h in (block.get("headers") or [])]
            rows = block.get("rows") or []
            if headers:
                lines.append("| " + " | ".join(headers) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                for row in rows:
                    values = list(row) if isinstance(row, (list, tuple)) else [row]
                    cells = [_as_text(values[i]) if i < len(values) else "" for i in range(len(headers))]
                    lines.append("| " + " | ".join(cells) + " |")
                lines.append("")
        elif kind == "page_break":
            lines.append("---")
            lines.append("")
        else:
            lines.append(_as_text(block.get("text")))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def build_markdown(blocks: Sequence[dict[str, Any]], *, title: str = "") -> bytes:
    return blocks_to_markdown(blocks, title=title).encode("utf-8")


def build_txt(blocks: Sequence[dict[str, Any]], *, title: str = "") -> bytes:
    parts: list[str] = []
    if title:
        parts.append(title)
        parts.append("=" * len(title))
        parts.append("")
    for block in list(blocks or [])[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph").casefold()
        if kind == "heading":
            parts.append(_as_text(block.get("text")))
            parts.append("")
        elif kind in ("bullets", "bullet", "unordered_list"):
            parts.extend(f"- {_as_text(item)}" for item in block.get("items") or [])
            parts.append("")
        elif kind in ("numbers", "number", "ordered_list"):
            parts.extend(f"{i}. {_as_text(item)}" for i, item in enumerate(block.get("items") or [], start=1))
            parts.append("")
        elif kind == "table":
            headers = [_as_text(h) for h in (block.get("headers") or [])]
            rows = block.get("rows") or []
            if headers:
                parts.append("\t".join(headers))
            for row in rows:
                values = list(row) if isinstance(row, (list, tuple)) else [row]
                parts.append("\t".join(_as_text(v) for v in values))
            parts.append("")
        else:
            parts.append(_as_text(block.get("text")))
            parts.append("")
    return ("\n".join(parts).rstrip("\n") + "\n").encode("utf-8")


def build_csv(rows: Any, *, title: str = "") -> bytes:
    table = _rows_of(rows)[:MAX_ROWS]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in table:
        writer.writerow([_as_text(cell) for cell in row])
    return buffer.getvalue().encode("utf-8")


def build_json(rows: Any, *, title: str = "", blocks: Optional[Sequence[dict[str, Any]]] = None) -> bytes:
    if rows is None and blocks is not None:
        payload: Any = [b for b in blocks if isinstance(b, dict)]
    elif isinstance(rows, list) and rows and isinstance(rows[0], dict):
        payload = rows
    elif rows is not None:
        payload = _rows_of(rows)
    else:
        payload = []
    document = {"title": title, "data": payload} if title else payload
    return json.dumps(document, ensure_ascii=False, indent=2, default=str).encode("utf-8")


# ── dispatch ──────────────────────────────────────────────────────────────────


#: How many headings one authored document may report (bounded).
MAX_REPORTED_SECTIONS = 40


def block_headings(blocks: Optional[Sequence[dict[str, Any]]]) -> list[str]:
    """The heading titles an authored document actually contains.

    The writer knows them, so it can RECORD them; that record is what the
    completion verifier checks a contract's required sections against (W14).
    """
    out: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "heading":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            out.append(text[:120])
        if len(out) >= MAX_REPORTED_SECTIONS:
            break
    return out


def report_authored_document(
    ctx: Any, *, title: str, filename: str, headings: Sequence[str],
    artifact_id: str = "", artifact_uri: str = "",
) -> None:
    """Report the document this run WROTE to the run's document collector.

    Best-effort and never fatal: the collector turns this into the bounded
    `authored` entry (its recorded section titles). Without a collector the
    document still exists — only the recorded structure is missing. The
    artifact id travels with it, so the chat can offer the FILE instead of
    leaving the user to hunt for it in the Artifact Center.
    """
    sink = getattr(ctx, "document_sink", None)
    if sink is None:
        return
    try:
        sink({
            "ok": True,
            "kind": "authored_document",
            "title": title or filename,
            "state": "DELIVERED",
            "artifactId": artifact_id or None,
            "uri": artifact_uri or None,
            "ir": {"sections": [{"title": str(h)} for h in headings[:MAX_REPORTED_SECTIONS]]},
        })
    except Exception:  # noqa: BLE001 - the report is additive; never break the write
        return


def build(
    fmt: str,
    *,
    title: str = "",
    blocks: Optional[Sequence[dict[str, Any]]] = None,
    sheets: Optional[Sequence[dict[str, Any]]] = None,
    slides: Optional[Sequence[dict[str, Any]]] = None,
    rows: Any = None,
) -> bytes:
    """Build file bytes for `fmt` from the neutral content vocabulary."""
    canonical = canonical_format(fmt)
    blocks = blocks or []
    if canonical == "docx":
        return build_docx(blocks, title=title)
    if canonical == "xlsx":
        return build_xlsx(sheets or [], title=title)
    if canonical == "pptx":
        return build_pptx(slides or [], title=title)
    if canonical == "markdown":
        return build_markdown(blocks, title=title)
    if canonical == "txt":
        return build_txt(blocks, title=title)
    if canonical == "csv":
        return build_csv(rows, title=title)
    if canonical == "json":
        return build_json(rows, title=title, blocks=blocks)
    raise DocumentError("DOCUMENT_FORMAT_UNSUPPORTED", f"unsupported format {fmt!r}")  # pragma: no cover


__all__ = [
    "DocumentError",
    "FORMAT_EXTENSION",
    "FORMAT_MIME",
    "MAX_REPORTED_SECTIONS",
    "block_headings",
    "build",
    "build_csv",
    "build_docx",
    "build_json",
    "build_markdown",
    "build_pptx",
    "build_txt",
    "build_xlsx",
    "blocks_to_markdown",
    "canonical_format",
    "slug_filename",
]
