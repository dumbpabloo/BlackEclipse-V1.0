"""GUI grounding backends — turning "the Continue button" into a real point.

The text ladder (accessibility → adapter → OCR) is exact when it works and blind
when it does not. A *grounding model* (UI-TARS-class) closes that gap: it is
shown the screenshot and asked where the described control is, in its own
declared coordinate space. This module owns that contract:

  * `GroundingBackend.locate(target, image)` returns boxes in IMAGE PIXELS;
  * the backend declares its coordinate space and is scaled by itself, so the
    caller never has to guess whether 1000 means pixels or a normalised grid;
  * an unconfigured endpoint is an honest `None`, not a fabricated point.

The HTTP shape is the OpenAI-compatible `/chat/completions` endpoint that vLLM,
TGI, LM Studio, Ollama and hosted UI-TARS providers all expose.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any, Optional

_BOX_RE = re.compile(
    r"<\|box_start\|>\s*\(?(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)?\s*[,;]?\s*"
    r"\(?(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)?\s*<\|box_end\|>"
)
_POINT_RE = re.compile(r"<\|point_start\|>\s*\(?(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)?")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROMPT = (
    "You are a precise GUI grounding model. Look at the screenshot and find the "
    "single element described below.\n"
    "Target: {target}\n"
    "Reply with exactly one of:\n"
    "  <|box_start|>(x1,y1),(x2,y2)<|box_end|>\n"
    "  <|point_start|>(x,y)\n"
    "Coordinates are in a {width}x{height} grid, top-left origin. No other text."
)


class GroundingBackend:
    """The interface. An unavailable backend is a first-class, honest state."""

    id = "unavailable"
    available = False
    coordinate_space: tuple[int, int] = (1000, 1000)

    async def locate(self, target: str, image: Any) -> list[dict[str, Any]]:
        return []

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "available": self.available,
                "coordinateSpace": list(self.coordinate_space)}


def _encode(image: Any, *, max_edge: int = 1280) -> Optional[str]:
    """PNG/JPEG data URL for the grounding endpoint (bounded)."""
    try:
        if image is None:
            return None
        if max(image.width, image.height) > max_edge:
            ratio = max_edge / float(max(image.width, image.height))
            image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


def _scale(value: float, source: int, target: int) -> float:
    if source <= 0:
        return value
    return value * (float(target) / float(source))


class UiTarsBackend(GroundingBackend):
    """An OpenAI-compatible UI-TARS / GUI-grounding endpoint."""

    id = "uitars"

    def __init__(self, *, url: str, model: str, api_key: str = "",
                 width: int = 1000, height: int = 1000, timeout_s: float = 30.0) -> None:
        self.url = (url or "").rstrip("/")
        self.model = model or ""
        self.api_key = api_key or ""
        self.coordinate_space = (max(1, int(width)), max(1, int(height)))
        self.timeout_s = float(timeout_s or 30.0)
        self.available = bool(self.url and self.model)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "endpoint": self.url, "model": self.model}

    async def locate(self, target: str, image: Any) -> list[dict[str, Any]]:
        if not self.available:
            return []
        data_url = _encode(image)
        if not data_url:
            return []
        width, height = self.coordinate_space
        prompt = _PROMPT.format(target=target, width=width, height=height)
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 128,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(self.url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
            text = ""
            choices = body.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    text = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                else:
                    text = str(content or "")
            return _parse_reply(text, space=(width, height), image_size=(image.width, image.height))
        except Exception:  # noqa: BLE001 - a grounding failure must never break an action
            return []


def _parse_reply(text: str, *, space: tuple[int, int], image_size: tuple[int, int]) -> list[dict[str, Any]]:
    """Parse a UI-TARS reply into image-pixel boxes (pure, testable)."""
    sw, sh = space
    iw, ih = image_size
    results: list[dict[str, Any]] = []

    def box(x1: float, y1: float, x2: float, y2: float) -> None:
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        results.append({
            "bbox": [
                _scale(left, sw, iw), _scale(top, sh, ih),
                _scale(right - left, sw, iw), _scale(bottom - top, sh, ih),
            ],
            "label": (text.strip().splitlines() or [""])[0][:120],
            "confidence": 0.9,
            "method": "uitars",
        })

    match = _BOX_RE.search(text or "")
    if match:
        box(*[float(g) for g in match.groups()])
        return results
    point = _POINT_RE.search(text or "")
    if point:
        x, y = float(point.group(1)), float(point.group(2))
        # A point has no size: use a 12px image-space box around it.
        px, py = _scale(x, sw, iw), _scale(y, sh, ih)
        results.append({
            "bbox": [px - 6, py - 6, 12, 12], "label": "point",
            "confidence": 0.85, "method": "uitars-point",
        })
        return results
    json_match = _JSON_RE.search(text or "")
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
            bbox = payload.get("bbox") or payload.get("box") or payload.get("bounds")
            if isinstance(bbox, list) and len(bbox) == 4:
                box(*[float(v) for v in bbox])
                return results
        except (ValueError, TypeError):
            return []
    return results


class NullGroundingBackend(GroundingBackend):
    def __init__(self, reason: str = "no grounding endpoint is configured") -> None:
        self.reason = reason
        self.available = False

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "reason": self.reason}


def build_grounding_backend(config: Any) -> Optional[GroundingBackend]:
    url = str(getattr(config, "grounding_url", "") or "").strip()
    model = str(getattr(config, "grounding_model", "") or "").strip()
    if not url or not model:
        return None
    return UiTarsBackend(
        url=url, model=model,
        api_key=str(getattr(config, "grounding_api_key", "") or ""),
        width=int(getattr(config, "grounding_width", 1000) or 1000),
        height=int(getattr(config, "grounding_height", 1000) or 1000),
        timeout_s=float(getattr(config, "grounding_timeout_seconds", 30) or 30),
    )


__all__ = [
    "GroundingBackend", "NullGroundingBackend", "UiTarsBackend",
    "build_grounding_backend",
]
