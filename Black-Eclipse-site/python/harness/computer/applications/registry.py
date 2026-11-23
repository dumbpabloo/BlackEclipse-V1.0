"""Adapter selection."""

from __future__ import annotations

from typing import Optional

from .adapters import ADAPTERS
from .base import ApplicationAdapter, GenericAdapter


class AdapterRegistry:
    def __init__(self, adapters: tuple[ApplicationAdapter, ...] = ADAPTERS) -> None:
        self._adapters = adapters
        self._generic = next((a for a in adapters if isinstance(a, GenericAdapter)), GenericAdapter())

    def register(self, adapter: Optional[ApplicationAdapter]) -> None:
        """Add a learned adapter AHEAD of the shipped/generic ones.

        A learned adapter describes an app Eclipse actually drove; it is more
        specific than the generic fallback and may extend a shipped adapter.
        """
        if adapter is None or isinstance(adapter, GenericAdapter):
            return
        existing = self.by_id(adapter.id)
        if existing is not None:
            self._adapters = tuple(a for a in self._adapters if a.id != adapter.id)
        # Insert BEFORE the generic fallback but AFTER the shipped adapters: a
        # hand-written adapter is more complete than a few learned controls, while
        # an app with no shipped adapter gets the learned one instead of generic.
        generic_index = next(
            (i for i, a in enumerate(self._adapters) if isinstance(a, GenericAdapter)),
            len(self._adapters),
        )
        self._adapters = (
            *self._adapters[:generic_index], adapter, *self._adapters[generic_index:],
        )

    def select(self, *, app_name: str = "", executable: str = "", title: str = "") -> ApplicationAdapter:
        for adapter in self._adapters:
            if isinstance(adapter, GenericAdapter):
                continue
            if adapter.matches(app_name=app_name, executable=executable, title=title):
                return adapter
        return self._generic

    def for_app(self, app: dict) -> ApplicationAdapter:
        return self.select(
            app_name=str(app.get("name") or ""),
            executable=str(app.get("executable") or ""),
            title=str(app.get("title") or ""),
        )

    def by_id(self, adapter_id: str) -> Optional[ApplicationAdapter]:
        for adapter in self._adapters:
            if adapter.id == adapter_id:
                return adapter
        return None

    def describe(self) -> list[dict]:
        return [a.describe() for a in self._adapters]


def build_adapter_registry() -> AdapterRegistry:
    return AdapterRegistry()


__all__ = ["AdapterRegistry", "build_adapter_registry"]
