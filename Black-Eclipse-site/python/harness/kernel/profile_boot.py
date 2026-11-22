"""Boot one profile composition into the kernel supervisor.

The default path is unchanged: when the kernel is off, no profile directory
exists, or the profile declares no rows, this is a no-op. When a profile is
present, its ordered bundle/profile/overlay rows mount beside the existing
service; a failed row restores the previous generation and is reported instead
of leaving a half-mounted tree.

The same loader powers ``eclipse kernel dump``, so what is dumped is what boot
applies.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .errors import CompositionError, SupervisorError
from .profile import DEFAULT_PATCH, Profile, load_profile
from .supervisor import Supervisor
from .watch import watch_enabled

log = logging.getLogger("harness.kernel.profile_boot")

DEFAULT_PROFILE = "web"


def default_profiles_root() -> Path:
    configured = os.environ.get("ECLIPSE_PROFILES_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".black-eclipse" / "profiles"


def bundle_paths() -> list[Path]:
    raw = os.environ.get("ECLIPSE_BUNDLE_PATHS") or ""
    paths = [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]
    paths.append(Path.home() / ".black-eclipse" / "plugins")
    return paths


class ProfileBoot:
    def __init__(
        self,
        runtime: Any,
        *,
        profiles_root: Optional[str | Path] = None,
        profile_name: Optional[str] = None,
        extra_bundles: Optional[list[str | Path]] = None,
    ) -> None:
        self.runtime = runtime
        self.profiles_root = Path(profiles_root).expanduser() if profiles_root else default_profiles_root()
        self.profile_name = profile_name or os.environ.get("ECLIPSE_PROFILE") or DEFAULT_PROFILE
        self.extra_bundles = [Path(item).expanduser() for item in (extra_bundles or [])]
        self.supervisor: Optional[Supervisor] = None
        self.loaded: Optional[Profile] = None
        self.status: dict[str, Any] = {"status": "not-started"}

    @property
    def directory(self) -> Path:
        return self.profiles_root / self.profile_name

    def load(self) -> Optional[Profile]:
        if not self.directory.is_dir():
            return None
        return load_profile(
            self.directory,
            bundle_paths=[*bundle_paths(), *self.extra_bundles, self.profiles_root],
        )

    async def apply(self) -> dict[str, Any]:
        if not getattr(self.runtime, "enabled", False):
            self.status = {"enabled": False, "status": "disabled", "rows": []}
            return self.status
        if self.runtime.kernel is None:
            self.status = {"enabled": True, "status": "not-configured", "rows": []}
            return self.status
        try:
            loaded = self.load()
        except CompositionError as exc:
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "invalid",
                "ok": False, "error": exc.to_dict(),
            }
            return self.status
        except Exception as exc:  # noqa: BLE001 - a malformed patch is a named rejection
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "invalid",
                "ok": False, "error": {"code": "KERNEL_PROFILE_INVALID", "message": str(exc)},
            }
            return self.status
        if loaded is None:
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "absent", "rows": [],
            }
            return self.status
        return await self._apply_loaded(loaded)

    async def reload(self) -> dict[str, Any]:
        """Re-read the profile and swap generations; a broken edit keeps the old one."""
        try:
            loaded = self.load()
        except CompositionError as exc:
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "invalid",
                "ok": False, "error": exc.to_dict(), "keptPrevious": self.supervisor is not None,
            }
            return self.status
        except Exception as exc:  # noqa: BLE001 - keep serving the previous generation
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "invalid",
                "ok": False, "error": {"code": "KERNEL_PROFILE_INVALID", "message": str(exc)},
                "keptPrevious": self.supervisor is not None,
            }
            return self.status
        if loaded is None:
            if self.supervisor is not None:
                await self.supervisor.stop()
                self.supervisor = None
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "absent", "rows": [],
            }
            return self.status
        previous = self.loaded
        if self.supervisor is not None:
            await self.supervisor.stop()
            self.supervisor = None
        status = await self._apply_loaded(loaded)
        if status.get("status") == "failed" and previous is not None:
            restored = await self._apply_loaded(previous)
            status["rolledBackToPrevious"] = restored.get("status") == "applied"
        return status

    async def _apply_loaded(self, loaded: Profile) -> dict[str, Any]:
        self.loaded = loaded
        if not getattr(self.runtime, "active", True):
            # Shadow: validate what would mount, report it, change nothing.
            problems = []
            for row in loaded.composition.enabled():
                if row.group or row.name.startswith("builtin:"):
                    continue
                try:
                    from .plugin import resolve_plugin  # noqa: PLC0415

                    resolve_plugin(row.name)
                except Exception as exc:  # noqa: BLE001 - reported, never mounted
                    problems.append({"code": "KERNEL_SHADOW_ROW_INVALID", "row": row.id, "message": str(exc)})
            self.status = {
                "enabled": True,
                "active": False,
                "mode": "shadow",
                "profile": self.profile_name,
                "status": "shadow",
                "ok": not problems,
                "rows": [row.to_dict() for row in loaded.composition.rows],
                "diagnostics": [*loaded.composition.diagnostics, *problems],
            }
            return self.status
        supervisor = Supervisor(self.runtime.kernel)
        try:
            result = await supervisor.apply(loaded.composition)
        except SupervisorError as exc:
            self.status = {
                "enabled": True, "profile": self.profile_name, "status": "failed",
                "ok": False, "error": exc.to_dict(),
                "rows": [row.to_dict() for row in loaded.composition.rows],
                "diagnostics": loaded.composition.diagnostics,
            }
            log.warning("kernel profile %s failed and was rolled back: %s", self.profile_name, exc)
            return self.status
        self.supervisor = supervisor
        if getattr(self.runtime, "composition_rows", None) is not None:
            self.runtime.composition_rows.attach(supervisor)
        self.status = {
            "enabled": True,
            "profile": self.profile_name,
            "status": "applied",
            "ok": True,
            "rows": [row.to_dict() for row in loaded.composition.rows],
            "diagnostics": loaded.composition.diagnostics,
            "result": result,
        }
        return self.status

    async def stop(self) -> None:
        watcher = getattr(self, "watcher", None)
        if watcher is not None:
            await watcher.stop()
            self.watcher = None
        if self.supervisor is not None:
            await self.supervisor.stop()
            self.supervisor = None
        self.status = {"enabled": getattr(self.runtime, "enabled", False), "status": "stopped", "rows": []}


async def start_profile(service: Any) -> dict[str, Any]:
    runtime = getattr(service, "kernel_runtime", None)
    if runtime is None:
        from .services.runtime import kernel_runtime  # noqa: PLC0415

        runtime = kernel_runtime
    boot = ProfileBoot(runtime)
    service._kernel_profile = boot
    status = await boot.apply()
    if watch_enabled() and getattr(runtime, "active", False):
        from .watch import ProfileWatcher  # noqa: PLC0415

        boot.watcher = ProfileWatcher(boot)
        await boot.watcher.start()
        status = {**status, "watch": True}
    return status


async def stop_profile(service: Any) -> None:
    boot = getattr(service, "_kernel_profile", None)
    if boot is not None:
        await boot.stop()
