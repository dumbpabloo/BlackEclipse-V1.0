"""Declarative policy stores used by evolution adapters.

They are ordinary kernel services: an entry is data with an exact undo. They do
not replace the runtime policy authority; they are the catalog an operator or a
later wiring step can consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolPolicyEntry:
    scope: str
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


class ToolPolicyService:
    def __init__(self) -> None:
        self._entries: dict[str, ToolPolicyEntry] = {}

    def get(self, scope: str) -> Optional[ToolPolicyEntry]:
        return self._entries.get(scope)

    def set(self, scope: str, *, allow: list[str], deny: list[str]) -> Callable[[], None]:
        previous = self._entries.get(scope)
        entry = ToolPolicyEntry(scope=scope, allow=list(allow), deny=list(deny))
        self._entries[scope] = entry

        def undo() -> None:
            if self._entries.get(scope) is entry:
                if previous is None:
                    self._entries.pop(scope, None)
                else:
                    self._entries[scope] = previous

        return undo

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"scope": entry.scope, "allow": list(entry.allow), "deny": list(entry.deny)}
            for entry in self._entries.values()
        ]


@dataclass
class ModelRouteEntry:
    agent_id: str
    provider: str
    model: str


class ModelRouteService:
    def __init__(self) -> None:
        self._routes: dict[str, ModelRouteEntry] = {}

    def get(self, agent_id: str) -> Optional[ModelRouteEntry]:
        return self._routes.get(agent_id)

    def set(self, agent_id: str, *, provider: str, model: str) -> Callable[[], None]:
        previous = self._routes.get(agent_id)
        entry = ModelRouteEntry(agent_id=agent_id, provider=provider, model=model)
        self._routes[agent_id] = entry

        def undo() -> None:
            if self._routes.get(agent_id) is entry:
                if previous is None:
                    self._routes.pop(agent_id, None)
                else:
                    self._routes[agent_id] = previous

        return undo

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"agentId": entry.agent_id, "provider": entry.provider, "model": entry.model}
            for entry in self._routes.values()
        ]


class CompositionRowStore:
    """A row catalog that can be bound to the live kernel supervisor.

    With no supervisor it is a metadata catalog. Once attached, `set` schedules
    the row's mount on the running loop and its undo schedules removal, so an
    evolution candidate reaches the same supervisor that owns profile rows.
    """

    def __init__(self, supervisor: Any = None) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._supervisor: Any = supervisor

    def attach(self, supervisor: Any) -> None:
        self._supervisor = supervisor

    def get(self, row_id: str) -> Optional[dict[str, Any]]:
        return self._rows.get(row_id)

    def set(self, row_id: str, row: dict[str, Any]) -> Callable[[], None]:
        previous = self._rows.get(row_id)
        self._rows[row_id] = dict(row)
        self._schedule("add", dict(row))

        def undo() -> None:
            if self._rows.get(row_id) == row:
                if previous is None:
                    self._rows.pop(row_id, None)
                else:
                    self._rows[row_id] = previous
            self._schedule("remove", row_id)

        return undo

    def _schedule(self, action: str, value: Any) -> None:
        if self._supervisor is None:
            return
        import asyncio  # noqa: PLC0415

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("a live composition row needs a running event loop") from exc
        if action == "add":
            from ..composition import Row  # noqa: PLC0415

            row = value
            payload = Row(
                id=str(row.get("id")),
                name=str(row.get("name")),
                config=row.get("config"),
                disabled=bool(row.get("disabled", False)),
                group=bool(row.get("group", False)),
                isolate=tuple(row.get("isolate") or ()),
            )
            loop.create_task(self._supervisor.add(payload))
        else:
            loop.create_task(self._supervisor.remove(str(value)))

    def catalog(self) -> list[dict[str, Any]]:
        return list(self._rows.values())

class PolicyService:
    """The kernel-facing view of the ONE policy authority.

    Rendering/allowlist decisions still come from ``harness.code.policy``; this
    service exposes them under the kernel contract so a consumer can delegate
    through the runtime without importing the authority directly. It holds no
    mutable policy of its own (tool/model overlays stay in their own stores).
    """

    def autonomy_profiles(self) -> tuple[str, ...]:
        from ...code.policy import AUTONOMY_PROFILES  # noqa: PLC0415

        return tuple(AUTONOMY_PROFILES)

    def normalize_autonomy(self, value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text if text in self.autonomy_profiles() else None

    def allowed_tools(self, autonomy: str, declared: tuple[str, ...]) -> tuple[str, ...]:
        from ...code.policy import allowed_tools  # noqa: PLC0415

        return tuple(allowed_tools(autonomy, declared))

    def code_grants(self, autonomy: str) -> frozenset[str]:
        from ...code.policy import code_grants  # noqa: PLC0415

        return frozenset(code_grants(autonomy))

    def describe(self) -> dict[str, Any]:
        return {"autonomyProfiles": list(self.autonomy_profiles())}
