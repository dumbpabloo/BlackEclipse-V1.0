"""The BlackEclipse Plugin Platform.

A first-class extension system: plugins are validated against a versioned
manifest, installed atomically, loaded under a permission-checked context, and
compose into Eclipse's existing Tool Registry, Event Bus, hook points, Task
System, Evidence engine and observability — never a parallel system.

Public surface:
    PluginManager     — lifecycle + composition authority
    PluginManifest    — the validated manifest contract
    Plugin / tool ... — the developer SDK
    PluginStore       — persistence on agent-memory.db
"""

from __future__ import annotations

from .errors import (
    CompatibilityError,
    DependencyError,
    LifecycleError,
    ManifestError,
    NotFound,
    PermissionDenied,
    PluginError,
)
from .manager import PluginManager
from .manifest import SDK_VERSION, PluginManifest, parse_manifest
from .model import PluginRecord, PluginState, TrustLevel
from .sdk import (
    Plugin,
    PluginToolError,
    agent_contribution,
    command,
    data_provider,
    event_handler,
    hook,
    model_provider,
    prompt_section,
    research_provider,
    skill,
    tool,
    ui_panel,
    webhook,
    worker,
    workflow_contribution,
)

__all__ = [
    "PluginManager",
    "PluginManifest",
    "parse_manifest",
    "SDK_VERSION",
    "PluginRecord",
    "PluginState",
    "TrustLevel",
    "Plugin",
    "PluginToolError",
    "tool",
    "skill",
    "hook",
    "command",
    "event_handler",
    "worker",
    "webhook",
    "ui_panel",
    "agent_contribution",
    "workflow_contribution",
    "prompt_section",
    "model_provider",
    "research_provider",
    "data_provider",
    "PluginError",
    "ManifestError",
    "PermissionDenied",
    "LifecycleError",
    "NotFound",
    "CompatibilityError",
    "DependencyError",
]
