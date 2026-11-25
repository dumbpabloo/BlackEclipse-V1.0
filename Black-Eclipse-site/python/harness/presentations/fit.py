"""Deterministic text fitting for slides.

The same arithmetic runs in the browser (`src/lib/presentation/fit.ts`) so the
preview and the exported file agree. It is deliberately conservative: an
average glyph width, integer line counts, and a fixed line-height multiple.

A slide that cannot fit reports `fits=False`; it is never silently clipped.
"""

from __future__ import annotations

#: Point ladder a role may step down to. The base size picks the closest entry.
FIT_LADDER: tuple[int, ...] = (34, 30, 28, 26, 24, 22, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10)

#: Average advance width as a fraction of the font size, per role family.
GLYPH_WIDTH = 0.55
GLYPH_WIDTH_BOLD = 0.60
#: Line box height as a multiple of the font size.
LINE_HEIGHT = 1.28
#: 96 CSS px per inch; 72 pt per inch.
PX_PER_INCH = 96.0
PX_PER_PT = PX_PER_INCH / 72.0


def text_width_in(text: str, pt: float, *, bold: bool = False) -> float:
    """Conservative rendered width of one line, in inches."""
    factor = GLYPH_WIDTH_BOLD if bold else GLYPH_WIDTH
    return len(text or "") * pt * PX_PER_PT * factor / PX_PER_INCH


def wrap_lines(text: str, width_in: float, pt: float, *, bold: bool = False) -> int:
    """How many wrapped lines `text` needs inside `width_in`."""
    if not text:
        return 1
    available = max(width_in, 0.1)
    lines = 0
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines += 1
            continue
        current = ""
        count = 1
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and text_width_in(candidate, pt, bold=bold) > available:
                count += 1
                current = word
            else:
                current = candidate
        lines += count
    return max(lines, 1)


def block_height_in(text: str, width_in: float, pt: float, *, bold: bool = False) -> float:
    return wrap_lines(text, width_in, pt, bold=bold) * pt * LINE_HEIGHT / 72.0


def fit_text(
    text: str,
    width_in: float,
    height_in: float,
    base_pt: float,
    *,
    min_pt: float = 10.0,
    bold: bool = False,
) -> tuple[int, bool]:
    """Largest ladder size at/below `base_pt` whose wrapped text fits.

    Returns `(pt, fits)`. When nothing fits, returns the smallest candidate and
    `fits=False` so the caller can warn honestly.
    """
    ladder = [size for size in FIT_LADDER if size <= base_pt]
    if not ladder:
        ladder = [int(min_pt)]
    for size in ladder:
        if size < min_pt:
            break
        if block_height_in(text, width_in, size, bold=bold) <= height_in:
            return size, True
    smallest = max(int(min_pt), min(ladder))
    return smallest, False


def fit_bullets(
    items: list[str],
    width_in: float,
    height_in: float,
    base_pt: float,
    *,
    min_pt: float = 12.0,
    gap_pt: float = 8.0,
) -> tuple[int, bool]:
    """Largest size where every bullet plus its gap fits the body region."""
    ladder = [size for size in FIT_LADDER if size <= base_pt] or [int(min_pt)]
    for size in ladder:
        if size < min_pt:
            break
        total = 0.0
        for item in items or [""]:
            total += block_height_in(item, width_in, size)
            total += gap_pt / 72.0
        if total <= height_in:
            return size, True
    return max(int(min_pt), min(ladder)), False


__all__ = [
    "FIT_LADDER",
    "LINE_HEIGHT",
    "block_height_in",
    "fit_bullets",
    "fit_text",
    "text_width_in",
    "wrap_lines",
]
