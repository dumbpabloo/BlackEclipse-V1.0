"""OSWorld driver: run Eclipse's computer-use engine against an OSWorld desktop.

OSWorld is the standard benchmark for desktop agents. Its `DesktopEnv` owns a
VM/container and exposes a controller; Eclipse's engine normally talks to a
local Win32/X11 driver. This adapter maps the SAME `ComputerDriver` contract onto
an OSWorld environment, so the action/observation/verification stack (and the
best-of-N runner) can be evaluated on OSWorld tasks without changing any of it.

The OSWorld API has drifted across releases, so every access is defensive: a
method the environment does not expose is an honest `UnsupportedAction` or a
documented fallback, never a fabricated observation.

Usage (from an OSWorld host):

    from desktop_env.envs.desktop_env import DesktopEnv
    from harness.computer.drivers.osworld import OSWorldDriver
    from harness.computer.manager import ComputerManager

    env = DesktopEnv(provider_name="vmware", action_space="pyautogui", ...)
    manager = ComputerManager(config=...)
    manager.driver = OSWorldDriver(env)
    await manager.start()
    ...

`pyautogui` code is what OSWorld executes, so each input method builds the same
primitive call OSWorld's own agent would emit.
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any, Optional

from ..errors import ScreenCaptureUnavailable, UnsupportedAction
from ..models import (
    ApplicationInfo,
    Bounds,
    Capabilities,
    DisplayInfo,
    DriverContract,
    WindowInfo,
)
from .base import ComputerDriver, InputResult, ScreenFrame


def _decode_screenshot(payload: Any) -> Optional[Any]:
    """bytes/base64/data-URL -> PIL image, or None."""
    try:
        from PIL import Image  # noqa: PLC0415

        if isinstance(payload, str):
            text = payload.split(",", 1)[-1] if payload.startswith("data:") else payload
            payload = base64.b64decode(text)
        if not isinstance(payload, (bytes, bytearray)):
            return None
        return Image.open(io.BytesIO(bytes(payload))).convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def _rect(value: Any) -> Optional[Bounds]:
    if not isinstance(value, dict):
        return None
    try:
        return Bounds(
            int(value.get("x", value.get("left", 0))),
            int(value.get("y", value.get("top", 0))),
            int(value.get("width", 0)),
            int(value.get("height", 0)),
        )
    except (TypeError, ValueError):
        return None


class OSWorldDriver(ComputerDriver):
    id = "osworld"
    label = "OSWorld desktop"
    platform = "osworld"

    def __init__(self, env: Any) -> None:
        self.env = env
        self._last_bounds: Optional[Bounds] = None

    # ── lifecycle / capabilities ─────────────────────────────────────────────

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        # The OSWorld environment's lifecycle belongs to the caller.
        return None

    def contract(self) -> DriverContract:
        return DriverContract(platform="osworld", name="osworld")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            platform="osworld", background_input=False, foreground_input=True,
            screen_capture=True, accessibility=False, multi_monitor=False,
            window_management=False, application_management=False, set_value=False,
            notes=["OSWorld input is foreground pyautogui code; accessibility is not host-visible"],
        )

    def health(self) -> dict[str, Any]:
        return {"driver": self.id, "healthy": self.env is not None, "platform": self.platform}

    # ── displays / windows ───────────────────────────────────────────────────

    def _controller(self) -> Any:
        return getattr(self.env, "controller", None)

    def list_displays(self) -> list[DisplayInfo]:
        width, height = 1920, 1080
        controller = self._controller()
        try:
            size = controller.get_vm_screen_size() if controller is not None else None
            if isinstance(size, dict):
                width = int(size.get("width", width))
                height = int(size.get("height", height))
        except Exception:  # noqa: BLE001 - a size probe is best-effort
            pass
        return [DisplayInfo(id="primary", bounds=Bounds(0, 0, width, height),
                            working_area=Bounds(0, 0, width, height), primary=True)]

    def list_windows(self) -> list[WindowInfo]:
        controller = self._controller()
        display = self.list_displays()[0].bounds
        try:
            raw = controller.get_open_windows() if controller is not None else None
        except Exception:  # noqa: BLE001
            raw = None
        windows: list[WindowInfo] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if isinstance(item, dict):
                    title = str(item.get("title") or item.get("name") or f"window {index}")
                    bounds = _rect(item.get("rect") or item.get("bounds")) or display
                else:
                    title = str(item)
                    bounds = display
                windows.append(WindowInfo(id=str(index), title=title, app="osworld",
                                          pid=1, bounds=bounds, focused=index == 0))
        if not windows:
            windows = [WindowInfo(id="0", title="OSWorld desktop", app="osworld",
                                  pid=1, bounds=display, focused=True)]
        return windows

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        for window in self.list_windows():
            if window.id == str(window_id):
                return window
        return None

    def list_applications(self) -> list[ApplicationInfo]:
        return [ApplicationInfo(id="0", name="osworld", pid=1)]

    # ── capture ──────────────────────────────────────────────────────────────

    async def capture(
        self, *, window_id: Optional[str] = None, region: Optional[Bounds] = None,
        display_id: Optional[str] = None,
    ) -> ScreenFrame:
        controller = self._controller()
        getter = getattr(controller, "get_screenshot", None) if controller is not None else None
        if not callable(getter):
            raise ScreenCaptureUnavailable(
                "This OSWorld environment does not expose a screenshot.",
                code="COMPUTER_SCREEN_CAPTURE_UNAVAILABLE",
            )
        try:
            payload = await asyncio.to_thread(getter)
        except Exception as exc:  # noqa: BLE001
            raise ScreenCaptureUnavailable(
                f"OSWorld screenshot failed: {exc}", code="COMPUTER_SCREEN_CAPTURE_UNAVAILABLE",
            ) from exc
        image = _decode_screenshot(payload)
        if image is None:
            raise ScreenCaptureUnavailable(
                "OSWorld returned a screenshot that is not a decodable image.",
                code="COMPUTER_SCREEN_CAPTURE_UNAVAILABLE",
            )
        bounds = region or self.list_displays()[0].bounds
        if region is not None:
            try:
                left = max(0, region.x - (self.list_displays()[0].bounds.x))
                top = max(0, region.y - (self.list_displays()[0].bounds.y))
                image = image.crop((left, top, left + region.width, top + region.height))
            except Exception:  # noqa: BLE001
                pass
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return ScreenFrame(
            data=image, width=image.width, height=image.height, bounds=bounds,
            png_bytes=buffer.getvalue(), method="osworld",
        )

    # ── input (pyautogui code, the OSWorld action language) ──────────────────

    async def _step(self, code: str, *, delivery_mode: str = "foreground") -> InputResult:
        step = getattr(self.env, "step", None)
        if not callable(step):
            raise UnsupportedAction("This OSWorld environment has no step().")
        try:
            result = step(code, 0.1)
            if asyncio.iscoroutine(result):
                await result
        except TypeError:
            # Some versions take only the action string.
            result = step(code)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedAction(f"OSWorld action failed: {exc}") from exc
        return InputResult(delivered=True, delivery_mode="foreground", detail="osworld pyautogui")

    async def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
                    delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        button_arg = button if button in ("left", "right", "middle") else "left"
        return await self._step(
            f"import pyautogui; pyautogui.click({int(x)}, {int(y)}, button={button_arg!r}, clicks={int(count)})"
        )

    async def mouse_down(self, x: int, y: int, *, button: str = "left",
                         delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.mouseDown({int(x)}, {int(y)}, button={button!r})")

    async def mouse_up(self, x: int, y: int, *, button: str = "left",
                       delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.mouseUp({int(x)}, {int(y)}, button={button!r})")

    async def move(self, x: int, y: int, *, delivery_mode: str = "background",
                   window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.moveTo({int(x)}, {int(y)})")

    async def scroll(self, *, dx: int = 0, dy: int = 0, at: Optional[tuple[int, int]] = None,
                     delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        if at:
            return await self._step(
                f"import pyautogui; pyautogui.moveTo({int(at[0])}, {int(at[1])}); "
                f"pyautogui.scroll({int(dy)})"
            )
        return await self._step(f"import pyautogui; pyautogui.scroll({int(dy)})")

    async def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
                   delivery_mode: str = "background", window_id: Optional[str] = None) -> InputResult:
        return await self._step(
            f"import pyautogui; pyautogui.moveTo({int(x1)}, {int(y1)}); "
            f"pyautogui.dragTo({int(x2)}, {int(y2)}, duration=0.4, button={button!r})"
        )

    async def type_text(self, text: str, *, delivery_mode: str = "background",
                        window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.write({str(text)!r}, interval=0.02)")

    async def key(self, keys: list[str], *, delivery_mode: str = "background",
                  window_id: Optional[str] = None) -> InputResult:
        if not keys:
            return InputResult(delivered=False, delivery_mode="foreground", detail="no keys")
        lowered = [str(k).lower() for k in keys]
        if len(lowered) == 1:
            return await self._step(f"import pyautogui; pyautogui.press({lowered[0]!r})")
        return await self._step(f"import pyautogui; pyautogui.hotkey({', '.join(repr(k) for k in lowered)})")

    async def key_down(self, key: str, *, delivery_mode: str = "background",
                       window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.keyDown({str(key).lower()!r})")

    async def key_up(self, key: str, *, delivery_mode: str = "background",
                     window_id: Optional[str] = None) -> InputResult:
        return await self._step(f"import pyautogui; pyautogui.keyUp({str(key).lower()!r})")

    def accessibility_tree(self, window_id: Optional[str] = None) -> None:
        # OSWorld's accessibility tree is environment-specific and not host
        # visible. Returning None makes the engine use screenshot/OCR honestly.
        return None


__all__ = ["OSWorldDriver"]
