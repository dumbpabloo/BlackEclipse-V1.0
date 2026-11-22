"""Scoped tool views and the tool service.

The process keeps ONE global ``ToolRegistry`` (native + MCP + plugin tools).
A scope adds a private overlay: entries registered through the scope are visible
only to that scope and its children, may shadow a global name, and are removed
when the scope is disposed. With no local entries the view is indistinguishable
from the global registry - that is what makes the Phase 3 wiring safe.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Optional

from ...tools import ToolDefinition, ToolRegistry


class _MergedTools:
    """A read-only, nearest-first view over several tool dictionaries."""

    def __init__(self, layers: list[dict[str, ToolDefinition]]) -> None:
        self._layers = layers

    def get(self, name: str, default: Any = None) -> Any:
        for layer in self._layers:
            if name in layer:
                return layer[name]
        return default

    def __getitem__(self, name: str) -> ToolDefinition:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __setitem__(self, name: str, value: ToolDefinition) -> None:
        # Registration goes through ScopedToolRegistry.register_local; a direct
        # write would silently mutate only the nearest layer.
        raise TypeError("a merged tool view is read-only; use register_local")

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for layer in self._layers:
            for name in layer:
                if name not in seen:
                    seen.add(name)
                    yield name

    def __contains__(self, name: object) -> bool:
        return self.get(str(name)) is not None

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def keys(self) -> list[str]:
        return list(self)

    def values(self) -> list[ToolDefinition]:
        return [self.get(name) for name in self]

    def items(self) -> list[tuple[str, ToolDefinition]]:
        return [(name, self.get(name)) for name in self]


class ScopedToolRegistry(ToolRegistry):
    """A ``ToolRegistry`` view with a local overlay and a parent chain.

    It is a real ``ToolRegistry`` subclass, so the existing execution pipeline
    (validation, permissions, timeouts) runs unchanged. Only discovery differs:
    ``_tools`` is a merged mapping rather than a mutable dict.
    """

    def __init__(
        self,
        base: ToolRegistry,
        *,
        key: str = "",
        parent: Optional["ScopedToolRegistry"] = None,
    ) -> None:
        self._base = base
        self._key = key
        self._parent = parent
        self._local: dict[str, ToolDefinition] = {}

    @property
    def _tools(self) -> _MergedTools:  # type: ignore[override]
        layers: list[dict[str, ToolDefinition]] = []
        current: Optional[ScopedToolRegistry] = self
        while current is not None:
            layers.append(current._local)
            current = current._parent
        layers.append(self._base._tools)  # noqa: SLF001 - the ONE global registry
        return _MergedTools(layers)

    @property
    def key(self) -> str:
        return self._key

    # -- registration ---------------------------------------------------------

    def register(self, tool: ToolDefinition) -> None:
        self.register_local(tool)

    def register_local(self, tool: ToolDefinition, *, shadow: bool = True) -> Callable[[], None]:
        if not tool.name or not tool.name.replace("_", "").isalnum():
            raise ValueError(f"Invalid tool name: {tool.name!r}")
        if tool.name in self._local:
            raise ValueError(f"Tool {tool.name!r} is already registered in this scope")
        if not shadow and self._base.get(tool.name) is not None:
            raise ValueError(f"Tool {tool.name!r} is already registered globally")
        self._local[tool.name] = tool

        def dispose() -> None:
            if self._local.get(tool.name) is tool:
                self._local.pop(tool.name, None)

        return dispose

    def unregister(self, name: str) -> bool:
        return self._local.pop(name, None) is not None

    # -- discovery ------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def list(self) -> list[ToolDefinition]:
        return self._tools.values()

    async def execute(self, name: str, call_id: str, arguments: dict[str, Any],
                      ctx: Any, **kwargs: Any) -> Any:
        """Execute locally-owned tools here, base tools through their owner.

        Delegating a base tool keeps the base registry's own overrides intact
        (the evaluation registry records names/args in `execute`). Without this,
        a scoped view would execute base tools itself and silently bypass that
        instrumentation.
        """
        current: Optional["ScopedToolRegistry"] = self
        while current is not None:
            if name in current._local:
                return await super().execute(name, call_id, arguments, ctx, **kwargs)
            current = current._parent
        base = self._base
        return await base.execute(name, call_id, arguments, ctx, **kwargs)

    def schemas(self, allowed: Optional[set[str]] = None) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.input_schema}
            for t in self.list()
            if allowed is None or t.name in allowed
        ]

    def langchain_tools(self, allowed: Optional[set[str]] = None):
        from ...tools import _args_model  # noqa: PLC0415

        tools = []
        for tool in self.list():
            if allowed is not None and tool.name not in allowed:
                continue
            tools.append(self._as_langchain(tool, _args_model(tool.name, tool.input_schema)))
        return tools


class ToolService:
    """Per-run / per-agent tool views over the ONE registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._views: dict[str, ScopedToolRegistry] = {}

    def view(self, key: str, *, parent: Optional[ScopedToolRegistry] = None) -> ScopedToolRegistry:
        cache_key = f"{key}:{id(parent)}"
        existing = self._views.get(cache_key)
        if existing is not None:
            return existing
        view = ScopedToolRegistry(self.registry, key=key, parent=parent)
        self._views[cache_key] = view
        return view

    def agent_scope(self, agent_id: str) -> ScopedToolRegistry:
        return self.view(f"agent:{agent_id}")

    def run_scope(self, run_id: str, *, agent_id: Optional[str] = None) -> ScopedToolRegistry:
        parent = self.agent_scope(agent_id) if agent_id else None
        return self.view(f"run:{run_id}", parent=parent)

    def register(self, scope: ScopedToolRegistry, tool: ToolDefinition) -> Callable[[], None]:
        return scope.register_local(tool)

    def reset(self) -> None:
        self._views.clear()
