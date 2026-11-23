"""Observation — building a `Snapshot` from whichever sources are available.

Modes:
  * `ax`   — the accessibility tree only, rendered as text + elements.
  * `som`  — a screen-of-marks: the (window) screenshot plus NUMBERED interactable
             elements from the accessibility tree, with bounds/roles/names/values.
  * `ocr`  — OCR words as elements (for canvas/graphical apps).
  * `vision` — a plain screenshot (+ optional model description).

Every snapshot gets a `snapshot_id` and a `generation`; the numbered element
handles (`#1`, `#2`, …) the model sees are validated against this snapshot before
an action runs, so a stale handle is refused rather than silently repointed.

The engine is honest: an absent AX tree yields an empty element list (never
invented elements), and OCR/vision are used only when their backends are real.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from .accessibility.base import looks_interactive
from .models import AXNode, Bounds, Snapshot, SnapshotElement


def _flatten(node: Optional[AXNode]) -> list[AXNode]:
    if node is None:
        return []
    return [n for n in node.walk() if n is not None]


def _identity(role: str, name: str, value: str, bounds: Optional[Bounds]) -> str:
    raw = f"{role}|{name}|{value}|{bounds.to_list() if bounds else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _replace_frame_image(frame: Any, image: Any) -> Any:
    """Return a ScreenFrame carrying a masked image (best-effort)."""
    try:
        from .drivers.base import ScreenFrame  # noqa: PLC0415

        return ScreenFrame(
            data=image, width=frame.width, height=frame.height,
            bounds=frame.bounds, png_bytes=None, scale=frame.scale,
            method=getattr(frame, "method", ""),
        )
    except Exception:  # noqa: BLE001
        return frame


def ax_digest(node: Optional[AXNode]) -> str:
    """A digest of the accessibility tree's content, for state-change detection."""
    if node is None:
        return ""
    parts = [f"{n.role}:{n.name}:{n.value}" for n in _flatten(node)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]


class ObservationEngine:
    """Builds snapshots for one session."""

    def __init__(self, *, driver: Any, ax: Any, ocr: Any, vision_router: Any, config: Any, capture: Any, security: Any = None) -> None:
        self._driver = driver
        self._ax = ax
        self._ocr = ocr
        self._vision = vision_router
        self._config = config
        self._capture = capture
        self._security = security
        self._counter = 0
        self._nonce: Optional[str] = None

    def next_snapshot_id(self) -> str:
        self._counter += 1
        # A per-engine nonce makes ids unique across SESSIONS, not just within one.
        # Without it two sessions observed in the same millisecond could mint the
        # same id, and one session's stale-handle check would accept the other's.
        if self._nonce is None:
            import uuid  # noqa: PLC0415

            self._nonce = uuid.uuid4().hex[:8]
        return f"snap-{self._nonce}-{self._counter}-{int(time.time() * 1000)}"

    async def observe(
        self,
        *,
        window_id: Optional[str] = None,
        mode: Optional[str] = None,
        persist_image: bool = True,
        session_id: str = "session",
        include_vision_description: bool = False,
        region: Optional[Any] = None,
        display_id: Optional[str] = None,
    ) -> Snapshot:
        mode = (mode or self._config.observation_default_mode).lower()
        # A `vision` observation is asking for a description by definition.
        include_vision_description = include_vision_description or mode == "vision"
        snapshot_id = self.next_snapshot_id()
        generation = self._counter

        window_title = ""
        window_app = ""
        bounds: Optional[Bounds] = None
        if region is not None:
            try:
                from .models import Bounds as _Bounds  # noqa: PLC0415

                bounds = region if isinstance(region, _Bounds) else _Bounds.from_any(region)
            except Exception:  # noqa: BLE001
                bounds = None
        if window_id:
            try:
                window = self._driver.get_window(window_id)
                if window is not None:
                    window_title = window.title
                    window_app = str(getattr(window, "app", "") or "")
                    if region is None:
                        bounds = window.bounds
            except Exception:  # noqa: BLE001
                pass

        ax_tree = None
        if mode in ("ax", "som"):
            try:
                ax_tree = self._driver.accessibility_tree(window_id)
            except Exception:  # noqa: BLE001
                ax_tree = None

        elements: list[SnapshotElement] = []
        if ax_tree is not None:
            elements = self._elements_from_ax(ax_tree)

        ocr_result = None
        frame = None
        image = None
        if mode in ("ocr", "som", "vision"):
            try:
                frame = await self._driver.capture(window_id=window_id, region=bounds if region is not None else None,
                                                   display_id=display_id)
                image = getattr(frame, "data", None)
            except Exception:  # noqa: BLE001
                frame = None
            if mode == "ocr" and image is not None and self._ocr is not None and getattr(self._ocr, "available", False):
                ocr_result = self._ocr.read(image, bounds=bounds)
                elements = self._elements_from_ocr(ocr_result)

        # SOM: if AX gave no interactables but an image exists, add OCR marks.
        if mode == "som" and not elements and image is not None and self._ocr is not None and getattr(self._ocr, "available", False):
            ocr_result = self._ocr.read(image, bounds=bounds)
            elements = self._elements_from_ocr(ocr_result)

        text = self._render_text(mode, elements, ocr_result)

        # SOM must be a SCREEN OF MARKS, not a list of numbers with no picture.
        # Draw numbered badges on a copy of the frame so the text handles and the
        # image agree. The security mask is applied AFTER this.
        if mode in ("som", "ocr") and elements and image is not None:
            try:
                from .screenshots.annotate import annotate_som  # noqa: PLC0415

                scale = 1.0
                if bounds is not None and bounds.width > 0:
                    scale = float(getattr(image, "width", bounds.width)) / float(bounds.width)
                annotated = annotate_som(
                    image, elements,
                    origin=(bounds.x, bounds.y) if bounds is not None else None,
                    scale=scale,
                )
                if annotated is not None:
                    image = annotated
                    frame = _replace_frame_image(frame, annotated)
            except Exception:  # noqa: BLE001 - annotation must never break an observation
                pass

        # ── Security: flag + mask sensitive content BEFORE the image is persisted.
        # A sensitive frame must never reach a cloud model or an exported
        # trajectory unmasked.
        security_report = None
        sensitive = False
        if self._security is not None:
            security_report = self._security.scan_snapshot(
                [e.to_dict() for e in elements], text=text,
                application=window_app, window_title=window_title,
            )
            sensitive = bool(security_report.sensitive)
            if sensitive:
                # The text/element values leave the machine too; redact them, not
                # only the image.
                text = self._security.redact_text(text)
                for element in elements:
                    if element.value:
                        element.value = self._security.redact_text(element.value)

        image_path = None
        if frame is not None and persist_image and mode != "ax":
            if sensitive and self._security is not None and image is not None:
                origin = (bounds.x, bounds.y) if bounds is not None else None
                scale = (float(image.width) / float(bounds.width)) if bounds and bounds.width else 1.0
                masked = self._security.mask_image(
                    image, security_report.regions, origin=origin, scale=scale,
                )
                image = masked
                frame = _replace_frame_image(frame, masked)
            image_path = self._capture.persist(frame, session_id=session_id, label="snapshot")

        snapshot = Snapshot(
            snapshot_id=snapshot_id, generation=generation,
            application=str(getattr(self._driver, "id", "")),
            window_id=window_id or "", window_title=window_title, bounds=bounds,
            mode=mode, elements=elements, text=text, image_path=image_path,
            ax_digest=ax_digest(ax_tree),
            ocr_digest=(hashlib.sha256(ocr_result.text.encode("utf-8")).hexdigest()[:20] if ocr_result and ocr_result.available else ""),
            chars=len(text),
            truncated=len(text) > self._config.snapshot_max_chars,
            sensitive=sensitive,
            security=(security_report.to_dict() if security_report else None),
            capture_method=str(getattr(frame, "method", "") or "") if frame is not None else "",
        )
        if include_vision_description and image is not None:
            # Route on the snapshot's ACTUAL sensitivity: a sensitive frame must
            # never be described by a cloud model unless policy allows it.
            engine = self._vision.engine(sensitive=snapshot.sensitive)
            if engine.available:
                try:
                    result = await engine.describe(image, bounds=bounds)
                    if result.text:
                        snapshot.text = (snapshot.text + "\n\n" + result.text)[: self._config.snapshot_max_chars]
                except Exception:  # noqa: BLE001
                    pass
            else:
                snapshot.text = (
                    snapshot.text
                    + "\n(no vision model is configured; the screenshot is still attached for a multimodal model)"
                )[: self._config.snapshot_max_chars]
        return snapshot

    # ── element builders ─────────────────────────────────────────────────────

    def _elements_from_ax(self, tree: AXNode) -> list[SnapshotElement]:
        elements: list[SnapshotElement] = []
        counter = 0
        for node in _flatten(tree):
            if not self._is_interactable(node):
                continue
            counter += 1
            elements.append(SnapshotElement(
                ref=str(counter), role=node.role, name=node.name, value=node.value,
                bounds=node.bounds, enabled=node.enabled, focused=node.focused,
                states=list(node.states), source="accessibility",
                identity=_identity(node.role, node.name, node.value, node.bounds),
            ))
        return elements

    def _is_interactable(self, node: AXNode) -> bool:
        if node.bounds is None or node.bounds.width <= 0 or node.bounds.height <= 0:
            return False
        if node.name or node.value:
            return True
        return looks_interactive(node.role)

    def _elements_from_ocr(self, ocr_result: Any) -> list[SnapshotElement]:
        elements: list[SnapshotElement] = []
        for i, word in enumerate(getattr(ocr_result, "words", []), start=1):
            elements.append(SnapshotElement(
                ref=str(i), role="text", name=word.text, bounds=word.bounds,
                source="ocr", confidence=word.confidence,
                identity=_identity("text", word.text, "", word.bounds),
            ))
        return elements

    # ── rendering ────────────────────────────────────────────────────────────

    def _render_text(self, mode: str, elements: list[SnapshotElement], ocr_result: Any) -> str:
        header = f"[observation mode: {mode}]"
        if not elements:
            if mode == "vision":
                body = "(screenshot captured; the image is attached to this result and no accessibility elements were found)"
            else:
                body = "(no interactable elements were found)"
            if ocr_result is not None and getattr(ocr_result, "available", False) and ocr_result.text:
                body = ocr_result.text
            return f"{header}\n{body}"
        lines = [header]
        for element in elements:
            box = element.bounds.to_dict() if element.bounds else {}
            value = f' = "{element.value}"' if element.value else ""
            lines.append(
                f"#{element.ref} {element.role} \"{element.name}\"{value} "
                f"at ({box.get('x', 0)},{box.get('y', 0)},{box.get('width', 0)}x{box.get('height', 0)})"
            )
        return "\n".join(lines)


__all__ = ["ObservationEngine", "ax_digest"]
