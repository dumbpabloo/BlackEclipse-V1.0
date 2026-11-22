"""Kernel: typed event dispatch and scoped registries."""

from __future__ import annotations

from harness.kernel import Kernel, ScopedRegistry
from harness.kernel.events import MODES


async def test_emit_runs_every_listener_in_order():
    kernel = Kernel()
    seen: list[str] = []
    kernel.root.on("demo", lambda: seen.append("a"), mode="emit", order=10)
    kernel.root.on("demo", lambda: seen.append("b"), mode="emit", order=5)
    await kernel.root.emit("demo")
    assert seen == ["b", "a"]
    await kernel.shutdown()


async def test_bail_stops_at_first_result():
    kernel = Kernel()
    seen: list[str] = []

    def first():
        seen.append("first")
        return "stop"

    def second():
        seen.append("second")

    kernel.root.on("check", first, mode="bail")
    kernel.root.on("check", second, mode="bail")
    assert await kernel.root.bail("check") == "stop"
    assert seen == ["first"]
    await kernel.shutdown()


async def test_serial_awaits_and_returns_first_non_none():
    kernel = Kernel()

    async def first():
        return None

    async def second():
        return "value"

    kernel.root.on("phase", first, mode="serial")
    kernel.root.on("phase", second, mode="serial")
    assert await kernel.root.serial("phase") == "value"
    await kernel.shutdown()


async def test_waterfall_receives_next_and_can_wrap():
    kernel = Kernel()

    async def outer(value, next_call):
        inner = await next_call()
        return f"{outer.__name__}[{inner}]"

    async def inner(value, next_call):
        return f"inner({value})"

    kernel.root.on("transform", outer, mode="waterfall")
    kernel.root.on("transform", inner, mode="waterfall")
    assert await kernel.root.waterfall("transform", "x", fallback="base") == "outer[inner(x)]"
    await kernel.shutdown()


async def test_waterfall_without_next_short_circuits():
    kernel = Kernel()

    def stop(value, next_call):
        return "stopped"

    kernel.root.on("transform", stop, mode="waterfall")
    assert await kernel.root.waterfall("transform", "x", fallback="base") == "stopped"
    await kernel.shutdown()


async def test_parallel_awaits_all_listeners():
    kernel = Kernel()
    seen: list[int] = []

    async def handler(value):
        seen.append(value)

    kernel.root.on("fan", handler, mode="parallel")
    await kernel.root.parallel("fan", 1)
    assert seen == [1]
    await kernel.shutdown()


async def test_listener_disposed_with_its_owner():
    kernel = Kernel()
    seen: list[str] = []
    scope = await kernel.root.scope("agent")
    scope.on("demo", lambda: seen.append("scope"))
    await scope.dispose()
    await kernel.root.emit("demo")
    assert seen == []
    await kernel.shutdown()


def test_modes_are_closed():
    assert set(MODES) == {"emit", "waterfall", "serial", "bail", "parallel"}


async def test_scoped_registry_inherits_down_and_shadows_per_scope():
    kernel = Kernel()
    registry: ScopedRegistry[str] = ScopedRegistry()
    root = await kernel.root.scope("root-scope")
    child = await root.scope("child")
    sibling = await kernel.root.scope("sibling")

    registry.register("tool", "base", scope=root)
    registry.register("tool", "child", scope=child)
    assert registry.names(root) == ["tool"]
    assert registry.get("tool", root) == "base"
    assert registry.get("tool", child) == "child"
    assert registry.get("tool", sibling) is None
    await kernel.shutdown()


async def test_scoped_registration_disposal_only_removes_its_layer():
    kernel = Kernel()
    registry: ScopedRegistry[str] = ScopedRegistry()
    root = await kernel.root.scope("root-scope")
    child = await root.scope("child")
    registry.register("tool", "base", scope=root)
    dispose = registry.register("tool", "child", scope=child)
    dispose()
    assert registry.get("tool", child) == "base"
    await kernel.shutdown()
