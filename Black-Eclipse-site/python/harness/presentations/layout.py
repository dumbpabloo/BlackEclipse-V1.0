"""Slide layout -- the ONE geometry both renderers use.

Regions are fractions of the slide (0..1), so 16:9 and 4:3 share one design and
the preview and the exporter cannot disagree about where a thing goes. Font
sizes come from `fit.py`; a region that cannot fit its content is reported as a
warning, never silently clipped.
"""

from __future__ import annotations

from typing import Any

from .fit import fit_bullets, fit_text
from .model import DeckSize, Slide, SlideLayout
from .themes import TYPE_SCALE, theme_tokens

#: Fractional regions per layout. `title`/`source`/`footer` are common where the
#: layout has a title band.
REGIONS: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "title": {
        "eyebrow": (0.08, 0.10, 0.84, 0.06),
        "title": (0.08, 0.26, 0.84, 0.30),
        "subtitle": (0.08, 0.60, 0.84, 0.12),
        "footer": (0.08, 0.92, 0.84, 0.05),
    },
    "section": {
        "eyebrow": (0.08, 0.18, 0.84, 0.06),
        "title": (0.08, 0.28, 0.84, 0.26),
        "subtitle": (0.08, 0.58, 0.84, 0.10),
        "footer": (0.08, 0.92, 0.84, 0.05),
    },
    "agenda": {
        "title": (0.06, 0.07, 0.88, 0.14),
        "body": (0.08, 0.26, 0.84, 0.60),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "bullets": {
        "title": (0.06, 0.07, 0.88, 0.14),
        "body": (0.08, 0.26, 0.84, 0.60),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "two_column": {
        "title": (0.06, 0.07, 0.88, 0.12),
        "left": (0.06, 0.23, 0.42, 0.64),
        "right": (0.52, 0.23, 0.42, 0.64),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "statement": {
        "statement": (0.10, 0.20, 0.80, 0.48),
        "attribution": (0.10, 0.72, 0.80, 0.08),
        "footer": (0.08, 0.92, 0.84, 0.05),
    },
    "quote": {
        "statement": (0.10, 0.18, 0.80, 0.50),
        "attribution": (0.10, 0.72, 0.80, 0.08),
        "footer": (0.08, 0.92, 0.84, 0.05),
    },
    "metric_row": {
        "title": (0.06, 0.07, 0.88, 0.13),
        "metrics": (0.06, 0.24, 0.88, 0.54),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "table": {
        "title": (0.06, 0.07, 0.88, 0.12),
        "table": (0.06, 0.23, 0.88, 0.63),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "chart": {
        "title": (0.06, 0.07, 0.88, 0.11),
        "chart": (0.06, 0.22, 0.88, 0.64),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "image_full": {
        "title": (0.05, 0.05, 0.90, 0.10),
        "image": (0.05, 0.17, 0.90, 0.72),
        "source": (0.05, 0.92, 0.90, 0.05),
    },
    "image_right": {
        "title": (0.06, 0.07, 0.88, 0.12),
        "left": (0.06, 0.23, 0.42, 0.63),
        "image": (0.52, 0.21, 0.42, 0.66),
        "source": (0.06, 0.92, 0.88, 0.05),
    },
    "closing": {
        "title": (0.08, 0.32, 0.84, 0.26),
        "subtitle": (0.08, 0.60, 0.84, 0.10),
        "footer": (0.08, 0.92, 0.84, 0.05),
    },
}

SLIDE_SIZES: dict[str, tuple[float, float]] = {
    DeckSize.WIDE.value: (13.333, 7.5),
    DeckSize.STANDARD.value: (10.0, 7.5),
}


def slide_size(size: str) -> tuple[float, float]:
    return SLIDE_SIZES.get(size, SLIDE_SIZES[DeckSize.WIDE.value])


def _rect(region: tuple[float, float, float, float], width_in: float, height_in: float) -> dict[str, float]:
    x, y, w, h = region
    return {
        "x": round(x * width_in, 4),
        "y": round(y * height_in, 4),
        "w": round(w * width_in, 4),
        "h": round(h * height_in, 4),
    }


def _bullet_text(slide: Slide) -> list[str]:
    return [str(item) for item in slide.bullets]


def _table_fit(rows: int, height_in: float) -> tuple[int, bool]:
    for size in (13, 12, 11, 10):
        line = size * 1.28 / 72.0
        if rows * line + 0.15 <= height_in:
            return size, True
    return 10, False


def layout_slide(slide: Slide, *, size: str = DeckSize.WIDE.value) -> dict[str, Any]:
    """Compute regions, ordered text, font sizes and honest warnings."""
    width_in, height_in = slide_size(size)
    spec = REGIONS.get(slide.layout.value, REGIONS["bullets"])
    regions = {name: _rect(box, width_in, height_in) for name, box in spec.items()}
    fonts: dict[str, int] = {}
    warnings: list[str] = []
    align: dict[str, str] = {}

    title = slide.title
    if "title" in regions:
        fonts["title"] = fit_text(
            title or "", regions["title"]["w"], regions["title"]["h"], TYPE_SCALE["title"],
            min_pt=20, bold=True,
        )[0]
        if title and not fit_text(
            title, regions["title"]["w"], regions["title"]["h"], TYPE_SCALE["title"],
            min_pt=20, bold=True,
        )[1]:
            warnings.append("overflow:title")
        align["title"] = "left"
    if "subtitle" in regions and slide.subtitle:
        fonts["subtitle"] = fit_text(
            slide.subtitle, regions["subtitle"]["w"], regions["subtitle"]["h"],
            TYPE_SCALE["subtitle"], min_pt=12,
        )[0]
        align["subtitle"] = "center" if slide.layout in (SlideLayout.TITLE, SlideLayout.CLOSING) else "left"
    if "eyebrow" in regions:
        fonts["eyebrow"] = TYPE_SCALE["eyebrow"]
        align["eyebrow"] = "left"
    if "body" in regions:
        items = _bullet_text(slide)
        if slide.layout is SlideLayout.BULLETS and slide.body:
            items = [slide.body] if not items else items
        pt, fits = fit_bullets(
            items or [""], regions["body"]["w"] - 0.35, regions["body"]["h"], TYPE_SCALE["bullet"],
        )
        fonts["body"] = pt
        if not fits:
            warnings.append("overflow:body")
        align["body"] = "left"
    if "left" in regions:
        column = slide.left
        items = list(column.bullets) if column else []
        if slide.layout is SlideLayout.IMAGE_RIGHT and slide.body:
            items = [slide.body]
        pt, fits = fit_bullets(
            items or [""], regions["left"]["w"] - 0.35, regions["left"]["h"] - 0.4, TYPE_SCALE["bullet"],
        )
        fonts["left"] = pt
        if not fits:
            warnings.append("overflow:left")
        align["left"] = "left"
    if "right" in regions:
        column = slide.right
        items = list(column.bullets) if column else []
        pt, fits = fit_bullets(
            items or [""], regions["right"]["w"] - 0.35, regions["right"]["h"] - 0.4, TYPE_SCALE["bullet"],
        )
        fonts["right"] = pt
        if not fits:
            warnings.append("overflow:right")
        align["right"] = "left"
    if "statement" in regions:
        text = slide.statement or slide.title or ""
        base = TYPE_SCALE["quote"] if slide.layout is SlideLayout.QUOTE else TYPE_SCALE["statement"]
        pt, fits = fit_text(
            text, regions["statement"]["w"], regions["statement"]["h"], base, min_pt=16, bold=slide.layout is SlideLayout.STATEMENT,
        )
        fonts["statement"] = pt
        if not fits:
            warnings.append("overflow:statement")
        align["statement"] = "left" if slide.layout is SlideLayout.QUOTE else "left"
    if "attribution" in regions:
        fonts["attribution"] = TYPE_SCALE["attribution"]
        align["attribution"] = "right"
    if "metrics" in regions:
        count = max(1, len(slide.metrics))
        card_w = (regions["metrics"]["w"] - 0.25 * (count - 1)) / count
        fonts["metric_value"] = fit_text(
            " ".join(metric.value for metric in slide.metrics) or "",
            card_w - 0.3, 0.9, TYPE_SCALE["metric_value"], min_pt=18, bold=True,
        )[0]
        fonts["metric_label"] = TYPE_SCALE["metric_label"]
        fonts["metric_note"] = TYPE_SCALE["metric_note"]
    if "table" in regions and slide.table is not None:
        pt, fits = _table_fit(len(slide.table.rows) + 1, regions["table"]["h"])
        fonts["table"] = pt
        if not fits:
            warnings.append("overflow:table")
    if "chart" in regions:
        fonts["chart_title"] = TYPE_SCALE["chart_title"]
    if "source" in regions:
        fonts["source"] = TYPE_SCALE["source"]
    if "footer" in regions:
        fonts["footer"] = TYPE_SCALE["page"]

    return {
        "id": slide.id,
        "layout": slide.layout.value,
        "regions": regions,
        "fonts": fonts,
        "align": align,
        "warnings": warnings,
        "title": title,
        "eyebrow": slide.eyebrow,
        "subtitle": slide.subtitle,
        "bullets": _bullet_text(slide),
        "body": slide.body,
        "statement": slide.statement,
        "attribution": slide.attribution,
        "left": slide.left.model_dump(by_alias=True) if slide.left else None,
        "right": slide.right.model_dump(by_alias=True) if slide.right else None,
        "metrics": [metric.model_dump(by_alias=True) for metric in slide.metrics],
        "table": slide.table.model_dump(by_alias=True) if slide.table else None,
        "chart": slide.chart.model_dump(by_alias=True) if slide.chart else None,
        "image": slide.image.model_dump(by_alias=True) if slide.image else None,
        "notes": slide.notes,
        "source": slide.source,
        "page": None,
    }


def slide_theme(preset: str, accent: str | None = None) -> dict[str, Any]:
    return theme_tokens(preset, accent)


__all__ = ["REGIONS", "SLIDE_SIZES", "layout_slide", "slide_size", "slide_theme"]
