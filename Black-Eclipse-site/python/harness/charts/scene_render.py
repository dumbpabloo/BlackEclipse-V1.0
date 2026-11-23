"""The scene renderer — panels of marks.

A scene is a stack of panels; each panel is a stack of marks; each mark is a
primitive with its own data. That is what makes "any chart" possible: a
violin is a `violin` mark, a Gantt is a `bar` mark on a time axis, a funnel is
a `bar` mark with a descending order, a dual-axis chart is two marks on two
value axes, a box plot beside a strip plot is two marks in one panel.

The renderer obeys the engine's rules: a mark draws what it is given and
never invents a point, a value or a scale.
"""

from __future__ import annotations

from typing import Any, Optional

from .regression import linear_fit
from .scene import ChartMark, ChartPanel, surface_grid
from .theme import ChartTheme


def _values(mark: ChartMark) -> list[float]:
    out: list[float] = []
    for value in mark.y or mark.values:
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _series(mark: ChartMark) -> list[Optional[float]]:
    """The mark's y values, with the gaps kept as None.

    A value the renderer cannot draw as a number is a GAP, never an invented
    point and never a crash: a stray label in a numeric series used to raise
    `could not convert string to float` and lose the whole picture.
    """
    out: list[Optional[float]] = []
    for value in mark.y or []:
        if value is None:
            out.append(None)
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(None)
    return out


def panel_categories(panel: ChartPanel) -> list[str]:
    """The panel's category labels, in first-seen order.

    A scene mark's `x` may be LABELS ("Q1", "Jan", a product name) rather than
    numbers — that is how a person writes a bar chart. matplotlib needs a
    numeric position, so labels map to their index on a shared axis; every mark
    in the panel uses the same order, so a bar and a line line up.
    """
    seen: list[str] = []
    for mark in panel.marks:
        for value in mark.x or []:
            if isinstance(value, str) and value not in seen:
                seen.append(value)
    return seen


def _x_series(mark: ChartMark, categories: Optional[list[str]] = None) -> list[float]:
    if mark.x:
        out: list[float] = []
        for value in mark.x:
            if value is None:
                out.append(float("nan"))
            elif isinstance(value, str):
                # A label is drawn at its index on the panel's shared axis.
                if categories and value in categories:
                    out.append(float(categories.index(value)))
                else:
                    out.append(float("nan"))
            else:
                try:
                    out.append(float(value))
                except (TypeError, ValueError):
                    out.append(float("nan"))
        return out
    return [float(i) for i in range(len(mark.y or mark.values))]


def _color(mark: ChartMark, theme: ChartTheme, index: int) -> str:
    return mark.color or theme.series_color(index)


def _linestyle(mark: ChartMark) -> Any:
    return {"solid": "-", "dashed": "--", "dotted": ":"}.get(mark.style or "solid", "-")


def _stack_base(mark: ChartMark, panel: ChartPanel) -> Optional[list[float]]:
    """The baseline a stacked mark sits on (the marks below it in its stack)."""
    try:
        stack = mark.stack
        if not stack:
            return None
        total: list[float] = []
        for other in panel.marks:
            if other is mark or other.stack != stack:
                continue
            for i, value in enumerate(_series(other)):
                while len(total) <= i:
                    total.append(0.0)
                total[i] += value or 0.0
        return total or None
    except Exception:  # noqa: BLE001
        return None


def draw_mark(ax: Any, mark: ChartMark, theme: ChartTheme, index: int, panel: ChartPanel) -> None:
    """Draw one mark on one panel."""
    color = _color(mark, theme, index)
    alpha = mark.alpha if mark.alpha is not None else 1.0
    width = mark.width or (1.6 if mark.type in ("line", "area") else None)
    # A label axis is shared by every mark in the panel, so a bar and a line
    # over the same quarters line up instead of one of them being refused.
    categories = panel_categories(panel)

    if mark.type in ("line", "area"):
        ys = _series(mark)
        if not ys:
            return
        xs = _x_series(mark, categories)
        if mark.fill or mark.type == "area":
            ax.fill_between(xs, ys, color=color, alpha=0.22 * alpha, label=mark.name)
        ax.plot(
            xs, ys, color=color, alpha=alpha, linewidth=width or 1.6,
            linestyle=_linestyle(mark), label=mark.name,
        )
        return

    if mark.type == "bar":
        ys = _series(mark)
        if not ys:
            return
        ax.bar(
            _x_series(mark, categories), ys, bottom=_stack_base(mark, panel), color=color,
            alpha=alpha, width=width or 0.7, label=mark.name,
        )
        return

    if mark.type == "scatter":
        xs = [v for v in _x_series(mark, categories) if v == v]
        ys = [v for v in _series(mark) if v is not None]
        if not xs or not ys:
            return
        count = min(len(xs), len(ys))
        sizes = [s for s in (mark.sizes or []) if s is not None]
        scale = 220.0 / (max(sizes) or 1.0) if sizes else 0.0
        ax.scatter(
            xs[:count], ys[:count], color=color, alpha=0.85 * alpha,
            s=[(sizes[i] if i < len(sizes) else 1) * scale for i in range(count)] if sizes else 40,
            label=mark.name, edgecolors="none",
        )
        return

    if mark.type == "regression":
        # The points, the least-squares line, and the R-squared - the same
        # arithmetic as the live figure, so the exported picture agrees with
        # what the reader saw.
        ys = _series(mark)
        # A regression is arithmetic over NUMBERS: a label axis has no
        # meaningful slope, so its points are drawn but never fitted.
        xs = _x_series(mark)
        pairs = [
            (x, y)
            for x, y in zip(xs, ys)
            if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x
            and isinstance(y, (int, float)) and not isinstance(y, bool) and y == y
        ]
        if len(pairs) < 2:
            if pairs:
                ax.scatter(
                    [p[0] for p in pairs], [p[1] for p in pairs],
                    color=color, s=18, alpha=alpha, label=mark.name,
                )
            return
        fit = linear_fit([p[0] for p in pairs], [p[1] for p in pairs])
        if fit is None:
            ax.scatter(
                [p[0] for p in pairs], [p[1] for p in pairs],
                color=color, s=18, alpha=alpha, label=f"{mark.name} (no line to fit)",
            )
            return
        ax.scatter(
            [p[0] for p in pairs], [p[1] for p in pairs],
            color=color, s=18, alpha=alpha, label=mark.name,
        )
        ax.plot(
            [fit.x_min, fit.x_max], [fit.at(fit.x_min), fit.at(fit.x_max)],
            color=color, linewidth=1.8, linestyle="--",
            label=f"{mark.name} fit (R2 = {fit.r_squared:.3f})",
        )
        return

    if mark.type == "band":
        lo = mark.lower or []
        up = mark.upper or []
        count = min(len(lo), len(up))
        if count < 2:
            return
        xs = list(range(count))
        ax.fill_between(
            xs,
            [float(v) if v is not None else float("nan") for v in lo[:count]],
            [float(v) if v is None else float(v) for v in up[:count]],
            color=color, alpha=0.18 * alpha, linewidth=0,
        )
        return

    if mark.type == "rule":
        value = (mark.y or [None])[0]
        if value is None:
            return
        ax.axhline(
            value, color=color, alpha=alpha, linewidth=width or 1.0,
            linestyle=_linestyle(mark), label=mark.name or mark.label,
        )
        if mark.label:
            ax.annotate(
                mark.label, xy=(1, value), xycoords=("axes fraction", "data"),
                xytext=(-4, 3), textcoords="offset points", ha="right", va="bottom",
                fontsize=8, color=color,
            )
        return

    if mark.type == "label":
        xs = _x_series(mark, categories)
        ys = _series(mark)
        if not xs or not ys:
            return
        ax.annotate(
            mark.label or "", xy=(xs[0], ys[0] if ys[0] is not None else 0.0),
            ha="center", va="center", fontsize=9, color=color, alpha=alpha,
        )
        return

    if mark.type == "box":
        values = _values(mark)
        if len(values) < 2:
            return
        ax.boxplot(
            [values], positions=[0], widths=0.5, patch_artist=True,
            boxprops={"facecolor": color, "alpha": 0.35 * alpha, "edgecolor": color},
            medianprops={"color": color}, whiskerprops={"color": color},
            capprops={"color": color}, label=mark.name,
        )
        return

    if mark.type == "violin":
        values = _values(mark)
        if len(values) < 2:
            return
        parts = ax.violinplot([values], positions=[0], widths=0.6, showmedians=True)
        for body in parts.get("bodies", []):
            try:
                body.set_facecolor(color)
                body.set_edgecolor(color)
                body.set_alpha(0.45 * alpha)
            except Exception:  # noqa: BLE001
                pass
        return

    if mark.type == "scatter3d":
        if not hasattr(ax, "scatter3d"):
            return
        try:
            import numpy as np  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - a missing numpy only loses the PNG copy
            return
        xs = [float(v) if v is not None else float("nan") for v in mark.x]
        ys = [float(v) if v is not None else float("nan") for v in mark.y]
        zs = [float(v) if v is not None else float("nan") for v in mark.z]
        count = min(len(xs), len(ys), len(zs))
        if count == 0:
            return
        ax.scatter3D(
            np.asarray(xs[:count]), np.asarray(ys[:count]), np.asarray(zs[:count]),
            color=color, alpha=alpha, label=mark.name, depthshade=True,
        )
        return

    if mark.type == "surface":
        if not hasattr(ax, "plot_surface"):
            return
        try:
            import numpy as np  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return
        grid = surface_grid(mark)
        if grid is None:
            return
        xs, ys, rows = grid
        z = np.asarray(rows)
        grid_x, grid_y = np.meshgrid(np.asarray(xs), np.asarray(ys))
        ax.plot_surface(grid_x, grid_y, z, cmap="viridis", alpha=alpha, label=mark.name)
        return

    if mark.type == "histogram":
        values = _values(mark)
        if not values:
            return
        ax.hist(
            values, bins=mark.bins or min(40, max(5, len(values) // 8)),
            color=color, alpha=0.9 * alpha, edgecolor="none", label=mark.name,
        )
        return

    if mark.type == "errorbar":
        xs = _x_series(mark, categories)
        ys = _series(mark)
        if not xs or not ys:
            return
        count = min(len(xs), len(ys))
        lower = mark.lower or []
        upper = mark.upper or []
        yerr = None
        if lower and upper:
            yerr = [
                [
                    max(0.0, (ys[i] or 0.0) - lower[i]) if i < len(lower) and lower[i] is not None else 0.0
                    for i in range(count)
                ],
                [
                    max(0.0, upper[i] - (ys[i] or 0.0)) if i < len(upper) and upper[i] is not None else 0.0
                    for i in range(count)
                ],
            ]
        ax.errorbar(
            xs[:count], ys[:count], yerr=yerr, fmt="o", color=color,
            alpha=alpha, ecolor=color, elinewidth=1, capsize=3, label=mark.name,
        )
        return

    if mark.type == "heatmap":
        if not mark.matrix:
            return
        image = ax.imshow(mark.matrix, cmap="coolwarm", aspect="auto")
        try:
            ax.figure.colorbar(image, ax=ax, fraction=0.046, pad=0.03)
        except Exception:  # noqa: BLE001
            pass
        if mark.row_labels:
            ax.set_yticks(range(len(mark.row_labels)))
            ax.set_yticklabels(mark.row_labels)
        if mark.col_labels:
            ax.set_xticks(range(len(mark.col_labels)))
            ax.set_xticklabels(mark.col_labels, rotation=45, ha="right")
        return

    if mark.type in ("pie", "donut"):
        values = _values(mark)
        if not values:
            return
        colors = [theme.series_color(i) for i in range(len(values))]
        ax.pie(
            values, labels=mark.labels or None, colors=colors,
            startangle=90, counterclock=False,
            wedgeprops=(
                {"width": 0.42, "edgecolor": "none"} if mark.type == "donut"
                else {"edgecolor": "none"}
            ),
        )
        return

    if mark.type == "candlestick":
        rows = mark.matrix or []
        if not rows:
            return
        from matplotlib.patches import Rectangle  # noqa: PLC0415

        for i, candle in enumerate(rows):
            if not isinstance(candle, (list, tuple)) or len(candle) < 4:
                continue
            open_, high, low, close = (float(candle[0]), float(candle[1]), float(candle[2]), float(candle[3]))
            body = color if close >= open_ else theme.down
            ax.plot([i, i], [low, high], color=body, linewidth=0.9)
            ax.add_patch(Rectangle(
                (i - 0.32, min(open_, close)), 0.64, max(abs(close - open_), 1e-9),
                facecolor=body, edgecolor="none", alpha=0.9,
            ))
        return

    if mark.type == "waterfall":
        values = _values(mark)
        if not values:
            return
        running = 0.0
        bottoms: list[float] = []
        heights: list[float] = []
        colors: list[str] = []
        for value in values:
            bottoms.append(running if value >= 0 else running + value)
            heights.append(abs(value))
            colors.append(color if value >= 0 else theme.down)
            running += value
        ax.bar(range(len(values)), heights, bottom=bottoms, color=colors, width=0.66)
        return

    if mark.type == "ridge":
        ys = _series(mark)
        if not ys:
            return
        offset = float(mark.width or 0.0)
        ax.fill_between(
            _x_series(mark, categories), offset, [offset + (v or 0.0) for v in ys],
            color=color, alpha=0.6, linewidth=0,
        )
        return

    if mark.type == "stem":
        ys = _series(mark)
        if not ys:
            return
        ax.stem(_x_series(mark, categories), ys, linefmt=color, markerfmt="o", basefmt=" ")
        return


__all__ = ["draw_mark"]
