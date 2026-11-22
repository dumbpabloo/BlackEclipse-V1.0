"""Session-scoped, versioned dynamic packages.

A user (or an approved agent flow) can define a small Python package, run an
immutable version in the existing code sandbox, update to a new version, stop it
and undefine it. Definitions are session-owned and process-local; they never
become a persistent plugin without promotion to a signed bundle.

Safety:
* `run` requires explicit approval (the API/tool layer is operator-gated).
* the child uses `code.sandbox.spawn` - scrubbed env, process-tree kill,
  wall-clock/output caps and the declared memory limit.
* calls, wall time and TTL are bounded; a violation stops the run.
* `stop`/`undefine` kill the process tree and remove the package directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


class DynamicError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DynamicPackage:
    id: str
    plugin_id: str
    session_id: str
    source: str
    digest: str
    label: str
    created_at: str
    directory: Path


@dataclass
class DynamicRun:
    plugin_id: str
    package_id: str
    session_id: str
    process: Any
    started_at: float
    expires_at: Optional[str]
    calls: int = 0
    max_calls: int = 100
    max_runtime_s: int = 60
    closed: bool = False
    error: str = ""


class DynamicPackages:
    def __init__(
        self,
        root: Optional[str | Path] = None,
        *,
        ttl_seconds: int = 3600,
        max_packages: int = 20,
        now: Optional[Any] = None,
    ) -> None:
        self.root = Path(root).expanduser() if root else Path.home() / ".black-eclipse" / "dynamic"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_packages = max(1, int(max_packages))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._packages: dict[str, DynamicPackage] = {}
        self._by_plugin: dict[tuple[str, str], list[str]] = {}
        self._current: dict[tuple[str, str], str] = {}
        self._runs: dict[tuple[str, str], DynamicRun] = {}

    # -- definitions ----------------------------------------------------------

    def define(
        self,
        *,
        session_id: str,
        plugin_id: str,
        source: str,
        label: str = "",
        approval: bool = False,
    ) -> dict[str, Any]:
        if not session_id or not plugin_id:
            raise DynamicError("DYNAMIC_OWNER_REQUIRED", "a package needs a session and plugin id")
        if not source.strip():
            raise DynamicError("DYNAMIC_SOURCE_EMPTY", "a package source must not be empty")
        owned = sum(1 for package in self._packages.values() if package.session_id == session_id)
        if owned >= self.max_packages:
            raise DynamicError("DYNAMIC_CAP", f"session package cap reached ({self.max_packages})")
        digest = "dyn_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
        package_id = f"pkg_{uuid.uuid4().hex[:12]}"
        directory = self.root / session_id / plugin_id / package_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "module.py").write_text(source, encoding="utf-8")
        package = DynamicPackage(
            id=package_id,
            plugin_id=plugin_id,
            session_id=session_id,
            source=source,
            digest=digest,
            label=label or plugin_id,
            created_at=self._now().isoformat(),
            directory=directory,
        )
        self._packages[package_id] = package
        self._by_plugin.setdefault((session_id, plugin_id), []).append(package_id)
        return self._package_view(package)

    def _owned(self, session_id: str, plugin_id: str) -> list[DynamicPackage]:
        ids = self._by_plugin.get((session_id, plugin_id), [])
        return [self._packages[pid] for pid in ids if pid in self._packages]

    def _package_for(self, session_id: str, plugin_id: str, package_id: str) -> DynamicPackage:
        package = self._packages.get(package_id)
        if package is None or package.session_id != session_id or package.plugin_id != plugin_id:
            raise DynamicError("DYNAMIC_NOT_FOUND", "package not found for this session")
        return package

    # -- run lifecycle --------------------------------------------------------

    async def run(
        self,
        *,
        session_id: str,
        plugin_id: str,
        package_id: str,
        approved: bool = False,
        ttl_seconds: Optional[int] = None,
        max_calls: int = 100,
        max_runtime_s: int = 60,
        memory_mb: int = 256,
    ) -> dict[str, Any]:
        if not approved:
            raise DynamicError("DYNAMIC_APPROVAL_REQUIRED", "running dynamic code requires explicit approval")
        package = self._package_for(session_id, plugin_id, package_id)
        await self.stop(session_id=session_id, plugin_id=plugin_id)
        from ..code import sandbox  # noqa: PLC0415

        runner = Path(__file__).with_name("runner.py")
        spec = sandbox.RunSpec(
            argv=[os.sys.executable, "-u", str(runner), str(package.directory)],
            cwd=package.directory,
            env=sandbox.build_env(),
            timeout_s=max(1, int(max_runtime_s) + 5),
            output_cap_bytes=256_000,
            network=False,
            memory_mb=max(0, int(memory_mb)),
        )
        process = sandbox.spawn(spec)
        ttl = self.ttl_seconds if ttl_seconds is None else max(0, int(ttl_seconds))
        expires = (self._now() + timedelta(seconds=ttl)).isoformat() if ttl else None
        run = DynamicRun(
            plugin_id=plugin_id,
            package_id=package.id,
            session_id=session_id,
            process=process,
            started_at=time.monotonic(),
            expires_at=expires,
            max_calls=max(1, int(max_calls)),
            max_runtime_s=max(1, int(max_runtime_s)),
        )
        self._runs[(session_id, plugin_id)] = run
        self._current[(session_id, plugin_id)] = package.id
        return self._run_view(run)

    async def invoke(
        self, *, session_id: str, plugin_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        run = self._runs.get((session_id, plugin_id))
        if run is None or run.closed:
            raise DynamicError("DYNAMIC_NOT_RUNNING", "no active run for this plugin")
        if run.calls >= run.max_calls:
            await self.stop(session_id=session_id, plugin_id=plugin_id)
            raise DynamicError("DYNAMIC_CALL_BUDGET", "the call budget is exhausted")
        if time.monotonic() - run.started_at > run.max_runtime_s:
            await self.stop(session_id=session_id, plugin_id=plugin_id)
            raise DynamicError("DYNAMIC_TIME_BUDGET", "the run exceeded its wall-clock budget")
        if run.expires_at and self._now() >= datetime.fromisoformat(run.expires_at):
            await self.stop(session_id=session_id, plugin_id=plugin_id)
            raise DynamicError("DYNAMIC_TTL", "the run expired")
        run.calls += 1
        request = {"id": f"call_{run.calls}", "payload": payload}
        try:
            await asyncio.to_thread(run.process.write_line, json.dumps(request))
            line = await asyncio.to_thread(run.process.read_line, run.max_runtime_s)
            response = json.loads(line)
        except Exception as exc:  # noqa: BLE001 - a broken child is a stopped run
            run.error = f"{type(exc).__name__}: {exc}"
            await self.stop(session_id=session_id, plugin_id=plugin_id)
            raise DynamicError("DYNAMIC_CALL_FAILED", run.error) from exc
        if not response.get("ok"):
            raise DynamicError("DYNAMIC_PACKAGE_ERROR", str(response.get("error") or "package failed"))
        return {"id": response.get("id"), "result": response.get("result")}

    async def stop(self, *, session_id: str, plugin_id: str) -> dict[str, Any]:
        run = self._runs.pop((session_id, plugin_id), None)
        if run is None:
            return {"stopped": False}
        run.closed = True
        try:
            await asyncio.to_thread(run.process.close)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        return {"stopped": True, "packageId": run.package_id}

    async def update(
        self,
        *,
        session_id: str,
        plugin_id: str,
        source: str,
        label: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        if not approved:
            raise DynamicError("DYNAMIC_APPROVAL_REQUIRED", "updating dynamic code requires explicit approval")
        package = self.define(
            session_id=session_id, plugin_id=plugin_id, source=source, label=label, approval=True,
        )
        result = await self.run(
            session_id=session_id,
            plugin_id=plugin_id,
            package_id=package["id"],
            approved=True,
        )
        return {"package": package, "run": result}

    async def undefine(self, *, session_id: str, plugin_id: str) -> dict[str, Any]:
        await self.stop(session_id=session_id, plugin_id=plugin_id)
        packages = self._owned(session_id, plugin_id)
        for package in packages:
            shutil.rmtree(package.directory, ignore_errors=True)
            self._packages.pop(package.id, None)
        self._by_plugin.pop((session_id, plugin_id), None)
        self._current.pop((session_id, plugin_id), None)
        return {"removed": [package.id for package in packages]}

    async def sweep_expired(self) -> list[str]:
        stopped: list[str] = []
        for (session_id, plugin_id), run in list(self._runs.items()):
            if not run.expires_at:
                continue
            if self._now() >= datetime.fromisoformat(run.expires_at):
                await self.stop(session_id=session_id, plugin_id=plugin_id)
                stopped.append(plugin_id)
        return stopped

    # -- inspection -----------------------------------------------------------

    def _package_view(self, package: DynamicPackage) -> dict[str, Any]:
        return {
            "id": package.id,
            "pluginId": package.plugin_id,
            "label": package.label,
            "digest": package.digest,
            "bytes": len(package.source.encode("utf-8")),
            "createdAt": package.created_at,
            "current": self._current.get((package.session_id, package.plugin_id)) == package.id,
        }

    def _run_view(self, run: DynamicRun) -> dict[str, Any]:
        return {
            "pluginId": run.plugin_id,
            "packageId": run.package_id,
            "calls": run.calls,
            "maxCalls": run.max_calls,
            "maxRuntimeS": run.max_runtime_s,
            "expiresAt": run.expires_at,
        }

    def status(self, *, session_id: str, plugin_id: Optional[str] = None) -> dict[str, Any]:
        plugins = [plugin_id] if plugin_id else sorted({
            key[1] for key in self._by_plugin if key[0] == session_id
        })
        return {
            "plugins": [
                {
                    "pluginId": pid,
                    "packages": [self._package_view(pkg) for pkg in self._owned(session_id, pid)],
                    "run": self._run_view(run) if (run := self._runs.get((session_id, pid))) else None,
                }
                for pid in plugins
            ]
        }
