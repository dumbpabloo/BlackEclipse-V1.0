"""The agent catalog: built-ins, versioned user presets, and plugin agents.

Resolution order is deliberate and identical on every entrance (chat, bot,
scheduled run): a locally registered agent, then a built-in, then a stored
preset. Built-ins stay in ``agents.py``; this service owns the lookup, not the
definitions.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

from ...schemas import AgentConfig


class AgentCatalog:
    def __init__(self) -> None:
        self._local: dict[str, AgentConfig] = {}

    def register(self, agent: AgentConfig, *, replace: bool = False) -> Callable[[], None]:
        if not agent.id:
            raise ValueError("an agent needs an id")
        if agent.id in self._local and not replace:
            raise ValueError(f"agent {agent.id!r} is already registered")
        self._local[agent.id] = agent

        def dispose() -> None:
            if self._local.get(agent.id) is agent:
                self._local.pop(agent.id, None)

        return dispose

    def resolve(self, agent_id: str) -> AgentConfig:
        local = self._local.get(agent_id)
        if local is not None:
            return local
        from ...agents import get_agent  # noqa: PLC0415 - leaf definitions

        try:
            return get_agent(agent_id)
        except ValueError:
            from ...agent_presets import agent_config_from_preset, get_preset_store  # noqa: PLC0415

            store = get_preset_store()
            preset = None
            if store is not None:
                try:
                    preset = store.get(agent_id)
                except sqlite3.Error:
                    # An optional preset store that was closed or is unavailable
                    # must not turn "unknown agent" into a raw database error: the
                    # built-in lookup is the authority, and its ValueError stands.
                    preset = None
            if preset is None:
                raise
            return agent_config_from_preset(preset)

    def has(self, agent_id: str) -> bool:
        if agent_id in self._local:
            return True
        from ...agents import AGENT_PRESETS  # noqa: PLC0415

        return agent_id in AGENT_PRESETS

    def get_local(self, agent_id: str) -> Optional[AgentConfig]:
        return self._local.get(agent_id)

    def remove(self, agent_id: str) -> bool:
        return self._local.pop(agent_id, None) is not None

    def names(self) -> list[str]:
        from ...agents import AGENT_PRESETS  # noqa: PLC0415

        return sorted(set(AGENT_PRESETS) | set(self._local))

    def catalog(self) -> list[dict[str, Any]]:
        from ...agents import AGENT_PRESETS  # noqa: PLC0415

        rows = []
        for agent_id in self.names():
            agent = self._local.get(agent_id) or AGENT_PRESETS.get(agent_id)
            rows.append({
                "id": agent_id,
                "name": getattr(agent, "name", agent_id),
                "source": "local" if agent_id in self._local else "builtin",
            })
        return rows

    def reset(self) -> None:
        self._local.clear()


def resolve_stored_agent(agent_id: str) -> Optional[AgentConfig]:
    from ...agent_presets import agent_config_from_preset, get_preset_store  # noqa: PLC0415

    store = get_preset_store()
    preset = store.get(agent_id) if store is not None else None
    return agent_config_from_preset(preset) if preset else None
