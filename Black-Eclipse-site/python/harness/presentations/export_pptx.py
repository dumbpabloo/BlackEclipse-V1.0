"""Render a deck render-model into a real `.pptx` with python-pptx.

The writer draws exactly the regions `render.py` computed. It never re-flows,
never invents content and never hides a warning: an unavailable chart/image is a
labelled placeholder, and the result carries `overflowSlides` so the tool can
say the deck does not fit.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from .model import DeckError, DeckSpec

try:  # imported lazily by the caller too; kept importable for tests
    from pptx import Presentation  # noqa: F401
except Exception:  # pragma: no cover - a missing library is reported by export
    Presentation = None  # type: ignore[assignment]


def _require_library() -> Any:
    try:
        from pptx import Presentation as _Presentation  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DeckError(
            "PRESENTATION_LIBRARY_MISSING",
            "python-pptx is not installed (PowerPoint export unavailable)",
        ) from exc
    return _Presentation


def _rgb(value: str) -> Any:
    from pptx.dml.color import RGBColor  # noqa: PLC0415

    text = (value or "#000000").lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _align(name: str) -> Any:
    from pptx.enum.text import PP_ALIGN  # noqa: PLC0415

    return {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(
        name, PP_ALIGN.LEFT
    )


def _textbox(
    slide: Any,
    region: dict[str, float],
    text: str,
    *,
    pt: float,
    color: str,
    font: str,
    bold: bool = False,
    align: str = "left",
    anchor: str = "t",
    italic: bool = False,
) -> Any:
    from pptx.enum.text import MSO_ANCHOR  # noqa: PLC0415
    from pptx.util import Inches, Pt  # noqa: PLC0415

    box = slide.shapes.add_textbox(
        Inches(region["x"]), Inches(region["y"]), Inches(region["w"]), Inches(region["h"])
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    frame.vertical_anchor = {
        "t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM
    }.get(anchor, MSO_ANCHOR.TOP)
    paragraph = frame.paragraphs[0]
    paragraph.alignment = _align(align)
    run = paragraph.add_run()
    run.text = str(text)
    run.font.size = Pt(pt)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = font
    run.font.color.rgb = _rgb(color)
    return box


def _background(slide: Any, color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _bullets(
    slide: Any,
    region: dict[str, float],
    items: list[str],
    *,
    pt: float,
    color: str,
    font: str,
    indent: float = 0.28,
) -> None:
    from pptx.util import Inches, Pt  # noqa: PLC0415

    box = slide.shapes.add_textbox(
        Inches(region["x"] + indent), Inches(region["y"]),
        Inches(max(region["w"] - indent, 0.5)), Inches(region["h"]),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    for index, item in enumerate(items or [""]):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.space_after = Pt(8)
        run = paragraph.add_run()
        run.text = f"•  {item}"
        run.font.size = Pt(pt)
        run.font.name = font
        run.font.color.rgb = _rgb(color)


def _column_heading(
    slide: Any,
    region: dict[str, float],
    heading: Optional[str],
    *,
    color: str,
    font: str,
    accent: str,
) -> None:
    if not heading:
        return
    header = {**region, "h": 0.32}
    _textbox(slide, header, heading, pt=13, color=accent, font=font, bold=True)


def _table(slide: Any, region: dict[str, float], table: dict[str, Any], *, pt: float,
           theme: dict[str, Any]) -> None:
    from pptx.util import Inches, Pt  # noqa: PLC0415

    headers = [str(item) for item in (table.get("headers") or [])]
    rows = [[str(cell) for cell in row] for row in (table.get("rows") or [])]
    columns = len(headers) or (len(rows[0]) if rows else 0)
    if columns == 0:
        _placeholder(slide, region, table.get("caption") or "No table data", theme)
        return
    shape = slide.shapes.add_table(
        len(rows) + 1, columns,
        Inches(region["x"]), Inches(region["y"]), Inches(region["w"]), Inches(region["h"]),
    )
    grid = shape.table
    for col in range(columns):
        cell = grid.cell(0, col)
        cell.text = headers[col] if col < len(headers) else ""
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(theme["surface_alt"])
        for paragraph in cell.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.bold = True
                run.font.size = Pt(pt)
                run.font.name = theme["font_body"]
                run.font.color.rgb = _rgb(theme["text"])
    for row_index, row in enumerate(rows, start=1):
        for col in range(columns):
            cell = grid.cell(row_index, col)
            cell.text = row[col] if col < len(row) else ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme["surface"] if row_index % 2 else theme["background"])
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(pt)
                    run.font.name = theme["font_body"]
                    run.font.color.rgb = _rgb(theme["text"])


def _chart_type(kind: str) -> Any:
    from pptx.enum.chart import XL_CHART_TYPE  # noqa: PLC0415

    return {
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "area": XL_CHART_TYPE.AREA,
        "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "stackedBar": XL_CHART_TYPE.COLUMN_STACKED,
        "pie": XL_CHART_TYPE.PIE,
        "donut": XL_CHART_TYPE.DOUGHNUT,
    }[kind]


def _chart(slide: Any, region: dict[str, float], chart: dict[str, Any], theme: dict[str, Any]) -> None:
    from pptx.chart.data import CategoryChartData  # noqa: PLC0415
    from pptx.util import Inches, Pt  # noqa: PLC0415

    categories = [str(item) for item in (chart.get("categories") or [])]
    series = chart.get("series") or []
    if not categories or not series:
        _placeholder(slide, region, "Chart data unavailable", theme)
        return
    data = CategoryChartData()
    data.categories = categories
    for item in series:
        values = [None if value is None else float(value) for value in item.get("data") or []]
        # A category axis needs one value per category; pad honestly with None.
        if len(values) < len(categories):
            values = values + [None] * (len(categories) - len(values))
        data.add_series(str(item.get("name") or "Series"), values[: len(categories)])
    frame = slide.shapes.add_chart(
        _chart_type(str(chart.get("kind") or "line")), Inches(region["x"]), Inches(region["y"]),
        Inches(region["w"]), Inches(region["h"]), data,
    )
    graphic = frame.chart
    try:
        graphic.font.size = Pt(10)
        graphic.font.name = theme["font_body"]
        graphic.font.color.rgb = _rgb(theme["muted"])
        graphic.has_legend = len(series) > 1
        if graphic.has_legend:
            from pptx.enum.chart import XL_LEGEND_POSITION  # noqa: PLC0415

            graphic.legend.position = XL_LEGEND_POSITION.BOTTOM
            graphic.legend.include_in_layout = False
        graphic.format.fill.solid()
        graphic.format.fill.fore_color.rgb = _rgb(theme["surface"])
        plot = graphic.plots[0]
        plot.gap_width = 60
        for axis in (getattr(graphic, "category_axis", None), getattr(graphic, "value_axis", None)):
            if axis is not None:
                axis.format.line.color.rgb = _rgb(theme["border"])
                axis.tick_labels.font.size = Pt(10)
                axis.tick_labels.font.color.rgb = _rgb(theme["muted"])
    except Exception:  # noqa: BLE001 - styling is cosmetic; the chart still exists
        pass


def _placeholder(slide: Any, region: dict[str, float], reason: str, theme: dict[str, Any]) -> None:
    from pptx.enum.shapes import MSO_SHAPE  # noqa: PLC0415
    from pptx.util import Inches, Pt  # noqa: PLC0415

    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(region["x"]), Inches(region["y"]), Inches(region["w"]), Inches(region["h"]),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(theme["surface"])
    shape.line.color.rgb = _rgb(theme["border"])
    frame = shape.text_frame
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    paragraph.text = ""
    run = paragraph.add_run()
    run.text = f"UNAVAILABLE\n{reason}"
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.name = theme["font_body"]
    run.font.color.rgb = _rgb(theme["muted"])


def _image(
    slide: Any,
    region: dict[str, float],
    image: dict[str, Any],
    data: Optional[bytes],
    theme: dict[str, Any],
) -> None:
    from pptx.util import Inches  # noqa: PLC0415

    if not data:
        _placeholder(slide, region, image.get("unavailableReason") or "image unavailable", theme)
        return
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as picture:
            image_w, image_h = picture.size
        box_ratio = region["w"] / max(region["h"], 0.01)
        image_ratio = image_w / max(image_h, 1)
        crop = None
        if str(image.get("fit") or "cover") == "cover" and abs(box_ratio - image_ratio) > 0.01:
            if image_ratio > box_ratio:
                excess = 1 - box_ratio / image_ratio
                crop = (excess / 2, 0, excess / 2, 0)
            else:
                excess = 1 - image_ratio / box_ratio
                crop = (0, excess / 2, 0, excess / 2)
        kwargs = {}
        if crop:
            kwargs = {
                "crop_left": crop[0], "crop_top": crop[1],
                "crop_right": crop[2], "crop_bottom": crop[3],
            }
        slide.shapes.add_picture(
            io.BytesIO(data), Inches(region["x"]), Inches(region["y"]),
            Inches(region["w"]), Inches(region["h"]), **kwargs,
        )
    except Exception:  # noqa: BLE001 - a bad image is a labelled placeholder
        _placeholder(slide, region, "image could not be placed", theme)


def _metric_cards(slide: Any, region: dict[str, float], metrics: list[dict[str, Any]],
                  fonts: dict[str, int], theme: dict[str, Any]) -> None:
    from pptx.enum.shapes import MSO_SHAPE  # noqa: PLC0415
    from pptx.util import Inches  # noqa: PLC0415

    count = max(1, len(metrics))
    gap = 0.22
    card_w = (region["w"] - gap * (count - 1)) / count
    for index, metric in enumerate(metrics[:count]):
        x = region["x"] + index * (card_w + gap)
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(region["y"]),
            Inches(card_w), Inches(region["h"]),
        )
        card.fill.solid()
        card.fill.fore_color.rgb = _rgb(theme["surface"])
        card.line.color.rgb = _rgb(theme["border"])
        value_region = {"x": x + 0.18, "y": region["y"] + 0.28, "w": card_w - 0.36, "h": 0.95}
        _textbox(slide, value_region, str(metric.get("value") or ""),
                 pt=fonts.get("metric_value", 30), color=theme["text"],
                 font=theme["font_heading"], bold=True)
        label_region = {"x": x + 0.18, "y": region["y"] + 1.32, "w": card_w - 0.36, "h": 0.4}
        _textbox(slide, label_region, str(metric.get("label") or ""),
                 pt=fonts.get("metric_label", 12), color=theme["muted"], font=theme["font_body"])
        if metric.get("delta"):
            delta_region = {"x": x + 0.18, "y": region["y"] + 1.74, "w": card_w - 0.36, "h": 0.36}
            _textbox(slide, delta_region, str(metric["delta"]),
                     pt=fonts.get("metric_note", 11), color=theme["accent"], font=theme["font_body"])
        if metric.get("note"):
            note_region = {"x": x + 0.18, "y": region["y"] + 2.1, "w": card_w - 0.36, "h": 0.6}
            _textbox(slide, note_region, str(metric["note"]),
                     pt=fonts.get("metric_note", 11), color=theme["muted"], font=theme["font_body"])


def _draw_slide(slide: Any, model: dict[str, Any], theme: dict[str, Any],
                image_resolver: Any) -> None:
    regions = model["regions"]
    fonts = model["fonts"]
    align = model.get("align") or {}
    layout = model["layout"]

    if "eyebrow" in regions and model.get("eyebrow"):
        _textbox(slide, regions["eyebrow"], str(model["eyebrow"]),
                 pt=fonts.get("eyebrow", 12), color=theme["accent"],
                 font=theme["font_body"], bold=True)
    if "title" in regions and model.get("title"):
        _textbox(slide, regions["title"], str(model["title"]),
                 pt=fonts.get("title", 30), color=theme["text"],
                 font=theme["font_heading"], bold=True, align=align.get("title", "left"),
                 anchor="m" if layout in ("title", "closing") else "t")
    if "subtitle" in regions and model.get("subtitle"):
        _textbox(slide, regions["subtitle"], str(model["subtitle"]),
                 pt=fonts.get("subtitle", 18), color=theme["muted"],
                 font=theme["font_body"], align=align.get("subtitle", "left"))
    if "body" in regions:
        items = model.get("bullets") or ([model["body"]] if model.get("body") else [])
        _bullets(slide, regions["body"], items, pt=fonts.get("body", 18),
                 color=theme["text"], font=theme["font_body"])
    if "left" in regions:
        column = model.get("left") or {}
        _column_heading(slide, regions["left"], column.get("heading"), color=theme["text"],
                        font=theme["font_heading"], accent=theme["accent"])
        body_region = {**regions["left"], "y": regions["left"]["y"] + (0.38 if column.get("heading") else 0)}
        items = list(column.get("bullets") or [])
        if not items and model.get("body") and layout == "image_right":
            items = [str(model["body"])]
        _bullets(slide, body_region, items, pt=fonts.get("left", 18),
                 color=theme["text"], font=theme["font_body"])
    if "right" in regions:
        column = model.get("right") or {}
        _column_heading(slide, regions["right"], column.get("heading"), color=theme["text"],
                        font=theme["font_heading"], accent=theme["accent"])
        body_region = {**regions["right"], "y": regions["right"]["y"] + (0.38 if column.get("heading") else 0)}
        _bullets(slide, body_region, list(column.get("bullets") or []),
                 pt=fonts.get("right", 18), color=theme["text"], font=theme["font_body"])
    if "statement" in regions and (model.get("statement") or model.get("title")):
        text = model.get("statement") or model.get("title") or ""
        _textbox(slide, regions["statement"], str(text),
                 pt=fonts.get("statement", 28), color=theme["text"],
                 font=theme["font_heading"], bold=layout == "statement", align="left", anchor="m")
    if "attribution" in regions and model.get("attribution"):
        _textbox(slide, regions["attribution"], str(model["attribution"]),
                 pt=fonts.get("attribution", 14), color=theme["muted"],
                 font=theme["font_body"], align="right")
    if "metrics" in regions:
        _metric_cards(slide, regions["metrics"], model.get("metrics") or [], fonts, theme)
    if "table" in regions and model.get("table"):
        _table(slide, regions["table"], model["table"], pt=fonts.get("table", 12), theme=theme)
    if "chart" in regions and model.get("chart"):
        chart = model["chart"]
        if chart.get("available"):
            _chart(slide, regions["chart"], chart, theme)
        else:
            _placeholder(slide, regions["chart"], str(chart.get("unavailableReason") or "chart unavailable"), theme)
    if "image" in regions and model.get("image"):
        image = model["image"]
        data = None
        if image.get("available") and image.get("artifactId") and image_resolver is not None:
            try:
                data = image_resolver(str(image["artifactId"]))
            except Exception:  # noqa: BLE001
                data = None
        _image(slide, regions["image"], image, data, theme)
    if "source" in regions and model.get("source"):
        _textbox(slide, regions["source"], str(model["source"]),
                 pt=fonts.get("source", 10), color=theme["muted"], font=theme["font_body"])
    if "footer" in regions:
        footer = str((model.get("footer") if model.get("footer") is not None else theme.get("footer") or "") or "")
        if footer:
            _textbox(slide, regions["footer"], footer, pt=fonts.get("footer", 10),
                     color=theme["muted"], font=theme["font_body"])
        _textbox(slide, {**regions["footer"], "x": regions["footer"]["x"] + regions["footer"]["w"] - 0.6,
                         "w": 0.6},
                 str(model.get("page") or ""), pt=fonts.get("footer", 10),
                 color=theme["muted"], font=theme["font_body"], align="right")
    elif "source" in regions:
        _textbox(slide, {**regions["source"], "x": regions["source"]["x"] + regions["source"]["w"] - 0.6,
                         "w": 0.6},
                 str(model.get("page") or ""), pt=fonts.get("source", 10),
                 color=theme["muted"], font=theme["font_body"], align="right")
    if model.get("notes"):
        slide.notes_slide.notes_text_frame.text = str(model["notes"])


def export_pptx(
    deck: DeckSpec,
    model: dict[str, Any],
    *,
    image_resolver: Any = None,
) -> tuple[bytes, dict[str, Any]]:
    """Return `(pptx bytes, summary)`. Summary states overflow honestly."""
    Presentation = _require_library()
    from pptx.util import Inches  # noqa: PLC0415

    presentation = Presentation()
    size = model["size"]
    presentation.slide_width = Inches(size["widthIn"])
    presentation.slide_height = Inches(size["heightIn"])
    presentation.core_properties.title = deck.meta.title
    if deck.meta.author:
        presentation.core_properties.author = deck.meta.author
    if deck.meta.subtitle:
        presentation.core_properties.subject = deck.meta.subtitle

    layout = presentation.slide_layouts[6]  # blank: every shape is ours
    theme = dict(model["theme"])
    theme["footer"] = deck.meta.footer or ""

    for slide_model in model["slides"]:
        slide = presentation.slides.add_slide(layout)
        _background(slide, theme["background"])
        _draw_slide(slide, slide_model, theme, image_resolver)

    buffer = io.BytesIO()
    presentation.save(buffer)
    summary = {
        "slides": len(model["slides"]),
        "warnings": model.get("warnings") or [],
        "overflowSlides": model.get("overflowSlides") or [],
    }
    return buffer.getvalue(), summary


__all__ = ["export_pptx"]
