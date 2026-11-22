"""Prompt service: the existing resolution plus ordered, scoped extra sections.

With no registered sections, ``resolve`` returns exactly what
``surface.resolve_surface_run`` returns - byte-identical for every built-in
agent. A scope (normally the agent id) may add sections; another scope never
sees them, and disposal removes exactly its own layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ...schemas import AgentConfig


@dataclass(frozen=True)
class PromptSection:
    id: str
    text: str
    order: int = 100
    label: str = ""
    scope: str = ""


class PromptService:
    def __init__(self) -> None:
        self._sections: dict[str, dict[str, PromptSection]] = {}

    def register(
        self, scope: str, section: PromptSection, *, replace: bool = False,
    ) -> Callable[[], None]:
        if not section.id:
            raise ValueError("a prompt section needs an id")
        layer = self._sections.setdefault(scope, {})
        if section.id in layer and not replace:
            raise ValueError(f"prompt section {section.id!r} is already registered in {scope!r}")
        previous = layer.get(section.id)
        layer[section.id] = section

        def dispose() -> None:
            current = self._sections.get(scope)
            if current and current.get(section.id) is section:
                if previous is None:
                    current.pop(section.id, None)
                else:
                    current[section.id] = previous
                if not current:
                    self._sections.pop(scope, None)

        return dispose

    def sections(self, scope: str = "") -> list[PromptSection]:
        return sorted(self._sections.get(scope, {}).values(), key=lambda s: (s.order, s.id))

    def render_sections(self, scope: str = "") -> str:
        return "\n\n".join(section.text for section in self.sections(scope) if section.text.strip())

    def remove(self, scope: str, section_id: str) -> Callable[[], None]:
        """Remove one section and return an undo that restores it exactly."""
        layer = self._sections.get(scope)
        if not layer or section_id not in layer:
            raise ValueError(f"prompt section {section_id!r} is not registered in {scope!r}")
        previous = layer.pop(section_id)
        if not layer:
            self._sections.pop(scope, None)

        def undo() -> None:
            self._sections.setdefault(scope, {})[section_id] = previous

        return undo

    def resolve(
        self,
        agent: AgentConfig,
        registry: Any,
        settings: Optional[dict[str, Any]],
        autonomy: Optional[str] = None,
        *,
        apply_code_surface: bool = True,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        scope: str = "",
    ) -> tuple[Any, str]:
        from ...surface import resolve_surface_run  # noqa: PLC0415

        resolution, prompt = resolve_surface_run(
            agent,
            registry,
            settings,
            autonomy,
            apply_code_surface=apply_code_surface,
            provider=provider,
            model=model,
        )
        extra = self.render_sections(scope or agent.id)
        if extra:
            prompt = f"{prompt}\n\n{extra}"
        return resolution, prompt

    def plan(self, **kwargs: Any) -> Any:
        from ...prompt_segments import build_prompt_plan  # noqa: PLC0415

        return build_prompt_plan(**kwargs)

    def reset(self) -> None:
        self._sections.clear()
