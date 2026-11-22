"""Phase 5: session-scoped dynamic packages in the existing sandbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.dynamic import DynamicError, DynamicPackages


SOURCE = """
def handle(payload):
    value = payload.get("value", 0)
    return {"echo": value, "kind": "demo"}
"""


@pytest.fixture()
def packages(tmp_path):
    return DynamicPackages(tmp_path / "dynamic", ttl_seconds=60, max_packages=3)


async def test_define_run_invoke_update_stop_undefine(tmp_path, packages):
    defined = packages.define(session_id="s1", plugin_id="demo", source=SOURCE)
    assert defined["digest"].startswith("dyn_")

    with pytest.raises(DynamicError) as info:
        await packages.run(session_id="s1", plugin_id="demo", package_id=defined["id"])
    assert info.value.code == "DYNAMIC_APPROVAL_REQUIRED"

    run = await packages.run(
        session_id="s1", plugin_id="demo", package_id=defined["id"], approved=True, max_calls=3,
    )
    assert run["packageId"] == defined["id"]
    result = await packages.invoke(session_id="s1", plugin_id="demo", payload={"value": 7})
    assert result["result"] == {"echo": 7, "kind": "demo"}

    updated = await packages.update(
        session_id="s1", plugin_id="demo", source=SOURCE.replace('"demo"', '"v2"'), approved=True,
    )
    assert updated["package"]["id"] != defined["id"]
    second = await packages.invoke(session_id="s1", plugin_id="demo", payload={"value": 8})
    assert second["result"]["kind"] == "v2"

    stopped = await packages.stop(session_id="s1", plugin_id="demo")
    assert stopped["stopped"] is True
    with pytest.raises(DynamicError):
        await packages.invoke(session_id="s1", plugin_id="demo", payload={})

    removed = await packages.undefine(session_id="s1", plugin_id="demo")
    assert len(removed["removed"]) == 2
    assert packages.status(session_id="s1", plugin_id="demo")["plugins"][0]["packages"] == []


async def test_packages_are_session_scoped(packages):
    defined = packages.define(session_id="s1", plugin_id="demo", source=SOURCE)
    with pytest.raises(DynamicError) as info:
        await packages.run(
            session_id="s2", plugin_id="demo", package_id=defined["id"], approved=True,
        )
    assert info.value.code == "DYNAMIC_NOT_FOUND"


async def test_call_budget_stops_the_run(packages):
    defined = packages.define(session_id="s1", plugin_id="demo", source=SOURCE)
    await packages.run(
        session_id="s1", plugin_id="demo", package_id=defined["id"], approved=True, max_calls=1,
    )
    await packages.invoke(session_id="s1", plugin_id="demo", payload={"value": 1})
    with pytest.raises(DynamicError) as info:
        await packages.invoke(session_id="s1", plugin_id="demo", payload={"value": 2})
    assert info.value.code == "DYNAMIC_CALL_BUDGET"


async def test_ttl_expiry_stops_the_run(tmp_path):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    packages = DynamicPackages(tmp_path / "dynamic", now=lambda: clock["now"], ttl_seconds=10)
    defined = packages.define(session_id="s1", plugin_id="demo", source=SOURCE)
    await packages.run(
        session_id="s1", plugin_id="demo", package_id=defined["id"], approved=True, ttl_seconds=10,
    )
    clock["now"] += timedelta(seconds=11)
    with pytest.raises(DynamicError) as info:
        await packages.invoke(session_id="s1", plugin_id="demo", payload={})
    assert info.value.code == "DYNAMIC_TTL"


def test_package_cap_and_empty_source(packages):
    for index in range(3):
        packages.define(session_id="s1", plugin_id=f"p{index}", source=SOURCE)
    with pytest.raises(DynamicError) as info:
        packages.define(session_id="s1", plugin_id="overflow", source=SOURCE)
    assert info.value.code == "DYNAMIC_CAP"
    with pytest.raises(DynamicError) as info:
        packages.define(session_id="s2", plugin_id="empty", source="  ")
    assert info.value.code == "DYNAMIC_SOURCE_EMPTY"
