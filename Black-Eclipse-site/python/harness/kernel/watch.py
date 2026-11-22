"""Hot reload for profile composition (operator opt-in, deterministic digest).

Watching is a polling digest, not a platform file-event API: the same code path
works on every host and tests can call ``poll()`` directly. Watching is enabled
only with ``ECLIPSE_KERNEL_WATCH=1`` and only when the kernel is ``on`` (a
``shadow`` kernel validates on demand but never swaps a live generation).

A broken edit is a no-op: the previous generation keeps serving. A successful
edit stops the old generation and mounts the new one through the same
``ProfileBoot`` path used at startup.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any, Optional

WATCH_ENV = "ECLIPSE_KERNEL_WATCH"
WATCH_INTERVAL_ENV = "ECLIPSE_KERNEL_WATCH_INTERVAL"
WATCHED_SUFFIXES = (".json", ".yaml", ".yml")


def watch_enabled(env: Optional[dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(WATCH_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def watch_interval(env: Optional[dict[str, str]] = None) -> float:
    source = env if env is not None else os.environ
    try:
        return max(0.05, float(source.get(WATCH_INTERVAL_ENV) or 1.0))
    except (TypeError, ValueError):
        return 1.0


def digest_paths(roots: list[Path]) -> str:
    """Hash every watched file (path + bytes) under the roots, in sorted order."""
    digest = hashlib.sha256()
    files: dict[str, Path] = {}
    for root in roots:
        root = Path(root)
        if root.is_file():
            files[str(root.resolve())] = root
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in WATCHED_SUFFIXES:
                files[str(path.resolve())] = path
    for name in sorted(files):
        digest.update(name.encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(files[name].read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def profile_roots(boot: Any) -> list[Path]:
    roots = [Path(boot.directory)]
    loaded = getattr(boot, "loaded", None)
    if loaded is not None:
        for bundle in loaded.bundles:
            if bundle.root is not None and not bundle.error:
                roots.append(Path(bundle.root))
    return roots


class ProfileWatcher:
    def __init__(self, boot: Any, *, interval: Optional[float] = None) -> None:
        self.boot = boot
        self.interval = interval if interval is not None else watch_interval()
        self.digest = digest_paths(profile_roots(boot))
        self.reloads = 0
        self.last_status: dict[str, Any] = {}
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

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

    async def poll(self) -> Optional[dict[str, Any]]:
        current = digest_paths(profile_roots(self.boot))
        if current == self.digest:
            return None
        self.digest = current
        status = await self.boot.reload()
        self.reloads += 1
        self.last_status = status
        return status

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.interval)
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad edit must not kill the watcher
                import logging  # noqa: PLC0415

                logging.getLogger("harness.kernel.watch").warning("profile watch reload failed", exc_info=True)
