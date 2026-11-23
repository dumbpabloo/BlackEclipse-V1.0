"""Platform-independent value objects for the Computer Use Engine.

These are the shared vocabulary every layer speaks: geometry, displays, windows,
applications, accessibility nodes, snapshots and their stable element handles,
grounded targets, actions and results. They carry no platform imports — a driver
translates the OS into these, and everything above the driver is portable.

Nothing here reaches the network or the OS. It is data plus tiny pure helpers.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ── Capability + contract ────────────────────────────────────────────────────

# The driver runtime contract. A driver DECLARES a contract; Eclipse refuses to
# operate with a missing, outdated or incompatible one rather than guessing.
DRIVER_CONTRACT_MAJOR = 1
DRIVER_CONTRACT_MINOR = 0


@dataclass(frozen=True)
class DriverContract:
    """The versioned runtime contract a `ComputerDriver` exposes."""

    major: int = DRIVER_CONTRACT_MAJOR
    minor: int = DRIVER_CONTRACT_MINOR
    platform: str = "unknown"
    name: str = "null"
    capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "major": self.major,
            "minor": self.minor,
            "platform": self.platform,
            "name": self.name,
            "capabilities": list(self.capabilities),
        }


@dataclass
class Capabilities:
    """What this machine/driver can actually do. Data, not routing logic."""

    platform: str = "unknown"
    background_input: bool = False
    foreground_input: bool = True
    accessibility: bool = False
    screen_capture: bool = False
    multi_monitor: bool = False
    window_management: bool = False
    application_management: bool = False
    set_value: bool = False
    ocr: bool = False
    vision: bool = False
    finance_cua: bool = True
    headless_desktop: bool = False
    # Honest reasons a capability is False.
    notes: list[str] = field(default_factory=list)

    def supports(self, capability: str) -> bool:
        return bool(getattr(self, capability, False))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Geometry ─────────────────────────────────────────────────────────────────


@dataclass
class Bounds:
    """A rectangle in virtual-screen (global) coordinates."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def to_list(self) -> list[int]:
        return [self.x, self.y, self.right, self.bottom]

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @staticmethod
    def from_any(value: Any) -> Optional["Bounds"]:
        """Coerce the several accepted shapes into Bounds, or None."""
        if value is None:
            return None
        if isinstance(value, Bounds):
            return value
        if isinstance(value, dict):
            try:
                return Bounds(
                    int(value.get("x", 0)), int(value.get("y", 0)),
                    int(value.get("width", value.get("w", 0))),
                    int(value.get("height", value.get("h", 0))),
                )
            except (TypeError, ValueError):
                return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                left, top, right, bottom = (int(v) for v in value)
                return Bounds(left, top, max(0, right - left), max(0, bottom - top))
            except (TypeError, ValueError):
                return None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            # A bare [x, y] is a POINT, not an LTRB rectangle. It must NOT be
            # silently misread as a rectangle (the old behaviour returned None and
            # dropped a caller's explicit coordinate).
            try:
                return Bounds(int(value[0]), int(value[1]), 1, 1)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def from_ltrb(left: int, top: int, right: int, bottom: int) -> "Bounds":
        return Bounds(int(left), int(top), max(0, int(right) - int(left)), max(0, int(bottom) - int(top)))


@dataclass
class Point:
    x: int = 0
    y: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


# ── Displays, windows, applications ──────────────────────────────────────────


@dataclass
class DisplayInfo:
    id: str
    bounds: Bounds
    working_area: Bounds
    scale: float = 1.0
    dpi: int = 96
    primary: bool = False
    orientation: str = "landscape"
    name: str = ""

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.bounds.width, self.bounds.height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "bounds": self.bounds.to_dict(),
            "workingArea": self.working_area.to_dict(), "scale": self.scale,
            "dpi": self.dpi, "primary": self.primary, "orientation": self.orientation,
            "name": self.name, "resolution": list(self.resolution),
        }


@dataclass
class WindowInfo:
    id: str
    title: str
    app: str = ""
    pid: int = 0
    bounds: Bounds = field(default_factory=Bounds)
    minimized: bool = False
    maximized: bool = False
    visible: bool = True
    focused: bool = False
    display_id: str = ""
    z_order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "application": self.app, "pid": self.pid,
            "bounds": self.bounds.to_dict(), "minimized": self.minimized,
            "maximized": self.maximized, "visible": self.visible, "focused": self.focused,
            "displayId": self.display_id, "zOrder": self.z_order,
        }


@dataclass
class ApplicationInfo:
    id: str
    name: str
    pid: int = 0
    executable: str = ""
    running: bool = True
    window_ids: list[str] = field(default_factory=list)
    focused: bool = False
    has_ui: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "pid": self.pid, "executable": self.executable,
            "running": self.running, "windowIds": list(self.window_ids),
            "focused": self.focused, "hasUi": self.has_ui,
        }


# ── Accessibility ────────────────────────────────────────────────────────────


@dataclass
class AXNode:
    """One node of an accessibility tree."""

    ref: str = ""                     # per-snapshot stable handle ("e3")
    role: str = "generic"
    name: str = ""
    value: str = ""
    description: str = ""
    bounds: Optional[Bounds] = None
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    interactive: bool = False
    states: list[str] = field(default_factory=list)
    children: list["AXNode"] = field(default_factory=list)
    # A driver-opaque native handle, kept only for identity re-validation. It is
    # never sent to the model.
    native_handle: Any = None

    def walk(self) -> list["AXNode"]:
        out: list[AXNode] = [self]
        for child in self.children:
            out.extend(child.walk())
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "role": self.role, "name": self.name, "value": self.value,
            "description": self.description, "bounds": self.bounds.to_dict() if self.bounds else None,
            "enabled": self.enabled, "focused": self.focused, "selected": self.selected,
            "interactive": self.interactive, "states": list(self.states),
            "children": [c.to_dict() for c in self.children],
        }


# ── Snapshots + stable element handles ───────────────────────────────────────


@dataclass
class SnapshotElement:
    """An interactable element in a snapshot — the model's click handle."""

    ref: str                          # "1", "2", … (the number the model sees)
    role: str
    name: str = ""
    value: str = ""
    bounds: Optional[Bounds] = None
    enabled: bool = True
    focused: bool = False
    states: list[str] = field(default_factory=list)
    source: str = "accessibility"     # accessibility | som | ocr | vision
    confidence: float = 1.0
    # Identity fingerprint for stale validation; never model-facing.
    identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "role": self.role, "name": self.name, "value": self.value,
            "bounds": self.bounds.to_dict() if self.bounds else None,
            "enabled": self.enabled, "focused": self.focused, "states": list(self.states),
            "source": self.source, "confidence": round(self.confidence, 2),
        }

    def label(self) -> str:
        extra = f" \"{self.name}\"" if self.name else ""
        return f"#{self.ref} {self.role}{extra}"


@dataclass
class Snapshot:
    """A point-in-time observation of one window (or the whole screen).

    Every action against an element handle is validated against the snapshot it
    came from. A changed generation, window or identity ⇒ STALE_ELEMENT.
    """

    snapshot_id: str
    generation: int
    created_at: float = field(default_factory=time.time)
    application: str = ""
    window_id: str = ""
    window_title: str = ""
    bounds: Optional[Bounds] = None
    mode: str = "som"                 # vision | ax | som | ocr
    elements: list[SnapshotElement] = field(default_factory=list)
    text: str = ""                    # bounded, model-facing rendering
    image_path: Optional[str] = None
    ax_digest: str = ""
    ocr_digest: str = ""
    screen_digest: str = ""
    full_path: Optional[str] = None
    truncated: bool = False
    chars: int = 0
    # How the frame was obtained: print_window | screen_grab (empty with no frame).
    capture_method: str = ""
    # True when the security layer flagged sensitive content (password/API
    # key/2FA/…). A sensitive snapshot must never be sent to a cloud vision model
    # and its persisted image is masked.
    sensitive: bool = False
    security: Optional[dict[str, Any]] = None

    def element(self, ref: str) -> Optional[SnapshotElement]:
        key = str(ref).lstrip("#")
        for element in self.elements:
            if element.ref == key:
                return element
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id, "generation": self.generation,
            "application": self.application, "windowId": self.window_id,
            "windowTitle": self.window_title, "mode": self.mode,
            "elementCount": len(self.elements),
            "interactiveCount": sum(1 for e in self.elements if e.role),
            "imagePath": self.image_path, "truncated": self.truncated,
            "chars": self.chars, "createdAt": self.created_at,
            "captureMethod": self.capture_method,
        }


# ── Grounded target ──────────────────────────────────────────────────────────


@dataclass
class Target:
    """The result of grounding an intent to a concrete on-screen target."""

    description: str
    method: str                       # accessibility | semantic | structured | ocr | vision | relative_coordinate | coordinate
    confidence: float
    bounds: Optional[Bounds] = None
    application: str = ""
    window_id: str = ""
    snapshot_id: str = ""
    element_ref: Optional[str] = None
    role: str = ""
    name: str = ""
    expected_semantics: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    @property
    def point(self) -> Optional[tuple[int, int]]:
        return self.bounds.center if self.bounds else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.description, "method": self.method,
            "confidence": round(self.confidence, 2),
            "bounds": self.bounds.to_list() if self.bounds else None,
            "application": self.application, "windowId": self.window_id,
            "snapshotId": self.snapshot_id, "elementRef": self.element_ref,
            "role": self.role, "name": self.name,
            "expectedSemantics": self.expected_semantics,
            "alternatives": self.alternatives,
        }


# ── Actions ──────────────────────────────────────────────────────────────────


@dataclass
class ActionResult:
    """The outcome of an action, always carrying a verdict for the agent."""

    action: str
    success: bool
    verdict: str
    delivery_mode: str = "background"
    target: Optional[dict[str, Any]] = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: Optional[dict[str, Any]] = None
    state: str = "ACTION_EXECUTED"
    observed_change: bool = False
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    error: Optional[dict[str, Any]] = None
    snapshot: Optional[dict[str, Any]] = None
    # The ordered recovery strategies appropriate to a failure, so the agent
    # follows the ladder instead of blindly repeating (never a second engine).
    recovery_plan: list[str] = field(default_factory=list)
    # An UNRELATED dialog/modal that appeared after the action (a save-as prompt,
    # an update nag, a permission denial). The agent must decide, not type blind.
    unexpected_dialog: Optional[dict[str, Any]] = None
    # What the bounded recovery ladder actually did (reobserve → retry), when a
    # failed verification triggered it.
    recovery: Optional[dict[str, Any]] = None
    # A workflow candidate captured after a verified success (automatic
    # procedural memory). Stored only when auto-capture is enabled.
    workflow: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "success": self.success, "verdict": self.verdict,
            "deliveryMode": self.delivery_mode, "target": self.target,
            "evidence": self.evidence, "verification": self.verification,
            "state": self.state, "observedChange": self.observed_change,
            "summary": self.summary, "warnings": self.warnings,
            "durationMs": self.duration_ms, "error": self.error, "snapshot": self.snapshot,
            "recoveryPlan": self.recovery_plan, "unexpectedDialog": self.unexpected_dialog,
            "recovery": self.recovery, "workflow": self.workflow,
        }


# ── Financial screen state ───────────────────────────────────────────────────


@dataclass
class ObservedFinancialState:
    """Screen-derived financial state. NOT authoritative market data.

    Carries explicit provenance so the evidence layer never confuses a reading
    off a screen with a provider/official filing.
    """

    application: str = ""
    window: str = ""
    symbol: Optional[str] = None
    fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source_type: str = "desktop_screen"
    timestamp: float = field(default_factory=time.time)
    capture_id: str = ""
    extraction_method: str = "ocr"

    def provenance(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "application": self.application,
            "window": self.window,
            "timestamp": self.timestamp,
            "capture_id": self.capture_id,
            "confidence": round(self.confidence, 2),
            "extraction_method": self.extraction_method,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application, "window": self.window, "symbol": self.symbol,
            "fields": self.fields, "confidence": round(self.confidence, 2),
            "provenance": self.provenance(),
        }


__all__ = [
    "AXNode",
    "ActionResult",
    "ApplicationInfo",
    "Bounds",
    "Capabilities",
    "DRIVER_CONTRACT_MAJOR",
    "DRIVER_CONTRACT_MINOR",
    "DisplayInfo",
    "DriverContract",
    "ObservedFinancialState",
    "Point",
    "Snapshot",
    "SnapshotElement",
    "Target",
    "WindowInfo",
]
