"""Kernel UI slots: declarative contributions with a server-side renderer.

A slot entry is a handler returning a declarative view spec (markdown/keyvalue/
table/list/status) plus display metadata. The client may render it; the server
can render it for inspection/tests. No HTML or DOM is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

SLOTS = ("chat.sidebar", "chat.toolbar", "chat.composer", "settings.section",
         "dashboard.widget", "plugins.panel", "command.palette")


@dataclass
class SlotEntry:
    slot: str
    id: str
    title: str
    plugin_id: str
    handler: Any


class UISlotService:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, SlotEntry]] = {}

    def register(
        self,
        slot: str,
        entry_id: str,
        handler: Any,
        *,
        title: str = "",
        plugin_id: str = "",
        replace: bool = False,
    ) -> Callable[[], None]:
        if not slot or not entry_id:
            raise ValueError("a UI slot needs a slot and id")
        layer = self._entries.setdefault(slot, {})
        existing = layer.get(entry_id)
        if existing is not None and not (replace and existing.plugin_id == plugin_id):
            raise ValueError(f"slot {slot!r} already has {entry_id!r}")
        previous = layer.get(entry_id)
        entry = SlotEntry(slot=slot, id=entry_id, title=title, plugin_id=plugin_id, handler=handler)
        layer[entry_id] = entry

        def dispose() -> None:
            if layer.get(entry_id) is entry:
                if previous is None:
                    layer.pop(entry_id, None)
                else:
                    layer[entry_id] = previous

        return dispose

    def slots(self) -> list[str]:
        return sorted(self._entries)

    def entries(self, slot: Optional[str] = None) -> list[dict[str, Any]]:
        selected = [slot] if slot else list(self._entries)
        return [
            {"slot": item.slot, "id": item.id, "title": item.title, "pluginId": item.plugin_id}
            for name in selected
            for item in self._entries.get(name, {}).values()
        ]

    def render(self, slot: str, entry_id: str, ctx: Any = None) -> dict[str, Any]:
        entry = self._entries.get(slot, {}).get(entry_id)
        if entry is None:
            raise KeyError(f"unknown slot entry {slot}/{entry_id}")
        handler = entry.handler
        value = handler(ctx) if ctx is not None else handler()
        if isinstance(value, dict):
            return value
        return {"type": "markdown", "text": str(value)}


def slot_for_ui_kind(kind: str) -> str:
    return {
        "panel": "plugins.panel",
        "sidebar": "chat.sidebar",
        "widget": "dashboard.widget",
        "settings": "settings.section",
        "command_palette": "command.palette",
        "dashboard_card": "dashboard.widget",
        "page": "plugins.panel",
    }.get(kind, "plugins.panel")
