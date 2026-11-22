"""Boot wiring for the Plugin Platform.

Constructs the ONE process-wide `PluginManager`, registers the agent-facing
plugin tools on the existing `ToolRegistry`, and starts/stops plugins with the
runtime lifespan. Mirrors `market/bootstrap.py`, `portfolio/bootstrap.py` and
`tasks/bootstrap.py`: this module keeps `service.py` under its size ratchet and
owns no agentic logic of its own.
"""

from __future__ import annotations

from typing import Any, Optional

_PLUGIN_MANAGER: Optional[Any] = None


def set_plugin_manager(manager: Any) -> None:
    global _PLUGIN_MANAGER
    _PLUGIN_MANAGER = manager


def get_plugin_manager() -> Optional[Any]:
    return _PLUGIN_MANAGER


def install_plugin_layer(service: Any) -> Any:
    """Build the manager, attach it to the service and register its agent tools."""
    from .manager import PluginManager
    from .tools import register_plugin_tools

    manager = PluginManager(
        store=service.store,
        tool_registry=service.registry,
        bus=getattr(service, "bus", None),
        service=service,
    )
    service.plugins = manager
    set_plugin_manager(manager)
    register_plugin_tools(service.registry)
    return manager


async def start_plugins(service: Any) -> dict[str, Any]:
    """Reload every plugin that was enabled before the last shutdown."""
    manager = getattr(service, "plugins", None)
    if manager is None:
        return {"started": [], "failed": []}
    try:
        result = await manager.start_enabled()
    except Exception:  # noqa: BLE001 - a plugin must never block boot
        import logging

        logging.getLogger("harness.plugins").warning("plugin startup failed", exc_info=True)
        return {"started": [], "failed": []}
    from .hot_reload import PluginWatcher, watch_enabled

    if watch_enabled():
        watcher = PluginWatcher(manager)
        await watcher.start()
        manager.watcher = watcher
        result = {**result, "watch": True}
    return result


async def stop_plugins(service: Any) -> None:
    manager = getattr(service, "plugins", None)
    if manager is None:
        return
    watcher = getattr(manager, "watcher", None)
    if watcher is not None:
        await watcher.stop()
        manager.watcher = None
    await manager.stop_all()


__all__ = ["install_plugin_layer", "start_plugins", "stop_plugins", "get_plugin_manager", "set_plugin_manager"]
