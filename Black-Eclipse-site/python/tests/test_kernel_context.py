"""Kernel: services, coeffects, effects and disposal."""

from __future__ import annotations

import asyncio

import pytest

from harness.kernel import Kernel, ServiceConflict, ServiceUnavailable
from harness.kernel.fiber import FiberState


def provider(ctx, config):
    ctx.provide("alpha", {"value": config or 1})


def consumer(ctx, config):
    ctx.provide("beta", ctx.require("alpha"))


consumer.inject = ("alpha",)


async def test_inject_gates_activation_and_service_removal_disposes_consumer():
    kernel = Kernel()
    beta = await kernel.root.plugin(consumer)
    assert beta.state is FiberState.PENDING

    dispose_alpha = kernel.root.provide("alpha", {"value": 1})
    await kernel.settle()
    assert beta.state is FiberState.ACTIVE
    assert kernel.root.require("beta") == {"value": 1}

    dispose_alpha()
    await kernel.settle()
    assert beta.state is FiberState.DISPOSED
    assert kernel.root.get("beta") is None
    await kernel.shutdown()


async def test_require_names_every_missing_service():
    kernel = Kernel()
    with pytest.raises(ServiceUnavailable) as info:
        kernel.root.inject("one", "two")
    assert info.value.detail["missing"] == ["one", "two"]
    await kernel.shutdown()


async def test_service_conflict_needs_explicit_replace():
    kernel = Kernel()
    kernel.root.provide("key", 1)
    with pytest.raises(ServiceConflict):
        kernel.root.provide("key", 2)
    kernel.root.provide("key", 3, replace=True)
    assert kernel.root.require("key") == 3
    await kernel.shutdown()


async def test_effects_unwind_in_reverse_order_and_are_idempotent():
    kernel = Kernel()
    order: list[str] = []

    def first() -> None:
        order.append("first")

    def second() -> None:
        order.append("second")

    ctx = await kernel.root.scope("agent")
    ctx.effect(lambda: first, label="first")
    ctx.effect(lambda: second, label="second")
    await ctx.dispose()
    await ctx.dispose()
    assert order == ["second", "first"]
    await kernel.shutdown()


async def test_a_failing_disposer_does_not_skip_remaining_cleanup():
    kernel = Kernel()
    order: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    ctx = await kernel.root.scope("agent")
    ctx.effect(lambda: (lambda: order.append("outer")))
    ctx.effect(lambda: boom)
    await ctx.dispose()
    assert order == ["outer"]
    await kernel.shutdown()


async def test_child_plugin_unloads_with_parent_scope():
    kernel = Kernel()
    scope = await kernel.root.scope("agent")
    fiber = await scope.plugin(provider)
    assert fiber.state is FiberState.ACTIVE
    assert scope.require("alpha") == {"value": 1}
    await scope.dispose()
    assert fiber.state is FiberState.DISPOSED
    assert kernel.root.get("alpha") is None
    await kernel.shutdown()


async def test_group_isolates_a_service_from_its_parent():
    kernel = Kernel()
    kernel.root.provide("shell", "global")
    group = await kernel.root.group(isolate=("shell",))
    group.provide("shell", "local")
    assert group.require("shell") == "local"
    assert kernel.root.require("shell") == "global"
    await kernel.shutdown()


async def test_parallel_scopes_do_not_see_each_other():
    kernel = Kernel()
    first = await kernel.root.scope("one")
    second = await kernel.root.scope("two")
    first.provide("name", "one")
    assert first.require("name") == "one"
    assert second.get("name") is None
    await kernel.shutdown()


async def test_failed_apply_is_recorded_not_raised():
    kernel = Kernel()

    def boom(ctx, config):
        raise RuntimeError("bad plugin")

    fiber = await kernel.root.plugin(boom)
    assert fiber.state is FiberState.FAILED
    assert fiber.describe()["error"].startswith("RuntimeError")
    await kernel.shutdown()
