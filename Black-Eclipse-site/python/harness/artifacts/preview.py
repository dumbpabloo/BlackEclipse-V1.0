"""Preview & thumbnail generation — bounded, asynchronous-safe, never executes.

The preview engine classifies content into a renderer kind and, where it can do
so safely and cheaply, produces (a) a bounded text extract for text-like formats
and (b) a PNG thumbnail for images / the first PDF page. It NEVER runs the file:
office documents are read as ZIP+XML text, not through an office runtime.

Video thumbnails come from a REGISTERED poster extractor (the video layer
installs one over the same FFmpeg tree the renderer uses —
`set_video_poster_extractor`), so this layer never imports the video layer. No
extractor or no extractable frame ⇒ `UNSUPPORTED`, never a fabricated poster.

Heavy or unsupported formats are reported honestly (`UNSUPPORTED`), and the
original bytes always remain downloadable.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Callable, Optional

from .content import preview_kind
from .model import Artifact, PreviewStatus

#: how many source bytes a text preview may read
MAX_PREVIEW_BYTES = 512 * 1024
MAX_PREVIEW_CHARS = 20_000
THUMBNAIL_MAX = 512
XML_TAG = re.compile(rb"<[^>]+>")

#: The optional video poster generator, registered by the video layer at boot.
#: Receives `(storage, artifact)` and returns PNG bytes of a representative
#: frame, or None when it cannot produce one (FFmpeg tree absent, unreadable
#: object). Kept as a module seam so the artifacts layer holds no video import.
_video_poster: Optional[Callable[[Any, Artifact], Optional[bytes]]] = None


def set_video_poster_extractor(
    extractor: Optional[Callable[[Any, Artifact], Optional[bytes]]],
) -> None:
    """Install (or clear) the video poster generator. Best-effort by contract."""
    global _video_poster
    _video_poster = extractor


def _text_extract(data: bytes) -> Optional[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError:
            return None
    return text[:MAX_PREVIEW_CHARS]


def _office_text(data: bytes) -> Optional[str]:
    """Extract text from a docx/xlsx/pptx (a ZIP of XML) without running office.

    Bounded: reads at most the first few XML parts and caps the result.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts: list[str] = []
            names = archive.namelist()
            wanted = [
                n for n in names
                if n.endswith(("word/document.xml", "xl/sharedStrings.xml"))
                or re.match(r"ppt/slides/slide\d+\.xml$", n)
                or n.endswith("xl/worksheets/sheet1.xml")
            ]
            for name in wanted[:12]:
                try:
                    raw = archive.read(name)
                except KeyError:
                    continue
                text = XML_TAG.sub(b" ", raw).decode("utf-8", "ignore")
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
            if not parts:
                return None
            return "\n\n".join(parts)[:MAX_PREVIEW_CHARS]
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def _deck_outline(data: bytes) -> Optional[str]:
    """A deck's readable preview: its title and slide outline, not raw JSON."""
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("slides"), list):
        return None
    meta = parsed.get("meta") if isinstance(parsed.get("meta"), dict) else {}
    lines = [str(meta.get("title") or "Untitled deck")]
    if meta.get("subtitle"):
        lines.append(str(meta["subtitle"]))
    lines.append("")
    for index, slide in enumerate(parsed["slides"][:40], start=1):
        if not isinstance(slide, dict):
            continue
        title = slide.get("title") or slide.get("statement") or "(untitled)"
        lines.append(f"{index}. [{slide.get('layout') or 'bullets'}] {title}")
    return "\n".join(lines)[:MAX_PREVIEW_CHARS]


def generate_text_preview(
    data: bytes, *, mime: str, kind: str, name: str = ""
) -> Optional[str]:
    if name.casefold().endswith(".deck.json"):
        outline = _deck_outline(data)
        if outline is not None:
            return outline
    if kind in ("text", "code", "markdown"):
        return _text_extract(data)
    if kind == "json":
        text = _text_extract(data)
        return text
    if kind == "table":
        return _text_extract(data)
    if kind == "html":
        text = _text_extract(data)
        return text
    if name.casefold().endswith((".docx", ".xlsx", ".pptx")):
        return _office_text(data)
    return None


def make_thumbnail(data: bytes) -> Optional[bytes]:
    """Produce a bounded PNG thumbnail for an image. None when unsupported."""
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - Pillow absent is a supported state
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGBA")
            image.thumbnail((THUMBNAIL_MAX, THUMBNAIL_MAX))
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a malformed image is a preview failure, not a crash
        return None


def make_pdf_thumbnail(data: bytes) -> Optional[bytes]:
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        pdf = pdfium.PdfDocument(data)
        page = pdf[0]
        bitmap = page.render(scale=1.5)
        image = bitmap.to_pil().convert("RGB")
        image.thumbnail((THUMBNAIL_MAX, THUMBNAIL_MAX))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def classify(artifact: Artifact) -> str:
    return preview_kind(artifact.mime_type, artifact.category, artifact.name)


def build(
    storage: Any, artifact: Artifact, *, limit: int = MAX_PREVIEW_BYTES
) -> dict[str, Any]:
    """Produce preview metadata for an artifact.

    Returns a dict with:
      kind          — the renderer
      status        — READY | UNSUPPORTED | FAILED
      text          — bounded text extract (optional)
      thumbnailKey  — storage key of a generated thumbnail (optional)
    The caller persists the result; this function performs the storage write for
    a thumbnail and reports its key. It never mutates the artifact row.
    """
    kind = classify(artifact)
    result: dict[str, Any] = {"kind": kind, "status": PreviewStatus.READY.value}
    if artifact.storage_key is None:
        return {"kind": kind, "status": PreviewStatus.UNSUPPORTED.value}

    if kind == "video":
        # A video's thumbnail is a REAL frame of the video itself, produced by
        # the registered poster extractor (the renderer's FFmpeg tree). No
        # extractor or no frame is an honest UNSUPPORTED — never a fabricated
        # poster and never a frame from some other video.
        thumb = None
        if _video_poster is not None:
            try:
                thumb = _video_poster(storage, artifact)
            except Exception:  # noqa: BLE001 - a poster failure is not an artifact failure
                thumb = None
        if thumb is None:
            result["status"] = PreviewStatus.UNSUPPORTED.value
            return result
        thumb_key = f"{artifact.storage_key}.thumb.png"
        try:
            storage.save_bytes_at(thumb_key, thumb)
            result["thumbnailKey"] = thumb_key
        except Exception:  # noqa: BLE001 - a failed thumbnail write is not fatal
            result["status"] = PreviewStatus.UNSUPPORTED.value
        return result

    try:
        data = storage.read_bytes(artifact.storage_key, limit=limit)
    except Exception:  # noqa: BLE001 - a missing/unreadable object is a preview failure
        return {"kind": kind, "status": PreviewStatus.FAILED.value}

    if kind == "image":
        thumb = make_thumbnail(data)
        if thumb is not None:
            thumb_key = f"{artifact.storage_key}.thumb.png"
            try:
                storage.save_bytes_at(thumb_key, thumb)
                result["thumbnailKey"] = thumb_key
            except Exception:  # noqa: BLE001
                pass
        return result
    if kind == "pdf":
        thumb = make_pdf_thumbnail(data)
        if thumb is not None:
            thumb_key = f"{artifact.storage_key}.thumb.png"
            try:
                storage.save_bytes_at(thumb_key, thumb)
                result["thumbnailKey"] = thumb_key
            except Exception:  # noqa: BLE001
                pass
        return result
    if kind in ("unsupported", "audio"):
        result["status"] = PreviewStatus.UNSUPPORTED.value
        return result
    text = generate_text_preview(data, mime=artifact.mime_type, kind=kind, name=artifact.name)
    if text is None:
        result["status"] = PreviewStatus.UNSUPPORTED.value
        return result
    result["text"] = text
    return result


__all__ = [
    "build",
    "classify",
    "generate_text_preview",
    "make_pdf_thumbnail",
    "make_thumbnail",
    "set_video_poster_extractor",
    "MAX_PREVIEW_CHARS",
]
