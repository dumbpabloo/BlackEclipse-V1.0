"""Regression tests for the defects found by the post-implementation audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.computer.procedural import build_workflow_candidate, store_workflow
from harness.computer.security import SecurityLayer
from harness.computer.best_of import _new_branch_session


class _StrictMemory:
    available = True

    def __init__(self) -> None:
        self.writes: list[dict] = []

    # The REAL ComputerMemory signature: `structured`, never `structured_data`.
    def remember(self, *, kind, content, application="", subject=None,
                 structured=None, scope_id=None, confidence=0.6):
        self.writes.append({
            "kind": kind, "content": content, "application": application,
            "structured": structured, "confidence": confidence,
        })
        return {"stored": True, "id": "m1"}


def _candidate():
    return {
        "application": "Tester", "task": "save the file", "verified": True,
        "steps": [
            {"action": "click", "target": "button Save", "verdict": "success"},
            {"action": "hotkey", "target": "ctrl+s", "verdict": "success"},
        ],
        "content": "Verified workflow", "confidence": 0.7,
    }


def test_workflow_store_uses_the_real_memory_signature():
    memory = _StrictMemory()
    result = store_workflow(memory, _candidate())
    assert result["stored"] is True
    assert len(memory.writes) == 1
    assert memory.writes[0]["structured"]["source"] == "auto_capture"


def test_mask_image_offsets_by_the_frame_origin():
    from PIL import Image

    layer = SecurityLayer()
    report = layer.scan_snapshot(
        [{"name": "Buying Power", "role": "text",
          "bounds": {"x": 120, "y": 80, "width": 60, "height": 20}}],
        application="TradingView",
    )
    image = Image.new("RGB", (200, 120), (255, 255, 255))
    masked = layer.mask_image(image, report.regions, origin=(100, 60))
    # Screen (120,80) is image (20,20) when the window starts at (100,60).
    assert masked.getpixel((25, 25)) == (0, 0, 0)
    # The un-offset position is NOT masked (old bug masked the wrong pixels).
    assert masked.getpixel((150, 90)) == (255, 255, 255)


def test_vision_results_include_name_so_the_grounder_can_score_them():
    from harness.computer.grounding.matching import GroundingQuery, rank_elements

    found = [{"name": "Continue", "text": "Continue", "role": "button",
              "bounds": [10, 10, 20, 10], "confidence": 0.9}]
    ranked = rank_elements(GroundingQuery(intent="Continue"), found)
    assert ranked and ranked[0][0] > 0


@pytest.mark.asyncio
async def test_branch_sessions_suppress_shared_memory_and_learning(tmp_path):
    from tests.test_computer_state_of_art import _FakeBranchManager, _mk
    from harness.computer.applications.learned import LearnedAdapterBook

    manager = _FakeBranchManager()
    session = await _new_branch_session(manager, 0)
    assert session.suppress_auto_memory is True

    # A real `done` on a suppressed session writes neither memory nor an adapter.
    real, driver, core = _mk(tmp_path, verification=True)
    real._config.memory_min_actions = 1  # noqa: SLF001
    real.suppress_auto_memory = True
    real.memory = _StrictMemory()
    real.learned_adapters = LearnedAdapterBook(tmp_path / "learned.json")
    snap = await real.observe()
    el = next(e for e in snap.elements if e.name == "Save")
    await core.act("click", element=el.ref, snapshot_id=snap.snapshot_id, capture_after=False)
    result = await core.act("done", text="save the file")
    assert result.workflow["stored"] is False
    assert "adapter" not in result.workflow
    assert real.memory.writes == []
    assert real.learned_adapters.get("tester") is None


@pytest.mark.asyncio
async def test_windowless_vision_capture_still_applies_the_app_profile(tmp_path):
    from harness.computer.models import AXNode, Bounds, DisplayInfo, WindowInfo
    from harness.computer.drivers.base import ScreenFrame
    from tests.test_computer_state_of_art import _mk

    class _TradingFake:
        id = "fake-trading"
        platform = "test"

        def __init__(self):
            self._w = WindowInfo(id="1", title="TradingView - NVDA", app="TradingView",
                                 pid=1, bounds=Bounds(0, 0, 800, 600), focused=True)

        def list_windows(self):
            return [self._w]

        def get_window(self, window_id):
            return self._w

        def list_displays(self):
            return [DisplayInfo(id="primary", bounds=Bounds(0, 0, 800, 600),
                                working_area=Bounds(0, 0, 800, 600), primary=True)]

        def accessibility_tree(self, window_id=None):
            root = AXNode(role="window", name="TradingView", bounds=Bounds(0, 0, 800, 600))
            root.children = [AXNode(role="text", name="Buying Power 25,000",
                                    bounds=Bounds(10, 10, 200, 24))]
            return root

        async def capture(self, *, window_id=None, region=None, display_id=None):
            from PIL import Image

            b = region or Bounds(0, 0, 800, 600)
            return ScreenFrame(data=Image.new("RGB", (b.width, b.height), (255, 255, 255)),
                               width=b.width, height=b.height, bounds=b)

    session, _driver, _core = _mk(tmp_path, fake=_TradingFake())
    snap = await session.observe(mode="vision")
    # A window-less vision capture resolves the FOCUSED window, so the app/title
    # are available to the security profile (before, both were empty).
    assert snap.window_id == "1"
    assert "TradingView" in snap.window_title


@pytest.mark.asyncio
async def test_sensitive_observation_redacts_text_and_element_values(tmp_path):
    from harness.computer.models import AXNode, Bounds
    from tests.test_computer_safety import _FakeDriver
    from tests.test_computer_state_of_art import _mk

    class _SecretDriver(_FakeDriver):
        def accessibility_tree(self, window_id=None):
            root = AXNode(role="window", name="Test", bounds=Bounds(0, 0, 800, 600))
            root.children = [AXNode(role="textbox", name="API key",
                                    value="sk-abcdefghijklmnop1234",
                                    bounds=Bounds(20, 20, 200, 24))]
            return root

    session, _driver, _core = _mk(tmp_path, fake=_SecretDriver())
    snap = await session.observe(mode="som")
    assert snap.sensitive is True
    assert "sk-abcdefghijklmnop" not in snap.text
    assert all("sk-abcdefghijklmnop" not in (e.value or "") for e in snap.elements)


@pytest.mark.asyncio
async def test_clipboard_get_is_redacted_and_marked_untrusted(tmp_path):
    from harness.computer.core import ComputerCore
    from harness.computer.drivers.base import ScreenFrame
    from harness.computer.models import Bounds, WindowInfo
    from tests.test_computer_safety import _FakeDriver
    from tests.test_computer_state_of_art import _mk

    class _ClipDriver(_FakeDriver):
        async def get_clipboard(self):
            return "token=sk-abcdefghijklmnop1234"

        async def capture(self, *, window_id=None, region=None, display_id=None):
            from PIL import Image

            b = region or Bounds(0, 0, 800, 600)
            return ScreenFrame(data=Image.new("RGB", (b.width, b.height), (0, 0, 0)),
                               width=b.width, height=b.height, bounds=b)

    session, _driver, _core = _mk(tmp_path, fake=_ClipDriver())
    result = await ComputerCore(session, config=session._config).act("clipboard_get")
    assert "«redacted»" in result.summary
    assert "UNTRUSTED" in result.summary
    assert result.evidence[0]["untrusted"] is True


@pytest.mark.asyncio
async def test_drag_and_note_honour_a_per_call_dry_run(tmp_path):
    from tests.test_computer_state_of_art import _DragDriver, _mk

    session, driver, core = _mk(tmp_path, fake=_DragDriver())
    await session.observe()
    drag = await core.act("drag", from_coordinates=[10, 10], to_coordinates=[20, 20], dry_run=True)
    assert drag.delivery_mode == "dry_run"
    assert driver.drags == []
    note = await core.act("note", text="should not store", dry_run=True)
    assert note.delivery_mode == "dry_run"
    assert session.notes == []


@pytest.mark.asyncio
async def test_loop_warning_fires_before_the_third_attempt(tmp_path):
    from tests.test_computer_state_of_art import _mk

    session, _driver, core = _mk(tmp_path, verification=False)
    snap = await session.observe()
    ref = snap.elements[0].ref
    await core.act("click", element=ref, snapshot_id=snap.snapshot_id, capture_after=False)
    await core.act("click", element=ref, snapshot_id=snap.snapshot_id, capture_after=False)
    assert session.trajectory_summary()["loopSuspected"] is True


@pytest.mark.asyncio
async def test_drag_element_handles_reject_a_stale_snapshot(tmp_path):
    from tests.test_computer_state_of_art import _DragDriver, _mk

    session, _driver, core = _mk(tmp_path, fake=_DragDriver())
    await session.observe()
    result = await core.act(
        "drag", from_element=1, to_element=2, snapshot_id="stale-id", capture_after=False,
    )
    assert not result.success
    assert result.error and result.error["code"] == "COMPUTER_STALE_ELEMENT"


@pytest.mark.asyncio
async def test_uncertain_verification_is_not_a_success(tmp_path):
    from tests.test_computer_state_of_art import _mk

    session, _driver, core = _mk(tmp_path, verification=True)
    snap = await session.observe()
    result = await core.act(
        "click", coordinates=[500, 380], allow_coordinates=True,
        snapshot_id=snap.snapshot_id, capture_after=False,
    )
    assert result.verification["status"] == "uncertain"
    assert result.success is False
    assert result.verdict == "recapture"
    assert session.action_history[-1]["result"] == "failed"


def test_base_accessibility_activate_accepts_value():
    from harness.computer.accessibility.base import AXProvider

    assert AXProvider().activate(window_id="1", name="Save", value="x") is False


@pytest.mark.asyncio
async def test_close_reports_a_failed_process_close(tmp_path):
    from tests.test_computer_state_of_art import _mk

    from harness.computer.drivers.base import InputResult

    class _CloseFails:
        id = "fake-close"
        platform = "test"

        def list_windows(self):
            return []

        def get_window(self, window_id):
            return None

        def list_displays(self):
            return []

        def list_applications(self):
            from harness.computer.models import ApplicationInfo

            return [ApplicationInfo(id="1", name="tester", pid=1)]

        async def capture(self, **_kwargs):
            return None

        async def close_application(self, app_id):
            return {"closed": False}

    session, _driver, core = _mk(tmp_path, fake=_CloseFails())
    result = await core.act("close", app="tester")
    assert result.success is False
    assert result.verdict == "retry"


def test_learned_suffix_is_stripped_from_the_application_identity():
    from types import SimpleNamespace
    from harness.computer.procedural import build_workflow_candidate

    session = SimpleNamespace(
        action_history=[
            {"action": "click", "target": "button Save", "result": "ok",
             "verdict": "success", "verification": "verified"},
            {"action": "done", "result": "ok", "verdict": "success"},
        ],
        notes=[],
        latest_snapshot=SimpleNamespace(application="Tester (learned v3)"),
    )
    candidate = build_workflow_candidate(session, "save", min_actions=1)
    assert candidate["application"] == "Tester"


@pytest.mark.asyncio
async def test_typing_is_delivered_without_echoing_the_text(tmp_path):
    from tests.test_computer_state_of_art import _mk

    session, driver, core = _mk(tmp_path, verification=False)
    await session.observe()
    result = await core.act("type", text="correct horse battery staple", capture_after=False)
    assert result.success is True
    assert driver.typed == ["correct horse battery staple"]
    assert "correct horse" not in result.summary
    assert all("correct horse" not in str(entry) for entry in session.action_history)


@pytest.mark.asyncio
async def test_focus_without_a_pattern_is_an_honest_retry(tmp_path):
    from tests.test_computer_state_of_art import _mk

    session, _driver, core = _mk(tmp_path)
    snap = await session.observe()
    element = next(e for e in snap.elements if e.name == "Save")
    result = await core.act("focus", element=element.ref, snapshot_id=snap.snapshot_id)
    assert result.success is False
    assert result.verdict == "retry"


@pytest.mark.asyncio
async def test_forget_stops_selection_in_a_live_registry():
    from harness.computer.applications.learned import LearnedAdapterBook
    from harness.computer.applications.registry import AdapterRegistry

    book = LearnedAdapterBook(None)
    book.record(_candidate())
    registry = AdapterRegistry()
    book.register_into(registry)
    assert registry.select(app_name="Tester").id.startswith("learned-")
    book.forget("Tester")
    assert registry.select(app_name="Tester").id == "generic"
