"""Opt-in plugin-code watcher: a digest poll that reloads changed plugins.

Automatic reload is a development affordance, so it is off unless
``HARNESS_PLUGIN_HOT_RELOAD=1``. A bundled, signed or verified plugin is refused
by `PluginManager.reload` and recorded as a skip; the watcher never bypasses the
manager's trust rules. A failed reload leaves the plugin unloaded/FAILED through
the manager's own path, and the watcher keeps polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .loader import checksum

log = logging.getLogger("harness.plugins.hot_reload")
WATCH_ENV = "HARNESS_PLUGIN_HOT_RELOAD"
INTERVAL_ENV = "HARNESS_PLUGIN_HOT_RELOAD_INTERVAL"


def watch_enabled(env: Optional[dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(WATCH_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def watch_interval(env: Optional[dict[str, str]] = None) -> float:
    source = env if env is not None else os.environ
    try:
        return max(0.1, float(source.get(INTERVAL_ENV) or 1.0))
    except (TypeError, ValueError):
        return 1.0


class PluginWatcher:
    def __init__(self, manager: Any, *, interval: Optional[float] = None) -> None:
        self.manager = manager
        self.interval = interval if interval is not None else watch_interval()
        self.digests = self._snapshot()
        self.reloads = 0
        self.skips: list[dict[str, Any]] = []
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

    def _snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for plugin_id in list(getattr(self.manager, "_active", {})):
            directory = self.manager.directory_of(plugin_id)
            if directory is not None:
                snapshot[plugin_id] = checksum(directory)
        return snapshot

    async def poll(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for plugin_id, digest in list(self._snapshot().items()):
            if self.digests.get(plugin_id) == digest:
                continue
            self.digests[plugin_id] = digest
            try:
                result = await self.manager.reload(plugin_id, operator=True)
            except Exception as exc:  # noqa: BLE001 - a refused/failed reload is recorded
                self.skips.append({"pluginId": plugin_id, "error": str(exc)})
                log.warning("plugin hot reload skipped for %s: %s", plugin_id, exc)
                continue
            self.reloads += 1
            results.append(result)
        return results

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.interval)
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must survive a bad edit
                log.warning("plugin hot reload poll failed", exc_info=True)
