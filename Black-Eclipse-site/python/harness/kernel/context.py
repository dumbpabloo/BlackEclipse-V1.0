"""The plugin kernel: context, services, coeffects, effects and scopes.

Two properties drive the design (see arXiv:2608.25512):

* **Temporal composability** - every registration is an effect with an inverse,
  owned by a fiber, so unloading a plugin is complete.
* **Spatial composability** - ``inject`` declares the services a plugin needs;
  the kernel activates it when they exist and disposes it when they disappear.

The kernel is intentionally small. It owns lifetime and resolution, not agent
logic, finance logic or persistence; those are services provided by plugins.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional

from .errors import KernelError, ServiceConflict, ServiceUnavailable
from .events import EventBus
from .fiber import Disposer, Fiber, FiberState
from .plugin import ResolvedPlugin, resolve_plugin


class Context:
    """A node in the plugin tree.

    A context provides services to itself and its descendants, owns effects
    (registrations, listeners, timers, children) and can mint a scope - a child
    context whose registrations are visible to it and its descendants only.
    """

    def __init__(
        self,
        kernel: "Kernel",
        fiber: Fiber,
        *,
        parent: Optional["Context"] = None,
        scope_key: Any = None,
        isolated: tuple[str, ...] = (),
        label: str = "",
        service_scope: Optional["Context"] = None,
    ) -> None:
        self.kernel = kernel
        self.fiber = fiber
        self.parent = parent
        self.scope_key = scope_key
        self.label = label
        self._services: dict[str, Any] = {}
        self._isolated: set[str] = set(isolated)
        # A plugin provides services into the context that mounted it (its
        # parent), so siblings in the same group/scope can consume them. A
        # scope/group provides into itself, which is what isolates it.
        self._service_scope: Context = service_scope or self

    # -- service resolution ---------------------------------------------------

    @property
    def parent_scope(self) -> Optional["Context"]:
        """The predecessor used by ``ScopedRegistry`` visibility walks."""
        return self.parent

    def has(self, key: str) -> bool:
        current: Optional[Context] = self
        while current is not None:
            if key in current._services:
                return True
            if key in current._isolated:
                return False
            current = current.parent
        return False

    def get(self, key: str, default: Any = None) -> Any:
        current: Optional[Context] = self
        while current is not None:
            if key in current._services:
                return current._services[key]
            if key in current._isolated:
                return default
            current = current.parent
        return default

    def require(self, key: str) -> Any:
        if not self.has(key):
            raise ServiceUnavailable(key, waiting_on=[key])
        return self.get(key)

    def inject(self, *keys: str) -> dict[str, Any]:
        """Resolve a dependency set, naming every missing key at once."""
        missing = [key for key in keys if not self.has(key)]
        if missing:
            raise ServiceUnavailable(missing[0], waiting_on=missing)
        return {key: self.get(key) for key in keys}

    def missing(self, keys: tuple[str, ...] | list[str]) -> list[str]:
        return [key for key in keys if not self.has(key)]

    # -- services -------------------------------------------------------------

    def provide(self, key: str, value: Any, *, replace: bool = False) -> Disposer:
        target = self._service_scope
        if key in target._services and not replace:
            raise ServiceConflict(key)
        sentinel = object()
        previous = target._services.get(key, sentinel)
        target._services[key] = value

        def remove() -> None:
            if target._services.get(key) is value:
                if previous is sentinel:
                    target._services.pop(key, None)
                else:
                    target._services[key] = previous
            self.kernel.service_changed(key)

        disposer = self.track(remove, label=f"service:{key}")
        self.kernel.service_changed(key)
        return disposer

    # -- effects --------------------------------------------------------------

    def track(self, disposer: Disposer, *, label: str = "") -> Disposer:
        return self.fiber.track(disposer, label=label)

    def effect(self, factory: Callable[[], Any], *, label: str = "") -> Disposer:
        """Run ``factory`` now and own whatever disposer it returns."""
        result = factory()
        if result is None:
            return lambda: None
        if not callable(result):
            raise KernelError(
                "KERNEL_EFFECT_INVALID",
                "ctx.effect() must return a callable disposer or None",
            )
        return self.track(result, label=label)

    def on(
        self,
        event: str,
        handler: Callable[..., Any],
        *,
        mode: str = "emit",
        order: int = 100,
        prepend: bool = False,
        label: str = "",
    ) -> Disposer:
        unmount = self.kernel.events.on(
            event, handler, mode=mode, order=order, prepend=prepend, owner=self, label=label,
        )
        return self.track(unmount, label=f"event:{event}")

    async def emit(self, event: str, *args: Any) -> None:
        await self.kernel.events.emit(event, *args)

    async def waterfall(self, event: str, *args: Any, fallback: Any = None) -> Any:
        return await self.kernel.events.waterfall(event, *args, fallback=fallback)

    async def serial(self, event: str, *args: Any) -> Any:
        return await self.kernel.events.serial(event, *args)

    async def bail(self, event: str, *args: Any) -> Any:
        return await self.kernel.events.bail(event, *args)

    async def parallel(self, event: str, *args: Any) -> None:
        await self.kernel.events.parallel(event, *args)

    # -- composition ----------------------------------------------------------

    async def plugin(
        self,
        plugin: Any,
        config: Any = None,
        *,
        strict: bool = False,
    ) -> Fiber:
        return await self.kernel.mount(self, plugin, config, strict=strict)

    async def scope(self, key: Any, *, label: str = "") -> "Context":
        return await self.kernel.new_scope(self, key, label=label)

    async def group(self, *, isolate: tuple[str, ...] = (), label: str = "") -> "Context":
        return await self.kernel.new_group(self, isolate=isolate, label=label)

    # -- teardown -------------------------------------------------------------

    async def dispose(self) -> None:
        await self.fiber.dispose()

    async def _dispose_effects(self) -> list[BaseException]:
        self.kernel.events.clear_owner(self)
        self._services.clear()
        return []


class Kernel:
    """Owns the root context, the fiber tree and the service-change reconciler."""

    def __init__(self) -> None:
        self.events = EventBus()
        self._fibers: dict[str, Fiber] = {}
        self._sequence = 0
        self._settle_lock = asyncio.Lock()
        self._settling = False
        self._settle_done = asyncio.Event()
        self._settle_done.set()
        self._settle_task: Optional[asyncio.Task[Any]] = None
        self._dirty = False
        root_fiber = Fiber(id="root", name="root", state=FiberState.ACTIVE)
        self.root = Context(self, root_fiber, label="root")
        root_fiber.ctx = self.root
        self._fibers["root"] = root_fiber
        # Set by KernelRuntime when this kernel is the live composition root.
        self.runtime: Any = None

    # -- mounting -------------------------------------------------------------

    async def mount(
        self,
        parent: Context,
        plugin: Any,
        config: Any = None,
        *,
        strict: bool = False,
    ) -> Fiber:
        if isinstance(plugin, str) and plugin.startswith("builtin:") and self.runtime is not None:
            plugin = self.runtime.builtin_plugin(plugin, config)
        resolved = resolve_plugin(plugin)
        self._sequence += 1
        fiber_id = f"f{self._sequence}"
        fiber = Fiber(id=fiber_id, name=resolved.name, parent=parent.fiber)
        ctx = Context(
            self,
            fiber,
            parent=parent,
            label=resolved.name,
            service_scope=parent,
        )
        fiber.ctx = ctx
        fiber.resolved = resolved
        fiber.config = config
        parent.fiber.children.append(fiber)
        self._fibers[fiber_id] = fiber
        missing = ctx.missing(resolved.inject)
        if missing:
            fiber.state = FiberState.PENDING
            self._dirty = True
            if strict:
                raise ServiceUnavailable(missing[0], waiting_on=missing)
            return fiber
        await self._activate(fiber, resolved, config)
        if strict and fiber.state is FiberState.FAILED:
            raise KernelError(
                "KERNEL_PLUGIN_FAILED",
                f"plugin {resolved.name!r} failed to load",
                detail={"plugin": resolved.name, "error": fiber.describe()["error"]},
            )
        await self.settle()
        return fiber

    async def _activate(self, fiber: Fiber, resolved: ResolvedPlugin, config: Any) -> None:
        fiber.state = FiberState.LOADING
        try:
            result = _call_apply(resolved.apply, fiber.ctx, config)
            if inspect.isawaitable(result):
                await result
            fiber.state = FiberState.ACTIVE
        except BaseException as exc:  # noqa: BLE001 - a plugin failure is data, not a crash
            fiber.error = exc
            fiber.state = FiberState.FAILED
            # A failed apply may have registered partial effects; unwind them now.
            await fiber.dispose_effects()
            await fiber.ctx._dispose_effects()  # noqa: SLF001 - same owner

    async def new_scope(self, parent: Context, key: Any, *, label: str = "") -> Context:
        self._sequence += 1
        fiber = Fiber(id=f"f{self._sequence}", name=label or f"scope:{key}", parent=parent.fiber, state=FiberState.ACTIVE)
        ctx = Context(self, fiber, parent=parent, scope_key=key, label=fiber.name)
        fiber.ctx = ctx
        parent.fiber.children.append(fiber)
        self._fibers[fiber.id] = fiber
        return ctx

    async def new_group(
        self, parent: Context, *, isolate: tuple[str, ...] = (), label: str = "",
    ) -> Context:
        self._sequence += 1
        fiber = Fiber(id=f"f{self._sequence}", name=label or "group", parent=parent.fiber, state=FiberState.ACTIVE)
        ctx = Context(self, fiber, parent=parent, isolated=isolate, label=fiber.name)
        fiber.ctx = ctx
        parent.fiber.children.append(fiber)
        self._fibers[fiber.id] = fiber
        return ctx

    # -- reconciliation -------------------------------------------------------

    def service_changed(self, key: str) -> None:
        self._dirty = True
        if self._settling:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._settle_task is not None and not self._settle_task.done():
            return
        self._settle_task = loop.create_task(self.settle())

    async def settle(self) -> None:
        if self._settling:
            await self._settle_done.wait()
            return
        async with self._settle_lock:
            if self._settling:
                await self._settle_done.wait()
                return
            self._settling = True
            self._settle_done.clear()
            try:
                for _ in range(100):
                    changed = False
                    for fiber in list(self._fibers.values()):
                        if fiber.state is FiberState.PENDING and fiber.resolved is not None:
                            if not fiber.ctx.missing(fiber.resolved.inject):
                                await self._activate(fiber, fiber.resolved, fiber.config)
                                changed = True
                    for fiber in list(self._fibers.values()):
                        if fiber is self.root.fiber or fiber.state is not FiberState.ACTIVE:
                            continue
                        if fiber.resolved is None:
                            continue
                        if fiber.ctx.missing(fiber.resolved.inject):
                            await fiber.dispose()
                            changed = True
                    self._prune()
                    if not changed:
                        break
                self._dirty = False
            finally:
                self._settling = False
                self._settle_done.set()

    def _prune(self) -> None:
        for fid, fiber in list(self._fibers.items()):
            if fiber.state is FiberState.DISPOSED:
                self._fibers.pop(fid, None)

    async def shutdown(self) -> None:
        if self._settle_task is not None and not self._settle_task.done():
            self._settle_task.cancel()
            try:
                await self._settle_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.root.dispose()
        self._fibers.clear()

    # -- inspection -----------------------------------------------------------

    def inspect(self) -> dict[str, Any]:
        return {
            "fibers": [fiber.describe() for fiber in self._fibers.values()],
            "services": sorted(self._service_keys(self.root)),
            "events": self.events.catalog(),
        }

    def _service_keys(self, ctx: Context) -> set[str]:
        keys = set(ctx._services)  # noqa: SLF001 - kernel-owned introspection
        for child in ctx.fiber.children:
            if child.ctx is not None:
                keys |= self._service_keys(child.ctx)
        return keys


def _call_apply(apply: Callable[..., Any], ctx: Context, config: Any) -> Any:
    try:
        signature = inspect.signature(apply)
        positional = [
            p for p in signature.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        positional = []
    if len(positional) >= 2:
        return apply(ctx, config)
    return apply(ctx)
