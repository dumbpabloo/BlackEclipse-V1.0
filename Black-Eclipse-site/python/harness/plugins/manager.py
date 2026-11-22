"""The Plugin Manager — the ONE authority for plugin lifecycle and composition.

It owns the registry, the runtime instances, the event bridge, the hook registry,
per-plugin config/secrets/storage and the health counters, and it exposes the
lifecycle operations: install, validate, enable, disable, start, stop, update,
rollback, uninstall and repair. State is persisted in `agent-memory.db`, so it
survives an Eclipse restart; `start_enabled()` reloads everything that was on.

Everything the manager does is transactional where it matters: an update
snapshots first and rolls back automatically on failure; an install is atomic
(write to a temp directory, then swap).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..persistence import now_iso
from . import doctor as doctor_mod
from . import health as health_mod
from . import marketplace
from .config import ConfigInvalid, resolve as resolve_config, validate_values
from .errors import (
    CompatibilityError,
    DependencyError,
    ManifestError,
    NotFound,
    PermissionDenied,
    PluginError,
)
from .events import PluginEventBridge
from .hooks import HookOutcome, HookRegistry
from .http import PluginHttp
from .loader import checksum, discover, evict_modules, find_manifest_file, instantiate, module_names_under, read_manifest
from .manifest import ECLIPSE_VERSION, PluginManifest
from .model import PluginRecord, PluginState, TrustLevel, assert_transition, can_transition
from .paths import bundled_root as _bundled_root, ensure_dir, plugin_data_dir, plugins_root as _plugins_root
from .permissions import ELEVATED_PERMISSIONS, GrantedPermissions
from .registry import ContributionRegistry
from .runtime import PluginContext
from .secrets import PluginSecrets, SecretsUnavailable, UnavailableSecrets
from .store import PluginStore
from .storage import PluginStorage

log = logging.getLogger("harness.plugins")

_SNAPSHOT_KEYS = ("manifest", "installationPath", "version", "checksum", "permissions", "trust")


@dataclass
class _Loaded:
    record: PluginRecord
    manifest: PluginManifest
    instance: Any
    ctx: PluginContext
    storage: PluginStorage
    permissions: GrantedPermissions
    workers: list[asyncio.Task[Any]] = field(default_factory=list)
    started_at: float = 0.0


class PluginManager:
    def __init__(
        self,
        store: Any,
        tool_registry: Any,
        bus: Any = None,
        service: Any = None,
        *,
        plugins_dir: Optional[Path] = None,
        bundled_dir: Optional[Path] = None,
    ) -> None:
        self._agent_store = store
        self._tool_registry = tool_registry
        self._bus = bus
        self._service = service
        self._plugins_dir = Path(plugins_dir) if plugins_dir else _plugins_root()
        self._bundled_dir = Path(bundled_dir) if bundled_dir else _bundled_root()

        self.store = PluginStore(store)
        try:
            self.secrets: Any = PluginSecrets(self.store)
            self.secrets_error: Optional[str] = None
        except SecretsUnavailable as exc:
            self.secrets = UnavailableSecrets(str(exc))
            self.secrets_error = str(exc)

        self.events = PluginEventBridge(bus=bus, on_failure=self._event_failure, context_for=self._context_for)
        if bus is not None:
            self.events.connect(bus)
        self.hooks = HookRegistry(on_failure=self._hook_failure)
        self.commands: dict[str, dict[str, Any]] = {}
        self.contributions = ContributionRegistry(
            tool_registry=tool_registry,
            event_bridge=self.events,
            hook_registry=self.hooks,
            command_registry=self.commands,
            telemetry=None,
        )
        self._active: dict[str, _Loaded] = {}
        self._ui_handlers: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # Test/integration seam: an httpx transport used for OAuth and registry
        # HTTP. None = a real network client.
        self._oauth_transport: Any = None

    # ── paths / discovery ────────────────────────────────────────────────────

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    def bundled_dirs(self) -> list[Path]:
        return discover(self._bundled_dir)

    def installed_dirs(self) -> list[Path]:
        return discover(self._plugins_dir)

    def directory_of(self, plugin_id: str) -> Optional[Path]:
        record = self.store.get_plugin(plugin_id)
        if record and record.installation_path and Path(record.installation_path).is_dir():
            return Path(record.installation_path)
        candidate = self._plugins_dir / plugin_id
        if candidate.is_dir() and (candidate / "plugin.yaml").exists() or (candidate / "plugin.json").exists():
            return candidate
        for bundled in self.bundled_dirs():
            try:
                manifest, _ = read_manifest(bundled)
            except Exception:  # noqa: BLE001
                continue
            if manifest.id == plugin_id:
                return bundled
        return None

    def path_of(self, plugin_id: str) -> Optional[str]:
        directory = self.directory_of(plugin_id)
        return str(directory) if directory else None

    def manifest_of(self, plugin_id: str) -> Optional[PluginManifest]:
        record = self.store.get_plugin(plugin_id)
        if record and record.manifest:
            try:
                return PluginManifest.model_validate(record.manifest)
            except Exception:  # noqa: BLE001 - a stored-but-invalid manifest is not a manifest
                pass
        directory = self.directory_of(plugin_id)
        if directory:
            try:
                manifest, _ = read_manifest(directory)
                return manifest
            except Exception:  # noqa: BLE001
                return None
        return None

    def discover(self) -> list[dict[str, Any]]:
        """Scan installed + bundled plugins and return validated descriptors.

        Discovery never enables anything: it reports what could be installed.
        """
        found: dict[str, dict[str, Any]] = {}
        for source, base in (("bundled", self._bundled_dir), ("installed", self._plugins_dir)):
            for path in discover(base):
                try:
                    manifest, _ = read_manifest(path)
                except PluginError as exc:
                    found[f"{source}:{path.name}"] = {"path": str(path), "valid": False, "error": exc.message}
                    continue
                found[manifest.id] = {
                    "id": manifest.id, "name": manifest.name, "version": manifest.version,
                    "source": source, "path": str(path), "valid": True,
                    "installed": self.store.get_plugin(manifest.id) is not None,
                }
        return list(found.values())

    # ── install ──────────────────────────────────────────────────────────────

    async def install(
        self, source: Any, *, trust: Optional[str] = None, enable: bool = False,
        operator: bool = False, replace: bool = False,
    ) -> dict[str, Any]:
        src_dir, package = self._resolve_source(source)
        if src_dir is not None:
            manifest, _raw = read_manifest(src_dir)
        else:
            manifest = self._manifest_from_package(package)

        existing = self.store.get_plugin(manifest.id)
        if existing is not None and not replace:
            # A bundled or already-installed plugin with the same id and version
            # is a no-op reinstall; a different version requires `replace`.
            if existing.version == manifest.version:
                if enable and not existing.enabled:
                    await self.enable(manifest.id, operator=operator)
                return {"ok": True, "alreadyInstalled": True, "plugin": existing.to_dict()}
            raise PluginError(
                "PLUGIN_ALREADY_INSTALLED",
                f"{manifest.id} {existing.version} is installed; use update to move to {manifest.version}",
            )

        # Compatibility + dependency preconditions BEFORE touching disk.
        reason = manifest.check_eclipse_version(ECLIPSE_VERSION)
        if reason:
            raise CompatibilityError(reason)

        target = self._plugins_dir / manifest.id
        placed = self._place_package(src_dir, package, target, manifest)
        digest = checksum(target)
        official = trust == "trusted" or self._is_bundled(src_dir)
        # C6: signature + review gate (official/bundled packages are exempt).
        from .signing import check_install  # noqa: PLC0415

        gate = {
            "verification": "official", "signed": False, "reviewed": bool(operator),
        }
        if not official:
            try:
                gate = check_install(
                    manifest, digest, operator=operator, directory=target,
                )
            except Exception:
                # A refused install leaves no package behind.
                shutil.rmtree(target, ignore_errors=True)
                raise
        verification = gate["verification"] if not official else "official"
        record = PluginRecord(
            plugin_id=manifest.id, name=manifest.name, version=manifest.version,
            publisher=manifest.publisher_id, manifest=manifest.to_dict(),
            status=PluginState.INSTALLED, enabled=False,
            trust=TrustLevel(trust) if trust else (
                TrustLevel.VERIFIED if self._is_bundled(src_dir) else TrustLevel.COMMUNITY
            ),
            installation_path=str(target), sdk_version=manifest.sdk_version,
            runtime_type=manifest.runtime.type, permissions=list(manifest.permissions),
            dependencies=[d.model_dump() for d in manifest.dependencies],
            checksum=digest, verification=verification,
            installed_at=now_iso(), updated_at=now_iso(),
        )
        self.store.upsert_plugin(record)
        self.store.record_history(manifest.id, "install", "ok", to_version=manifest.version,
                                  snapshot=self._snapshot(record))
        self.store.log(manifest.id, "info", f"installed {manifest.version} from {placed}")

        if enable:
            await self.enable(manifest.id, operator=operator)
        return {"ok": True, "plugin": self.store.get_plugin(manifest.id).to_dict()}

    async def install_package(self, manifest: dict[str, Any], files: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        """Install from an in-memory package (a manifest dict + text files)."""
        return await self.install({"manifest": manifest, "files": files}, **kwargs)

    # ── enable / disable ─────────────────────────────────────────────────────

    async def enable(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        async with self._lock:
            return await self._enable(plugin_id, operator=operator)

    async def _enable(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        record = self.store.get_plugin(plugin_id)
        if record is None:
            raise NotFound(plugin_id)
        if plugin_id in self._active and record.status == PluginState.RUNNING:
            return {"ok": True, "plugin": record.to_dict(), "alreadyEnabled": True}
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            self.store.set_state(plugin_id, PluginState.FAILED, enabled=False,
                                 last_error="manifest missing")
            raise ManifestError(f"manifest for {plugin_id!r} is missing or invalid")

        self._check_dependencies(manifest)
        elevated = sorted(set(manifest.permissions) & ELEVATED_PERMISSIONS)
        if elevated and not operator:
            raise PermissionDenied(elevated[0], plugin_id=plugin_id, action="enable")

        if record.status == PluginState.QUARANTINED:
            raise PluginError("PLUGIN_QUARANTINED", "plugin is quarantined; run repair first")
        if record.status == PluginState.FAILED:
            assert_transition(record.status, PluginState.INSTALLED)
            self.store.set_state(plugin_id, PluginState.INSTALLED)
            record = self.store.get_plugin(plugin_id)

        assert_transition(record.status, PluginState.ENABLED)
        self.store.set_state(plugin_id, PluginState.ENABLED, enabled=True, last_error="")
        try:
            loaded = await self._load(record, manifest)
        except Exception as exc:  # noqa: BLE001 - a load failure must be observable
            self.store.set_state(plugin_id, PluginState.FAILED, enabled=False, last_error=str(exc))
            self.store.bump_health(plugin_id, crashes=1)
            self.store.record_history(plugin_id, "enable", "failed")
            self.store.log(plugin_id, "error", f"enable failed: {exc}")
            self._publish("plugin.failed", {"pluginId": plugin_id, "error": str(exc)})
            raise
        self.store.set_state(plugin_id, PluginState.RUNNING, enabled=True, health="unknown")
        self.store.bump_health(plugin_id, restarts=0)
        self.store.record_history(plugin_id, "enable", "ok", to_version=manifest.version)
        self._publish("plugin.enabled", {"pluginId": plugin_id, "version": manifest.version})
        return {"ok": True, "plugin": self.store.get_plugin(plugin_id).to_dict(),
                "contributions": self.contributions.descriptors(plugin_id)}

    async def disable(self, plugin_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._disable(plugin_id)

    async def _disable(self, plugin_id: str) -> dict[str, Any]:
        record = self.store.get_plugin(plugin_id)
        if record is None:
            raise NotFound(plugin_id)
        if plugin_id in self._active:
            await self._unload(plugin_id)
        if can_transition(record.status, PluginState.DISABLED):
            self.store.set_state(plugin_id, PluginState.DISABLED, enabled=False)
        else:
            self.store.set_state(plugin_id, PluginState.DISABLED, enabled=False)
        self.store.record_history(plugin_id, "disable", "ok")
        self._publish("plugin.disabled", {"pluginId": plugin_id})
        return {"ok": True, "plugin": self.store.get_plugin(plugin_id).to_dict()}

    async def start(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        return await self.enable(plugin_id, operator=operator)

    async def stop(self, plugin_id: str) -> dict[str, Any]:
        return await self.disable(plugin_id)

    async def reload(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        """Re-import an installed plugin's code in place (operator/dev only).

        Exact unload first (workers, contributions, UI, storage), then evict the
        plugin's modules and re-import. Bundled and signed/verified plugins are
        refused: their code is pinned, so a change is an update, not a reload.
        A failed re-import leaves the plugin unloaded and `FAILED` (never a
        half-mounted tree).
        """
        if not operator:
            raise PluginError("PLUGIN_RELOAD_OPERATOR", "reload requires an explicit operator action")
        async with self._lock:
            record = self.store.get_plugin(plugin_id)
            if record is None:
                raise NotFound(plugin_id)
            if plugin_id not in self._active:
                raise PluginError("PLUGIN_NOT_RUNNING", "only a running plugin can be reloaded")
            directory = self.directory_of(plugin_id)
            if directory is None:
                raise PluginError("PLUGIN_RELOAD_FAILED", "installation directory is missing")
            if self._is_bundled(directory):
                raise PluginError("PLUGIN_RELOAD_BUNDLED", "bundled plugins are shipped code; update the app instead")
            if record.trust in (TrustLevel.TRUSTED, TrustLevel.VERIFIED) or record.verification in (
                "signed", "trusted-publisher",
            ):
                raise PluginError(
                    "PLUGIN_RELOAD_TRUSTED",
                    "a signed or verified plugin cannot be hot-reloaded; install a new signed update",
                )
            manifest = self.manifest_of(plugin_id)
            if manifest is None:
                raise ManifestError(f"manifest for {plugin_id!r} is missing or invalid")
            if manifest.id != plugin_id:
                raise PluginError("PLUGIN_ID_MISMATCH", f"manifest id {manifest.id!r} != {plugin_id!r}")
            previous = record.checksum
            evicted = module_names_under(directory)
            await self._unload(plugin_id)
            record.last_error = None
            try:
                await self._load(record, manifest, verify_integrity=False)
            except Exception as exc:  # noqa: BLE001 - observable failure, exact unload already done
                self.store.set_state(plugin_id, PluginState.FAILED, enabled=False, last_error=str(exc))
                self.store.bump_health(plugin_id, crashes=1)
                self.store.record_history(plugin_id, "reload", "failed")
                self.store.log(plugin_id, "error", f"reload failed: {exc}")
                self._publish("plugin.failed", {"pluginId": plugin_id, "error": str(exc), "reload": True})
                raise
            record.checksum = checksum(directory)
            record.verification = "unsigned"
            record.last_error = None
            self.store.upsert_plugin(record)
            self.store.set_state(plugin_id, PluginState.RUNNING, enabled=True, health="unknown")
            self.store.record_history(plugin_id, "reload", "ok")
            self.store.log(plugin_id, "info", f"reloaded from {directory}")
            self._publish("plugin.reloaded", {"pluginId": plugin_id, "version": manifest.version})
            return {
                "ok": True,
                "reloaded": True,
                "changed": record.checksum != previous,
                "evictedModules": evicted,
                "plugin": self.store.get_plugin(plugin_id).to_dict(),
                "contributions": self.contributions.descriptors(plugin_id),
            }

    # ── update / rollback / repair ───────────────────────────────────────────

    async def update(self, plugin_id: str, source: Any, *, operator: bool = False) -> dict[str, Any]:
        async with self._lock:
            record = self.store.get_plugin(plugin_id)
            if record is None:
                raise NotFound(plugin_id)
            src_dir, package = self._resolve_source(source)
            if src_dir is not None:
                new_manifest, _ = read_manifest(src_dir)
            else:
                new_manifest = self._manifest_from_package(package)
            if new_manifest.id != plugin_id:
                raise PluginError("PLUGIN_ID_MISMATCH",
                                  f"update package id {new_manifest.id!r} != {plugin_id!r}")
            was_enabled = bool(record.enabled) or plugin_id in self._active
            snapshot = self._snapshot(record)
            prior_version = record.version

            assert_transition(record.status, PluginState.UPDATING)
            self.store.set_state(plugin_id, PluginState.UPDATING)
            self.store.record_history(plugin_id, "update", "started",
                                      from_version=prior_version, to_version=new_manifest.version,
                                      snapshot=snapshot)
            if plugin_id in self._active:
                await self._unload(plugin_id)

            target = self._plugins_dir / plugin_id
            backup = self._plugins_dir / f".backup-{plugin_id}"
            try:
                reason = new_manifest.check_eclipse_version(ECLIPSE_VERSION)
                if reason:
                    raise CompatibilityError(reason)
                self._apply_package(src_dir, package, target, backup, new_manifest)
                digest = checksum(target)
                manifest = new_manifest
                # C6: the update path is an install too; a required signature or
                # review that is missing rolls back via the except block below.
                from .signing import check_install  # noqa: PLC0415

                gate = check_install(manifest, digest, operator=operator, directory=target)
                record = PluginRecord(
                    plugin_id=plugin_id, name=manifest.name, version=manifest.version,
                    publisher=manifest.publisher_id, manifest=manifest.to_dict(),
                    status=PluginState.INSTALLED, enabled=False, trust=record.trust,
                    installation_path=str(target), sdk_version=manifest.sdk_version,
                    runtime_type=manifest.runtime.type, permissions=list(manifest.permissions),
                    dependencies=[d.model_dump() for d in manifest.dependencies],
                    checksum=digest,
                    verification="signature" if gate["signed"] else record.verification,
                    installed_at=record.installed_at, updated_at=now_iso(),
                )
                self.store.upsert_plugin(record)
                if backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                self.store.set_state(plugin_id, PluginState.INSTALLED, enabled=False)
                self.store.record_history(plugin_id, "update", "ok",
                                          from_version=prior_version, to_version=manifest.version)
                if was_enabled:
                    return await self._enable(plugin_id, operator=operator)
                return {"ok": True, "plugin": self.store.get_plugin(plugin_id).to_dict()}
            except Exception as exc:  # noqa: BLE001 - update failure rolls back automatically
                self.store.set_state(plugin_id, PluginState.ROLLING_BACK, last_error=str(exc))
                self._restore_backup(plugin_id, target, backup)
                self._restore_record_from_snapshot(plugin_id, snapshot)
                self.store.set_state(plugin_id, PluginState.INSTALLED,
                                     enabled=bool(was_enabled), last_error=str(exc))
                self.store.record_history(plugin_id, "update", "rolled_back",
                                          from_version=prior_version, to_version=new_manifest.version)
                self.store.log(plugin_id, "error", f"update failed, rolled back: {exc}")
                if was_enabled:
                    await self._enable(plugin_id, operator=operator)
                raise

    async def rollback(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        async with self._lock:
            record = self.store.get_plugin(plugin_id)
            if record is None:
                raise NotFound(plugin_id)
            # The newest recorded snapshot of a DIFFERENT version (a snapshot of
            # the current version is not an earlier state).
            history = self.store.list_history(plugin_id, limit=50)
            snapshot = next(
                (h["snapshot"] for h in history
                 if h.get("snapshot") and h["snapshot"].get("manifest")
                 and h["snapshot"].get("installationPath")
                 and h["snapshot"].get("version") != record.version),
                None,
            )
            if snapshot is None:
                raise PluginError("PLUGIN_ROLLBACK_UNAVAILABLE", "no earlier version to roll back to")
            was_enabled = bool(record.enabled) or plugin_id in self._active
            assert_transition(record.status, PluginState.ROLLING_BACK)
            self.store.set_state(plugin_id, PluginState.ROLLING_BACK)
            if plugin_id in self._active:
                await self._unload(plugin_id)
            manifest = PluginManifest.model_validate(snapshot["manifest"])
            record.manifest = manifest.to_dict()
            record.version = manifest.version
            record.installation_path = snapshot.get("installationPath", record.installation_path)
            record.checksum = snapshot.get("checksum", record.checksum)
            record.permissions = list(snapshot.get("permissions", manifest.permissions))
            record.status = PluginState.INSTALLED
            record.updated_at = now_iso()
            self.store.upsert_plugin(record)
            self.store.set_state(plugin_id, PluginState.INSTALLED, enabled=False)
            self.store.record_history(plugin_id, "rollback", "ok", to_version=manifest.version)
            if was_enabled:
                return await self._enable(plugin_id, operator=operator)
            return {"ok": True, "plugin": self.store.get_plugin(plugin_id).to_dict()}

    async def repair(self, plugin_id: str, *, operator: bool = False) -> dict[str, Any]:
        record = self.store.get_plugin(plugin_id)
        if record is None:
            raise NotFound(plugin_id)
        directory = self.directory_of(plugin_id)
        if directory is None:
            raise PluginError("PLUGIN_REPAIR_FAILED", "installation directory is missing")
        try:
            manifest, _ = read_manifest(directory)
        except PluginError as exc:
            self.store.set_state(plugin_id, PluginState.FAILED, enabled=False, last_error=exc.message)
            raise
        was_enabled = bool(record.enabled) or plugin_id in self._active
        if plugin_id in self._active:
            await self._unload(plugin_id)
        record.manifest = manifest.to_dict()
        record.version = manifest.version
        record.permissions = list(manifest.permissions)
        record.checksum = checksum(directory)
        record.installation_path = str(directory)
        record.updated_at = now_iso()
        record.status = PluginState.INSTALLED
        self.store.upsert_plugin(record)
        self.store.set_state(plugin_id, PluginState.INSTALLED, enabled=False, last_error="", health="unknown")
        self.store.record_history(plugin_id, "repair", "ok", to_version=manifest.version)
        if was_enabled:
            return await self.enable(plugin_id, operator=operator)
        return {"ok": True, "plugin": self.store.get_plugin(plugin_id).to_dict()}

    async def uninstall(self, plugin_id: str) -> dict[str, Any]:
        async with self._lock:
            record = self.store.get_plugin(plugin_id)
            if record is None:
                raise NotFound(plugin_id)
            if plugin_id in self._active:
                await self._unload(plugin_id)
            self.contributions.unregister_all(plugin_id)
            self.commands.pop(plugin_id, None)
            # Remove installed files only when they live under the plugin root
            # (never a bundled reference plugin).
            directory = self.directory_of(plugin_id)
            if directory and self._is_under(directory, self._plugins_dir):
                shutil.rmtree(directory, ignore_errors=True)
            self.store.delete_plugin(plugin_id)
            shutil.rmtree(plugin_data_dir(plugin_id), ignore_errors=True)
            self._publish("plugin.uninstalled", {"pluginId": plugin_id})
            return {"ok": True, "pluginId": plugin_id, "uninstalled": True}

    # ── load / unload internals ──────────────────────────────────────────────

    async def _load(self, record: PluginRecord, manifest: PluginManifest,
                    *, verify_integrity: bool = True) -> _Loaded:
        directory = self.directory_of(record.plugin_id)
        if directory is None:
            raise PluginError("PLUGIN_LOAD_FAILED", "installation directory is missing")
        if verify_integrity:
            integrity = self.check_integrity(record.plugin_id)
            if not integrity.get("ok"):
                raise PluginError("PLUGIN_INTEGRITY_FAILED",
                                  f"checksum mismatch for {record.plugin_id}: {integrity.get('detail')}")
        permissions = GrantedPermissions.from_declared(record.plugin_id, manifest.permissions)
        ctx = self._build_context(record, manifest, permissions)
        storage = ctx.storage
        instance: Any = None
        try:
            if self._should_sandbox(record):
                from .sandbox import SandboxedPlugin  # noqa: PLC0415

                instance = SandboxedPlugin(self, record, manifest, directory, ctx)
                await instance.start()
            elif manifest.runtime.type == "python":
                instance = instantiate(manifest, directory)
            else:
                instance = _DeclarativeInstance(manifest)
            registered = self.contributions.register_all(record, manifest, instance, ctx)
        except Exception:
            storage.close()
            if instance is not None and hasattr(instance, "close"):
                try:
                    await instance.close()
                except Exception:  # noqa: BLE001
                    pass
            evict_modules(directory)
            raise
        for desc in registered:
            self.store.register_contribution(record.plugin_id, desc["kind"], desc["name"], desc.get("meta"))
        self._collect_ui_handlers(record.plugin_id, instance)
        loaded = _Loaded(record=record, manifest=manifest, instance=instance, ctx=ctx,
                         storage=storage, permissions=permissions, started_at=time.monotonic())
        self._active[record.plugin_id] = loaded
        try:
            await _maybe_await(instance.on_load(ctx))
            await _maybe_await(instance.on_enable(ctx))
            loaded.workers = self._start_workers(record.plugin_id, registered, instance, ctx)
        except Exception:
            await self._unload(record.plugin_id)
            raise
        self.store.bump_health(record.plugin_id, uptimeS=0.0)
        self.store.set_health(record.plugin_id, status="healthy", startedAt=now_iso())
        return loaded

    async def _unload(self, plugin_id: str) -> None:
        self._ui_handlers.pop(plugin_id, None)
        loaded = self._active.pop(plugin_id, None)
        if loaded is None:
            self.contributions.unregister_all(plugin_id)
            directory = self.directory_of(plugin_id)
            if directory is not None:
                evict_modules(directory)
            return
        for task in loaded.workers:
            task.cancel()
        if loaded.workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*loaded.workers, return_exceptions=True), timeout=2.0
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):  # noqa: PERF203
                pass  # a stubborn worker must not block unload
        try:
            await _maybe_await(loaded.instance.on_disable(loaded.ctx))
        except Exception as exc:  # noqa: BLE001 - disable failures are logged, not fatal
            self.store.log(plugin_id, "warning", f"on_disable raised: {exc}")
        try:
            await _maybe_await(loaded.instance.on_unload(loaded.ctx))
        except Exception as exc:  # noqa: BLE001
            self.store.log(plugin_id, "warning", f"on_unload raised: {exc}")
        self.contributions.unregister_all(plugin_id)
        try:
            if loaded.started_at:
                self.store.bump_health(plugin_id, uptimeS=max(0.0, time.monotonic() - loaded.started_at))
            loaded.storage.close()
        except Exception:  # noqa: BLE001
            pass
        # Module isolation: a disabled plugin leaves no importable modules behind,
        # so re-enabling (or a different version) cannot see stale code.
        directory = self.directory_of(plugin_id)
        if directory is not None:
            evicted = evict_modules(directory)
            if evicted:
                self.store.log(plugin_id, "info", f"unloaded modules: {', '.join(evicted)}")

    def _build_context(self, record: PluginRecord, manifest: PluginManifest,
                       permissions: GrantedPermissions) -> PluginContext:
        stored = self.store.get_config(record.plugin_id)
        config = resolve_config(manifest, stored)
        storage = PluginStorage(self.store, manifest, permissions)
        allowed_hosts = None
        try:
            value = config.get("allowedHosts", None)
            if isinstance(value, list):
                allowed_hosts = [str(v) for v in value]
        except ConfigInvalid:
            allowed_hosts = None
        http = PluginHttp(
            record.plugin_id, permissions,
            timeout_s=20.0,
            allowed_hosts=allowed_hosts,
            on_telemetry=lambda name, data: self.plugin_telemetry(record.plugin_id, name, data),
        )
        auth_client = None
        if manifest.authentication.type == "oauth2":
            from .oauth import OAuthClient  # noqa: PLC0415

            auth_client = OAuthClient(
                self.secrets, record.plugin_id, manifest.authentication,
                transport=self._oauth_transport,
            )
        return PluginContext(
            manifest=manifest, permissions=permissions, config=config, storage=storage,
            secrets=self.secrets, http=http, host=self, store=self.store, auth=auth_client,
        )

    def _start_workers(self, plugin_id: str, registered: list[dict[str, Any]], instance: Any, ctx: Any) -> list[asyncio.Task[Any]]:
        workers: list[asyncio.Task[Any]] = []
        for desc in registered:
            if desc["kind"] != "worker":
                continue
            handler = self._worker_handler(instance, desc["name"])
            if handler is None:
                continue
            interval = float(desc.get("meta", {}).get("intervalSeconds", 0) or 0)

            async def _runner(handler: Any = handler, interval: float = interval, pid: str = plugin_id) -> None:
                try:
                    while True:
                        try:
                            await _maybe_await(handler(ctx))
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - a worker failure is data
                            self.store.log(pid, "error", f"worker raised: {exc}")
                            self.store.bump_health(pid, crashes=1)
                        if interval <= 0:
                            return
                        await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

            workers.append(asyncio.create_task(_runner()))
        return workers

    @staticmethod
    def _worker_handler(instance: Any, name: str) -> Optional[Callable[..., Any]]:
        for contribution in (instance.contributions() if hasattr(instance, "contributions") else []):
            if contribution.get("kind") == "worker" and contribution.get("name") == name:
                return contribution["handler"]  # a bound method: (ctx) -> coroutine
        return None

    def _collect_ui_handlers(self, plugin_id: str, instance: Any) -> None:
        try:
            contributions = instance.contributions() if hasattr(instance, "contributions") else []
        except Exception:  # noqa: BLE001
            contributions = []
        handlers = {
            c["id"]: c["handler"]
            for c in contributions
            if c.get("kind") == "ui" and c.get("id") and c.get("handler") is not None
        }
        if handlers:
            self._ui_handlers[plugin_id] = handlers

    async def render_ui(self, plugin_id: str, panel_id: str) -> dict[str, Any]:
        """Render a plugin UI panel to a safe, declarative view spec.

        The plugin runs under its own permission-checked context and returns data,
        never DOM/HTML — the host renders a fixed vocabulary of view types.
        """
        if plugin_id not in self._active:
            raise PluginError("PLUGIN_NOT_ACTIVE", f"plugin {plugin_id!r} is not running")
        handler = self._ui_handlers.get(plugin_id, {}).get(panel_id)
        if handler is None:
            raise PluginError("PLUGIN_UI_UNKNOWN", f"no UI panel {panel_id!r} on {plugin_id!r}")
        ctx = self._context_for(plugin_id)
        result = handler(ctx) if _accepts_ctx(handler) else handler()
        if asyncio.iscoroutine(result):
            result = await result
        return {"pluginId": plugin_id, "panelId": panel_id, "view": _normalize_view(result)}

    def ui_panels(self, plugin_id: str) -> list[dict[str, Any]]:
        return [
            {"id": c["name"], "title": c.get("meta", {}).get("title") or c["name"],
             "kind": c.get("meta", {}).get("uiKind", "panel")}
            for c in self.contribution_descriptors(plugin_id) if c.get("kind") == "ui"
        ]

    # ── commands / hooks / capabilities ──────────────────────────────────────

    async def run_command(self, plugin_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        commands = self.commands.get(plugin_id)
        if not commands or name not in commands:
            raise PluginError("PLUGIN_COMMAND_NOT_FOUND", f"plugin {plugin_id!r} has no command {name!r}")
        item = commands[name]
        ctx = self._context_for(plugin_id)
        result = item["handler"](args or {}, ctx)
        if asyncio.iscoroutine(result):
            result = await result
        return {"ok": True, "pluginId": plugin_id, "command": name, "result": result}

    def commands_for(self, plugin_id: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for pid, commands in self.commands.items():
            if plugin_id and pid != plugin_id:
                continue
            for name, item in commands.items():
                out.append({"pluginId": pid, "name": name, "description": item.get("description", ""),
                            "schema": item.get("schema")})
        return out

    async def run_hook(self, name: str, payload: Optional[dict[str, Any]] = None) -> HookOutcome:
        if not self.hooks.has(name):
            return HookOutcome(name=name, proceed=True, data=dict(payload or {}))
        return await self.hooks.run(name, payload, context_for=self._context_for)

    def capabilities(self) -> list[dict[str, Any]]:
        caps: list[dict[str, Any]] = []
        for record in self.store.list_plugins():
            for desc in self.contributions.descriptors(record.plugin_id):
                caps.append({
                    "capability": desc["name"],
                    "kind": desc["kind"],
                    "namespaced": desc.get("namespaced"),
                    "source_type": "plugin",
                    "plugin_id": record.plugin_id,
                    "plugin_version": record.version,
                    "enabled": record.enabled,
                })
        return caps

    async def invoke_capability(self, capability: str, args: dict[str, Any], run_ctx: Any) -> Any:
        """Invoke a plugin tool by bare name, namespaced name, or `plugin.cap`."""
        from .registry import tool_name_for

        target = capability
        if not target.startswith("plug_"):
            if "." in capability:
                plugin_id, _, bare = capability.partition(".")
                target = tool_name_for(plugin_id, bare)
            else:
                # Resolve a unique bare capability across plugins.
                matches = [c["namespaced"] for c in self.capabilities()
                           if c["kind"] == "tool" and c["capability"] == capability and c["namespaced"]]
                if len(matches) != 1:
                    raise PluginError(
                        "PLUGIN_CAPABILITY_AMBIGUOUS" if matches else "PLUGIN_CAPABILITY_NOT_FOUND",
                        f"capability {capability!r} resolves to {len(matches)} tool(s)",
                    )
                target = matches[0]
        tool = self._tool_registry.get(target)
        if tool is None:
            raise PluginError("PLUGIN_CAPABILITY_NOT_FOUND", f"no enabled plugin provides {capability!r}")
        result = await self._tool_registry.execute(target, f"plugin_{uuid.uuid4().hex[:8]}", args, run_ctx)
        if not result.ok:
            raise PluginError(result.error.code if result.error else "PLUGIN_TOOL_FAILED", result.text)
        return result

    # ── config / secrets / webhooks ──────────────────────────────────────────

    def config_view(self, plugin_id: str) -> dict[str, Any]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        stored = self.store.get_config(plugin_id)
        try:
            config = resolve_config(manifest, stored)
        except ConfigInvalid:
            config = None
        presence = set(self.store.list_secret_names(plugin_id))
        view = config.public_view(presence) if config else {"fields": []}
        view["pluginId"] = plugin_id
        return view

    def set_config(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        clean = validate_values(manifest, values)
        secret_keys = set(manifest.secret_keys())
        rebuilt: Optional[PluginContext] = None
        for key, value in clean.items():
            if key in secret_keys or key in self.store.list_secret_names(plugin_id):
                if isinstance(value, str):
                    self.secrets.set(plugin_id, key, value)
                continue
            self.store.set_config(plugin_id, key, value)
        # A live plugin gets a rebuilt context so the new values take effect.
        loaded = self._active.get(plugin_id)
        if loaded is not None:
            rebuilt = self._build_context(loaded.record, loaded.manifest, loaded.permissions)
            loaded.ctx.config = rebuilt.config
        return {"ok": True, "config": self.config_view(plugin_id)}

    def set_secret(self, plugin_id: str, name: str, value: str) -> dict[str, Any]:
        if self.store.get_plugin(plugin_id) is None:
            raise NotFound(plugin_id)
        self.secrets.set(plugin_id, name, value)
        return {"ok": True, "pluginId": plugin_id, "name": name}

    def delete_secret(self, plugin_id: str, name: str) -> dict[str, Any]:
        self.secrets.delete(plugin_id, name)
        return {"ok": True, "pluginId": plugin_id, "name": name}

    def secret_names(self, plugin_id: str) -> list[str]:
        return self.secrets.names(plugin_id)

    def authentication_ready(self, plugin_id: str) -> bool:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            return False
        auth = manifest.authentication
        if auth.type == "none":
            return True
        # An api_key/bearer field is a secret or declared config secret.
        if auth.type == "oauth2":
            from .oauth import K_ACCESS  # noqa: PLC0415

            return self.secrets.has(plugin_id, K_ACCESS)
        candidates = {
            "api_key": [auth.api_key_field],
            "bearer": ["token"],
            "basic": [auth.username_field, auth.password_field],
        }.get(auth.type, [])
        return all(self.secrets.has(plugin_id, c) for c in candidates) if candidates else False

    # ── OAuth flows (host-managed) ───────────────────────────────────────────

    def _oauth_client(self, plugin_id: str) -> Any:
        from .oauth import OAuthClient  # noqa: PLC0415

        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        if manifest.authentication.type != "oauth2":
            raise PluginError("PLUGIN_AUTH_NOT_CONFIGURED", "this plugin declares no oauth2 authentication")
        return OAuthClient(self.secrets, plugin_id, manifest.authentication, transport=self._oauth_transport)

    async def oauth_start(self, plugin_id: str, *, client_id: Optional[str] = None, scope: str = "") -> dict[str, Any]:
        return await self._oauth_client(plugin_id).start(client_id=client_id, scope=scope)

    async def oauth_complete(self, plugin_id: str, code: str, state: str = "", *,
                             client_id: Optional[str] = None) -> dict[str, Any]:
        return await self._oauth_client(plugin_id).complete(code, state, client_id=client_id)

    async def oauth_poll(self, plugin_id: str, *, client_id: Optional[str] = None) -> dict[str, Any]:
        return await self._oauth_client(plugin_id).poll_device(client_id=client_id)

    async def oauth_refresh(self, plugin_id: str) -> dict[str, Any]:
        return await self._oauth_client(plugin_id).refresh()

    async def oauth_revoke(self, plugin_id: str) -> dict[str, Any]:
        return await self._oauth_client(plugin_id).revoke()

    def auth_status(self, plugin_id: str) -> dict[str, Any]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        if manifest.authentication.type != "oauth2":
            return {"pluginId": plugin_id, "type": manifest.authentication.type,
                    "ready": self.authentication_ready(plugin_id)}
        return {"type": "oauth2", **self._oauth_client(plugin_id).status(),
                "ready": self.authentication_ready(plugin_id)}

    async def handle_webhook(self, plugin_id: str, webhook_id: str, payload: dict[str, Any],
                             headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        declared = {w.id for w in manifest.contributes.webhooks}
        if webhook_id not in declared:
            raise PluginError("PLUGIN_WEBHOOK_UNKNOWN", f"unknown webhook {webhook_id!r}")
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        secret_name = f"webhook:{webhook_id}"
        if self.secrets.has(plugin_id, secret_name):
            expected = self.secrets.get(plugin_id, secret_name) or ""
            provided = headers.get("x-hub-signature-256") or headers.get("x-signature") or ""
            if not _verify_signature(expected, json.dumps(payload), provided):
                raise PluginError("PLUGIN_WEBHOOK_UNAUTHORIZED", "webhook signature verification failed")
        delivery = headers.get("x-delivery-id") or headers.get("x-github-delivery")
        if delivery:
            seen = self.store.kv_get(plugin_id, "webhooks", f"delivery:{delivery}")
            if seen:
                return {"ok": True, "duplicate": True, "pluginId": plugin_id}
            self.store.kv_set(plugin_id, "webhooks", f"delivery:{delivery}", now_iso())
        self.store.log_event(plugin_id, f"webhook.{webhook_id}", "received", {"keys": sorted(payload)[:20]})
        self.events.publish(plugin_id, f"{plugin_id.replace('-', '_')}.{webhook_id}", payload)
        return {"ok": True, "pluginId": plugin_id, "webhook": webhook_id}

    # ── outbound webhooks ────────────────────────────────────────────────────

    def set_webhook_target(self, plugin_id: str, webhook_id: str, url: str, *,
                           signing_secret: Optional[str] = None) -> dict[str, Any]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        declared = {w.id: w for w in manifest.contributes.webhooks}
        webhook = declared.get(webhook_id)
        if webhook is None:
            raise PluginError("PLUGIN_WEBHOOK_UNKNOWN", f"unknown webhook {webhook_id!r}")
        if webhook.direction != "outbound":
            raise PluginError("PLUGIN_WEBHOOK_DIRECTION", f"webhook {webhook_id!r} is not outbound")
        if not str(url).startswith(("http://", "https://")):
            raise PluginError("PLUGIN_WEBHOOK_URL", "target url must be http(s)")
        self.store.kv_set(plugin_id, "webhooks", f"outbound:{webhook_id}:url", str(url))
        if signing_secret is not None:
            self.secrets.set(plugin_id, f"webhook:{webhook_id}", str(signing_secret))
        return {"ok": True, "pluginId": plugin_id, "webhook": webhook_id, "url": str(url)}

    def webhook_targets(self, plugin_id: str) -> list[dict[str, Any]]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            return []
        out = []
        for webhook in manifest.contributes.webhooks:
            if webhook.direction != "outbound":
                continue
            out.append({
                "webhook": webhook.id,
                "direction": "outbound",
                "url": self.store.kv_get(plugin_id, "webhooks", f"outbound:{webhook.id}:url"),
                "signed": self.secrets.has(plugin_id, f"webhook:{webhook.id}"),
            })
        return out

    async def deliver_webhook(self, plugin_id: str, webhook_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx  # noqa: PLC0415

        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            raise NotFound(plugin_id)
        declared = {w.id: w for w in manifest.contributes.webhooks}
        webhook = declared.get(webhook_id)
        if webhook is None or webhook.direction != "outbound":
            raise PluginError("PLUGIN_WEBHOOK_UNKNOWN", f"outbound webhook {webhook_id!r} is not declared")
        url = self.store.kv_get(plugin_id, "webhooks", f"outbound:{webhook_id}:url")
        if not url:
            raise PluginError("PLUGIN_WEBHOOK_NO_TARGET", f"no target is configured for {webhook_id!r}")
        body = json.dumps(payload, default=str)
        headers = {"Content-Type": "application/json", "X-Plugin-Id": plugin_id, "X-Webhook-Id": webhook_id}
        secret = self.secrets.get(plugin_id, f"webhook:{webhook_id}") if self.secrets.has(plugin_id, f"webhook:{webhook_id}") else None
        if secret:
            headers["X-Signature-256"] = f"sha256={_sign(secret, body)}"

        last_error = ""
        attempts = 3
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(transport=self._oauth_transport, timeout=20.0) as client:
                    response = await client.post(url, content=body, headers=headers)
                if response.status_code < 300:
                    self.store.log_event(plugin_id, f"webhook.{webhook_id}.delivery", "ok",
                                         {"status": response.status_code, "attempt": attempt + 1})
                    return {"ok": True, "status": response.status_code, "attempts": attempt + 1}
                last_error = f"HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - delivery failures are data
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(min(4.0, 0.25 * (2 ** attempt)))
        self.store.log_event(plugin_id, f"webhook.{webhook_id}.delivery", "dead_letter",
                             {"error": last_error, "attempts": attempts})
        self.store.bump_health(plugin_id, httpFailures=1)
        return {"ok": False, "error": last_error, "deadLetter": True, "attempts": attempts}

    def list_webhook_deliveries(self, plugin_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        events = self.store.list_events(plugin_id) if plugin_id else self.store.list_events()
        return [e for e in events if ".delivery" in str(e.get("type", ""))][:limit]

    # ── introspection / health / doctor / marketplace ────────────────────────

    def is_active(self, plugin_id: str) -> bool:
        return plugin_id in self._active

    def contribution_descriptors(self, plugin_id: str) -> list[dict[str, Any]]:
        return self.contributions.descriptors(plugin_id) or self.store.list_contributions(plugin_id)

    def health_report(self, plugin_id: str) -> dict[str, Any]:
        record = self.store.get_plugin(plugin_id)
        return health_mod.health_report(self.store, plugin_id, record.status if record else None)

    def summary(self) -> dict[str, Any]:
        records = self.store.list_plugins()
        return {
            "installed": len(records),
            "enabled": sum(1 for r in records if r.enabled),
            "errors": sum(1 for r in records if r.status in (PluginState.FAILED, PluginState.QUARANTINED)),
            "secretsAvailable": self.secrets_error is None,
            "secretsError": self.secrets_error,
            "tools": len(self.contributions.all_tool_names()),
        }

    def list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.store.list_plugins()]

    def inspect(self, plugin_id: str) -> dict[str, Any]:
        record = self.store.get_plugin(plugin_id)
        if record is None:
            raise NotFound(plugin_id)
        return {
            "plugin": record.to_dict(),
            "health": self.health_report(plugin_id),
            "contributions": self.contribution_descriptors(plugin_id),
            "events": self.events.subscriptions(plugin_id),
            "commands": self.commands_for(plugin_id),
            "config": self.config_view(plugin_id),
            "secrets": self.secret_names(plugin_id),
            "dependencies": self.dependency_status(plugin_id),
            "history": self.store.list_history(plugin_id, limit=20),
            "logs": self.logs(plugin_id, limit=50),
            "recentEvents": self.store.list_events(plugin_id, limit=50),
        }

    def logs(self, plugin_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_logs(plugin_id, limit)

    def doctor(self, plugin_id: str, *, connectivity: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return doctor_mod.diagnose(self, plugin_id, connectivity=connectivity)

    def available(self) -> list[dict[str, Any]]:
        return marketplace.list_available(self)

    def search(self, query: str = "", *, category: Optional[str] = None, installed_only: bool = False) -> list[dict[str, Any]]:
        return marketplace.search(self, query, category=category, installed_only=installed_only)

    # ── hosted registry ──────────────────────────────────────────────────────

    def registry_index(self, source: Optional[str] = None) -> dict[str, Any]:
        from .registry_client import load_index  # noqa: PLC0415

        return load_index(source, transport=self._oauth_transport)

    def registry_search(self, query: str = "", *, category: Optional[str] = None,
                        source: Optional[str] = None) -> list[dict[str, Any]]:
        from .registry_client import search as _search  # noqa: PLC0415

        return _search(self.registry_index(source), query, category=category)

    async def install_from_registry(
        self, plugin_id: str, *, source: Optional[str] = None, version: Optional[str] = None,
        enable: bool = False, operator: bool = False,
    ) -> dict[str, Any]:
        from . import semver
        from .registry_client import download_and_extract  # noqa: PLC0415

        index = self.registry_index(source)
        candidates = [e for e in index.get("plugins", []) if e.get("id") == plugin_id]
        if not candidates:
            raise PluginError("PLUGIN_REGISTRY_NOT_FOUND", f"{plugin_id!r} is not in the registry")
        if version:
            entry = next((e for e in candidates if str(e.get("version")) == version), None)
            if entry is None:
                raise PluginError("PLUGIN_REGISTRY_NOT_FOUND", f"{plugin_id!r} {version} is not in the registry")
        else:
            entry = max(candidates, key=lambda e: semver.Version.parse(_pad_version(str(e["version"]))))
        path = download_and_extract(entry, transport=self._oauth_transport)
        try:
            result = await self.install(
                path, trust=entry.get("trust"), enable=enable, operator=operator, replace=True,
            )
        finally:
            shutil.rmtree(path, ignore_errors=True)
        return {**result, "registry": index.get("name"), "version": entry.get("version"),
                "trust": entry.get("trust"), "verified": entry.get("verification", "unsigned")}

    # ── dependency / integrity helpers ───────────────────────────────────────

    def dependency_status(self, plugin_id: str) -> list[dict[str, Any]]:
        manifest = self.manifest_of(plugin_id)
        if manifest is None:
            return []
        from . import semver

        out = []
        for dep in manifest.dependencies:
            installed = self.store.get_plugin(dep.id)
            satisfied = installed is not None and semver.satisfies(installed.version, dep.version)
            out.append({"id": dep.id, "version": dep.version, "optional": dep.optional,
                        "installedVersion": installed.version if installed else None,
                        "satisfied": satisfied})
        return out

    def _check_dependencies(self, manifest: PluginManifest) -> None:
        for status in self.dependency_status(manifest.id):
            if not status["satisfied"] and not status["optional"]:
                raise DependencyError(
                    f"required dependency {status['id']} {status['version']} is not satisfied",
                    detail=status,
                )
            if not status["satisfied"] and status["optional"]:
                self.store.log(manifest.id, "warning", f"optional dependency {status['id']} missing")

    def check_integrity(self, plugin_id: str) -> dict[str, Any]:
        directory = self.directory_of(plugin_id)
        if directory is None:
            return {"ok": False, "detail": "installation directory missing"}
        if self._is_bundled(directory):
            return {"ok": True, "detail": "bundled (trusted)"}
        record = self.store.get_plugin(plugin_id)
        if record is None or not record.checksum:
            return {"ok": True, "detail": "no recorded checksum"}
        current = checksum(directory)
        return {"ok": current == record.checksum, "detail": current[:12] if current == record.checksum else "changed"}

    def granted_permissions(self, plugin_id: str) -> set[str]:
        record = self.store.get_plugin(plugin_id)
        if record is None:
            return set()
        return set(record.permissions) if record.enabled else set()

    @staticmethod
    def elevated_permissions() -> frozenset[str]:
        return ELEVATED_PERMISSIONS

    # ── host callbacks (used by PluginContext) ───────────────────────────────

    def publish_plugin_event(self, plugin_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.events.publish(plugin_id, event_type, data)
        self.store.log_event(plugin_id, event_type, "published")

    def plugin_telemetry(self, plugin_id: str, name: str, data: dict[str, Any]) -> None:
        self.store.log_event(plugin_id, name, "telemetry", data)
        if name == "plugin.http.failed":
            self.store.bump_health(plugin_id, httpFailures=1)

    def log_plugin(self, plugin_id: str, level: str, message: str) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(
            "[plugin:%s] %s", plugin_id, message
        )

    async def create_task(self, plugin_id: str, title: str, details: dict[str, Any]) -> dict[str, Any]:
        if self._service is None:
            return {"ok": False, "reason": "task runtime unavailable"}
        manager = getattr(self._service, "task_scheduler", None) or getattr(self._service, "tasks", None)
        if manager is None or not hasattr(manager, "create_task"):
            return {"ok": False, "reason": "task runtime unavailable"}
        try:
            return {"ok": True, "task": manager.create_task(title=title, details=details)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    async def invoke_agent(self, plugin_id: str, agent_id: str, objective: str) -> dict[str, Any]:
        if self._service is None or not hasattr(self._service, "run"):
            return {"ok": False, "reason": "agent runtime unavailable"}
        from ..schemas import ModelSettings, RunInput, RunRequest

        run_id = f"plugin_{plugin_id}_{uuid.uuid4().hex[:12]}"
        try:
            request = RunRequest(
                runId=run_id,
                input=RunInput(query=objective, context={"pluginId": plugin_id}),
                agentId=agent_id,
                modelSettings=ModelSettings(provider="", model=""),
                callbackBaseUrl=os.environ.get("HARNESS_CALLBACK_URL", "http://127.0.0.1:3000"),
            )
            result = await self._service.run(request)
            return {"ok": True, "runId": run_id, "status": getattr(result, "status", "unknown")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    async def run_workflow(self, plugin_id: str, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "reason": "workflow runtime unavailable via plugin host"}

    def remember(self, plugin_id: str, **kwargs: Any) -> dict[str, Any]:
        if self._service is None or not hasattr(self._service, "remember"):
            return {"ok": False, "reason": "memory runtime unavailable"}
        try:
            return {"ok": True, "memory": self._service.remember(**kwargs)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    def recall(self, plugin_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        if self._service is None or not hasattr(self._service, "recall"):
            return []
        try:
            return self._service.recall(**kwargs)
        except Exception:  # noqa: BLE001
            return []

    def add_plugin_evidence(self, plugin_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        store = getattr(self._service, "evidence_store", None) if self._service else None
        if store is None:
            return {"ok": False, "reason": "evidence engine unavailable"}
        stamped = {**evidence, "source_type": evidence.get("source_type", "plugin"),
                   "plugin_id": plugin_id, "retrieval_time": now_iso()}
        try:
            store.save_evidence(stamped) if hasattr(store, "save_evidence") else None
            return {"ok": True, "evidence": stamped}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    # ── lifecycle: process start/stop ────────────────────────────────────────

    async def start_enabled(self) -> dict[str, Any]:
        started, failed = [], []
        for record in self.store.list_plugins():
            if not record.enabled:
                continue
            try:
                await self.enable(record.plugin_id, operator=True)
                started.append(record.plugin_id)
            except Exception as exc:  # noqa: BLE001 - one plugin must not block boot
                failed.append({"pluginId": record.plugin_id, "error": str(exc)})
                self.store.bump_health(record.plugin_id, crashes=1)
        return {"started": started, "failed": failed}

    async def stop_all(self) -> None:
        for plugin_id in list(self._active):
            try:
                await self._unload(plugin_id)
            except Exception:  # noqa: BLE001 - shutdown must never raise
                log.warning("plugin %s unload failed", plugin_id, exc_info=True)

    # ── internals ────────────────────────────────────────────────────────────

    def _context_for(self, plugin_id: str) -> Optional[PluginContext]:
        loaded = self._active.get(plugin_id)
        return loaded.ctx if loaded else None

    def _event_failure(self, plugin_id: str, reason: str) -> None:
        self.store.bump_health(plugin_id, eventFailures=1)
        self.store.log_event(plugin_id, "event.handler", "failed", {"error": reason})
        self.store.log(plugin_id, "warning", f"event handler failed: {reason}")

    def _hook_failure(self, plugin_id: str, name: str, error: str) -> None:
        self.store.bump_health(plugin_id, hookFailures=1)
        self.store.log(plugin_id, "warning", f"hook {name} failed: {error}")

    def _snapshot(self, record: PluginRecord) -> dict[str, Any]:
        return {key: getattr(record, _snake(key)) if hasattr(record, _snake(key)) else None
                for key in _SNAPSHOT_KEYS}

    def _resolve_source(self, source: Any) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
        if isinstance(source, dict):
            return None, source
        raw = str(source)
        if not Path(raw).exists():
            # Treated as a plugin id: installed first, then bundled reference.
            installed = self._plugins_dir / raw
            if find_manifest_file(installed) is not None:
                return installed, None
            for candidate in self.bundled_dirs():
                try:
                    manifest, _ = read_manifest(candidate)
                except Exception:  # noqa: BLE001
                    continue
                if manifest.id == raw:
                    return candidate, None
            raise PluginError("PLUGIN_SOURCE_NOT_FOUND", f"could not resolve plugin source {raw!r}")
        path = Path(raw)
        if path.is_dir():
            if find_manifest_file(path) is not None:
                return path, None
            children = discover(path)
            named = [c for c in children if c.name == path.name]
            if named:
                return named[0], None
            if len(children) == 1:
                return children[0], None
            raise PluginError(
                "PLUGIN_SOURCE_NOT_FOUND",
                f"{raw!r} is a directory but contains no unambiguous plugin manifest",
            )
        raise PluginError("PLUGIN_SOURCE_NOT_FOUND", f"{raw!r} is not a plugin directory")

    def _manifest_from_package(self, package: Optional[dict[str, Any]]) -> PluginManifest:
        if not package or "manifest" not in package:
            raise ManifestError("an in-memory package requires a 'manifest' mapping")
        try:
            return PluginManifest.model_validate(package["manifest"])
        except Exception as exc:  # noqa: BLE001
            raise ManifestError(f"manifest validation failed: {exc}") from exc

    def _place_package(self, src: Optional[Path], package: Optional[dict[str, Any]], target: Path,
                       manifest: Optional[PluginManifest] = None) -> str:
        """Write a package to `target`: copy a source dir OR materialize in-memory files."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if src is not None and src.resolve() != target.resolve():
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target, dirs_exist_ok=True)
        elif package is not None:
            target.mkdir(parents=True, exist_ok=True)
            _write_package(target, package, manifest)
        return str(target)

    def _apply_package(self, src: Optional[Path], package: Optional[dict[str, Any]], target: Path,
                       backup: Path, manifest: Optional[PluginManifest] = None) -> None:
        if target.exists():
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(target), str(backup))
        try:
            if src is not None and src.resolve() != target.resolve():
                shutil.copytree(src, target)
            elif package is not None:
                target.mkdir(parents=True, exist_ok=True)
                _write_package(target, package, manifest)
        except Exception:
            self._restore_backup(target.name, target, backup)
            raise

    def _restore_record_from_snapshot(self, plugin_id: str, snapshot: dict[str, Any]) -> None:
        """Restore the DB record's version/path/checksum/manifest after a rollback."""
        record = self.store.get_plugin(plugin_id)
        if record is None:
            return
        record.manifest = snapshot.get("manifest") or record.manifest
        record.version = snapshot.get("version") or record.version
        record.installation_path = snapshot.get("installationPath") or record.installation_path
        record.checksum = snapshot.get("checksum") or record.checksum
        if snapshot.get("permissions"):
            record.permissions = list(snapshot["permissions"])
        record.updated_at = now_iso()
        self.store.upsert_plugin(record)

    def _restore_backup(self, plugin_id: str, target: Path, backup: Path) -> None:
        if backup.exists():
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            try:
                shutil.move(str(backup), str(target))
            except Exception:  # noqa: BLE001 - best effort
                pass

    @staticmethod
    def _is_under(path: Path, base: Path) -> bool:
        try:
            path.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False

    def _should_sandbox(self, record: PluginRecord) -> bool:
        """Whether this plugin runs in the isolated subprocess.

        Default: an `untrusted` Python plugin is sandboxed; trusted/verified and
        declarative plugins run in-process. `HARNESS_PLUGIN_SANDBOX` overrides
        (1/true = sandbox every Python plugin; 0/false = never sandbox).
        """
        if record.runtime_type != "python":
            return False
        env = os.environ.get("HARNESS_PLUGIN_SANDBOX", "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            return True
        if env in ("0", "false", "no", "off"):
            return False
        return record.trust == TrustLevel.UNTRUSTED

    def _is_bundled(self, directory: Optional[Path]) -> bool:
        if directory is None:
            return False
        return self._is_under(directory, self._bundled_dir)

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish({
                "id": f"{event_type}_{now_iso()}", "runId": None, "type": event_type,
                "seq": 0, "at": now_iso(), "data": data, "source": {"source_type": "plugin_host"},
            })
        except Exception:  # noqa: BLE001 - events must never break a lifecycle op
            pass


class _DeclarativeInstance:
    """A manifest-only plugin: contributes nothing executable, but is adoptable."""

    def __init__(self, manifest: PluginManifest) -> None:
        self._manifest = manifest

    def contributions(self) -> list[dict[str, Any]]:
        return []

    async def on_load(self, ctx: Any) -> None:  # noqa: ANN001
        return None

    async def on_enable(self, ctx: Any) -> None:
        return None

    async def on_disable(self, ctx: Any) -> None:
        return None

    async def on_unload(self, ctx: Any) -> None:
        return None


def _write_package(target: Path, package: dict[str, Any], manifest: Optional[PluginManifest] = None) -> None:
    files = package.get("files") or {}
    for rel, content in files.items():
        rel_path = Path(str(rel))
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise PluginError("PLUGIN_PACKAGE_PATH_ESCAPE", f"package file escapes root: {rel!r}")
        dest = (target / rel_path).resolve()
        if target.resolve() not in dest.parents and dest != target.resolve():
            raise PluginError("PLUGIN_PACKAGE_PATH_ESCAPE", f"package file escapes root: {rel!r}")
        ensure_dir(dest.parent)
        dest.write_text(str(content), encoding="utf-8")
    # Materialize the manifest on disk unless the package already provides one,
    # so the installed directory is self-describing (the sandbox child reads it).
    if manifest is not None and find_manifest_file(target) is None:
        import yaml  # noqa: PLC0415

        (target / "plugin.yaml").write_text(
            yaml.safe_dump(manifest.to_dict(), sort_keys=False), encoding="utf-8",
        )


def _snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _accepts_ctx(handler: Any) -> bool:
    """Whether a contribution handler expects the plugin context as its argument."""
    try:
        import inspect  # noqa: PLC0415

        required = [
            p for p in inspect.signature(handler).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
        return len(required) >= 1
    except (TypeError, ValueError):
        return False


def _normalize_view(result: Any) -> dict[str, Any]:
    """Coerce a plugin UI handler's return into the safe view vocabulary."""
    if result is None:
        return {"type": "empty", "body": "This panel has no content."}
    if isinstance(result, str):
        return {"type": "markdown", "body": result}
    if isinstance(result, list):
        return {"type": "list", "items": result}
    if isinstance(result, dict) and "type" in result:
        return result
    if isinstance(result, dict):
        return {"type": "keyvalue", "items": [{"label": str(k), "value": v} for k, v in result.items()]}
    return {"type": "markdown", "body": str(result)}


def _pad_version(value: str) -> str:
    """Pad a partial version to full SemVer so `max()` never raises on it."""
    core = value.split("+")[0].split("-")[0]
    parts = core.split(".")
    while len(parts) < 3:
        parts.append("0")
    suffix = value[len(core):]
    return ".".join(parts[:3]) + suffix


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_signature(secret: str, body: str, provided: str) -> bool:
    provided = provided.split("=", 1)[-1] if "=" in provided else provided
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided or "")


__all__ = ["PluginManager"]
