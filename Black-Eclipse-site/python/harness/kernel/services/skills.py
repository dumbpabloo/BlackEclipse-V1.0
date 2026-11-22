"""A kernel skill catalog over the existing SkillRegistry.

Plugin-contributed skills are ephemeral: registration returns an exact undo that
removes the skill from both the kernel catalog and the underlying registry on
plugin unload. No user-skill persistence row is written, because a plugin's
contribution must disappear with the plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ...skills.loader import LoadedSkill
from ...skills.manifest import SkillManifest


@dataclass(frozen=True)
class SkillEntry:
    id: str
    name: str
    description: str
    instructions: str
    plugin_id: str
    version: str = "1.0.0"


class SkillService:
    def __init__(self, registry: Any = None) -> None:
        self.registry = registry
        self._entries: dict[str, SkillEntry] = {}

    def attach(self, registry: Any) -> None:
        self.registry = registry

    def register(
        self,
        *,
        plugin_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions: str,
        version: str = "1.0.0",
    ) -> Callable[[], None]:
        if skill_id in self._entries:
            existing = self._entries[skill_id]
            if existing.plugin_id != plugin_id:
                raise ValueError(f"skill {skill_id!r} is already provided by a plugin")
            # Re-enabling the same plugin replaces its own contribution.
            self._entries.pop(skill_id, None)
            if self.registry is not None:
                self.registry.unregister(skill_id)
        entry = SkillEntry(
            id=skill_id,
            name=name or skill_id,
            description=description or name or skill_id,
            instructions=instructions,
            plugin_id=plugin_id,
            version=version,
        )
        self._entries[skill_id] = entry
        registered = False
        if self.registry is not None:
            if self.registry.get(skill_id) is not None:
                self._entries.pop(skill_id, None)
                raise ValueError(f"skill {skill_id!r} already exists outside the plugin")
            manifest = SkillManifest(
                id=skill_id,
                name=entry.name,
                version=version,
                description=entry.description,
                category="research",
                status="enabled",
                instructions=None,
                author=f"plugin:{plugin_id}",
                origin="author",
            )
            loaded = LoadedSkill(
                manifest=manifest,
                folder="",
                instructions_path=None,
                instructions_text=instructions,
                provenance="external",
            )
            self.registry.register(loaded)
            if self.registry.get(skill_id) is None:
                self._entries.pop(skill_id, None)
                raise ValueError(f"skill {skill_id!r} was quarantined by the content scan")
            registered = True

        def undo() -> None:
            if self._entries.get(skill_id) is entry:
                self._entries.pop(skill_id, None)
            if registered and self.registry is not None:
                self.registry.unregister(skill_id)

        return undo

    def get(self, skill_id: str) -> Optional[SkillEntry]:
        return self._entries.get(skill_id)

    def list(self) -> list[SkillEntry]:
        return list(self._entries.values())

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"id": entry.id, "name": entry.name, "pluginId": entry.plugin_id, "version": entry.version}
            for entry in self._entries.values()
        ]

    def reset(self) -> None:
        self._entries.clear()
