"""Asset adapters: the only code that knows how to apply a kind.

An adapter validates one content shape and returns an undo callable. The
evolution service never touches a prompt, preset or bundle directly, so a new
asset kind is one adapter, not a new branch in the pipeline.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ..kernel.services.agents import AgentCatalog
from ..kernel.services.prompt import PromptSection, PromptService
from ..schemas import AgentConfig
from .assets import AssetError, AssetKind

Undo = Callable[[], None]


@runtime_checkable
class AssetAdapter(Protocol):
    kind: AssetKind

    def validate(self, content: dict[str, Any]) -> None: ...

    def read(self, target: str, content: dict[str, Any]) -> Any: ...

    def apply(self, target: str, content: dict[str, Any]) -> Undo: ...

    def retire(self, target: str, content: dict[str, Any]) -> Undo: ...


class PromptSectionAdapter:
    kind = AssetKind.PROMPT_SECTION

    def __init__(self, service: PromptService, *, default_scope: str = "") -> None:
        self.service = service
        self.default_scope = default_scope

    def _scope(self, content: dict[str, Any]) -> str:
        return str(content.get("scope") or self.default_scope)

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("text") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "prompt_section text is required")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        for section in self.service.sections(self._scope(content)):
            if section.id == target:
                return {"text": section.text, "order": section.order}
        return None

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        scope = self._scope(content)
        section = PromptSection(
            id=target,
            text=str(content.get("text") or ""),
            order=int(content.get("order", 100)),
            scope=scope,
        )
        return self.service.register(scope, section, replace=True)

    def retire(self, target: str, content: dict[str, Any]) -> Undo:
        return self.service.remove(self._scope(content), target)


class AgentPresetAdapter:
    kind = AssetKind.AGENT_PRESET

    def __init__(self, catalog: AgentCatalog) -> None:
        self.catalog = catalog

    def validate(self, content: dict[str, Any]) -> None:
        if not str(content.get("systemPrompt") or "").strip():
            raise AssetError("EVOLUTION_CONTENT_INVALID", "agent_preset systemPrompt is required")

    def read(self, target: str, content: dict[str, Any]) -> Any:
        agent = self.catalog.get_local(target)
        return None if agent is None else agent.model_dump()

    def apply(self, target: str, content: dict[str, Any]) -> Undo:
        previous = self.catalog.get_local(target)
        agent = AgentConfig(
            id=target,
            name=str(content.get("name") or target),
            systemPrompt=str(content["systemPrompt"]),
            tools=content.get("tools"),
            maxSteps=int(content.get("maxSteps", 8)),
            allowMcpTools=bool(content.get("allowMcpTools", False)),
            allowPluginTools=bool(content.get("allowPluginTools", False)),
        )
        self.catalog.register(agent, replace=True)

        def undo() -> None:
            if previous is None:
                self.catalog.remove(target)
            else:
                self.catalog.register(previous, replace=True)

        return undo

    def retire(self, target: str, content: dict[str, Any]) -> Undo:
        previous = self.catalog.get_local(target)
        if previous is None:
            raise AssetError("EVOLUTION_TARGET_MISSING", f"agent {target!r} is not locally registered")
        self.catalog.remove(target)

        def undo() -> None:
            self.catalog.register(previous, replace=True)

        return undo


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[AssetKind, AssetAdapter] = {}

    def register(self, adapter: AssetAdapter) -> None:
        if adapter.kind in self._adapters:
            raise AssetError("EVOLUTION_ADAPTER_DUPLICATE", f"adapter for {adapter.kind.value} already exists")
        self._adapters[adapter.kind] = adapter

    def get(self, kind: AssetKind) -> Optional[AssetAdapter]:
        return self._adapters.get(kind)

    def require(self, kind: AssetKind) -> AssetAdapter:
        adapter = self.get(kind)
        if adapter is None:
            raise AssetError("EVOLUTION_ADAPTER_MISSING", f"no adapter for asset kind {kind.value!r}")
        return adapter

    def kinds(self) -> list[str]:
        return sorted(kind.value for kind in self._adapters)
