"""Render a deck render-model to PDF or PNG images with Matplotlib.

No LibreOffice and no browser: the vendored Matplotlib backend draws the same
neutral render model the `.pptx` writer uses, so a PDF/PNG export works on a
fresh clone. Fidelity is honest: text, tables, metric cards, images and charts
are drawn; animations and PowerPoint-only effects do not exist here.

Unavailable charts/images are labelled placeholders, exactly as in the pptx.
"""

from __future__ import annotations

import io
import textwrap
import zipfile
from typing import Any, Optional

from .model import DeckError, DeckSpec


def _plt() -> Any:
    try:
        import matplotlib  # noqa: PLC0415

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415

        return plt
    except Exception as exc:  # noqa: BLE001
        raise DeckError(
            "PRESENTATION_LIBRARY_MISSING",
            "Matplotlib is not installed (PDF/PNG deck export unavailable)",
        ) from exc


def _rgb(value: str) -> str:
    return value if value.startswith("#") else f"#{value}"


def _align(name: str) -> str:
    return {"left": "left", "center": "center", "right": "right"}.get(name, "left")


def _wrap(text: str, width_in: float, pt: float) -> list[str]:
    if not text:
        return []
    chars = max(int(width_in / max(pt * 0.55 / 72.0, 1e-6)), 8)
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        lines.extend(textwrap.wrap(paragraph, width=chars) or [""])
    return lines


def _text(
    ax: Any,
    region: dict[str, float],
    text: str,
    *,
    pt: float,
    color: str,
    family: str,
    bold: bool = False,
    align: str = "left",
    valign: str = "top",
    max_lines: Optional[int] = None,
) -> None:
    if not text:
        return
    lines = _wrap(text, region["w"], pt)
    if max_lines is not None:
        lines = lines[:max_lines]
    line_h = pt * 1.28 / 72.0
    total = line_h * len(lines)
    if valign == "middle":
        y = region["y"] + (region["h"] - total) / 2
    elif valign == "bottom":
        y = region["y"] + region["h"] - total
    else:
        y = region["y"]
    x = region["x"]
    if align == "center":
        x += region["w"] / 2
    elif align == "right":
        x += region["w"]
    for index, line in enumerate(lines):
        ax.text(
            x, y + index * line_h, line,
            fontsize=pt, color=_rgb(color), family=family,
            fontweight="bold" if bold else "normal", ha=_align(align), va="top",
            clip_on=True,
        )


def _bullets(ax: Any, region: dict[str, float], items: list[str], *,
             pt: float, color: str, family: str) -> None:
    y = region["y"]
    line_h = pt * 1.28 / 72.0
    gap = 8 / 72.0
    for item in items:
        lines = _wrap(item, region["w"] - 0.28, pt)
        for index, line in enumerate(lines):
            prefix = "•  " if index == 0 else "   "
            ax.text(region["x"] + 0.05, y + index * line_h, f"{prefix}{line}",
                    fontsize=pt, color=_rgb(color), family=family, va="top", clip_on=True)
        y += line_h * len(lines) + gap
        if y > region["y"] + region["h"]:
            break


def _card(ax: Any, region: dict[str, float], theme: dict[str, Any]) -> None:
    from matplotlib.patches import FancyBboxPatch  # noqa: PLC0415

    ax.add_patch(FancyBboxPatch(
        (region["x"], region["y"]), region["w"], region["h"],
        boxstyle="round,pad=0,rounding_size=0.08",
        facecolor=_rgb(theme["surface"]), edgecolor=_rgb(theme["border"]), linewidth=0.8,
    ))


def _metrics(ax: Any, region: dict[str, float], metrics: list[dict[str, Any]],
             fonts: dict[str, int], theme: dict[str, Any]) -> None:
    count = max(1, len(metrics))
    gap = 0.22
    card_w = (region["w"] - gap * (count - 1)) / count
    for index, metric in enumerate(metrics[:count]):
        x = region["x"] + index * (card_w + gap)
        card = {"x": x, "y": region["y"], "w": card_w, "h": region["h"]}
        _card(ax, card, theme)
        _text(ax, {"x": x + 0.16, "y": region["y"] + 0.22, "w": card_w - 0.32, "h": 0.9},
              str(metric.get("value") or ""), pt=fonts.get("metric_value", 30),
              color=theme["text"], family=theme["font_heading"], bold=True)
        _text(ax, {"x": x + 0.16, "y": region["y"] + 1.18, "w": card_w - 0.32, "h": 0.4},
              str(metric.get("label") or ""), pt=fonts.get("metric_label", 12),
              color=theme["muted"], family=theme["font_body"])
        y = 1.58
        for key, color in (("delta", theme["accent"]), ("note", theme["muted"])):
            if metric.get(key):
                _text(ax, {"x": x + 0.16, "y": region["y"] + y, "w": card_w - 0.32, "h": 0.6},
                      str(metric[key]), pt=fonts.get("metric_note", 11), color=color,
                      family=theme["font_body"], max_lines=2)
                y += 0.42


def _table(ax: Any, region: dict[str, float], table: dict[str, Any], *,
           pt: float, theme: dict[str, Any]) -> None:
    from matplotlib.patches import Rectangle  # noqa: PLC0415

    headers = [str(item) for item in (table.get("headers") or [])]
    rows = [[str(cell) for cell in row] for row in (table.get("rows") or [])]
    columns = len(headers) or (len(rows[0]) if rows else 0)
    if columns == 0:
        _text(ax, region, table.get("caption") or "No table data", pt=14,
              color=theme["muted"], family=theme["font_body"])
        return
    row_h = min(region["h"] / (len(rows) + 1), 0.42)
    col_w = region["w"] / columns
    for row_index, values in enumerate([headers, *rows]):
        y = region["y"] + row_index * row_h
        for col in range(columns):
            x = region["x"] + col * col_w
            ax.add_patch(Rectangle(
                (x, y), col_w, row_h,
                facecolor=_rgb(theme["surface_alt"] if row_index == 0 else (
                    theme["surface"] if row_index % 2 else theme["background"])),
                edgecolor=_rgb(theme["border"]), linewidth=0.4,
            ))
            cell = values[col] if col < len(values) else ""
            ax.text(x + 0.06, y + row_h / 2, cell, fontsize=pt,
                    color=_rgb(theme["text"]), family=theme["font_body"],
                    fontweight="bold" if row_index == 0 else "normal",
                    ha="left", va="center", clip_on=True)


def _placeholder(ax: Any, region: dict[str, float], reason: str, theme: dict[str, Any]) -> None:
    from matplotlib.patches import FancyBboxPatch  # noqa: PLC0415

    ax.add_patch(FancyBboxPatch(
        (region["x"], region["y"]), region["w"], region["h"],
        boxstyle="round,pad=0,rounding_size=0.06",
        facecolor=_rgb(theme["surface"]), edgecolor=_rgb(theme["border"]), linestyle="--",
    ))
    _text(ax, {**region, "y": region["y"] + region["h"] / 2 - 0.25},
          f"UNAVAILABLE\n{reason}", pt=14, color=theme["muted"],
          family=theme["font_body"], bold=True, align="center")


def _chart_spec(chart: dict[str, Any]) -> Any:
    from ..charts.spec import ChartSeries, ChartSpec  # noqa: PLC0415

    kind = str(chart.get("kind") or "line")
    categories = [str(item) for item in (chart.get("categories") or [])]
    series = chart.get("series") or []
    if kind in ("pie", "donut"):
        values = list(series[0]["data"]) if series else list(chart.get("values") or [])
        return ChartSpec(
            kind=kind, labels=categories, values=[float(v or 0) for v in values],
            title=chart.get("title"), theme="eclipse-dark",
            width=900, height=520,
        )
    return ChartSpec(
        kind=kind, categories=categories,
        series=[ChartSeries(name=str(item.get("name") or "Series"),
                            data=[None if value is None else float(value) for value in item.get("data") or []])
                for item in series],
        title=chart.get("title"), theme="eclipse-dark",
        width=900, height=520,
    )


def _chart(ax: Any, region: dict[str, float], chart: dict[str, Any], theme: dict[str, Any]) -> None:
    if not chart.get("available"):
        _placeholder(ax, region, str(chart.get("unavailableReason") or "chart unavailable"), theme)
        return
    try:
        from ..charts.render import render_chart  # noqa: PLC0415

        png = render_chart(_chart_spec(chart))
        image = _plt().imread(io.BytesIO(png))
        ax.imshow(image, extent=[region["x"], region["x"] + region["w"],
                                 region["y"] + region["h"], region["y"]],
                  aspect="auto", zorder=2)
    except Exception:  # noqa: BLE001 - a chart failure is a labelled gap
        _placeholder(ax, region, "chart could not be drawn", theme)


def _image(ax: Any, region: dict[str, float], image: dict[str, Any],
           data: Optional[bytes], theme: dict[str, Any]) -> None:
    if not data:
        _placeholder(ax, region, str(image.get("unavailableReason") or "image unavailable"), theme)
        return
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as picture:
            picture = picture.convert("RGB")
            box_ratio = region["w"] / max(region["h"], 0.01)
            width, height = picture.size
            image_ratio = width / max(height, 1)
            if str(image.get("fit") or "cover") == "cover":
                if image_ratio > box_ratio:
                    new_w = int(height * box_ratio)
                    left = (width - new_w) // 2
                    picture = picture.crop((left, 0, left + new_w, height))
                else:
                    new_h = int(width / box_ratio)
                    top = (height - new_h) // 2
                    picture = picture.crop((0, top, width, top + new_h))
            ax.imshow(_plt().imread(io.BytesIO(_png_bytes(picture))),
                      extent=[region["x"], region["x"] + region["w"],
                              region["y"] + region["h"], region["y"]],
                      aspect="auto", zorder=2)
    except Exception:  # noqa: BLE001
        _placeholder(ax, region, "image could not be placed", theme)


def _png_bytes(picture: Any) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_slide(fig: Any, model: dict[str, Any], theme: dict[str, Any],
                image_resolver: Any, background: str) -> None:
    from matplotlib.patches import Rectangle  # noqa: PLC0415

    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, model["widthIn"])
    ax.set_ylim(model["heightIn"], 0)
    ax.add_patch(Rectangle((0, 0), model["widthIn"], model["heightIn"],
                           facecolor=_rgb(background), edgecolor="none"))
    regions = model["regions"]
    fonts = model["fonts"]
    align = model.get("align") or {}

    if regions.get("eyebrow") and model.get("eyebrow"):
        _text(ax, regions["eyebrow"], str(model["eyebrow"]), pt=fonts.get("eyebrow", 12),
              color=theme["accent"], family=theme["font_body"], bold=True)
    if regions.get("title") and model.get("title"):
        _text(ax, regions["title"], str(model["title"]), pt=fonts.get("title", 30),
              color=theme["text"], family=theme["font_heading"], bold=True,
              align=align.get("title", "left"),
              valign="middle" if model["layout"] in ("title", "closing") else "top")
    if regions.get("subtitle") and model.get("subtitle"):
        _text(ax, regions["subtitle"], str(model["subtitle"]), pt=fonts.get("subtitle", 18),
              color=theme["muted"], family=theme["font_body"], align=align.get("subtitle", "left"))
    if regions.get("body"):
        items = model.get("bullets") or ([model["body"]] if model.get("body") else [])
        _bullets(ax, regions["body"], items, pt=fonts.get("body", 18),
                 color=theme["text"], family=theme["font_body"])
    if regions.get("left"):
        column = model.get("left") or {}
        if column.get("heading"):
            _text(ax, {**regions["left"], "h": 0.32}, str(column["heading"]), pt=13,
                  color=theme["accent"], family=theme["font_heading"], bold=True)
        body = {**regions["left"], "y": regions["left"]["y"] + (0.38 if column.get("heading") else 0)}
        items = list(column.get("bullets") or [])
        if not items and model.get("body") and model["layout"] == "image_right":
            items = [str(model["body"])]
        _bullets(ax, body, items, pt=fonts.get("left", 18),
                 color=theme["text"], family=theme["font_body"])
    if regions.get("right"):
        column = model.get("right") or {}
        if column.get("heading"):
            _text(ax, {**regions["right"], "h": 0.32}, str(column["heading"]), pt=13,
                  color=theme["accent"], family=theme["font_heading"], bold=True)
        body = {**regions["right"], "y": regions["right"]["y"] + (0.38 if column.get("heading") else 0)}
        _bullets(ax, body, list(column.get("bullets") or []), pt=fonts.get("right", 18),
                 color=theme["text"], family=theme["font_body"])
    if regions.get("statement"):
        text = model.get("statement") or model.get("title") or ""
        _text(ax, regions["statement"], str(text), pt=fonts.get("statement", 28),
              color=theme["text"], family=theme["font_heading"],
              bold=model["layout"] == "statement", valign="middle")
    if regions.get("attribution") and model.get("attribution"):
        _text(ax, regions["attribution"], str(model["attribution"]), pt=fonts.get("attribution", 14),
              color=theme["muted"], family=theme["font_body"], align="right")
    if regions.get("metrics"):
        _metrics(ax, regions["metrics"], model.get("metrics") or [], fonts, theme)
    if regions.get("table") and model.get("table"):
        _table(ax, regions["table"], model["table"], pt=fonts.get("table", 12), theme=theme)
    if regions.get("chart") and model.get("chart"):
        _chart(ax, regions["chart"], model["chart"], theme)
    if regions.get("image") and model.get("image"):
        image = model["image"]
        data = None
        if image.get("available") and image.get("artifactId") and image_resolver is not None:
            try:
                data = image_resolver(str(image["artifactId"]))
            except Exception:  # noqa: BLE001
                data = None
        _image(ax, regions["image"], image, data, theme)
    if regions.get("source") and model.get("source"):
        _text(ax, regions["source"], str(model["source"]), pt=fonts.get("source", 10),
              color=theme["muted"], family=theme["font_body"], max_lines=2)
    page = str(model.get("page") or "")
    if regions.get("footer"):
        footer = str(model.get("footer") or "")
        if footer:
            _text(ax, regions["footer"], footer, pt=fonts.get("footer", 10),
                  color=theme["muted"], family=theme["font_body"])
        _text(ax, {**regions["footer"], "x": regions["footer"]["x"] + regions["footer"]["w"] - 0.6,
                   "w": 0.6}, page, pt=fonts.get("footer", 10), color=theme["muted"],
              family=theme["font_body"], align="right")
    elif regions.get("source"):
        _text(ax, {**regions["source"], "x": regions["source"]["x"] + regions["source"]["w"] - 0.6,
                   "w": 0.6}, page, pt=fonts.get("source", 10), color=theme["muted"],
              family=theme["font_body"], align="right")


def _summary(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "slides": len(model["slides"]),
        "warnings": model.get("warnings") or [],
        "overflowSlides": model.get("overflowSlides") or [],
    }


def render_slide_png(model: dict[str, Any], index: int = 0, *, dpi: int = 72,
                     image_resolver: Any = None) -> bytes:
    """One slide as a bounded PNG (used for the deck artifact's thumbnail)."""
    plt = _plt()
    from matplotlib.figure import Figure  # noqa: PLC0415

    slides = model.get("slides") or []
    if not slides:
        raise DeckError("DECK_INVALID", "the deck has no slides to render")
    slide = slides[max(0, min(index, len(slides) - 1))]
    theme = dict(model["theme"])
    background = theme["background"]
    fig = Figure(figsize=(model["size"]["widthIn"], model["size"]["heightIn"]))
    _draw_slide(fig, {**slide, "widthIn": model["size"]["widthIn"],
                      "heightIn": model["size"]["heightIn"]}, theme, image_resolver, background)
    image = io.BytesIO()
    fig.savefig(image, format="png", dpi=dpi, facecolor=background,
                bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    return image.getvalue()


def export_pdf(deck: DeckSpec, model: dict[str, Any], *, image_resolver: Any = None) -> tuple[bytes, dict[str, Any]]:
    plt = _plt()
    from matplotlib.backends.backend_pdf import PdfPages  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415

    buffer = io.BytesIO()
    theme = dict(model["theme"])
    background = theme["background"]
    with PdfPages(buffer) as pdf:
        for slide in model["slides"]:
            fig = Figure(figsize=(model["size"]["widthIn"], model["size"]["heightIn"]))
            _draw_slide(fig, {**slide, "widthIn": model["size"]["widthIn"],
                              "heightIn": model["size"]["heightIn"]}, theme, image_resolver, background)
            pdf.savefig(fig)
            plt.close(fig)
    return buffer.getvalue(), _summary(model)


def export_png_zip(deck: DeckSpec, model: dict[str, Any], *, image_resolver: Any = None) -> tuple[bytes, dict[str, Any]]:
    plt = _plt()
    from matplotlib.figure import Figure  # noqa: PLC0415

    theme = dict(model["theme"])
    background = theme["background"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, slide in enumerate(model["slides"], start=1):
            fig = Figure(figsize=(model["size"]["widthIn"], model["size"]["heightIn"]))
            _draw_slide(fig, {**slide, "widthIn": model["size"]["widthIn"],
                              "heightIn": model["size"]["heightIn"]}, theme, image_resolver, background)
            image = io.BytesIO()
            fig.savefig(image, format="png", dpi=150, facecolor=background)
            plt.close(fig)
            archive.writestr(f"slide-{index:02d}.png", image.getvalue())
    return buffer.getvalue(), _summary(model)


__all__ = ["export_pdf", "export_png_zip", "render_slide_png"]
