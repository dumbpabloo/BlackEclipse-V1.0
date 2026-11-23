"""Draw the numbered marks that make `som` mean what it says.

The model receives the screenshot AND the text element list. Unless the numbers
exist on the image as well, the two do not correspond and the model has to guess
which rectangle is `#7`. This module renders a bounded set of badges onto a copy
of the frame:

  * accessibility elements — warm red;
  * OCR words — blue;
  * vision-grounded elements — green.

It is deliberately dependency-light (Pillow, already required for capture). When
Pillow is absent or there is nothing to draw, the original image is returned
unchanged — never a broken frame.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

_MAX_MARKS = 120
_RED = (220, 38, 38)
_BLUE = (37, 99, 235)
_GREEN = (22, 163, 74)
_WHITE = (255, 255, 255)


def _color_for(source: str) -> tuple[int, int, int]:
    lowered = (source or "").lower()
    if lowered == "ocr":
        return _BLUE
    if lowered == "vision":
        return _GREEN
    return _RED


def _font(size: int) -> Any:
    try:
        from PIL import ImageFont  # noqa: PLC0415

        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:  # noqa: BLE001 - any platform, any font dir
            try:
                return ImageFont.truetype("DejaVuSans.ttf", size)
            except Exception:  # noqa: BLE001
                return ImageFont.load_default()
    except Exception:  # noqa: BLE001 - Pillow absent
        return None


def annotate_som(
    image: Any,
    elements: Iterable[Any],
    *,
    origin: Optional[tuple[int, int]] = None,
    scale: float = 1.0,
    max_marks: int = _MAX_MARKS,
) -> Any:
    """Return a copy of `image` with numbered element badges drawn on it.

    `origin` is the screen coordinate of the image's top-left pixel (the captured
    window's bounds). Element bounds are screen coordinates; `scale` converts
    them into image space (e.g. a downscaled capture).
    """
    if image is None:
        return image
    try:
        from PIL import ImageDraw  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - Pillow absent: honest no-op
        return image

    try:
        width, height = int(image.width), int(image.height)
    except Exception:  # noqa: BLE001
        return image
    if width <= 0 or height <= 0:
        return image

    items = [e for e in elements if getattr(e, "bounds", None) is not None]
    if not items:
        return image
    items = items[: max(1, max_marks)]

    ox, oy = origin or (0, 0)
    factor = float(scale or 1.0)
    if factor <= 0:
        factor = 1.0

    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font(max(9, int(min(width, height) * 0.015)))
    for element in items:
        bounds = element.bounds
        try:
            x1 = int((bounds.x - ox) * factor)
            y1 = int((bounds.y - oy) * factor)
            x2 = int((bounds.right - ox) * factor)
            y2 = int((bounds.bottom - oy) * factor)
        except Exception:  # noqa: BLE001
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        # Ignore marks that fall outside the captured frame.
        if x2 < 0 or y2 < 0 or x1 > width or y1 > height:
            continue
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width - 1, x2), min(height - 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        color = _color_for(getattr(element, "source", ""))
        try:
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
        except Exception:  # noqa: BLE001
            continue
        label = str(getattr(element, "ref", "") or "")
        if not label:
            continue
        text = f"#{label}"
        try:
            box = draw.textbbox((0, 0), text, font=font) if font else (0, 0, 6 * len(text), 10)
            tw, th = box[2] - box[0], box[3] - box[1]
        except Exception:  # noqa: BLE001
            tw, th = 6 * len(text), 10
        bx = max(0, min(width - tw - 4, x1))
        by = max(0, y1 - th - 4)
        try:
            draw.rectangle([bx, by, bx + tw + 4, by + th + 4], fill=color)
            draw.text((bx + 2, by + 1), text, fill=_WHITE, font=font)
        except Exception:  # noqa: BLE001
            continue
    return out


__all__ = ["annotate_som"]
