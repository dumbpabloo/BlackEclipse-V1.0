"""Import an existing `.pptx` as an editable deck OUTLINE.

This is deliberately not a lossless round-trip: PowerPoint's design (masters,
free positioning, animations, SmartArt) has no representation in the closed
DeckSpec vocabulary. The importer keeps what it can state honestly -- slide
titles, text lines as bullets, speaker notes and table cells as text -- and names
what it dropped in `warnings`.
"""

from __future__ import annotations

import io
from typing import Any

from .model import MAX_BULLETS, MAX_BULLET_CHARS, MAX_NOTES_CHARS, MAX_SLIDES, DeckError, DeckSpec, parse_deck


def deck_from_pptx(
    data: bytes, *, title: str = "", max_slides: int = MAX_SLIDES
) -> tuple[DeckSpec, list[str]]:
    try:
        from pptx import Presentation  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DeckError(
            "PRESENTATION_LIBRARY_MISSING",
            "python-pptx is not installed (PowerPoint import unavailable)",
        ) from exc

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DeckError("DECK_INVALID", f"the file is not a readable .pptx: {exc}") from exc

    slides: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, source in enumerate(presentation.slides, start=1):
        if index > max_slides:
            warnings.append(f"stopped after {max_slides} slides")
            break
        title_shape = source.shapes.title
        slide_title = ""
        try:
            if title_shape is not None and title_shape.has_text_frame:
                slide_title = title_shape.text_frame.text.strip()
        except Exception:  # noqa: BLE001 - a malformed placeholder is not fatal
            slide_title = ""

        # A deck exported by Eclipse draws plain textboxes (no title placeholder),
        # so when there is no placeholder pick the text box with the largest run
        # size as the title. It is a heuristic and the result is still editable.
        if not slide_title:
            best: tuple[float, Any, str] | None = None
            for shape in source.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if not text:
                        continue
                    for run in paragraph.runs:
                        size = getattr(getattr(run, "font", None), "size", None)
                        points = float(size.pt) if size is not None else 0.0
                        if best is None or points > best[0]:
                            best = (points, shape, text)
            if best is not None:
                title_shape = best[1]
                slide_title = best[2]

        bullets: list[str] = []
        title_id = title_shape.shape_id if title_shape is not None else None
        for shape in source.shapes:
            if title_id is not None and shape.shape_id == title_id:
                continue
            if getattr(shape, "has_table", False):
                warnings.append(f"slide {index}: table imported as text rows")
                for row in shape.table.rows:
                    line = " | ".join(cell.text.strip() for cell in row.cells).strip(" |")
                    if line:
                        bullets.append(line)
                continue
            if not getattr(shape, "has_text_frame", False):
                if getattr(shape, "has_chart", False):
                    warnings.append(f"slide {index}: chart not imported")
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if text:
                    bullets.append(text)

        if not slide_title:
            slide_title = bullets.pop(0) if bullets else f"Slide {index}"
        if len(bullets) > MAX_BULLETS:
            warnings.append(
                f"slide {index}: {len(bullets)} text lines trimmed to {MAX_BULLETS}"
            )
            bullets = bullets[:MAX_BULLETS]
        bullets = [line[:MAX_BULLET_CHARS] for line in bullets]
        notes = ""
        try:
            if source.has_notes_slide:
                notes = source.notes_slide.notes_text_frame.text.strip()
        except Exception:  # noqa: BLE001
            notes = ""
        slides.append({
            "layout": "bullets" if bullets else "section",
            "title": slide_title[:200] or f"Slide {index}",
            "bullets": bullets,
            **({"notes": notes[:MAX_NOTES_CHARS]} if notes else {}),
        })

    if not slides:
        raise DeckError("DECK_INVALID", "the presentation has no slides")
    deck_title = (title or "").strip() or (
        presentation.core_properties.title or ""
    ).strip() or "Imported deck"
    deck = parse_deck({
        "meta": {"title": deck_title[:200]},
        "slides": slides,
    })
    return deck, warnings


__all__ = ["deck_from_pptx"]
