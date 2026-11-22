"""Desired-vs-live reconciliation with generation rollback.

The supervisor is the only place that changes the mounted composition at
runtime. It applies add / update / disable / remove, waits for the kernel to
settle, and restores the previous generation if any row fails. A half-mounted
tree is never reported as success.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .composition import Composition, Row
from .context import Kernel
from .errors import SupervisorError
from .fiber import Fiber, FiberState


@dataclass
class _Live:
    row: Row
    fiber: Fiber


class Supervisor:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel
        self._live: dict[str, _Live] = {}
        self._last: dict[str, _Live] | None = None
        self._lock = asyncio.Lock()
        self._generation = 0

    async def apply(self, composition: Composition) -> dict[str, Any]:
        async with self._lock:
            previous = dict(self._live)
            desired = {
                row.id: row
                for row in composition.enabled()
                if row.when is None or bool(row.when)
            }
            self._generation += 1
            added: list[str] = []
            updated: list[str] = []
            removed: list[str] = []
            try:
                # Stop rows that disappear or change before mounting replacements.
                for row_id in list(previous):
                    if row_id not in desired:
                        await self._dispose(previous[row_id].fiber)
                        removed.append(row_id)
                    elif _identity(previous[row_id].row) != _identity(desired[row_id]):
                        await self._dispose(previous[row_id].fiber)
                        updated.append(row_id)

                next_live: dict[str, _Live] = {}
                self._live = next_live
                for row_id, row in desired.items():
                    existing = previous.get(row_id)
                    if existing is not None and row_id in updated:
                        existing = None
                    if existing is None:
                        fiber = await self._mount(row)
                        next_live[row_id] = _Live(row=row, fiber=fiber)
                        if fiber.state is FiberState.FAILED:
                            raise SupervisorError(
                                "KERNEL_ROW_FAILED",
                                f"row {row_id!r} failed to mount",
                                row=row_id,
                            )
                        if row_id not in updated:
                            added.append(row_id)
                    else:
                        next_live[row_id] = existing

                await self.kernel.settle()
                pending = [
                    row_id for row_id, live in next_live.items()
                    if live.fiber.state is FiberState.PENDING
                ]
                self._last = previous
                self._live = next_live
                await self.kernel.root.emit(
                    "kernel/config-reload",
                    {"generation": self._generation, "added": added, "updated": updated, "removed": removed},
                )
                return {
                    "generation": self._generation,
                    "added": added,
                    "updated": updated,
                    "removed": removed,
                    "pending": pending,
                    "ok": True,
                }
            except Exception as exc:
                rollback = await self._restore(previous)
                if isinstance(exc, SupervisorError):
                    detail = dict(exc.detail)
                    detail["rollback"] = "restored" if rollback else "failed"
                    raise SupervisorError(exc.code, exc.message, row=str(detail.get("row", "")),
                                          rollback=detail["rollback"]) from exc
                raise SupervisorError(
                    "KERNEL_CONFIG_FAILED",
                    f"composition failed: {type(exc).__name__}: {exc}",
                    rollback="restored" if rollback else "failed",
                ) from exc

    async def _mount(self, row: Row) -> Fiber:
        if row.group:
            group = await self.kernel.new_group(self.kernel.root, isolate=row.isolate, label=row.id)
            plugins = (row.config or {}).get("plugins") if isinstance(row.config, dict) else None
            if isinstance(plugins, list):
                for child in plugins:
                    if not isinstance(child, dict) or not child.get("name"):
                        continue
                    fiber = await self.kernel.mount(group, child["name"], child.get("config"))
                    if fiber.state is FiberState.FAILED:
                        return fiber
            return group.fiber
        return await self.kernel.mount(self.kernel.root, row.name, row.config)

    async def _dispose(self, fiber: Fiber) -> None:
        await fiber.dispose()

    async def _restore(self, previous: dict[str, _Live]) -> bool:
        for live in list(self._live.values()):
            try:
                await live.fiber.dispose()
            except BaseException:  # noqa: BLE001 - restoration continues
                pass
        self._live = {}
        restored: dict[str, _Live] = {}
        try:
            for row_id, live in previous.items():
                if live.fiber.state is FiberState.DISPOSED:
                    fiber = await self._mount(live.row)
                else:
                    fiber = live.fiber
                restored[row_id] = _Live(row=live.row, fiber=fiber)
            await self.kernel.settle()
            self._live = restored
            return True
        except BaseException:  # noqa: BLE001 - restoration failure is reported, not hidden
            return False

    async def rollback(self) -> dict[str, Any]:
        async with self._lock:
            if self._last is None:
                return {"ok": False, "reason": "no previous generation"}
            previous = self._last
            self._last = None
            for live in list(self._live.values()):
                await live.fiber.dispose()
            self._live = {}
            restored: dict[str, _Live] = {}
            for row_id, live in previous.items():
                fiber = await self._mount(live.row)
                restored[row_id] = _Live(row=live.row, fiber=fiber)
            await self.kernel.settle()
            self._live = restored
            return {"ok": True, "restored": sorted(restored)}

    async def stop(self) -> dict[str, Any]:
        """Dispose every mounted profile row; used at shutdown and profile reload."""
        async with self._lock:
            rows = sorted(self._live)
            for live in list(self._live.values()):
                await live.fiber.dispose()
            self._live = {}
            self._last = None
            await self.kernel.settle()
            return {"ok": True, "removed": rows}

    async def add(self, row: Row) -> dict[str, Any]:
        """Mount one row without disturbing the others (composition-row assets)."""
        async with self._lock:
            if row.id in self._live:
                raise SupervisorError("KERNEL_ROW_EXISTS", f"row {row.id!r} is already mounted", row=row.id)
            fiber = await self._mount(row)
            if fiber.state is FiberState.FAILED:
                raise SupervisorError("KERNEL_ROW_FAILED", f"row {row.id!r} failed to mount", row=row.id)
            self._live[row.id] = _Live(row=row, fiber=fiber)
            await self.kernel.settle()
            return {"ok": True, "id": row.id, "state": fiber.state.value}

    async def remove(self, row_id: str) -> dict[str, Any]:
        async with self._lock:
            live = self._live.pop(row_id, None)
            if live is None:
                return {"ok": False, "reason": "not-mounted"}
            await live.fiber.dispose()
            await self.kernel.settle()
            return {"ok": True, "id": row_id}

    def status(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "rows": [
                {
                    "id": row_id,
                    "name": live.row.name,
                    "state": live.fiber.state.value,
                    "config": _stable(live.row.config),
                    "error": live.fiber.describe()["error"],
                }
                for row_id, live in sorted(self._live.items())
            ],
        }


def _identity(row: Row) -> tuple[Any, Any, Any, Any, Any]:
    return (
        row.name,
        _stable(row.config),
        row.disabled,
        tuple(row.isolate),
        row.group,
    )


def _stable(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)
