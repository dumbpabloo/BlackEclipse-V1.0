"""Phase 3 runtime seam: services behind ``ECLIPSE_KERNEL``.

Default OFF. When enabled, the kernel owns three services over the existing
objects:

* ``tools``   - global ``ToolRegistry`` plus per-agent/per-run overlays;
* ``prompt``  - the existing surface resolution plus scoped extra sections;
* ``agents``  - built-ins, stored presets and locally registered agents.

The graph consults this runtime only through the small helpers below, so the
disabled path is exactly the old code. Nothing here replaces the tool executor,
the model runtime or persistence.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..context import Kernel
from ..supervisor import Supervisor
from ...schemas import AgentConfig
from .agents import AgentCatalog
from .extras import ContributionService
from .policy import CompositionRowStore, ModelRouteService, PolicyService, ToolPolicyService
from .prompt import PromptSection, PromptService
from .sessions import SessionService
from .skills import SkillService
from .tools import ScopedToolRegistry, ToolService
from .ui import UISlotService, slot_for_ui_kind

_TRUE = {"1", "true", "yes", "on", "enable", "enabled"}
MODES = ("off", "shadow", "on")


def _mode_from_value(value: Optional[str], *, enabled: Optional[bool] = None) -> str:
    if enabled is not None:
        return "on" if enabled else "off"
    text = (value or "").strip().lower()
    if text == "shadow":
        return "shadow"
    # Default ON: the kernel is the boot composition. `ECLIPSE_KERNEL=off` is the
    # documented rollback switch; `shadow` validates without changing behavior.
    return "off" if text in {"0", "false", "no", "off", "disable", "disabled"} else "on"


def _flag(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in _TRUE or value.strip().lower() == "shadow"


class KernelRuntime:
    def __init__(self, *, enabled: Optional[bool] = None, mode: Optional[str] = None) -> None:
        if mode is not None:
            chosen = mode.strip().lower()
            self._mode = chosen if chosen in MODES else "off"
        else:
            self._mode = _mode_from_value(os.environ.get("ECLIPSE_KERNEL"), enabled=enabled)
        self.kernel: Optional[Kernel] = None
        self.supervisor: Any = None
        self.tools: Optional[ToolService] = None
        self.prompt: Optional[PromptService] = None
        self.agents: Optional[AgentCatalog] = None
        self.skills: Optional[SkillService] = None
        self.extras: Optional[ContributionService] = None
        self.policies: Optional[ToolPolicyService] = None
        self.policy_authority: Optional[PolicyService] = None
        self.sessions: Optional[SessionService] = None
        self.model_routes: Optional[ModelRouteService] = None
        self.composition_rows: Optional[CompositionRowStore] = None
        self.memory_policy: Any = None
        self.ui_slots: Optional[UISlotService] = None
        self._provided: dict[str, set[str]] = {}
        self._registry: Any = None
        self.provider_registry: Any = None
        self.search_registry: Any = None

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        chosen = str(value or "").strip().lower()
        self._mode = chosen if chosen in MODES else "off"

    @property
    def enabled(self) -> bool:
        """The kernel is loaded/validating (``shadow`` or ``on``)."""
        return self._mode != "off"

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._mode = "on" if value else "off"

    @property
    def active(self) -> bool:
        """The kernel may change live behavior (``on`` only, not ``shadow``)."""
        return self._mode == "on"

    # -- lifecycle ------------------------------------------------------------

    def configure(self, registry: Any) -> "KernelRuntime":
        if not self.enabled:
            return self
        if self.kernel is not None and self._registry is registry:
            return self
        self._registry = registry
        self.kernel = Kernel()
        self.kernel.runtime = self
        self.supervisor = Supervisor(self.kernel)
        self.tools = ToolService(registry)
        self.prompt = PromptService()
        self.agents = AgentCatalog()
        self.skills = SkillService()
        self.extras = ContributionService()
        self.policies = ToolPolicyService()
        self.policy_authority = PolicyService()
        self.sessions = SessionService()
        self.model_routes = ModelRouteService()
        self.composition_rows = CompositionRowStore(self.supervisor)
        self.ui_slots = UISlotService()
        self.kernel.root.provide("tools", self.tools)
        self.kernel.root.provide("prompt", self.prompt)
        self.kernel.root.provide("agents", self.agents)
        self.kernel.root.provide("skills", self.skills)
        self.kernel.root.provide("contributions", self.extras)
        self.kernel.root.provide("tool_policy", self.policies)
        self.kernel.root.provide("policy", self.policy_authority)
        self.kernel.root.provide("sessions", self.sessions)
        self.kernel.root.provide("model_routes", self.model_routes)
        self.kernel.root.provide("composition_rows", self.composition_rows)
        self.kernel.root.provide("ui_slots", self.ui_slots)
        return self

    def reset(self, *, enabled: Optional[bool] = None, mode: Optional[str] = None, registry: Any = None) -> "KernelRuntime":
        if mode is not None:
            self.mode = mode
        elif enabled is not None:
            self.enabled = bool(enabled)
        self.kernel = None
        self.supervisor = None
        self.tools = None
        self.prompt = None
        self.agents = None
        self.skills = None
        self.extras = None
        self.policies = None
        self.policy_authority = None
        self.sessions = None
        self.model_routes = None
        self.composition_rows = None
        self.memory_policy = None
        self.ui_slots = None
        self._provided.clear()
        self._registry = registry
        if self.enabled and registry is not None:
            self.configure(registry)
        return self

    # -- graph helpers --------------------------------------------------------

    def view_for(self, run_id: str, base: Any, *, agent_id: Optional[str] = None) -> Any:
        """The tool view a run sees. ``shadow``/unconfigured returns ``base``.

        The runtime owns views over ITS registry. A caller that supplies a
        different registry (the evaluation runner builds a private one) keeps
        that registry: returning the runtime's view would hide every tool the
        caller just registered.
        """
        if not self.active or self.tools is None:
            return base
        if base is not self._registry:
            return ScopedToolRegistry(base, key=f"run:{run_id}")
        return self.tools.run_scope(run_id, agent_id=agent_id)

    def agent_scope(self, agent_id: str) -> Any:
        if not self.active or self.tools is None:
            return None
        return self.tools.agent_scope(agent_id)

    def resolve_agent(self, agent_id: str) -> AgentConfig:
        if self.active:
            if self.agents is None:
                self.agents = AgentCatalog()
            return self.agents.resolve(agent_id)
        from ...agents import get_agent  # noqa: PLC0415

        return get_agent(agent_id)

    def resolve_surface(
        self,
        agent: AgentConfig,
        registry: Any,
        settings: Optional[dict[str, Any]],
        autonomy: Optional[str] = None,
        *,
        apply_code_surface: bool = True,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> tuple[Any, str]:
        if self.active:
            if self.prompt is None:
                self.prompt = PromptService()
            return self.prompt.resolve(
                agent,
                registry,
                settings,
                autonomy,
                apply_code_surface=apply_code_surface,
                provider=provider,
                model=model,
                scope=agent.id,
            )
        from ...surface import resolve_surface_run  # noqa: PLC0415

        return resolve_surface_run(
            agent,
            registry,
            settings,
            autonomy,
            apply_code_surface=apply_code_surface,
            provider=provider,
            model=model,
        )

    # -- plugin contributions -------------------------------------------------

    def attach_skills(self, registry: Any) -> None:
        if self.enabled:
            if self.skills is None:
                self.skills = SkillService(registry)
            else:
                self.skills.attach(registry)

    def attach_retrieval(self, registry: Any = None) -> Any:
        if not self.enabled:
            return None
        if registry is None:
            from ...retrieval.registry import build_default_registry  # noqa: PLC0415

            registry = build_default_registry()
        self.provider_registry = registry
        if self.extras is not None:
            self.extras.attach_retrieval(registry)
        return registry

    def attach_search(self, registry: Any = None) -> Any:
        if not self.enabled:
            return None
        if registry is None:
            from ...research.web_providers import build_default_registry, set_registry  # noqa: PLC0415

            registry = build_default_registry()
            set_registry(registry)
        self.search_registry = registry
        if self.extras is not None:
            self.extras.attach_search(registry)
        return registry

    def attach_workflows(self, store: Any) -> None:
        if self.enabled and self.extras is not None:
            self.extras.attach_workflows(store)

    def attach_models(self, registry: Any) -> None:
        if self.enabled and self.extras is not None:
            self.extras.attach_models(registry)

    def attach_ui(self, service: Any) -> None:
        if self.enabled and self.extras is not None:
            self.extras.attach_ui(service)

    def register_slot(
        self,
        slot: str,
        entry_id: str,
        handler: Any,
        *,
        title: str = "",
        plugin_id: str = "",
    ) -> Any:
        if not self.enabled:
            raise RuntimeError("kernel runtime is disabled")
        if self.ui_slots is None:
            self.ui_slots = UISlotService()
        return self.ui_slots.register(
            slot, entry_id, handler, title=title, plugin_id=plugin_id, replace=True,
        )

    def tool_policy_for(self, scope: str) -> Optional[dict[str, list[str]]]:
        if not self.active or self.policies is None:
            return None
        entry = self.policies.get(scope)
        if entry is None:
            return None
        return {"allow": list(entry.allow), "deny": list(entry.deny)}

    def apply_model_route(self, agent_id: str, settings: Any) -> Any:
        """Return a model selection with the contributed route applied.

        Only a same-provider route is applied: credentials and the base URL in
        `settings` belong to that provider. A cross-provider route is ignored
        rather than sending a key to a different endpoint.
        """
        if not self.active or self.model_routes is None:
            return settings
        route = self.model_routes.get(agent_id)
        if route is None:
            return settings
        current = str(getattr(settings, "provider", "") or "")
        if route.provider and route.provider != current:
            return settings
        try:
            return settings.model_copy(update={"model": route.model})
        except Exception:  # noqa: BLE001 - an opaque settings object stays untouched
            return settings

    def attach_composition(self, supervisor: Any) -> None:
        if self.enabled and self.composition_rows is not None:
            self.composition_rows.attach(supervisor)

    def attach_memory_policy(self, policy: Any) -> None:
        if self.enabled:
            self.memory_policy = policy

    def attach_sessions(self, store: Any) -> None:
        if self.enabled and self.sessions is not None:
            self.sessions.attach(store)

    # -- core service rows ----------------------------------------------------

    def _load_factory(self, path: str) -> Any:
        import importlib  # noqa: PLC0415

        module_name, _, attr = path.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, attr or "factory")
        if not callable(factory):
            raise TypeError(f"factory {path!r} is not callable")
        return factory

    def replace_core(self, key: str, value: Any) -> Any:
        """Replace a core service for live composition; returns an exact undo."""
        if key not in ("tools", "prompt", "agents"):
            raise ValueError(f"unsupported core service {key!r}")
        if key == "tools":
            if isinstance(value, ToolService):
                service = value
            else:
                service = ToolService(value)
        else:
            service = value
        previous = getattr(self, key, None)
        disposer = self.kernel.root.provide(key, service, replace=True)
        setattr(self, key, service)

        def undo() -> None:
            disposer()
            setattr(self, key, previous)

        return undo

    def builtin_plugin(self, spec: str, config: Any) -> Any:
        key = spec.split(":", 1)[1] if ":" in spec else ""
        if key not in ("tools", "prompt", "agents"):
            raise ValueError(f"unknown builtin service {spec!r}")
        data = config if isinstance(config, dict) else {}
        factory_path = str(data.get("factory") or "")
        if not factory_path:
            raise ValueError(f"{spec} requires config.factory")

        class _BuiltinServicePlugin:
            name = spec

            def apply(self, ctx: Any) -> None:
                value = self_runtime._load_factory(factory_path)()
                undo = self_runtime.replace_core(key, value)
                ctx.track(undo, label=f"core:{key}")

        self_runtime = self
        return _BuiltinServicePlugin()

    def has_service(self, key: str) -> bool:
        if not self.enabled:
            return False
        if self.kernel is not None and self.kernel.root.has(key):
            return True
        return key in {"tools", "prompt", "agents", "skills"}

    def register_agent(self, plugin_id: str, agent: AgentConfig) -> Any:
        if not self.enabled:
            raise RuntimeError("kernel runtime is disabled")
        if self.agents is None:
            self.agents = AgentCatalog()
        undos = [self.agents.register(agent, replace=True)]
        if self.kernel is not None:
            undos.append(self.kernel.root.provide(f"plugin.agent.{plugin_id}.{agent.id}", agent))
        return _undo_all(undos)

    def register_prompt(self, plugin_id: str, section: PromptSection, *, scope: str = "") -> Any:
        if not self.enabled:
            raise RuntimeError("kernel runtime is disabled")
        if self.prompt is None:
            self.prompt = PromptService()
        del plugin_id  # the section scope carries ownership; the undo is exact
        return self.prompt.register(scope or section.scope or "", section, replace=True)

    def register_skill(
        self,
        plugin_id: str,
        *,
        skill_id: str,
        name: str,
        description: str,
        instructions: str,
    ) -> Any:
        if not self.enabled:
            raise RuntimeError("kernel runtime is disabled")
        if self.skills is None:
            self.skills = SkillService()
        return self.skills.register(
            plugin_id=plugin_id,
            skill_id=skill_id,
            name=name,
            description=description,
            instructions=instructions,
        )

    def register_extra(
        self,
        kind: str,
        name: str,
        value: Any,
        *,
        plugin_id: str,
    ) -> Any:
        if not self.enabled:
            raise RuntimeError("kernel runtime is disabled")
        if self.extras is None:
            self.extras = ContributionService()
        return self.extras.register(kind, name, value, plugin_id=plugin_id, replace=True)

    def declare_provides(self, plugin_id: str, keys: list[str]) -> None:
        self._provided[plugin_id] = set(keys)

    def provided(self, plugin_id: str) -> list[str]:
        return sorted(self._provided.get(plugin_id, set()))

    def clear_provides(self, plugin_id: str) -> None:
        self._provided.pop(plugin_id, None)

    # -- inspection -----------------------------------------------------------

    def inspect(self) -> dict[str, Any]:
        payload = self.status()
        payload["tools"] = {
            "global": len(self.tools.registry.names()) if self.tools is not None else 0,
            "scopes": len(self.tools._views) if self.tools is not None else 0,  # noqa: SLF001
        }
        payload["skills"] = self.skills.catalog() if self.skills is not None else []
        payload["contributions"] = self.extras.catalog() if self.extras is not None else {}
        payload["toolPolicies"] = self.policies.catalog() if self.policies is not None else []
        payload["modelRoutes"] = self.model_routes.catalog() if self.model_routes is not None else []
        payload["compositionRows"] = self.composition_rows.catalog() if self.composition_rows is not None else []
        payload["uiSlots"] = self.ui_slots.entries() if self.ui_slots is not None else []
        payload["policy"] = self.policy_authority.describe() if self.policy_authority is not None else {}
        payload["sessions"] = self.sessions.describe() if self.sessions is not None else {}
        payload["provided"] = {key: sorted(value) for key, value in self._provided.items()}
        return payload

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "mode": self.mode,
            "configured": self.kernel is not None,
            "services": sorted(self.kernel.inspect()["services"]) if self.kernel is not None else [],
            "toolScopes": len(self.tools._views) if self.tools is not None else 0,  # noqa: SLF001
            "promptScopes": len(self.prompt._sections) if self.prompt is not None else 0,  # noqa: SLF001
            "agents": self.agents.names() if self.agents is not None else [],
        }


def _undo_all(undos: list[Any]) -> Any:
    def undo() -> None:
        for item in reversed(undos):
            try:
                item()
            except BaseException:  # noqa: BLE001 - teardown continues
                pass

    return undo


kernel_runtime = KernelRuntime()
