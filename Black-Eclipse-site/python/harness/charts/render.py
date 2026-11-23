"""The chart RENDERER — a validated spec becomes a PNG.

The renderer is deliberately dumb and total:

* it draws what the spec says and NOTHING else — it never invents a point, a
  mean, an axis or a scale;
* it never labels a chart as evidence. A figure is a *computed* picture; the
  label rides the metadata, not the pixels (ADR-H123);
* when matplotlib is not installed, it says so honestly
  (``CHART_RENDER_UNAVAILABLE``) instead of shipping an empty image.

Rendering happens in the harness process, not in the analysis sandbox: a chart
SPEC is *data* (validated by pydantic, free of code and paths), so it needs
no code isolation — and rendering it here means the figure, the artifact and
the run record all come from one place.
"""

from __future__ import annotations

import io
import math
import sys
import warnings
from typing import Any, Optional

from .spec import Candle, ChartPoint, ChartSeries, ChartSpec
from .theme import ChartTheme, get_theme

#: The refusal codes the renderer reports. Stable and testable.
CHART_RENDER_UNAVAILABLE = "CHART_RENDER_UNAVAILABLE"
CHART_RENDER_FAILED = "CHART_RENDER_FAILED"
CHART_SPEC_INVALID = "CHART_SPEC_INVALID"


class ChartRenderError(RuntimeError):
    """A rendering refusal. ``code`` is stable and testable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _pyplot() -> Any:
    """Import matplotlib lazily and honestly.

    A missing matplotlib is a missing PICTURE, not a missing chart: the
    numbers that came with the spec are still true. So the failure is named,
    not swallowed.
    """
    try:
        import matplotlib  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ChartRenderError(
            CHART_RENDER_UNAVAILABLE,
            "matplotlib is not installed, so the chart cannot be drawn: "
            f"{exc} (interpreter: {sys.executable})",
        ) from exc
    try:
        matplotlib.use("Agg", force=False)
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ChartRenderError(
            CHART_RENDER_UNAVAILABLE,
            f"matplotlib could not start: {exc} (interpreter: {sys.executable})",
        ) from exc
    return plt


def _numpy() -> Any:
    try:
        import numpy as np  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ChartRenderError(
            CHART_RENDER_UNAVAILABLE,
            f"numpy could not start: {exc} (interpreter: {sys.executable})",
        ) from exc
    return np


def _clean(values: list[Optional[float]]) -> list[float]:
    """Drop the gaps (a `None` is a missing reading, never a zero)."""
    return [float(v) for v in values if v is not None]


def _axis_categories(spec: ChartSpec, length: int) -> Any:
    """The x positions: the categories when they fit, else the index."""
    if spec.categories and len(spec.categories) >= length:
        return list(range(length)), list(spec.categories[:length])
    return list(range(length)), [str(i) for i in range(length)]


#: The drawing resolution. The figure is drawn in FEWER INCHES at this DPI, so
#: the text and the strokes are a fixed fraction of the PICTURE - a chart shown
#: at 860 px has legible labels, whether it was drawn at 720 px or 1400 px.
DRAW_DPI = 150


def _format_value(value: float, spec: ChartSpec, value_format: Optional[str] = None) -> str:
    """Write an axis value the way the chart says it should be read.

    The engine draws numbers; a chart whose values are percents or dollars
    says so in the spec, so the reader is never left guessing what `0.25`
    means.
    """
    fmt = value_format or spec.value_format
    if fmt == "percent":
        scaled = value * 100 if abs(value) <= 1 else value
        return f"{scaled:.0f}%"
    if fmt == "currency":
        return f"{spec.currency_symbol}{value:,.0f}"
    if fmt == "compact":
        absolute = abs(value)
        if absolute >= 1e9:
            return f"{value / 1e9:.1f}B"
        if absolute >= 1e6:
            return f"{value / 1e6:.1f}M"
        if absolute >= 1e3:
            return f"{value / 1e3:.1f}K"
        return f"{value:,.0f}"
    if fmt == "r":
        return f"{value:.1f}R"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _apply_value_format(ax: Any, spec: ChartSpec) -> None:
    """Write the value axis in the chart's own units."""
    if not spec.value_format:
        return
    # Matplotlib calls a tick formatter with (value, position) — the formatter
    # must accept both, or every chart with a value format fails to draw.
    def formatter(value: float, _position: Any = None) -> str:
        return _format_value(float(value), spec)

    try:
        ax.yaxis.set_major_formatter(formatter)
    except Exception:  # noqa: BLE001 - a chart without a value axis is fine
        pass


def _draw_volume(ax: Any, spec: ChartSpec, theme: ChartTheme) -> None:
    """The volume pane under a price chart."""
    values = [float(v) if v is not None else float("nan") for v in spec.volume_data]
    colors = list(spec.volume_colors)
    bar_colors = [
        c if c and isinstance(c, str) and c.startswith("#") else theme.series_color(1)
        for c in (colors + [None] * len(values))[: len(values)]
    ]
    ax.bar(range(len(values)), values, color=bar_colors, width=0.7, alpha=0.65)
    ax.set_ylabel("Volume", fontsize=8, color=theme.muted)


def _draw_highlights(ax: Any, spec: ChartSpec, theme: ChartTheme) -> None:
    """Shade the spans of the category axis the caller marked (a recession)."""
    for span in spec.highlights:
        start = max(0, int(span.start))
        end = max(start, int(span.end))
        try:
            ax.axvspan(
                start - 0.5, end + 0.5, color=span.color or "#ff3e3e",
                alpha=0.12, linewidth=0,
            )
        except Exception:  # noqa: BLE001 - the chart is the important part
            continue


def _apply_log_scale(ax: Any, spec: ChartSpec) -> None:
    """A log value axis, when the data says so (a macro series that spans decades)."""
    if not spec.log_scale:
        return
    try:
        ax.set_yscale("log")
    except Exception:  # noqa: BLE001 - a linear axis is a fine fallback
        pass


def _draw_reference_lines(ax: Any, spec: ChartSpec, theme: ChartTheme) -> None:
    """Draw the named horizontal lines (an entry, a stop, a target)."""
    for line in spec.reference_lines:
        color = line.color or theme.muted
        ax.axhline(
            line.value, color=color, linewidth=1.0,
            linestyle="--" if line.dashed else "-", alpha=0.85,
        )
        try:
            ax.annotate(
                line.label,
                xy=(1.0, line.value),
                xycoords=("axes fraction", "data"),
                xytext=(-4, 3),
                textcoords="offset points",
                ha="right",
                va="bottom",
                fontsize=8,
                color=color,
            )
        except Exception:  # noqa: BLE001 - the line is the important part
            pass


def _set_category_labels(ax: Any, positions: list[int], labels: list[str]) -> None:
    """Write the category labels, THINNING them when the axis is crowded.

    Ninety daily dates printed at once are unreadable (and they overlap into a
    smear). The engine keeps every Nth label, so the axis stays legible at any
    density - the DATA is never thinned, only the labels. Long labels (dates)
    are turned, because a row of `2026-07-01` strings cannot share a line.
    """
    count = len(positions)
    if count == 0:
        return
    longest = max((len(label) for label in labels), default=0)
    rotated = longest > 6 or count > 8
    if count <= 14:
        ax.set_xticks(positions)
        ax.set_xticklabels(labels, rotation=45 if rotated else 0, ha="right" if rotated else "center")
        return
    step = max(1, count // 12)
    shown = positions[::step]
    ax.set_xticks(shown)
    ax.set_xticklabels([labels[i] for i in range(0, count, step)], rotation=45, ha="right")


def _apply_titles(ax: Any, spec: ChartSpec) -> None:
    if spec.title:
        ax.set_title(spec.title, loc="left", pad=26 if spec.subtitle else 10)
    if spec.subtitle:
        # The subtitle sits UNDER the title, never on it.
        ax.annotate(
            spec.subtitle,
            xy=(0, 1.0),
            xycoords="axes fraction",
            xytext=(0, 6),
            textcoords="offset points",
            ha="left",
            va="bottom",
            color=_theme_of(ax).muted,
            fontsize=9.5,
        )
    if spec.x_label:
        ax.set_xlabel(spec.x_label)
    if spec.y_label:
        ax.set_ylabel(spec.y_label)


def _theme_of(ax: Any) -> ChartTheme:
    return getattr(ax, "_eclipse_theme", None) or get_theme(None)


def _draw_line_family(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    np = _numpy()
    series = spec.series
    longest = max((len(s.data) for s in series), default=0)
    positions, labels = _axis_categories(spec, longest)
    plotted = False
    for index, entry in enumerate(series):
        values = entry.data
        if not values:
            continue
        color = entry.color or theme.series_color(index)
        xs = list(range(len(values)))
        ys = [float(v) if v is not None else float("nan") for v in values]
        style = entry.style or ("area" if spec.kind == "area" else "line")
        if style == "bar":
            ax.bar(xs, ys, color=color, width=0.7, label=entry.name, alpha=0.9)
        elif style == "scatter":
            ax.scatter(xs, ys, color=color, s=18, label=entry.name)
        else:
            if spec.smooth and len(ys) > 2:
                finite = [(i, v) for i, v in enumerate(ys) if not math.isnan(v)]
                if len(finite) > 2:
                    fx = [p[0] for p in finite]
                    fy = [p[1] for p in finite]
                    dense = np.linspace(fx[0], fx[-1], min(len(fx) * 3, 2_000))
                    ys = list(np.interp(dense, fx, fy))
                    xs = list(dense)
            if style == "area":
                ax.fill_between(xs, ys, color=color, alpha=0.22, label=entry.name)
                ax.plot(xs, ys, color=color, linewidth=1.6, label=entry.name)
            elif style == "step":
                ax.step(xs, ys, where="post", color=color, linewidth=1.6, label=entry.name)
            else:
                ax.plot(
                    xs, ys, color=color, linewidth=1.6, label=entry.name,
                    linestyle="--" if entry.dashed else "-",
                )
            if spec.show_points:
                ax.scatter(xs, ys, color=color, s=14)
        band = entry.band
        if band and band.lower and band.upper:
            # A forecast band: the range the series lives in, drawn as a
            # translucent area. It is presentation, never a probability.
            lower = [float(v) if v is not None else float("nan") for v in band.lower]
            upper = [float(v) if v is not None else float("nan") for v in band.upper]
            count = min(len(lower), len(upper))
            if count > 1:
                ax.fill_between(
                    list(range(count)), lower[:count], upper[:count],
                    color=color, alpha=0.16, linewidth=0,
                )
        plotted = True
    if spec.stacked and len(series) > 1:
        # A stacked family is drawn as cumulative areas so the stack is real.
        ax.clear()
        bottom = [0.0] * longest
        for index, entry in enumerate(series):
            values = list(entry.data) + [None] * (longest - len(entry.data))
            ys = [float(v) if v is not None else 0.0 for v in values]
            color = entry.color or theme.series_color(index)
            ax.fill_between(
                positions, bottom, [b + y for b, y in zip(bottom, ys)],
                color=color, alpha=0.55, label=entry.name,
            )
            bottom = [b + y for b, y in zip(bottom, ys)]
    if not plotted:
        raise ChartRenderError(CHART_SPEC_INVALID, "the chart has no values to draw")
    if len(labels) <= 24:
        _set_category_labels(ax, positions, labels)
    if len(series) > 1 or spec.kind == "area":
        ax.legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=min(4, len(series)))


def _draw_bar_family(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    series = spec.series
    longest = max((len(s.data) for s in series), default=0)
    positions, labels = _axis_categories(spec, longest)
    if spec.stacked:
        bottom = [0.0] * longest
        for index, entry in enumerate(series):
            values = list(entry.data) + [None] * (longest - len(entry.data))
            ys = [float(v) if v is not None else 0.0 for v in values]
            color = entry.color or theme.series_color(index)
            point_colors = [
                c if c and isinstance(c, str) and c.startswith("#") else color
                for c in (list(entry.colors) + [None] * longest)[:longest]
            ]
            ax.bar(
                positions, ys, bottom=bottom, color=point_colors, width=0.68,
                label=entry.name, alpha=0.92,
            )
            bottom = [b + y for b, y in zip(bottom, ys)]
    else:
        width = 0.8 / max(1, len(series))
        for index, entry in enumerate(series):
            values = list(entry.data) + [None] * (longest - len(entry.data))
            ys = [float(v) if v is not None else float("nan") for v in values]
            color = entry.color or theme.series_color(index)
            # Per-point colours let one series show a sign (green up, red
            # down) without splitting it into two series.
            point_colors = [
                c if c and isinstance(c, str) and c.startswith("#") else color
                for c in (list(entry.colors) + [None] * longest)[:longest]
            ]
            offset = (index - (len(series) - 1) / 2) * width
            ax.bar(
                [p + offset for p in positions], ys, width=width, color=point_colors,
                label=entry.name, alpha=0.92,
            )
    if len(labels) <= 24:
        _set_category_labels(ax, positions, labels)
    else:
        ax.set_xticks(positions[:: max(1, len(positions) // 12)])
    if len(series) > 1:
        ax.legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=min(4, len(series)))


def _draw_pie(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    values = [float(v) for v in spec.values]
    labels = list(spec.labels)
    total = sum(abs(v) for v in values)
    if total <= 0:
        raise ChartRenderError(CHART_SPEC_INVALID, "a pie chart needs at least one non-zero value")
    colors = [
        c if c and isinstance(c, str) and c.startswith("#") else theme.series_color(i)
        for i, c in enumerate(list(spec.colors) + [None] * len(values))
    ][: len(values)]
    wedges, _texts, autotexts = ax.pie(
        values,
        labels=labels if len(labels) <= 12 else None,
        colors=colors,
        autopct="%1.0f%%" if len(values) <= 12 else None,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "none"} if spec.kind == "donut" else {"edgecolor": "none"},
    )
    for text in autotexts:
        text.set_color("#0b0b0d")
        text.set_fontsize(9)
    if not labels and len(values) <= 12:
        legend_labels = [f"{i + 1}" for i in range(len(values))]
        ax.legend(wedges, legend_labels, loc="center left", bbox_to_anchor=(0.98, 0.5))
    if spec.kind == "donut":
        ax.text(0, 0, f"{total:,.0f}", ha="center", va="center",
                color=theme.ink, fontsize=15, fontweight="semibold")
        ax.text(0, -0.12, "total", ha="center", va="center", color=theme.muted, fontsize=9)


def _draw_scatter(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    points = spec.points
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    sizes = [p.size for p in points]
    if any(s is not None for s in sizes):
        biggest = max((s for s in sizes if s is not None), default=1.0) or 1.0
        scaled = [24 + 220 * ((s or biggest) / biggest) for s in sizes]
    else:
        scaled = [40] * len(points)
    for point, size in zip(points, scaled):
        color = point.color or theme.direction_color(point.y)
        ax.scatter([point.x], [point.y], s=size, color=color, alpha=0.85,
                   edgecolors="none")
    if len(points) <= 14:
        for point in points:
            if point.label:
                ax.annotate(
                    point.label, (point.x, point.y),
                    textcoords="offset points", xytext=(6, 6), fontsize=8.5,
                    color=theme.muted,
                )
    ax.axhline(0, color=theme.axis, linewidth=0.8)


def _draw_histogram(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    values = _clean(spec.series[0].data)
    if not values:
        raise ChartRenderError(CHART_SPEC_INVALID, "the histogram has no values to draw")
    count = len(values)
    bins = min(40, max(5, count // 8 if count else 5))
    ax.hist(values, bins=bins, color=spec.series[0].color or theme.series_color(0),
            edgecolor="none", alpha=0.9)
    if count >= 2:
        mean = sum(values) / count
        ax.axvline(mean, color=theme.ink, linewidth=1.0, linestyle="--", alpha=0.8)
        ax.annotate(
            f"mean {mean:,.4g}", xy=(mean, ax.get_ylim()[1]),
            xytext=(4, -12), textcoords="offset points", fontsize=8.5,
            color=theme.ink,
        )


def _draw_heatmap(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    np = _numpy()
    matrix = np.array(spec.matrix, dtype=float)
    image = ax.imshow(matrix, cmap="coolwarm", aspect="auto")
    ax.figure.colorbar(image, ax=ax, fraction=0.046, pad=0.03)
    if spec.row_labels:
        ax.set_yticks(range(len(spec.row_labels)))
        ax.set_yticklabels(spec.row_labels)
    if spec.col_labels:
        ax.set_xticks(range(len(spec.col_labels)))
        ax.set_xticklabels(spec.col_labels, rotation=45, ha="right")
    ax.grid(False)


def _draw_candlestick(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    from matplotlib.patches import Rectangle  # noqa: PLC0415

    candles = spec.candles
    colors = list(spec.colors)
    for index, candle in enumerate(candles):
        rising = candle.close >= candle.open
        color = theme.up if rising else theme.down
        if index < len(colors) and colors[index]:
            color = colors[index]
        ax.plot([index, index], [candle.low, candle.high], color=color, linewidth=0.9)
        body_low = min(candle.open, candle.close)
        body_high = max(candle.open, candle.close)
        height = max(body_high - body_low, 1e-9)
        ax.add_patch(Rectangle(
            (index - 0.32, body_low), 0.64, height,
            facecolor=color, edgecolor="none", alpha=0.9,
        ))
    if spec.categories and len(spec.categories) >= len(candles):
        # The shared thinning: ninety daily dates printed at once overlap into a
        # smear, so every Nth label is kept (the DATA is never thinned).
        shown = list(range(0, len(candles), max(1, len(candles) // 12)))
        _set_category_labels(
            ax, shown, [str(spec.categories[i]) for i in shown],
        )


def _draw_waterfall(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    values = [float(v) for v in spec.series[0].data if v is not None]
    if not values:
        raise ChartRenderError(CHART_SPEC_INVALID, "the waterfall has no values to draw")
    running = 0.0
    bottoms: list[float] = []
    heights: list[float] = []
    colors: list[str] = []
    for value in values:
        bottoms.append(running if value >= 0 else running + value)
        heights.append(abs(value))
        colors.append(theme.up if value >= 0 else theme.down)
        running += value
    ax.bar(range(len(values)), heights, bottom=bottoms, color=colors, width=0.66)
    ax.axhline(0, color=theme.axis, linewidth=0.8)
    if spec.categories and len(spec.categories) >= len(values):
        ax.set_xticks(range(len(values)))
        ax.set_xticklabels([str(c) for c in spec.categories[: len(values)]], rotation=30, ha="right")


def _draw_radar(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    values = [float(v) for v in spec.values]
    labels = list(spec.labels)
    count = len(values)
    angles = [n / count * 2 * math.pi for n in range(count)]
    angles.append(angles[0])
    closed = values + [values[0]]
    ax.plot(angles, closed, color=theme.series_color(0), linewidth=1.6)
    ax.fill(angles, closed, color=theme.series_color(0), alpha=0.22)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels if labels else [str(i) for i in range(count)])
    ax.set_yticklabels([])
    ax.grid(True, alpha=0.25)


def _draw_gauge(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    """A half-circle gauge: the reading as an arc, the number in the middle."""
    from matplotlib.patches import Wedge  # noqa: PLC0415

    value = float(spec.value or 0.0)
    maximum = float(spec.gauge_max or 100.0)
    fraction = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    radius = 1.0
    # The track, then the reading.
    ax.add_patch(Wedge((0, 0), radius, 0, 180, width=0.16, facecolor="#ffffff14", edgecolor="none"))
    ax.add_patch(Wedge(
        (0, 0), radius, 0, 180 * fraction, width=0.16,
        facecolor=theme.series_color(0), edgecolor="none",
    ))
    ax.text(0, -0.12, f"{value:,.0f}", ha="center", va="center", color=theme.ink, fontsize=22, fontweight="bold")
    ax.set_xlim(-1.15, 1.15)
    ax.set_ylim(-0.55, 1.15)
    ax.set_aspect("equal")
    ax.axis("off")


def _squarify(values: list[float], x: float, y: float, width: float, height: float) -> list[tuple[float, float, float, float]]:
    """A simple squarified treemap layout.

    Each tile is a rectangle whose AREA is proportional to its value — the
    whole point of a treemap. The layout is deterministic: the same tiles in
    the same order always draw the same picture.
    """
    total = sum(values)
    if total <= 0 or width <= 0 or height <= 0:
        return []
    scale = (width * height) / total
    rects: list[tuple[float, float, float, float]] = []
    remaining = list(values)
    cx, cy, cw, ch = x, y, width, height
    while remaining:
        value = remaining.pop(0)
        area = value * scale
        if cw >= ch:
            tile_w = area / ch if ch > 0 else 0.0
            rects.append((cx, cy, tile_w, ch))
            cx += tile_w
            cw -= tile_w
        else:
            tile_h = area / cw if cw > 0 else 0.0
            rects.append((cx, cy, cw, tile_h))
            cy += tile_h
            ch -= tile_h
    return rects


def _treemap_values(tiles: list[Any]) -> list[float]:
    """A group's area is the sum of its children's."""
    out: list[float] = []
    for tile in tiles:
        if tile.children:
            out.append(sum(_treemap_values(tile.children)))
        else:
            out.append(tile.value)
    return out


def _draw_treemap_tiles(
    ax: Any, tiles: list[Any], x: float, y: float, width: float, height: float,
    theme: ChartTheme, depth: int = 0,
) -> None:
    """Lay out a treemap's tiles (and their groups) inside a rectangle."""
    from matplotlib.patches import Rectangle  # noqa: PLC0415

    values = _treemap_values(tiles)
    rects = _squarify(values, x, y, width, height)
    band = 0.035 if depth == 0 else 0.0
    for tile, (tx, ty, tw, th) in zip(tiles, rects):
        if tw <= 0 or th <= 0:
            continue
        if tile.children:
            # A group: a thin band across the top, then its children inside.
            label_h = min(th * 0.18, max(th * 0.05, 0.012))
            if label_h > 0:
                ax.add_patch(Rectangle(
                    (tx, ty + th - label_h), tw, label_h,
                    facecolor="#ffffff10", edgecolor="none",
                ))
                ax.text(
                    tx + 0.006, ty + th - label_h / 2, tile.label or tile.name,
                    ha="left", va="center", color=theme.ink, fontsize=8.5,
                    fontweight="bold", clip_on=True,
                )
            _draw_treemap_tiles(
                ax, tile.children, tx, ty, tw, max(th - label_h, 0.0), theme, depth + 1,
            )
            continue
        color = tile.color or theme.series_color(0)
        ax.add_patch(Rectangle(
            (tx + 0.003, ty + 0.003), max(tw - 0.006, 0.0), max(th - 0.006, 0.0),
            facecolor=color, edgecolor="none",
        ))
        if tw > 0.1 and th > 0.06:
            ax.text(
                tx + 0.008, ty + th - 0.02, tile.label or tile.name,
                ha="left", va="top", color="#0b0b0d", fontsize=8, fontweight="bold",
                clip_on=True,
            )


def _draw_treemap(ax: Any, spec: ChartSpec, theme: ChartTheme, plt: Any) -> None:
    """A treemap: each tile's AREA is its value, its colour its state."""
    tiles = [t for t in spec.tiles if t.value > 0 or t.children]
    if not tiles:
        raise ChartRenderError(CHART_SPEC_INVALID, "the treemap has no tiles with a size")
    _draw_treemap_tiles(ax, tiles, 0.0, 0.0, 1.0, 1.0, theme)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")


def _render_scene(spec: ChartSpec, theme: ChartTheme, plt: Any) -> bytes:
    """Render a SCENE: a stack of panels, each a stack of marks.

    This is how "any chart" is drawn: a violin, a Gantt, a funnel, a ridgeline
    or a dual-axis chart is not a new kind - it is a composition of the
    primitives.
    """
    from .scene_render import draw_mark, panel_categories  # noqa: PLC0415

    panels = spec.panels
    count = len(panels)
    heights = [float(p.height or 1.0) for p in panels]
    total = sum(heights) or 1.0
    ratios = [h / total for h in heights]

    figure = plt.figure(figsize=(spec.width / DRAW_DPI, spec.height / DRAW_DPI), dpi=DRAW_DPI)
    grid = figure.add_gridspec(count, 1, height_ratios=ratios, hspace=0.34)
    # A panel that carries a 3D mark gets a real 3D axes, so the PNG copy is
    # not a blank pair of flat axes under the live Plotly scene.
    axes = []
    for index, panel in enumerate(panels):
        is_3d = any(mark.type in ("scatter3d", "surface") for mark in panel.marks)
        axes.append(
            figure.add_subplot(grid[index], projection="3d") if is_3d
            else figure.add_subplot(grid[index])
        )

    for panel, ax in zip(panels, axes):
        setattr(ax, "_eclipse_theme", theme)
        for index, mark in enumerate(panel.marks):
            draw_mark(ax, mark, theme, index, panel)
        # A label axis is drawn as ticks on the shared index, so "Q1/Q2/Q3"
        # reads as labels instead of bare 0/1/2. Charts that own their axis
        # (a heatmap's grid, a pie) set their own ticks and are left alone.
        categories = panel_categories(panel)
        owns_axis = any(
            mark.type in ("heatmap", "pie", "donut", "candlestick", "waterfall")
            for mark in panel.marks
        )
        if categories and not owns_axis and getattr(ax, "name", "") != "3d":
            try:
                ax.set_xticks(range(len(categories)))
                crowded = len(categories) > 6
                ax.set_xticklabels(
                    categories,
                    rotation=45 if crowded else 0,
                    ha="right" if crowded else "center",
                )
            except Exception:  # noqa: BLE001 - ticks are cosmetic, never fatal
                pass
        if panel.title:
            ax.set_title(panel.title, loc="left", fontsize=11, fontweight="bold", color=theme.ink, pad=8)
        if panel.x_label:
            ax.set_xlabel(panel.x_label, color=theme.muted, fontsize=9)
        if panel.y_label:
            ax.set_ylabel(panel.y_label, color=theme.muted, fontsize=9)
        if panel.value_format:
            def formatter(value: float, _position: Any = None, _fmt: str = panel.value_format) -> str:
                return _format_value(float(value), spec, _fmt)

            try:
                ax.yaxis.set_major_formatter(formatter)
            except Exception:  # noqa: BLE001
                pass
        if panel.log_scale:
            try:
                ax.set_yscale("log")
            except Exception:  # noqa: BLE001
                pass
        for line in panel.reference_lines:
            ax.axhline(
                line.value, color=line.color or theme.muted, linewidth=1.0,
                linestyle="--" if line.dashed else "-", alpha=0.85,
            )
        for span in panel.highlights:
            try:
                ax.axvspan(span.start - 0.5, span.end + 0.5, color=span.color or "#ff3e3e", alpha=0.12, linewidth=0)
            except Exception:  # noqa: BLE001
                continue
        labelled = [m for m in panel.marks if m.name]
        if len(labelled) > 1:
            ax.legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=min(4, len(labelled)), fontsize=8)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        figure.tight_layout(rect=(0, 0.05 if (spec.footnote or spec.source) else 0, 1, 1))
    if spec.footnote:
        figure.text(0.01, 0.015, spec.footnote, ha="left", va="bottom", color=theme.muted, fontsize=8.5)
    if spec.source:
        figure.text(0.99, 0.015, spec.source, ha="right", va="bottom", color=theme.muted, fontsize=8.5)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", transparent=True)
    plt.close(figure)
    return buffer.getvalue()


_DISPATCH = {
    "line": _draw_line_family,
    "area": _draw_line_family,
    "bar": _draw_bar_family,
    "stackedBar": _draw_bar_family,
    "pie": _draw_pie,
    "donut": _draw_pie,
    "scatter": _draw_scatter,
    "histogram": _draw_histogram,
    "heatmap": _draw_heatmap,
    "candlestick": _draw_candlestick,
    "waterfall": _draw_waterfall,
    "radar": _draw_radar,
    "gauge": _draw_gauge,
    "treemap": _draw_treemap,
}


def _render_figure(spec: ChartSpec, theme: ChartTheme, plt: Any) -> tuple[bytes, Any]:
    """Draw the figure and return the PNG bytes and the axes that were drawn."""
    kind = spec.kind
    with plt.rc_context(theme.rc):
        if spec.panels:
            try:
                return _render_scene(spec, theme, plt), None
            except ChartRenderError:
                raise
            except Exception as exc:  # noqa: BLE001 - a draw failure is named
                raise ChartRenderError(
                    CHART_RENDER_FAILED, f"the chart could not be drawn: {exc}",
                ) from exc
        drawer = _DISPATCH.get(kind)
        if drawer is None:  # pragma: no cover - the spec's Literal already refuses it
            raise ChartRenderError(CHART_SPEC_INVALID, f"unknown chart kind {kind!r}")
        has_volume = any(v is not None for v in spec.volume_data)
        if has_volume:
            # A price chart's volume pane: the chart on top, the volume below.
            figure = plt.figure(figsize=(spec.width / DRAW_DPI, spec.height / DRAW_DPI), dpi=DRAW_DPI)
            grid = figure.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
            ax = figure.add_subplot(grid[0])
            volume_ax = figure.add_subplot(grid[1], sharex=ax)
        else:
            figure = plt.figure(figsize=(spec.width / DRAW_DPI, spec.height / DRAW_DPI), dpi=DRAW_DPI)
            ax = figure.add_subplot(111)
            volume_ax = None
        try:
            if kind == "radar":
                ax = figure.add_subplot(111, polar=True)
            setattr(ax, "_eclipse_theme", theme)
            drawer(ax, spec, theme, plt)
            _apply_titles(ax, spec)
            _apply_value_format(ax, spec)
            _apply_log_scale(ax, spec)
            _draw_highlights(ax, spec, theme)
            _draw_reference_lines(ax, spec, theme)
            if volume_ax is not None:
                _draw_volume(volume_ax, spec, theme)
                volume_ax.tick_params(labelleft=False)
                volume_ax.grid(True, alpha=0.12)
            if spec.footnote:
                figure.text(
                    0.01, 0.015, spec.footnote, ha="left", va="bottom",
                    color=theme.muted, fontsize=8.5,
                )
            if spec.source:
                figure.text(
                    0.99, 0.015, spec.source, ha="right", va="bottom",
                    color=theme.muted, fontsize=8.5,
                )
            # A gauge/radar/treemap has axes that `tight_layout` cannot fit; the
            # warning is expected there and the layout is already exact.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                figure.tight_layout(rect=(0, 0.05 if (spec.footnote or spec.source) else 0, 1, 1))
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", transparent=True)
            return buffer.getvalue(), ax
        except ChartRenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChartRenderError(
                CHART_RENDER_FAILED, f"the chart could not be drawn: {exc}",
            ) from exc
        finally:
            plt.close(figure)


def render_chart_with_interaction(spec: ChartSpec) -> tuple[bytes, Optional[dict[str, Any]]]:
    """Render the spec into PNG bytes, plus the map that makes it hoverable.

    The bytes are a transparent-background PNG, ready to sit on a frosted
    glass card without hiding the frost. The map describes where each category
    sits on the picture and what each series' value is there, so the browser can
    draw a crosshair and a tooltip over the picture (the picture itself cannot
    be hovered).
    """
    plt = _pyplot()
    theme = get_theme(spec.theme)
    kind = spec.kind
    png, axes = _render_figure(spec, theme, plt)
    interaction = None
    if axes is not None:
        from .interaction import build_interaction  # noqa: PLC0415

        interaction = build_interaction(spec, theme, axes)
    return png, interaction


def render_chart(spec: ChartSpec) -> bytes:
    """Render the spec into PNG bytes.

    The bytes are a transparent-background PNG, ready to sit on a frosted
    glass card without hiding the frost.
    """
    return render_chart_with_interaction(spec)[0]
    plt = _pyplot()
    theme = get_theme(spec.theme)
    kind = spec.kind
    axes_used: Any = None

    with plt.rc_context(theme.rc):
        if spec.panels:
            # A scene owns its figure: it has one panel per row.
            return _render_scene(spec, theme, plt), None
        drawer = _DISPATCH.get(kind)
        if drawer is None:  # pragma: no cover - the spec's Literal already refuses it
            raise ChartRenderError(CHART_SPEC_INVALID, f"unknown chart kind {kind!r}")
        has_volume = any(v is not None for v in spec.volume_data)
        if has_volume:
            # A price chart's volume pane: the chart on top, the volume below.
            figure = plt.figure(figsize=(spec.width / DRAW_DPI, spec.height / DRAW_DPI), dpi=DRAW_DPI)
            grid = figure.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
            ax = figure.add_subplot(grid[0])
            volume_ax = figure.add_subplot(grid[1], sharex=ax)
        else:
            figure = plt.figure(figsize=(spec.width / DRAW_DPI, spec.height / DRAW_DPI), dpi=DRAW_DPI)
            ax = figure.add_subplot(111)
            volume_ax = None
        try:
            if kind == "radar":
                ax = figure.add_subplot(111, polar=True)
            setattr(ax, "_eclipse_theme", theme)
            drawer(ax, spec, theme, plt)
            _apply_titles(ax, spec)
            _apply_value_format(ax, spec)
            _apply_log_scale(ax, spec)
            _draw_highlights(ax, spec, theme)
            _draw_reference_lines(ax, spec, theme)
            if volume_ax is not None:
                _draw_volume(volume_ax, spec, theme)
                volume_ax.tick_params(labelleft=False)
                volume_ax.grid(True, alpha=0.12)
            if spec.footnote:
                figure.text(
                    0.01, 0.015, spec.footnote, ha="left", va="bottom",
                    color=theme.muted, fontsize=8.5,
                )
            if spec.source:
                figure.text(
                    0.99, 0.015, spec.source, ha="right", va="bottom",
                    color=theme.muted, fontsize=8.5,
                )
            # A gauge/radar/treemap has axes that `tight_layout` cannot fit; the
            # warning is expected there and the layout is already exact.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                figure.tight_layout(rect=(0, 0.05 if (spec.footnote or spec.source) else 0, 1, 1))
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", transparent=True)
            return buffer.getvalue(), ax
        except ChartRenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChartRenderError(
                CHART_RENDER_FAILED, f"the chart could not be drawn: {exc}",
            ) from exc
        finally:
            plt.close(figure)


def render_chart_to_png(spec: ChartSpec, path: str) -> int:
    """Render the spec and write the PNG to ``path``. Returns the byte count."""
    data = render_chart(spec)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)


__all__ = [
    "CHART_RENDER_FAILED",
    "CHART_RENDER_UNAVAILABLE",
    "CHART_SPEC_INVALID",
    "ChartRenderError",
    "render_chart",
    "render_chart_to_png",
]
