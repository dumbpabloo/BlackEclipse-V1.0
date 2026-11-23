"""The platform-independent `ComputerDriver` interface.

The core NEVER imports Win32 / AX / X11 / Wayland. Everything the platform
provides is expressed through this interface; `computer/drivers/<platform>.py`
implements it. The public Eclipse API stays stable across drivers.

A driver reports its `contract()` and `capabilities()`; an action it cannot
perform raises `UnsupportedAction` rather than pretending. Input may be delivered
in one of two modes: `background` (do not touch the user's cursor/focus) or
`foreground` (briefly raise/focus — a higher-impact action the core escalates
explicitly). A driver that only supports foreground reports it honestly.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import (
    AXNode,
    ApplicationInfo,
    Bounds,
    Capabilities,
    DisplayInfo,
    DriverContract,
    WindowInfo,
)


class ScreenFrame:
    """A captured image plus its geometry. The bytes are PNG when encoded."""

    __slots__ = ("data", "width", "height", "bounds", "png_bytes", "scale", "method")

    def __init__(
        self,
        *,
        data: Any = None,
        width: int = 0,
        height: int = 0,
        bounds: Optional[Bounds] = None,
        png_bytes: Optional[bytes] = None,
        scale: float = 1.0,
        method: str = "",
    ) -> None:
        self.data = data              # a PIL.Image when available, else None
        self.width = width
        self.height = height
        self.bounds = bounds or Bounds(0, 0, width, height)
        self.png_bytes = png_bytes
        self.scale = scale
        # print_window | screen_grab ("" when unknown).
        self.method = method

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height,
            "bounds": self.bounds.to_dict(), "scale": self.scale,
            "hasBytes": self.png_bytes is not None, "method": self.method,
        }


class InputResult:
    """What a driver returns from a delivered input event."""

    __slots__ = ("delivered", "delivery_mode", "detail", "blocked_reason")

    def __init__(
        self,
        *,
        delivered: bool,
        delivery_mode: str = "background",
        detail: str = "",
        blocked_reason: Optional[str] = None,
    ) -> None:
        self.delivered = delivered
        self.delivery_mode = delivery_mode
        self.detail = detail
        self.blocked_reason = blocked_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered, "deliveryMode": self.delivery_mode,
            "detail": self.detail, "blockedReason": self.blocked_reason,
        }


class ComputerDriver:
    """Base driver. Every method is honest: unimplemented ⇒ UnsupportedAction."""

    id = "base"
    label = "Base driver"
    platform = "unknown"

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:  # pragma: no cover - overridden
        return None

    async def stop(self) -> None:  # pragma: no cover - overridden
        return None

    def health(self) -> dict[str, Any]:
        return {"driver": self.id, "healthy": True, "platform": self.platform}

    def contract(self) -> DriverContract:
        return DriverContract(platform=self.platform, name=self.id)

    def capabilities(self) -> Capabilities:
        return Capabilities(platform=self.platform)

    # ── displays ─────────────────────────────────────────────────────────────

    def list_displays(self) -> list[DisplayInfo]:
        return [DisplayInfo(id="primary", bounds=Bounds(), working_area=Bounds(), primary=True)]

    def display_of_point(self, x: int, y: int) -> Optional[DisplayInfo]:
        for display in self.list_displays():
            if display.bounds.contains(x, y):
                return display
        return None

    # ── windows ──────────────────────────────────────────────────────────────

    def list_windows(self) -> list[WindowInfo]:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot enumerate windows.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        for window in self.list_windows():
            if window.id == window_id:
                return window
        return None

    async def focus_window(self, window_id: str, *, foreground: bool = False) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot focus windows.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def window_control(self, window_id: str, operation: str, **kwargs: Any) -> dict[str, Any]:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction(f"This platform driver cannot {operation} a window.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    # ── applications ─────────────────────────────────────────────────────────

    def list_applications(self) -> list[ApplicationInfo]:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot enumerate applications.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def launch_application(self, target: str, *, args: list[str] | None = None) -> ApplicationInfo:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot launch applications.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def close_application(self, app_id: str) -> dict[str, Any]:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot close applications.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    # ── capture ──────────────────────────────────────────────────────────────

    async def capture(
        self,
        *,
        window_id: Optional[str] = None,
        region: Optional[Bounds] = None,
        display_id: Optional[str] = None,
    ) -> ScreenFrame:
        from ..errors import ScreenCaptureUnavailable  # noqa: PLC0415

        raise ScreenCaptureUnavailable("This platform driver cannot capture the screen.", code="COMPUTER_SCREEN_CAPTURE_UNAVAILABLE")

    # ── accessibility ────────────────────────────────────────────────────────

    def accessibility_tree(self, window_id: Optional[str] = None) -> Optional[AXNode]:
        return None

    # ── input ────────────────────────────────────────────────────────────────

    async def click(
        self, x: int, y: int, *, button: str = "left", count: int = 1,
        delivery_mode: str = "background", window_id: Optional[str] = None,
    ) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot click.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def mouse_down(self, x: int, y: int, *, button: str = "left", delivery_mode: str = "background",
                         window_id: Optional[str] = None) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot press the mouse.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def mouse_up(self, x: int, y: int, *, button: str = "left", delivery_mode: str = "background",
                       window_id: Optional[str] = None) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot release the mouse.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def move(self, x: int, y: int, *, delivery_mode: str = "background",
                   window_id: Optional[str] = None) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot move the pointer.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def scroll(self, *, dx: int = 0, dy: int = 0, at: Optional[tuple[int, int]] = None,
                     delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot scroll.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left", delivery_mode: str = "background",
                   window_id: Optional[str] = None) -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot drag.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def type_text(self, text: str, *, delivery_mode: str = "background") -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot type.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def key(self, keys: list[str], *, delivery_mode: str = "background") -> InputResult:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot send keys.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    async def set_value(self, bounds: Bounds, value: str, *, window_id: Optional[str] = None) -> dict[str, Any]:
        from ..errors import UnsupportedAction  # noqa: PLC0415

        raise UnsupportedAction("This platform driver cannot set a value directly.", code="COMPUTER_CAPABILITY_UNSUPPORTED")

    # ── clipboard ────────────────────────────────────────────────────────────

    async def get_clipboard(self) -> Optional[str]:
        """The clipboard's text, or None when the platform cannot read it."""
        return None

    async def set_clipboard(self, text: str) -> bool:
        """Replace the clipboard's text. False when unsupported or refused."""
        return False

    async def key_down(self, key: str, *, delivery_mode: str = "background",
                       window_id: Optional[str] = None) -> Any:
        """Press and HOLD a key (used for modifier-clicks). Best-effort."""
        return await self.key([key], delivery_mode=delivery_mode, window_id=window_id)

    async def key_up(self, key: str, *, delivery_mode: str = "background",
                     window_id: Optional[str] = None) -> Any:
        """Release a held key."""
        return await self.key([key], delivery_mode=delivery_mode, window_id=window_id)

    def pointer_position(self) -> Optional[tuple[int, int]]:
        return None


__all__ = ["ComputerDriver", "InputResult", "ScreenFrame"]
