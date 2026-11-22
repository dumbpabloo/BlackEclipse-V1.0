"""Executable contribution catalog: research/data providers, workflows, models, UI.

These are not a second transport. Each entry is an object a consumer can use
(the provider callable, Workflow, model descriptor, or UI handler) plus the
plugin that owns it. Registration returns an exact undo, so disable/uninstall
removes the contribution from both the catalog and any attached registry.

This slice is a kernel catalog, not a rewrite of the retrieval planner, workflow
runtime or model adapter chain; `harness_inspect` exposes it and later wiring can
consume the same entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

KINDS = ("research_provider", "data_provider", "workflow", "model", "ui")


@dataclass
class ExtraEntry:
    kind: str
    name: str
    plugin_id: str
    value: Any


class ContributionService:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, ExtraEntry]] = {kind: {} for kind in KINDS}
        self._retrieval: Any = None
        self._search: Any = None
        self._workflows: Any = None
        self._models: Any = None
        self._ui: Any = None

    # -- attachment to the real consumers -------------------------------------

    def attach_retrieval(self, registry: Any) -> None:
        self._retrieval = registry
        for entry in self._entries["data_provider"].values():
            self._attach_data(entry.name, entry.value)

    def attach_search(self, registry: Any) -> None:
        self._search = registry
        for entry in self._entries["research_provider"].values():
            self._attach_research(entry.name, entry.value)

    def attach_workflows(self, store: Any) -> None:
        self._workflows = store
        for entry in self._entries["workflow"].values():
            self._attach_workflow(entry.name, entry.value)

    def attach_models(self, registry: Any) -> None:
        self._models = registry
        for entry in self._entries["model"].values():
            self._attach_model(entry.name, entry.value)

    def attach_ui(self, service: Any) -> None:
        self._ui = service
        for entry in self._entries["ui"].values():
            self._attach_ui(entry.name, entry.value)

    @staticmethod
    def _dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _attach_data(self, name: str, value: Any) -> None:
        if self._retrieval is None:
            return
        from ...retrieval.registry import ProviderDescriptor  # noqa: PLC0415

        if isinstance(value, ProviderDescriptor):
            descriptor = value
        else:
            data = self._dict(value)
            descriptor = ProviderDescriptor(
                id=str(data.get("id") or name),
                label=str(data.get("label") or name),
                tier=str(data.get("tier") or "plugin"),
                capabilities=tuple(data.get("capabilities") or data.get("kinds") or ()),
                source_name=data.get("sourceName"),
                implemented=bool(data.get("implemented", True)),
            )
        self._retrieval.register(descriptor)

    def _attach_research(self, name: str, value: Any) -> Callable[[], None]:
        if self._search is None:
            return lambda: None
        if not hasattr(value, "name") or not hasattr(value, "search"):
            return lambda: None  # a descriptor without a runnable provider is not registered
        self._search.register(value, kind=str(getattr(value, "kind", "web")))
        return lambda: self._search.unregister(str(getattr(value, "name", name)))

    def _attach_workflow(self, name: str, value: Any) -> Callable[[], None]:
        if self._workflows is None:
            return lambda: None
        from ...workflows.model import Workflow  # noqa: PLC0415

        workflow = value if isinstance(value, Workflow) else Workflow(**(self._dict(value) or {"id": name}))
        self._workflows.save(workflow)
        return lambda: self._workflows.delete(workflow.id, workflow.version)

    def _attach_model(self, name: str, value: Any) -> Callable[[], None]:
        if self._models is None:
            return lambda: None
        from ...model_runtime.registry import ProviderInfo  # noqa: PLC0415

        data = self._dict(value)
        info = ProviderInfo(
            id=str(data.get("id") or data.get("name") or name),
            label=str(data.get("label") or name),
            kind=str(data.get("kind") or "custom"),
            keyless=bool(data.get("keyless", True)),
            openai_compatible=bool(data.get("openai_compatible", True)),
            default_base_url=data.get("default_base_url"),
        )
        self._models.register(info)
        return lambda: self._models.remove_provider(info.id)

    def _attach_ui(self, name: str, value: Any) -> Callable[[], None]:
        if self._ui is None or not hasattr(self._ui, "register_dynamic_panel"):
            return lambda: None
        return self._ui.register_dynamic_panel(name, value)

    def register(
        self,
        kind: str,
        name: str,
        value: Any,
        *,
        plugin_id: str,
        replace: bool = False,
    ) -> Callable[[], None]:
        if kind not in KINDS:
            raise ValueError(f"unknown contribution kind {kind!r}")
        if not name:
            raise ValueError("a contribution needs a name")
        layer = self._entries[kind]
        existing = layer.get(name)
        if existing is not None and not (replace and existing.plugin_id == plugin_id):
            raise ValueError(f"{kind} {name!r} is already contributed")
        previous = layer.get(name)
        entry = ExtraEntry(kind=kind, name=name, plugin_id=plugin_id, value=value)
        layer[name] = entry
        if kind == "data_provider":
            self._attach_data(name, value)
            extra_undo = lambda: None
        elif kind == "research_provider":
            extra_undo = self._attach_research(name, value)
        elif kind == "workflow":
            extra_undo = self._attach_workflow(name, value)
        elif kind == "model":
            extra_undo = self._attach_model(name, value)
        else:
            extra_undo = self._attach_ui(name, value)

        def dispose() -> None:
            if kind == "data_provider" and self._retrieval is not None:
                self._retrieval.unregister(str((self._dict(value) or {}).get("id") or name))
            if kind == "research_provider" and self._search is not None:
                self._search.unregister(str(getattr(value, "name", name)))
            if kind in ("workflow", "model", "ui") and extra_undo is not None:
                extra_undo()
            if layer.get(name) is entry:
                if previous is None:
                    layer.pop(name, None)
                else:
                    layer[name] = previous

        return dispose

    def get(self, kind: str, name: str) -> Optional[ExtraEntry]:
        return self._entries.get(kind, {}).get(name)

    def list(self, kind: Optional[str] = None) -> list[ExtraEntry]:
        if kind is not None:
            return list(self._entries.get(kind, {}).values())
        return [entry for entries in self._entries.values() for entry in entries.values()]

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        return {
            kind: [
                {
                    "name": entry.name,
                    "pluginId": entry.plugin_id,
                    "type": type(entry.value).__name__,
                    "callable": callable(entry.value),
                }
                for entry in entries.values()
            ]
            for kind, entries in self._entries.items()
        }

    def reset(self) -> None:
        self._entries = {kind: {} for kind in KINDS}
