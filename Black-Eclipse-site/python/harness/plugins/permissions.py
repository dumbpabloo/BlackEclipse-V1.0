"""The plugin permission vocabulary and its runtime enforcement.

A plugin declares the permissions it needs in its manifest. The host then grants
EXACTLY that set to the plugin's runtime context, and every sensitive SDK method
calls `require(...)` before touching a resource. Declaration in the manifest is
necessary but not sufficient: enforcement happens here, at the call site, so a
plugin cannot exceed its declared permissions even if it tries to.

The vocabulary is closed. An unknown permission makes the manifest invalid, so a
plugin can never invent a permission the host does not understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .errors import PermissionDenied

# The closed permission vocabulary. Extending this list is the ONLY way to grant
# a new class of access; a plugin declaring anything else is rejected.
PLUGIN_PERMISSIONS: frozenset[str] = frozenset(
    {
        "network",
        "filesystem.read",
        "filesystem.write",
        "process.spawn",
        "terminal",
        "browser",
        "database",
        "secrets",
        "events.subscribe",
        "events.publish",
        "task.create",
        "workflow.execute",
        "agent.invoke",
        "agents.contribute",
        "skills.contribute",
        "prompts.contribute",
        "models.contribute",
        "memory.read",
        "memory.write",
        "research.execute",
        "ui",
        "notifications",
        "webhooks",
        "workers",
    }
)

# Permissions that ALWAYS require an explicit operator grant before a plugin may
# even be enabled — they are not implied by installing the plugin.
ELEVATED_PERMISSIONS: frozenset[str] = frozenset(
    {
        "process.spawn",
        "terminal",
        "browser",
        "database",
        "filesystem.write",
    }
)


def unknown_permissions(declared: Iterable[str]) -> list[str]:
    """Permissions declared by a manifest that are not in the vocabulary."""
    return sorted({p for p in declared if p and p not in PLUGIN_PERMISSIONS})


@dataclass
class GrantedPermissions:
    """The resolved permission set for one plugin instance.

    `declared` is what the manifest asked for; `granted` is what the operator
    actually approved (a subset, possibly widened only by an operator). Every
    SDK call site checks `granted`.
    """

    plugin_id: str
    declared: frozenset[str] = field(default_factory=frozenset)
    granted: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_declared(cls, plugin_id: str, declared: Iterable[str], *, grant_all: bool = True) -> "GrantedPermissions":
        declared_set = frozenset(p for p in declared if p)
        granted = declared_set if grant_all else frozenset()
        return cls(plugin_id=plugin_id, declared=declared_set, granted=granted)

    def has(self, permission: str) -> bool:
        return permission in self.granted

    def require(self, permission: str, action: str = "") -> None:
        if permission not in self.granted:
            raise PermissionDenied(permission, plugin_id=self.plugin_id, action=action)

    def with_granted(self, granted: Iterable[str]) -> "GrantedPermissions":
        return GrantedPermissions(
            plugin_id=self.plugin_id,
            declared=self.declared,
            granted=frozenset(p for p in granted if p in PLUGIN_PERMISSIONS),
        )


__all__ = [
    "PLUGIN_PERMISSIONS",
    "ELEVATED_PERMISSIONS",
    "GrantedPermissions",
    "unknown_permissions",
    "PermissionDenied",
]
