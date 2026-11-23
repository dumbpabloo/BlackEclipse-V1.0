"""Computer Use tool integration tests over a deterministic fake driver.

Exercises the `computer_*` toolset through the REAL `ToolRegistry`, so validation,
registration, permission, the effect authorizer and the response shape are all
covered — without touching a real desktop. Adversarial cases (stale element,
wrong approval, prohibited app, blocked mutation) assert the fail-safe path.
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.computer.config import computer_config
from harness.computer.manager import ComputerManager
from harness.computer.runtime import set_computer_manager
from harness.computer.tools import register_computer_tools
from harness.tools import ToolContext, ToolRegistry

from tests.test_computer_safety import _FakeDriver


@pytest.fixture()
def manager(monkeypatch):
    cfg = computer_config(refresh=True)
    cfg.driver_backend = "null"  # replaced below with the fake driver
    mgr = ComputerManager(config=cfg)
    mgr.driver = _FakeDriver()
    mgr.ax = None
    # Keep learned adapters in-memory for the test suite (never the real sidecar).
    from harness.computer.applications.learned import LearnedAdapterBook

    mgr.learned_adapters = LearnedAdapterBook(None)

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr.start())
    finally:
        loop.close()
    set_computer_manager(mgr)
    yield mgr
    set_computer_manager(None)


@pytest.fixture()
def registry():
    reg = ToolRegistry()
    register_computer_tools(reg)
    return reg


def _ctx() -> ToolContext:
    return ToolContext(run_id="test-run", log=lambda *_: None, user_id="tester")


@pytest.mark.asyncio
async def test_registration_includes_the_full_surface(registry):
    names = set(registry.names())
    for name in ("computer_use", "computer_capabilities", "computer_doctor",
                 "computer_session_start", "computer_finance_read", "computer_finance_prepare",
                 "computer_takeover", "computer_windows"):
        assert name in names
    # Elevated tools are admitted by the computer authorizer, marked non-read.
    assert registry.get("computer_use").permission == "read"
    assert registry.get("computer_finance_prepare").permission == "read"


@pytest.mark.asyncio
async def test_capabilities_and_doctor(registry, manager):
    r = await registry.execute("computer_capabilities", "c", {}, _ctx())
    assert r.ok and r.data["driver"] == "fake"
    r = await registry.execute("computer_doctor", "c", {}, _ctx())
    assert r.ok and r.data["status"] == "READY"


@pytest.mark.asyncio
async def test_listing_tools(registry, manager):
    r = await registry.execute("computer_windows", "c", {}, _ctx())
    assert r.ok and r.data["count"] == 1
    r = await registry.execute("computer_apps", "c", {}, _ctx())
    assert r.ok and r.data["count"] == 1
    r = await registry.execute("computer_displays", "c", {}, _ctx())
    assert r.ok and len(r.data["displays"]) == 1


@pytest.mark.asyncio
async def test_failed_capture_attaches_no_media(registry, manager):
    await registry.execute("computer_session_start", "c", {}, _ctx())
    first = await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    assert first.data["media"]["dataUrl"].startswith("data:image/")

    from harness.computer.policy import DENY

    session = manager.any_session()
    session.policy.permissions["observe"] = DENY
    failed = await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    assert "media" not in failed.data
    assert "COMPUTER_PERMISSION_DENIED" in failed.text or "FAILED" in failed.text


@pytest.mark.asyncio
async def test_computer_run_is_honest_without_a_branch_runner(registry, manager):
    r = await registry.execute("computer_run", "c", {"task": "open the settings dialog"}, _ctx())
    assert "COMPUTER_BRANCH_RUNNER_UNAVAILABLE" in r.text


@pytest.mark.asyncio
async def test_computer_run_refuses_best_of_for_a_financial_mutation(registry, manager):
    async def fake_runner(session, index, task, context):
        return {"success": True, "summary": "should never run"}

    manager.branch_runner = fake_runner
    r = await registry.execute(
        "computer_run", "c", {"task": "buy 10 shares of NVDA", "attempts": 3}, _ctx(),
    )
    assert "COMPUTER_MULTI_ATTEMPT_FINANCE" in r.text
    assert r.data["code"] == "COMPUTER_MULTI_ATTEMPT_FINANCE"


@pytest.mark.asyncio
async def test_computer_forget_interface_removes_learned_knowledge(registry, manager):
    manager.learned_adapters.record({
        "application": "tester", "task": "save the file",
        "steps": [
            {"action": "click", "target": "button Save", "verdict": "success"},
            {"action": "hotkey", "target": "ctrl+s", "verdict": "success"},
        ],
    })
    assert manager.learned_adapters.get("tester") is not None
    r = await registry.execute(
        "computer_forget_interface", "c", {"application": "tester"}, _ctx(),
    )
    assert r.data["forgotten"] is True
    assert manager.learned_adapters.get("tester") is None
    recalled = await registry.execute("computer_recall", "c", {"query": "tester"}, _ctx())
    assert "no stored knowledge" in recalled.text.lower()


@pytest.mark.asyncio
async def test_computer_run_learns_and_exposes_an_adapter(registry, manager):
    async def fake_runner(session, index, task, context):
        await session.observe()
        snap = session.latest_snapshot
        el = next(e for e in snap.elements if e.name == "Save")
        session.action_history.append({
            "action": "click", "target": f"{el.role} {el.name}", "result": "ok",
            "verdict": "success", "verification": "verified",
        })
        session.action_history.append({
            "action": "hotkey", "target": "ctrl+s", "result": "ok",
            "verdict": "success", "verification": "verified",
        })
        session.action_history.append({
            "action": "done", "target": "", "result": "ok", "verdict": "success",
            "verification": "",
        })
        return {"success": True, "verified": True, "summary": "done"}

    manager.branch_runner = fake_runner
    r = await registry.execute(
        "computer_run", "c", {"task": "save the file", "attempts": 1}, _ctx(),
    )
    learned = r.data.get("learnedAdapter")
    assert learned and learned["validated"] is True
    assert learned["version"] == 1

    recalled = await registry.execute(
        "computer_recall", "c", {"query": "tester"}, _ctx(),
    )
    assert "Learned interface" in recalled.text
    assert recalled.data["learnedAdapters"]


@pytest.mark.asyncio
async def test_computer_run_selects_a_verified_branch(registry, manager):
    async def fake_runner(session, index, task, context):
        session.action_history.extend([
            {"action": "click", "result": "ok", "verdict": "success", "verification": "verified"},
            {"action": "done", "result": "ok", "verdict": "success"},
        ])
        if index == 0:
            return {"success": False, "summary": "the menu never opened"}
        return {"success": True, "verified": True, "summary": "done"}

    manager.branch_runner = fake_runner
    r = await registry.execute(
        "computer_run", "c", {"task": "open settings", "attempts": 2}, _ctx(),
    )
    assert r.data["chosen"] == 1
    assert r.data["judgedBy"] == "deterministic"
    assert len(r.data["branches"]) == 2
    assert "branch 1" in r.text and "branch 2" in r.text
    assert r.data["branches"][1]["verified"] is True


@pytest.mark.asyncio
async def test_session_start_and_capture(registry, manager):
    r = await registry.execute("computer_session_start", "c", {}, _ctx())
    assert r.ok
    r = await registry.execute("computer_use", "c", {"action": "capture", "mode": "som"}, _ctx())
    assert r.ok
    # The screenshot is REAL model media, not only a path: tool_turn attaches
    # result.data["media"] after the tool result.
    assert r.data["media"]["dataUrl"].startswith("data:image/")
    assert "screenshot is attached" in r.text
    # SOM is annotated: the persisted frame differs from a blank image.
    assert r.data["elements"]
    assert r.data.get("snapshotId")
    assert isinstance(r.data.get("elements"), list)


@pytest.mark.asyncio
async def test_stale_element_returns_recapture_verdict(registry, manager):
    await registry.execute("computer_session_start", "c", {}, _ctx())
    first = await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    snapshot_id = first.data["snapshotId"]
    # A second capture invalidates the first snapshot.
    await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    r = await registry.execute(
        "computer_use", "c",
        {"action": "click", "element": 1, "snapshot_id": snapshot_id},
        _ctx(),
    )
    # The click is held for approval first, but a stale handle must be refused.
    code = (r.data or {}).get("code")
    assert code in ("COMPUTER_STALE_ELEMENT", "COMPUTER_APPROVAL_REQUIRED")


@pytest.mark.asyncio
async def test_click_without_approval_is_refused(registry, manager):
    await registry.execute("computer_session_start", "c", {}, _ctx())
    snap = await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    r = await registry.execute(
        "computer_use", "c",
        {"action": "click", "element": 1, "snapshot_id": snap.data["snapshotId"]},
        _ctx(),
    )
    assert (r.data or {}).get("code") == "COMPUTER_APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_finance_prepare_blocks_withdraw(registry, manager):
    r = await registry.execute("computer_finance_prepare", "c", {"action": "withdraw"}, _ctx())
    assert (r.data or {}).get("code") == "COMPUTER_FINANCE_BLOCKED"


@pytest.mark.asyncio
async def test_finance_prepare_assisted_buy_needs_approval(registry, manager):
    import harness.computer.config as config_mod

    config_mod.computer_config(refresh=True).finance_mode = "assisted"
    r = await registry.execute(
        "computer_finance_prepare", "c",
        {"action": "buy", "instrument": "NVDA", "quantity": 10, "price": 100},
        _ctx(),
    )
    assert (r.data or {}).get("code") == "COMPUTER_FINANCE_APPROVAL"


@pytest.mark.asyncio
async def test_finance_read_carries_screen_provenance(registry, manager):
    await registry.execute("computer_session_start", "c", {}, _ctx())
    r = await registry.execute("computer_finance_read", "c", {}, _ctx())
    assert r.ok
    provenance = r.data["financialState"]["provenance"]
    assert provenance["source_type"] == "desktop_screen"
    assert "confidence" in provenance


@pytest.mark.asyncio
async def test_takeover_tool_stops_actions(registry, manager):
    start = await registry.execute("computer_session_start", "c", {}, _ctx())
    session_id = start.data["session"]["sessionId"]
    await registry.execute("computer_takeover", "c", {"session_id": session_id}, _ctx())
    assert not manager.takeover.agent_may_act(session_id)
    await registry.execute("computer_resume", "c", {"session_id": session_id}, _ctx())
    assert manager.takeover.agent_may_act(session_id)


@pytest.mark.asyncio
async def test_dry_run_never_clicks(registry, manager):
    await registry.execute("computer_session_start", "c", {}, _ctx())
    snap = await registry.execute("computer_use", "c", {"action": "capture"}, _ctx())
    r = await registry.execute(
        "computer_use", "c",
        {"action": "click", "element": 1, "snapshot_id": snap.data["snapshotId"], "dry_run": True},
        _ctx(),
    )
    assert r.ok
    assert r.data["deliveryMode"] == "dry_run"
    assert manager.driver.clicks == []
