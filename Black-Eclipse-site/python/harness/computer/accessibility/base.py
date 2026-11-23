"""The accessibility provider interface and shared heuristics.

An `AXProvider` turns one window into an `AXNode` tree (or None when it cannot).
`INTERACTIVE_ROLES` and `looks_interactive` are shared so the snapshot engine
marks the same roles across platforms.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import AXNode

# Roles a person can act on. Used to mark snapshot elements as interactive.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "pushbutton", "link", "hyperlink", "textbox", "edit", "textfield",
    "checkbox", "check box", "radio", "radiobutton", "combobox", "combo box",
    "listbox", "list", "listitem", "menuitem", "menu", "option", "searchbox",
    "slider", "spinbutton", "spinner", "switch", "tab", "tabitem", "treeitem",
    "tree", "document", "cell", "gridcell", "datagrid", "edit control",
    "header", "menubar", "toolbar", "splitbutton", "dropdown", "text",
})


def looks_interactive(role: str, *, states: Optional[list[str]] = None) -> bool:
    return (role or "").strip().lower() in INTERACTIVE_ROLES


class AXProvider:
    """Base accessibility provider. `available` is the honest capability flag."""

    id = "base"
    available = False

    def tree(self, window_id: Optional[str] = None) -> Optional[AXNode]:
        return None

    def activate(self, *, window_id: Optional[str] = None, name: str = "",
                 role: str = "", bounds: Any = None, value: str = "") -> bool:
        """Activate an element through its accessibility pattern.

        The default is an honest False: the caller then falls back to a real
        click. A provider that supports UIA patterns overrides this.
        """
        return False

    def focus(self, *, window_id: Optional[str] = None, name: str = "",
              role: str = "", bounds: Any = None) -> bool:
        """Give an element keyboard focus without a click. Honest False default."""
        return False

    def describe(self) -> dict[str, Any]:
        return {"provider": self.id, "available": self.available}


__all__ = ["AXProvider", "INTERACTIVE_ROLES", "looks_interactive"]
