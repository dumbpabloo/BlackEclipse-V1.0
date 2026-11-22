"""Supervisor: reconcile, generation rollback and teardown."""

from __future__ import annotations

import sys
import types

import pytest

from harness.kernel import Kernel, Supervisor
from harness.kernel.composition import Composition, Row
from harness.kernel.errors import SupervisorError


def good(ctx, config):
    ctx.provide("good", config or {})


def second(ctx, config):
    ctx.provide("second", config or {})


def boom(ctx, config):
    raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _fixture_module():
    module = types.ModuleType("kernel_fixtures")
    module.good = good
    module.second = second
    module.boom = boom
    sys.modules["kernel_fixtures"] = module
    yield
    sys.modules.pop("kernel_fixtures", None)


def _composition(*rows: Row) -> Composition:
    return Composition(rows=list(rows))


async def test_add_update_remove_and_disable():
    kernel = Kernel()
    supervisor = Supervisor(kernel)
    first = _composition(Row(id="good", name="kernel_fixtures:good", config={"v": 1}))
    result = await supervisor.apply(first)
    assert result["added"] == ["good"]
    assert kernel.root.require("good") == {"v": 1}

    updated = _composition(Row(id="good", name="kernel_fixtures:good", config={"v": 2}))
    result = await supervisor.apply(updated)
    assert result["updated"] == ["good"]
    assert kernel.root.require("good") == {"v": 2}

    removed = _composition()
    result = await supervisor.apply(removed)
    assert result["removed"] == ["good"]
    assert kernel.root.get("good") is None
    await kernel.shutdown()


async def test_disable_is_removal():
    kernel = Kernel()
    supervisor = Supervisor(kernel)
    await supervisor.apply(_composition(Row(id="good", name="kernel_fixtures:good")))
    disabled = _composition(Row(id="good", name="kernel_fixtures:good", disabled=True))
    result = await supervisor.apply(disabled)
    assert result["removed"] == ["good"]
    assert kernel.root.get("good") is None
    await kernel.shutdown()


async def test_failed_row_restores_previous_generation():
    kernel = Kernel()
    supervisor = Supervisor(kernel)
    await supervisor.apply(_composition(Row(id="good", name="kernel_fixtures:good", config={"v": 1})))

    with pytest.raises(SupervisorError) as info:
        await supervisor.apply(_composition(
            Row(id="good", name="kernel_fixtures:good", config={"v": 9}),
            Row(id="boom", name="kernel_fixtures:boom"),
        ))
    assert info.value.code == "KERNEL_ROW_FAILED"
    assert info.value.detail["rollback"] == "restored"
    assert kernel.root.require("good") == {"v": 1}
    assert kernel.root.get("boom") is None
    await kernel.shutdown()


async def test_rollback_returns_to_the_previous_generation():
    kernel = Kernel()
    supervisor = Supervisor(kernel)
    await supervisor.apply(_composition(Row(id="good", name="kernel_fixtures:good", config={"v": 1})))
    await supervisor.apply(_composition(Row(id="second", name="kernel_fixtures:second")))
    assert kernel.root.get("good") is None
    assert kernel.root.require("second") is not None

    result = await supervisor.rollback()
    assert result == {"ok": True, "restored": ["good"]}
    assert kernel.root.require("good") == {"v": 1}
    assert kernel.root.get("second") is None
    await kernel.shutdown()


async def test_pending_row_is_reported_not_failed():
    kernel = Kernel()
    supervisor = Supervisor(kernel)

    def needs(ctx, config):
        ctx.require("absent")

    needs.inject = ("absent",)
    module = sys.modules["kernel_fixtures"]
    module.needs = needs
    result = await supervisor.apply(_composition(Row(id="needs", name="kernel_fixtures:needs")))
    assert result["ok"] is True
    assert result["pending"] == ["needs"]
    await kernel.shutdown()
