"""Deck -> neutral render model.

`RenderDeck` is plain JSON: normalized regions in inches, resolved chart series,
image availability, font sizes and warnings. React renders it and
`export_pptx` renders it; neither re-derives layout.

Resolution is injected: images come from the Artifact Center and price series
from the market bridge. A resolver that cannot answer is not an error -- it is
`available: false` with a reason, so the slide shows UNAVAILABLE instead of an
invented picture.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .layout import layout_slide, slide_size, slide_theme
from .model import DeckSpec, DeckSize, Slide

ImageResolver = Callable[[str], Optional[bytes]]
PriceResolver = Callable[[dict[str, Any]], Optional[dict[str, Any]]]

#: Deck kinds the pptx writer draws natively. Anything else is either rendered
#: as a picture by the chart engine or reported unavailable.
NATIVE_CHART_KINDS = frozenset({"line", "area", "bar", "stackedBar", "pie", "donut"})


def _chart_data(chart: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], Optional[str]]:
    categories = [str(item) for item in (chart.get("categories") or [])]
    series = [
        {"name": str(item.get("name") or "Series"), "data": list(item.get("data") or []),
         "color": item.get("color")}
        for item in (chart.get("series") or [])
    ]
    values = list(chart.get("values") or [])
    if values and not series:
        series = [{"name": chart.get("title") or "Series", "data": values, "color": None}]
        if not categories:
            categories = [str(label) for label in (chart.get("labels") or [])]
    return categories, series, None


def _resolve_chart(
    slide: Slide,
    deck: DeckSpec,
    price_resolver: Optional[PriceResolver],
) -> Optional[dict[str, Any]]:
    if slide.chart is None:
        return None
    chart = slide.chart.model_dump(by_alias=True)
    categories, series, _ = _chart_data(chart)
    source_label = None

    reference = chart.get("dataSource")
    if reference and (not series or not categories) and price_resolver is not None:
        source = deck.data_sources.get(reference)
        if source is None:
            return {
                **chart,
                "available": False,
                "unavailableReason": f"unknown data source {reference!r}",
            }
        try:
            resolved = price_resolver(source.model_dump(by_alias=True))
        except Exception:  # noqa: BLE001 - a failed fetch is an honest unavailable
            resolved = None
        if not resolved or not resolved.get("values"):
            return {
                **chart,
                "available": False,
                "unavailableReason": f"data source {reference!r} could not be resolved",
            }
        categories = [str(item) for item in resolved.get("categories") or []]
        series = [{
            "name": resolved.get("label") or source.symbol,
            "data": list(resolved["values"]),
            "color": None,
        }]
        source_label = resolved.get("source") or f"{source.symbol} {source.range}"
    elif reference:
        source_label = reference

    kind = str(chart.get("kind") or "line")
    if kind not in NATIVE_CHART_KINDS:
        return {
            **chart,
            "available": False,
            "unavailableReason": f"chart kind {kind!r} is not drawn in decks yet",
        }
    if kind in ("pie", "donut"):
        if not series or not series[0]["data"] or not categories:
            return {
                **chart,
                "available": False,
                "unavailableReason": "a pie chart needs labelled values",
            }
    elif not series or not any(item["data"] for item in series):
        return {
            **chart,
            "available": False,
            "unavailableReason": "no chart data was supplied or resolved",
        }
    return {
        **chart,
        "available": True,
        "categories": categories,
        "series": series,
        "resolvedSource": source_label,
    }


def _resolve_image(
    slide: Slide,
    image_resolver: Optional[ImageResolver],
) -> Optional[dict[str, Any]]:
    if slide.image is None:
        return None
    image = slide.image.model_dump(by_alias=True)
    artifact_id = image.get("artifactId")
    if not artifact_id:
        return {**image, "available": False, "unavailableReason": "no image artifact was supplied"}
    if image_resolver is None:
        return {**image, "available": False, "unavailableReason": "the artifact store is unavailable"}
    try:
        data = image_resolver(str(artifact_id))
    except Exception:  # noqa: BLE001
        data = None
    if not data:
        return {
            **image,
            "available": False,
            "unavailableReason": f"image artifact {artifact_id!r} could not be read",
        }
    return {**image, "available": True, "bytes": len(data)}


def render_deck(
    deck: DeckSpec,
    *,
    price_resolver: Optional[PriceResolver] = None,
    image_resolver: Optional[ImageResolver] = None,
) -> dict[str, Any]:
    """Build the neutral render model for a validated deck."""
    width_in, height_in = slide_size(deck.size.value)
    slides: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for index, slide in enumerate(deck.slides, start=1):
        rendered = layout_slide(slide, size=deck.size.value)
        rendered["page"] = index
        rendered["chart"] = _resolve_chart(slide, deck, price_resolver)
        rendered["image"] = _resolve_image(slide, image_resolver)
        if rendered["chart"] is not None and not rendered["chart"].get("available", True):
            rendered["warnings"] = [*rendered["warnings"], "chart_unavailable"]
        if rendered["image"] is not None and not rendered["image"].get("available", True):
            rendered["warnings"] = [*rendered["warnings"], "image_unavailable"]
        slides.append(rendered)
        for warning in rendered["warnings"]:
            warnings.append({"slideId": rendered["id"], "code": warning})

    return {
        "schemaVersion": 1,
        "size": {
            "name": deck.size.value,
            "widthIn": width_in,
            "heightIn": height_in,
        },
        "theme": slide_theme(deck.theme.preset.value, deck.theme.accent),
        "meta": deck.meta.model_dump(by_alias=True),
        "slideCount": len(slides),
        "slides": slides,
        "warnings": warnings,
        "overflowSlides": sorted({item["slideId"] for item in warnings if item["code"].startswith("overflow:")}),
    }


def outline(deck: DeckSpec) -> list[dict[str, Any]]:
    return [
        {
            "id": slide.id,
            "layout": slide.layout.value,
            "title": slide.title or slide.statement or "",
            "hasNotes": bool(slide.notes),
        }
        for slide in deck.slides
    ]


def default_size_name(size: DeckSize | str) -> str:
    return size.value if isinstance(size, DeckSize) else str(size)


__all__ = [
    "NATIVE_CHART_KINDS",
    "default_size_name",
    "outline",
    "render_deck",
]
