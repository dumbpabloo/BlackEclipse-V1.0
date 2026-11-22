"""Fibers: one owner for every plugin instance and its disposable effects."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

Disposer = Callable[[], Any]


class FiberState(str, Enum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


@dataclass
class _Effect:
    label: str
    dispose: Disposer
    disposed: bool = False


async def _close(disposer: Disposer) -> Optional[BaseException]:
    try:
        result = disposer()
        if inspect.isawaitable(result):
            await result
        return None
    except BaseException as exc:  # noqa: BLE001 - a bad disposer must not abort teardown
        return exc


@dataclass
class Fiber:
    """A mounted plugin or scope.

    ``dispose()`` is idempotent and unwinds children first, then its own effects
    in reverse registration order. A failing disposer is collected, never
    allowed to skip the remaining cleanup.
    """

    id: str
    name: str
    state: FiberState = FiberState.PENDING
    ctx: Any = None
    parent: Optional["Fiber"] = None
    error: Optional[BaseException] = None
    resolved: Any = None
    config: Any = None
    children: list["Fiber"] = field(default_factory=list)
    _effects: list[_Effect] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _disposed: bool = False
    _errors: list[BaseException] = field(default_factory=list)

    def track(self, disposer: Disposer, *, label: str = "") -> Disposer:
        entry = _Effect(label or getattr(disposer, "__name__", "effect"), disposer)
        self._effects.append(entry)

        def release() -> None:
            if entry.disposed:
                return
            entry.disposed = True
            try:
                self._effects.remove(entry)
            except ValueError:
                pass
            result = disposer()
            if inspect.isawaitable(result):
                # The caller is sync; schedule the cleanup rather than dropping it.
                try:
                    asyncio.get_running_loop().create_task(_await_disposer(result))
                except RuntimeError:
                    pass

        return release

    async def dispose(self) -> None:
        async with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self.state = FiberState.UNLOADING
            for child in reversed(self.children):
                try:
                    await child.dispose()
                except BaseException as exc:  # noqa: BLE001
                    self._errors.append(exc)
            self.children.clear()
            self._errors.extend(await self.dispose_effects())
            if self.ctx is not None:
                try:
                    errors = await self.ctx._dispose_effects()  # noqa: SLF001 - same owner
                    self._errors.extend(errors)
                except BaseException as exc:  # noqa: BLE001
                    self._errors.append(exc)
            self.state = FiberState.DISPOSED

    async def dispose_effects(self) -> list[BaseException]:
        errors: list[BaseException] = []
        for entry in reversed(self._effects):
            if entry.disposed:
                continue
            entry.disposed = True
            error = await _close(entry.dispose)
            if error is not None:
                errors.append(error)
        self._effects.clear()
        return errors

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "error": None if self.error is None else f"{type(self.error).__name__}: {self.error}",
            "children": len(self.children),
            "effects": sum(1 for e in self._effects if not e.disposed),
        }


async def _await_disposer(result: Any) -> None:
    try:
        await result
    except BaseException:  # noqa: BLE001 - detached cleanup cannot surface
        pass
