"""Adapters for the remaining declarative asset kinds.

Each adapter validates one content shape and returns an exact undo. The stores
are real services (skill catalog, hook registry, memory policy, tool-policy
catalog, model-route catalog, composition-row catalog); a plugin bundle is
deliberately operator-only and cannot be applied by this engine.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..kernel.services.policy import (
    CompositionRowStore,
    ModelRouteService,
    ToolPolicyService,
)
from ..kernel.services.skills import SkillService
from ..plugins.hooks import HOOK_POINTS, HookRegistry
from .assets import AssetError, AssetKind
from .adapters import Undo


class SkillAdapter:
    kind = AssetKind.SKILL

    def __init__(self, skills: SkillService, *, plugin_id: str = "evolution") -> None:
        self.skills = skills
        self.plugin_id = plugin_id

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("instructions") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "a skill needs instructions")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        entry = self.skills.get(target)
        return None if entry is None else {"instructions": entry.instructions, "name": entry.name}

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        return self.skills.register(
            plugin_id=self.plugin_id,
            skill_id=target,
            name=str(content.get("name") or target),
            description=str(content.get("description") or target),
            instructions=str(content["instructions"]),
        )


class HookAdapter:
    kind = AssetKind.HOOK

    def __init__(
        self,
        hooks: HookRegistry,
        *,
        handlers: Optional[dict[str, Callable[..., Any]]] = None,
        owner: str = "evolution",
    ) -> None:
        self.hooks = hooks
        self.handlers = dict(handlers or {})
        self.owner = owner

    def validate(self, content: dict[str, Any]) -> None:
        name = str(content.get("name") or "")
        if name not in HOOK_POINTS:
            raise AssetError("EVOLUTION_CONTENT_INVALID", f"unknown hook point {name!r}")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        for row in self.hooks.registered(self.owner):
            if row["name"] == target:
                return row
        return None

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        handler = self.handlers.get(target) or (lambda *args, **kwargs: None)
        self.hooks.register(
            self.owner,
            target,
            handler,
            order=int(content.get("order", 100)),
            on_error=str(content.get("onError", "continue")),
        )
        return lambda: self.hooks.unregister(self.owner, target)


class MemoryPolicyAdapter:
    kind = AssetKind.MEMORY_POLICY

    def __init__(self, policy: Any, *, updated_by: str = "evolution") -> None:
        self.policy = policy
        self.updated_by = updated_by

    def validate(self, content: dict[str, Any]) -> None:
        from ..eclipse_memory.policy import validate  # noqa: PLC0415

        key = str(content.get("key") or "")
        if not key:
            raise AssetError("EVOLUTION_CONTENT_INVALID", "a memory policy needs a key")
        validate(key, content.get("value"))

    def read(self, target: str, content: dict[str, Any]) -> Any:
        return self.policy.get_all().get(str(content.get("key") or target))

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        key = str(content.get("key") or target)
        previous = self.policy.get_all().get(key, ...)
        self.policy.set(key, content.get("value"), updated_by=self.updated_by)

        def undo() -> None:
            if previous is ...:
                self.policy.reset(key)
            else:
                self.policy.set(key, previous, updated_by=self.updated_by)

        return undo


class ToolPolicyAdapter:
    kind = AssetKind.TOOL_POLICY

    def __init__(self, policies: ToolPolicyService) -> None:
        self.policies = policies

    def validate(self, content: dict[str, Any]) -> None:
        if not isinstance(content.get("allow", []), list) or not isinstance(content.get("deny", []), list):
            raise AssetError("EVOLUTION_CONTENT_INVALID", "allow/deny must be lists")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        entry = self.policies.get(target)
        return None if entry is None else {"allow": entry.allow, "deny": entry.deny}

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        return self.policies.set(
            target,
            allow=[str(item) for item in content.get("allow", [])],
            deny=[str(item) for item in content.get("deny", [])],
        )


class ModelRouteAdapter:
    kind = AssetKind.MODEL_ROUTE

    def __init__(self, routes: ModelRouteService) -> None:
        self.routes = routes

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("provider") or "").strip() or not str(content.get("model") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "a model route needs provider and model")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        entry = self.routes.get(target)
        return None if entry is None else {"provider": entry.provider, "model": entry.model}

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        return self.routes.set(
            target,
            provider=str(content["provider"]),
            model=str(content["model"]),
        )


class CompositionRowAdapter:
    kind = AssetKind.COMPOSITION_ROW

    def __init__(self, rows: CompositionRowStore) -> None:
        self.rows = rows

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("id") or "").strip() or not str(content.get("name") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "a composition row needs id and name")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        return self.rows.get(target)

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        return self.rows.set(target, dict(content))


class PluginBundleAdapter:
    kind = AssetKind.PLUGIN_BUNDLE

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("name") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "a plugin bundle needs a name")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        return None

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        raise AssetError(
            "EVOLUTION_PLUGIN_BUNDLE_OPERATOR",
            "installing a plugin bundle is an operator-signed action; the evolution engine cannot apply it",
        )
