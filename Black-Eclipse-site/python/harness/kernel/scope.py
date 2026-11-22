"""A scoped registry used by tools, prompt sections, skills and hooks.

The kernel's ``Context`` provides services and lifetime. This module provides
the second-order primitive: an ordered set of named contributions where a child
scope sees its ancestors' entries, may shadow a same-named entry, and disposes
its own layer without touching the ancestor. It is deliberately not a sandbox or
an authority boundary - it is a visibility/isolation primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    name: str
    value: T
    scope: Any
    order: int = 100
    owner: Any = None


@dataclass
class ScopedRegistry(Generic[T]):
    """Named entries with nearest-scope precedence and effect-owned removal."""

    _layers: dict[Any, dict[str, _Entry[T]]] = field(default_factory=dict)

    def _chain(self, scope: Any) -> list[Any]:
        chain: list[Any] = []
        current = scope
        seen: set[Any] = set()
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            current = getattr(current, "parent_scope", None)
        return chain

    def register(
        self,
        name: str,
        value: T,
        *,
        scope: Any = None,
        order: int = 100,
        owner: Any = None,
    ) -> Callable[[], None]:
        if not name:
            raise ValueError("a scoped registration needs a name")
        layer = self._layers.setdefault(scope, {})
        if name in layer:
            raise ValueError(f"{name!r} is already registered in this scope")
        entry = _Entry(name=name, value=value, scope=scope, order=order, owner=owner)
        layer[name] = entry

        def dispose() -> None:
            current = self._layers.get(scope)
            if current and current.get(name) is entry:
                current.pop(name, None)
                if not current:
                    self._layers.pop(scope, None)

        return dispose

    def resolve(self, scope: Any = None) -> list[_Entry[T]]:
        """All visible entries, nearest scope first, stable within a layer."""
        merged: dict[str, _Entry[T]] = {}
        for key in self._chain(scope):
            layer = self._layers.get(key, {})
            for name in sorted(layer, key=lambda n: (layer[n].order, n)):
                if name not in merged:
                    merged[name] = layer[name]
        return list(merged.values())

    def get(self, name: str, scope: Any = None) -> Optional[T]:
        for entry in self.resolve(scope):
            if entry.name == name:
                return entry.value
        return None

    def names(self, scope: Any = None) -> list[str]:
        return [entry.name for entry in self.resolve(scope)]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.name,
                "scope": str(entry.scope),
                "order": entry.order,
                "owner": str(entry.owner),
            }
            for layer in self._layers.values()
            for entry in layer.values()
        ]
