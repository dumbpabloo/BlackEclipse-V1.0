"""Automatic procedural memory + per-app redaction profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.computer.best_of import run_best_of
from harness.computer.grounding.matching import GroundingQuery
from harness.computer.procedural import (
    build_workflow_candidate, capture_after_success,
)
from harness.computer.security import AppProfile, SecurityLayer


class _Memory:
    available = True

    def __init__(self) -> None:
        self.writes: list[dict] = []

    # Mirrors ComputerMemory.remember's real keyword contract (`structured`).
    def remember(self, *, kind, content, application="", subject=None,
                 structured=None, scope_id=None, confidence=0.6):
        self.writes.append({
            "kind": kind, "content": content, "application": application,
            "structured": structured, "confidence": confidence,
        })
        return {"stored": True, "id": f"m{len(self.writes)}"}


class _EmptySession:
    def __init__(self, history, *, notes=None, application="tester"):
        from types import SimpleNamespace

        self.action_history = history
        self.notes = list(notes or [])
        self.latest_snapshot = SimpleNamespace(application=application)


def _history(*, done=True, verified=True, failed=False, steps=2):
    rows = [
        {"action": "click", "target": "button Save", "result": "ok", "verdict": "success",
         "verification": "verified" if verified else "uncertain"},
        {"action": "type", "target": "", "result": "ok", "verdict": "success",
         "verification": "verified" if verified else "uncertain"},
    ][:steps]
    if failed:
        rows.append({"action": "fail", "target": "", "result": "failed", "verdict": "abort"})
    if done:
        rows.append({"action": "done", "target": "", "result": "ok", "verdict": "success"})
    return rows


def test_verified_workflow_candidate_has_no_typed_text_and_scrubs_secrets():
    session = _EmptySession(
        _history(),
        notes=["the ticket is open", "password=hunter2"],
    )
    candidate = build_workflow_candidate(
        session, "open the order ticket with password=hunter2", min_actions=1,
    )
    assert candidate is not None
    assert candidate["kind"] == "workflow"
    assert candidate["verified"] is True
    assert candidate["steps"][0]["action"] == "click"
    assert "Save" in candidate["steps"][0]["target"]
    assert "hunter2" not in str(candidate)
    assert "«redacted»" in candidate["content"]


def test_candidate_requires_done_verification_and_no_fail():
    assert build_workflow_candidate(_EmptySession(_history(done=False)), "t", min_actions=1) is None
    assert build_workflow_candidate(_EmptySession(_history(verified=False)), "t", min_actions=1) is None
    assert build_workflow_candidate(_EmptySession(_history(failed=True)), "t", min_actions=1) is None
    assert build_workflow_candidate(_EmptySession([]), "t", min_actions=1) is None


def test_auto_capture_writes_only_when_enabled():
    session = _EmptySession(_history())
    memory = _Memory()

    off = capture_after_success(session, "save the file", memory=memory, auto=False, min_actions=1)
    assert off["candidate"] is not None and off["stored"] is False
    assert memory.writes == []

    on = capture_after_success(session, "save the file", memory=memory, auto=True, min_actions=1)
    assert on["stored"] is True
    assert len(memory.writes) == 1
    assert memory.writes[0]["kind"] == "workflow"
    assert memory.writes[0]["structured"]["source"] == "auto_capture"


@pytest.mark.asyncio
async def test_done_action_surfaces_and_stores_a_candidate(tmp_path):
    from tests.test_computer_state_of_art import _mk

    session, _driver, core = _mk(tmp_path, verification=True)
    session._config.memory_min_actions = 1  # noqa: SLF001
    session.memory = _Memory()

    snap = await session.observe()
    el = next(e for e in snap.elements if e.name == "Save")
    await core.act("click", element=el.ref, snapshot_id=snap.snapshot_id, capture_after=False)
    result = await core.act("done", text="save the file")
    assert result.workflow and result.workflow["candidate"]
    assert result.workflow["stored"] is False  # auto-capture is off by default

    session._config.memory_auto_capture = True  # noqa: SLF001
    stored = await core.act("done", text="save the file")
    assert stored.workflow["stored"] is True
    assert len(session.memory.writes) == 1


@pytest.mark.asyncio
async def test_best_of_stores_only_the_winner_workflow():
    from harness.computer.best_of import Branch
    from tests.test_computer_state_of_art import _FakeBranchManager

    manager = _FakeBranchManager()
    memory = _Memory()

    async def branch(session, index, task, context):
        session.action_history.extend(_history())
        session.latest_snapshot = None
        if index == 0:
            return {"success": False, "summary": "failed"}
        return {"success": True, "verified": True, "summary": "done"}

    outcome = await run_best_of(
        manager=manager, task="save the file", run_branch=branch, attempts=2,
        memory=memory, auto_capture=True,
    )
    assert outcome.chosen == 1
    assert outcome.workflow is not None
    assert outcome.workflow_stored is True
    assert len(memory.writes) == 1
    assert isinstance(outcome.branches[1], Branch)


# ── per-app redaction ────────────────────────────────────────────────────────


def _text_element(name, role="text"):
    return {"name": name, "role": role, "bounds": [10, 10, 100, 20]}


def test_trading_profile_widens_the_sensitive_vocabulary():
    layer = SecurityLayer()
    generic = layer.scan_snapshot([_text_element("Buying Power")], application="calculator")
    assert generic.sensitive is False

    report = layer.scan_snapshot([_text_element("Buying Power")], application="TradingView")
    assert report.sensitive is True
    assert any("trading_terminal" in reason for reason in report.reasons)


def test_password_manager_masks_every_text_field():
    layer = SecurityLayer()
    report = layer.scan_snapshot(
        [_text_element("", role="textbox")], application="1Password",
    )
    assert report.sensitive is True
    assert any("password_manager" in reason for reason in report.reasons)


def test_profile_pattern_matches_an_ibkr_account_id():
    layer = SecurityLayer()
    report = layer.scan_snapshot([], text="Account U1234567", application="TWS")
    assert report.sensitive is True


def test_mask_image_blacks_out_the_profile_region():
    from PIL import Image

    layer = SecurityLayer()
    report = layer.scan_snapshot([_text_element("Buying Power")], application="MetaTrader 5")
    image = Image.new("RGB", (200, 100), (255, 255, 255))
    masked = layer.mask_image(image, report.regions)
    assert masked.getpixel((20, 20)) == (0, 0, 0)
    assert masked.getpixel((190, 90)) == (255, 255, 255)


def test_custom_profile_is_honoured():
    profile = AppProfile(id="myapp", app_match=("myapp",), labels=("client value",))
    layer = SecurityLayer(profiles=[profile])
    assert layer.scan_snapshot([_text_element("Client Value")], application="myapp").sensitive
    assert not layer.scan_snapshot([_text_element("Client Value")], application="other").sensitive


@pytest.mark.asyncio
async def test_observation_flags_a_trading_window(tmp_path):
    from harness.computer.models import AXNode, Bounds

    class _TradingFake:
        id = "fake"
        platform = "test"

        def __init__(self):
            from harness.computer.models import WindowInfo

            self._w = WindowInfo(id="1", title="TradingView - NVDA", app="TradingView",
                                 pid=1, bounds=Bounds(0, 0, 800, 600), focused=True)

        def list_windows(self):
            return [self._w]

        def get_window(self, window_id):
            return self._w

        def list_displays(self):
            from harness.computer.models import DisplayInfo

            return [DisplayInfo(id="primary", bounds=Bounds(0, 0, 800, 600),
                                working_area=Bounds(0, 0, 800, 600), primary=True)]

        def accessibility_tree(self, window_id=None):
            root = AXNode(role="window", name="TradingView", bounds=Bounds(0, 0, 800, 600))
            root.children = [AXNode(role="text", name="Buying Power 25,000",
                                    bounds=Bounds(10, 10, 200, 24))]
            return root

        async def capture(self, *, window_id=None, region=None, display_id=None):
            from PIL import Image
            from harness.computer.drivers.base import ScreenFrame

            b = region or Bounds(0, 0, 800, 600)
            return ScreenFrame(data=Image.new("RGB", (b.width, b.height), (255, 255, 255)),
                               width=b.width, height=b.height, bounds=b)

    from tests.test_computer_state_of_art import _mk

    session, _driver, _core = _mk(tmp_path, fake=_TradingFake())
    snap = await session.observe(mode="som")
    assert snap.sensitive is True
    assert any("trading_terminal" in reason for reason in (snap.security or {}).get("reasons", []))


# ── learned per-app interfaces ───────────────────────────────────────────────


def _candidate(task="save the file", application="TesterApp", steps=None):
    return {
        "application": application, "task": task, "verified": True,
        "steps": steps or [
            {"action": "click", "target": "button Save", "verdict": "success"},
            {"action": "hotkey", "target": "ctrl+s", "verdict": "success"},
            {"action": "set_value", "target": "textbox Ticker", "verdict": "success"},
        ],
    }


def test_derive_adapter_validates_and_extracts_controls():
    from harness.computer.applications.learned import derive_adapter

    adapter = derive_adapter(_candidate())
    assert adapter is not None
    assert adapter.id == "testerapp"
    assert adapter.version == 1
    assert adapter.validated is True
    assert adapter.controls["save"]["role"] == "button"
    assert adapter.controls["ticker"]["name"] == "Ticker"
    assert "ctrl+s" in adapter.shortcuts.values()
    assert "TesterApp" in adapter.recall_text()
    assert adapter.evidence


def test_adapter_is_rejected_without_a_named_control_or_app():
    from harness.computer.applications.learned import derive_adapter

    only_typing = _candidate(steps=[{"action": "type", "target": "", "verdict": "success"}])
    assert derive_adapter(only_typing) is None
    assert derive_adapter(_candidate(application="")) is None


def test_adapter_versions_merge_and_persist(tmp_path):
    from harness.computer.applications.learned import LearnedAdapterBook

    path = tmp_path / "learned.json"
    book = LearnedAdapterBook(path)
    first = book.record(_candidate())
    assert first.version == 1

    second = book.record(_candidate(task="update the ticker", steps=[
        {"action": "click", "target": "button Save As", "verdict": "success"},
        {"action": "click", "target": "textbox Ticker", "verdict": "success"},
    ]))
    assert second.version == 2
    assert "save" in second.controls and "save as" in second.controls
    assert len(second.recipes) == 2

    reloaded = LearnedAdapterBook(path)
    stored = reloaded.get("TesterApp")
    assert stored is not None and stored.version == 2
    assert reloaded.select(app_name="TesterApp") is not None
    assert "Save As" in reloaded.recall("save")[0].recall_text()


@pytest.mark.asyncio
async def test_done_learns_and_registers_a_selectable_adapter(tmp_path):
    from harness.computer.applications.learned import LearnedAdapterBook
    from tests.test_computer_state_of_art import _mk

    session, _driver, core = _mk(tmp_path, verification=True)
    session._config.memory_min_actions = 1  # noqa: SLF001
    session.learned_adapters = LearnedAdapterBook(tmp_path / "learned.json")

    snap = await session.observe()
    el = next(e for e in snap.elements if e.name == "Save")
    await core.act("click", element=el.ref, snapshot_id=snap.snapshot_id, capture_after=False)
    await core.act("hotkey", combo="ctrl+s", capture_after=False)
    result = await core.act("done", text="save the file")

    assert result.workflow and result.workflow.get("adapter")
    learned = result.workflow["adapter"]
    assert learned["validated"] is True
    # The session's own registry now selects the learned adapter for that app.
    selected = session.adapter_for(app_name="tester", title="Test")
    assert selected.id.startswith("learned-")
    assert selected.semantic_selector(GroundingQuery(name="Save")) is not None


def test_live_observation_validates_and_then_retires_a_stale_adapter():
    from harness.computer.applications.learned import LearnedAdapterBook, STALE_AFTER_MISSES
    from harness.computer.applications.registry import AdapterRegistry

    book = LearnedAdapterBook(None)
    record = book.record(_candidate())
    assert record is not None and record.stale is False

    # A live snapshot that still contains "Save" confirms it.
    book.observe("TesterApp", ["Save", "Cancel"])
    assert record.last_validated_at > 0
    assert record.stale is False

    # The UI changed: repeated observations without a known control retire it.
    for _ in range(STALE_AFTER_MISSES):
        book.observe("TesterApp", ["Something Else"])
    assert record.stale is True
    assert book.select(app_name="TesterApp") is None

    registry = AdapterRegistry()
    book.register_into(registry)
    assert registry.select(app_name="TesterApp").id == "generic"

    # A new verified run resets staleness and bumps the version.
    relearned = book.record(_candidate(task="save again"))
    assert relearned.version == record.version + 1
    assert relearned.stale is False
    assert book.select(app_name="TesterApp") is not None

    assert book.forget("TesterApp") is True
    assert book.get("TesterApp") is None

