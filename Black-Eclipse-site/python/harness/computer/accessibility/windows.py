"""The Windows accessibility provider.

Two tiers, in order of preference and honesty:

  1. **UI Automation** via the `uiautomation` package when it is installed. This
     yields a rich, semantic tree (roles, names, values, bounds, patterns) — the
     ideal first rung of the grounding hierarchy.
  2. **Win32 child-window enumeration** as the always-available baseline. Many
     legacy/standard controls ARE windows, so this still yields real handles with
     text and class-derived roles without any third-party package. It is weaker
     than UIA (modern custom-drawn toolkits expose nothing), which is reported
     honestly rather than faked.

`build_ax_provider` returns the best available tier, or the null provider.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any, Optional

from ..models import AXNode, Bounds
from .base import AXProvider, looks_interactive
from .null import NullAXProvider

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # pragma: no cover
    user32 = None  # type: ignore[assignment]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


# Class-name substring → role. A heuristic, but a tested and observable one.
_CLASS_ROLE_HINTS: tuple[tuple[str, str], ...] = (
    ("button", "button"),
    ("edit", "textbox"),
    ("combobox", "combobox"),
    ("listbox", "listbox"),
    ("syslistview", "list"),
    ("systreeview", "tree"),
    ("systabcontrol", "tab"),
    ("static", "text"),
    ("checkbox", "checkbox"),
    ("radiobutton", "radio"),
    ("msctls_trackbar", "slider"),
    ("msctls_updown", "spinbutton"),
    ("msctls_progress", "progressbar"),
    ("msctls_statusbar", "statusbar"),
    ("toolbarwindow", "toolbar"),
    ("menuitem", "menuitem"),
    ("rich edit", "textbox"),
    ("richedit", "textbox"),
    ("scintilla", "textbox"),
    ("document", "document"),
    ("internet explorer_serverwnd", "document"),
    ("chrome_renderwidgethost", "document"),
    ("grid", "grid"),
)


def _role_from_class(class_name: str) -> str:
    lowered = (class_name or "").lower()
    for hint, role in _CLASS_ROLE_HINTS:
        if hint in lowered:
            return role
    return "pane"


class Win32AXProvider(AXProvider):
    """The always-available baseline: enumerate child windows as AX nodes."""

    id = "win32"
    available = _IS_WINDOWS
    tier = "baseline"

    def tree(self, window_id: Optional[str] = None) -> Optional[AXNode]:
        if not _IS_WINDOWS or not window_id:
            return None
        try:
            hwnd = int(window_id)
        except ValueError:
            return None
        root = AXNode(role="window", name=self._window_text(hwnd), bounds=self._window_bounds(hwnd), native_handle=hwnd)
        children = self._children(hwnd, depth=0)
        root.children = children
        return root

    def _children(self, parent: int, depth: int) -> list[AXNode]:
        if depth > 6:
            return []
        out: list[AXNode] = []

        EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: Any, _data: Any) -> int:
            try:
                text = self._window_text(hwnd)
                class_name = self._class_name(hwnd)
                role = _role_from_class(class_name)
                bounds = self._screen_bounds(hwnd)
                enabled = bool(user32.IsWindowEnabled(hwnd))
                visible = bool(user32.IsWindowVisible(hwnd))
                if not visible and not text:
                    return 1
                node = AXNode(
                    role=role, name=text, bounds=bounds, enabled=enabled,
                    interactive=looks_interactive(role), native_handle=int(hwnd),
                    states=[s for s, present in (("disabled", not enabled),) if present],
                )
                node.children = self._children(int(hwnd), depth + 1)
                out.append(node)
            except Exception:  # noqa: BLE001
                pass
            return 1

        try:
            user32.EnumChildWindows(parent, EnumChildProc(_cb), 0)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _window_text(self, hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _window_bounds(self, hwnd: int) -> Optional[Bounds]:
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return Bounds.from_ltrb(rect.left, rect.top, rect.right, rect.bottom)
        return None

    def _screen_bounds(self, hwnd: int) -> Optional[Bounds]:
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return Bounds.from_ltrb(rect.left, rect.top, rect.right, rect.bottom)
        return None


class UiaAXProvider(AXProvider):
    """The rich tier: UI Automation via the `uiautomation` package."""

    id = "uia"
    available = False
    tier = "uia"

    def __init__(self) -> None:
        try:
            import uiautomation as auto  # noqa: PLC0415

            self._auto = auto
            self.available = True
        except Exception:  # noqa: BLE001 - an absent optional dep is honest
            self._auto = None
            self.available = False

    def tree(self, window_id: Optional[str] = None) -> Optional[AXNode]:
        if not self.available or not window_id:
            return None
        try:
            handle = int(window_id)
            control = self._auto.ControlFromHandle(handle)
            if control is None:
                return None
            return self._convert(control, depth=0)
        except Exception:  # noqa: BLE001
            return None

    def _convert(self, control: Any, depth: int) -> Optional[AXNode]:
        if depth > 12 or control is None:
            return None
        try:
            rect = control.BoundingRectangle
            bounds = Bounds(rect.left, rect.top, max(0, rect.right - rect.left), max(0, rect.bottom - rect.top))
        except Exception:  # noqa: BLE001
            bounds = None
        try:
            role = control.ControlTypeName or "control"
            role = role[:-7].lower() if role.endswith("Control") else role.lower()
        except Exception:  # noqa: BLE001
            role = "generic"
        try:
            name = control.Name or ""
        except Exception:  # noqa: BLE001
            name = ""
        value = ""
        try:
            pattern = control.GetValuePattern()
            value = pattern.Value or ""
        except Exception:  # noqa: BLE001
            value = ""
        enabled = True
        try:
            enabled = bool(control.IsEnabled)
        except Exception:  # noqa: BLE001
            pass
        node = AXNode(
            role=role, name=name, value=value, bounds=bounds, enabled=enabled,
            interactive=looks_interactive(role), native_handle=control,
        )
        try:
            for child in control.GetChildren():
                converted = self._convert(child, depth + 1)
                if converted is not None:
                    node.children.append(converted)
        except Exception:  # noqa: BLE001
            pass
        return node

    # ── pattern activation ───────────────────────────────────────────────────

    def activate(self, *, window_id: Optional[str] = None, name: str = "",
                 role: str = "", bounds: Any = None, value: str = "") -> bool:
        """Invoke/Toggle/Select/SetValue a UIA control without a mouse.

        Matching is by accessible name (exact first, then containment) and, when
        supplied, by the control's centre point so two same-named buttons in
        different rows are not confused.
        """
        if not self.available or not window_id:
            return False
        try:
            handle = int(window_id)
            root = self._auto.ControlFromHandle(handle)
        except Exception:  # noqa: BLE001
            return False
        if root is None:
            return False
        needle = (name or "").strip().lower()
        if not needle:
            return False
        centre = None
        if bounds is not None:
            try:
                centre = bounds.center
            except Exception:  # noqa: BLE001
                centre = None
        match = self._find_control(root, needle, centre, depth=0)
        if match is None:
            return False
        try:
            if value:
                pattern = match.GetValuePattern()
                if pattern is not None:
                    pattern.SetValue(str(value))
                    return True
        except Exception:  # noqa: BLE001 - no value pattern
            pass
        for getter, call in (
            ("GetInvokePattern", "Invoke"),
            ("GetTogglePattern", "Toggle"),
            ("GetSelectionItemPattern", "Select"),
            ("GetExpandCollapsePattern", "Expand"),
        ):
            try:
                pattern = getattr(match, getter)()
                if pattern is not None:
                    getattr(pattern, call)()
                    return True
            except Exception:  # noqa: BLE001 - try the next pattern
                continue
        return False

    def focus(self, *, window_id: Optional[str] = None, name: str = "",
              role: str = "", bounds: Any = None) -> bool:
        """Set keyboard focus on a UIA control (no mouse, works when obscured)."""
        if not self.available or not window_id or not name:
            return False
        try:
            root = self._auto.ControlFromHandle(int(window_id))
        except Exception:  # noqa: BLE001
            return False
        if root is None:
            return False
        centre = None
        if bounds is not None:
            try:
                centre = bounds.center
            except Exception:  # noqa: BLE001
                centre = None
        match = self._find_control(root, name.strip().lower(), centre, depth=0)
        if match is None:
            return False
        try:
            match.SetFocus()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _find_control(self, control: Any, needle: str, centre: Any, *, depth: int) -> Any:
        if depth > 12 or control is None:
            return None
        try:
            label = (control.Name or "").strip().lower()
        except Exception:  # noqa: BLE001
            label = ""
        if label and (label == needle or needle in label):
            if centre is None:
                return control
            try:
                rect = control.BoundingRectangle
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                if abs(cx - centre[0]) <= 8 and abs(cy - centre[1]) <= 8:
                    return control
            except Exception:  # noqa: BLE001
                return control
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            return None
        for child in children:
            found = self._find_control(child, needle, centre, depth=depth + 1)
            if found is not None:
                return found
        return None


def build_ax_provider() -> AXProvider:
    """The best accessibility provider available on this machine."""
    if not _IS_WINDOWS:
        return NullAXProvider("accessibility enumeration is only implemented for Windows")
    uia = UiaAXProvider()
    if uia.available:
        return uia
    baseline = Win32AXProvider()
    if baseline.available:
        return baseline
    return NullAXProvider()


__all__ = ["UiaAXProvider", "Win32AXProvider", "build_ax_provider"]
