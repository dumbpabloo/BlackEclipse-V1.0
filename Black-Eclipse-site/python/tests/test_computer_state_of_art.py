"""State-of-the-art upgrade tests: pixels, grounding, progress, action surface.

These are unit-level and deterministic. They assert the behaviours the gap
analysis found missing, so a regression is caught without a real desktop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.computer.config import ComputerConfig
from harness.computer.core import ComputerCore
from harness.computer.grounding.backends import (
    UiTarsBackend, _parse_reply, build_grounding_backend,
)
from harness.computer.models import Bounds, SnapshotElement
from harness.computer.security import SecurityLayer
from harness.computer.policy import ALLOW, default_policy
from harness.computer.session import ComputerSession
from harness.computer.accessibility.null import NullAXProvider
from harness.computer.ocr.tesseract import NullOCREngine
from harness.computer.vision.backends import UnavailableVision
from harness.computer.vision.router import VisionRouter
from harness.tools import _args_model
from harness.computer.schemas import INPUT_SCHEMAS

from tests.test_computer_safety import _FakeDriver


class _DragDriver(_FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.drags: list[tuple[int, int, int, int]] = []

    async def drag(self, x1, y1, x2, y2, *, button="left", delivery_mode="background", window_id=None):
        from harness.computer.drivers.base import InputResult

        self.drags.append((x1, y1, x2, y2))
        return InputResult(delivered=True, delivery_mode=delivery_mode)


def _mk(tmp_path: Path, *, verification: bool = True, fake=None):
    cfg = ComputerConfig(
        verification_enabled=verification, recovery_enabled=True,
        snapshots_dir=tmp_path, screenshots_dir=tmp_path / "shots",
        recordings_dir=tmp_path / "rec",
    )
    driver = fake or _FakeDriver()
    policy = default_policy()
    for perm in ("click", "type", "key", "set_value", "drag", "foreground",
                 "launch_app", "close_app", "clipboard_read", "clipboard_write",
                 "window_control"):
        policy.permissions[perm] = ALLOW
    session = ComputerSession(
        session_id="t", driver=driver, ax=NullAXProvider(), ocr=NullOCREngine(),
        vision_router=VisionRouter(local=UnavailableVision()), config=cfg,
        policy=policy, risk_mode="read_only",
        security=SecurityLayer(),
    )
    return session, driver, ComputerCore(session, config=cfg)


def test_schema_keeps_drag_coordinates_and_new_actions():
    model = _args_model("computer_use", INPUT_SCHEMAS["computer_use"])
    args = model(**{
        "action": "drag", "from_coordinates": [1, 2], "to_coordinates": [30, 40],
        "allow_coordinates": True, "confirm_coordinates": True,
    })
    dumped = args.model_dump()
    assert dumped["from_coordinates"] == [1, 2]
    assert dumped["to_coordinates"] == [30, 40]
    assert dumped["allow_coordinates"] is True
    assert dumped["confirm_coordinates"] is True
    for action in ("invoke", "press_and_hold", "wait_for", "open", "close",
                   "clipboard_get", "clipboard_set", "scroll_into_view",
                   "note", "done", "fail"):
        assert action in INPUT_SCHEMAS["computer_use"]["properties"]["action"]["enum"]


def test_annotate_som_draws_marks():
    from PIL import Image
    from harness.computer.screenshots.annotate import annotate_som

    image = Image.new("RGB", (200, 120), (10, 10, 10))
    element = SnapshotElement(ref="1", role="button", name="Save",
                              bounds=Bounds(20, 20, 40, 20), identity="x")
    out = annotate_som(image, [element])
    assert out is not image
    assert out.size == image.size
    # A mark exists somewhere near the element (outline/badge), so some pixels differ.
    assert list(out.tobytes()) != list(image.tobytes())
    # No elements: unchanged copy.
    same = annotate_som(image, [])
    assert list(same.tobytes()) == list(image.tobytes())


@pytest.mark.asyncio
async def test_observation_persists_an_annotated_frame_and_media(tmp_path):
    from harness.computer.tools import _snapshot_media

    session, _driver, _core = _mk(tmp_path)
    snap = await session.observe(mode="som")
    assert snap.image_path and Path(snap.image_path).exists()
    media = _snapshot_media(snap)
    assert media is not None
    assert media["kind"] == "image"
    assert media["dataUrl"].startswith("data:image/")
    assert "screenshot" in media["caption"].lower()


def test_grounding_reply_scales_boxes_and_points():
    # A 1000x1000 grid into a 2000x1000 image: x doubles, y stays.
    boxes = _parse_reply("<|box_start|>(100,200),(300,400)<|box_end|>",
                         space=(1000, 1000), image_size=(2000, 1000))
    assert boxes and boxes[0]["bbox"] == [200, 200, 400, 200]
    points = _parse_reply("<|point_start|>(500,500)", space=(1000, 1000), image_size=(1000, 500))
    assert points and abs(points[0]["bbox"][0] - 494) <= 1
    assert _parse_reply("no coordinates here", space=(1000, 1000), image_size=(10, 10)) == []


def test_grounding_backend_factory_is_honest():
    cfg = ComputerConfig()
    assert build_grounding_backend(cfg) is None
    cfg.grounding_url = "http://127.0.0.1:9/v1/chat/completions"
    cfg.grounding_model = "ui-tars"
    backend = build_grounding_backend(cfg)
    assert isinstance(backend, UiTarsBackend) and backend.available
    assert backend.coordinate_space == (1000, 1000)


@pytest.mark.asyncio
async def test_vision_ground_maps_image_to_screen(tmp_path):
    from PIL import Image

    session, _driver, _core = _mk(tmp_path)
    snap = await session.observe(mode="som")
    # Pretend the captured window starts at (100, 50) so the mapping is visible.
    snap.bounds = Bounds(100, 50, 800, 600)
    Image.new("RGB", (800, 600), (0, 0, 0)).save(snap.image_path)

    class _Stub:
        available = True

        async def locate(self, target, image):
            return [{"bbox": [80, 30, 40, 20], "label": target, "confidence": 0.9}]

    session._grounding_backend = _Stub()
    found = await session.vision_ground("Continue")
    assert found == [{
        "name": "Continue", "text": "Continue", "role": "element",
        "bounds": [180, 80, 40, 20], "confidence": 0.9,
    }]


@pytest.mark.asyncio
async def test_drag_by_coordinates_works(tmp_path):
    session, driver, core = _mk(tmp_path, fake=_DragDriver())
    await session.observe()
    result = await core.act(
        "drag", from_coordinates=[10, 10], to_coordinates=[200, 200], capture_after=False,
    )
    assert result.success, result.summary
    assert driver.drags == [(10, 10, 200, 200)]


@pytest.mark.asyncio
async def test_loop_guard_blocks_the_third_identical_action(tmp_path):
    session, _driver, core = _mk(tmp_path, verification=False)
    snap = await session.observe()
    ref = snap.elements[0].ref
    first = await core.act("click", element=ref, snapshot_id=snap.snapshot_id, capture_after=False)
    second = await core.act("click", element=ref, snapshot_id=snap.snapshot_id, capture_after=False)
    third = await core.act("click", element=ref, snapshot_id=snap.snapshot_id, capture_after=False)
    assert first.verdict in ("success", "recapture")
    assert second.verdict in ("success", "recapture")
    assert third.verdict == "replan"
    assert third.error and third.error["code"] == "COMPUTER_LOOP_SUSPECTED"


@pytest.mark.asyncio
async def test_notes_are_kept_and_reported(tmp_path):
    session, _driver, core = _mk(tmp_path)
    result = await core.act("note", text="the order ticket is already open")
    assert result.success
    assert session.notes == ["the order ticket is already open"]
    trajectory = session.trajectory_summary()
    assert trajectory["notes"] == session.notes


@pytest.mark.asyncio
async def test_expectation_ax_contains_verifies_and_fails(tmp_path):
    session, _driver, core = _mk(tmp_path, verification=True)
    snap = await session.observe()
    el = next(e for e in snap.elements if e.name == "Save")
    ok = await core.act(
        "click", element=el.ref, snapshot_id=snap.snapshot_id,
        expect={"axContains": "STATUS=CLICKED"}, capture_after=False,
    )
    assert ok.verification["status"] == "verified", ok.verification

    snap2 = await session.observe()
    el2 = next(e for e in snap2.elements if e.name == "Save")
    bad = await core.act(
        "click", element=el2.ref, snapshot_id=snap2.snapshot_id,
        expect={"axContains": "NEVER-PRESENT"}, capture_after=False,
    )
    assert bad.verification["status"] == "failed"
    # The bounded recovery ladder actually ran (reobserve -> retry).
    assert bad.recovery is not None
    assert bad.recovery["attempts"], bad.recovery


@pytest.mark.asyncio
async def test_clipboard_is_honest_without_platform_support(tmp_path):
    session, _driver, core = _mk(tmp_path)
    result = await core.act("clipboard_get")
    assert not result.success
    assert result.error and result.error["code"] == "COMPUTER_CLIPBOARD_UNAVAILABLE"


@pytest.mark.asyncio
async def test_wait_for_returns_when_text_is_present(tmp_path):
    session, _driver, core = _mk(tmp_path)
    await session.observe()
    result = await core.act("wait_for", target="STATUS=", ms=500)
    assert result.success


def test_media_history_is_bounded():
    from langchain_core.messages import HumanMessage

    from harness.code.media import model_message
    from harness.tool_turn import _prune_media_messages

    image = {"kind": "image", "mime": "image/jpeg", "name": "s.jpg", "bytes": 3,
             "dataUrl": "data:image/jpeg;base64,AAA", "caption": "Desktop screenshot of Test."}
    messages = [model_message([image]) for _ in range(12)]
    pruned = _prune_media_messages(messages, keep=8)
    assert len(pruned) == 8
    assert all(isinstance(m, HumanMessage) for m in pruned)
    # The caption is used as the intro, not the read_file text.
    first = messages[0].content[0]
    assert first["text"] == "Desktop screenshot of Test."


# ── best-of-N ────────────────────────────────────────────────────────────────


class _FakeBranchSession:
    def __init__(self, index: int) -> None:
        self.id = f"session-{index}"
        self.run_id = f"run-{index}"
        self.action_history: list[dict] = []
        self.notes: list[str] = []
        self.latest_snapshot = None
        from types import SimpleNamespace

        self.state = SimpleNamespace(terminal=False)


class _FakeBranchManager:
    def __init__(self, *, isolated: bool = False) -> None:
        from types import SimpleNamespace

        self.provisioner = SimpleNamespace(isolated=isolated, available=isolated)
        self.created: list[_FakeBranchSession] = []
        self.destroyed: list[str] = []
        self.live = 0
        self.max_live = 0

    async def create_session(self, **_kwargs):
        session = _FakeBranchSession(len(self.created))
        self.created.append(session)
        return session

    async def destroy_session(self, session_id: str):
        self.destroyed.append(session_id)
        return {"destroyed": True, "sessionId": session_id}


def _record(session, *, done=True, verified=True):
    session.action_history.extend([
        {"action": "click", "result": "ok", "verdict": "success", "verification": "verified"},
        {"action": "done" if done else "note", "result": "ok", "verdict": "success",
         "verification": "verified" if verified else "uncertain"},
    ])


@pytest.mark.asyncio
async def test_best_of_prefers_a_verified_success_and_destroys_losers():
    from harness.computer.best_of import run_best_of

    manager = _FakeBranchManager()

    async def branch(session, index, task, context):
        if index == 0:
            _record(session, done=False)
            return {"success": False, "verified": False, "summary": "gave up"}
        _record(session, done=True, verified=True)
        return {"success": True, "verified": True, "summary": "done"}

    outcome = await run_best_of(manager=manager, task="do it", run_branch=branch, attempts=2)
    assert outcome.chosen == 1
    assert outcome.judged_by == "deterministic"
    assert len(outcome.branches) == 2
    assert outcome.branches[1].verified
    assert len(manager.destroyed) == 2


@pytest.mark.asyncio
async def test_best_of_runs_branches_concurrently_only_when_isolated():
    import asyncio
    from harness.computer.best_of import run_best_of

    manager = _FakeBranchManager(isolated=True)

    async def branch(session, index, task, context):
        manager.live += 1
        manager.max_live = max(manager.max_live, manager.live)
        await asyncio.sleep(0.01)
        manager.live -= 1
        _record(session)
        return {"success": True, "verified": True, "summary": "done"}

    outcome = await run_best_of(manager=manager, task="do it", run_branch=branch, attempts=3)
    assert outcome.isolated is True
    assert manager.max_live == 3
    assert outcome.chosen is not None

    serial = _FakeBranchManager(isolated=False)
    serial.max_live = 0
    serial.live = 0

    async def serial_branch(session, index, task, context):
        serial.live += 1
        serial.max_live = max(serial.max_live, serial.live)
        await asyncio.sleep(0.01)
        serial.live -= 1
        _record(session)
        return {"success": True, "verified": True, "summary": "done"}

    await run_best_of(manager=serial, task="do it", run_branch=serial_branch, attempts=3)
    assert serial.max_live == 1


@pytest.mark.asyncio
async def test_best_of_never_picks_an_unsafe_branch():
    from harness.computer.best_of import run_best_of

    manager = _FakeBranchManager()

    async def branch(session, index, task, context):
        if index == 0:
            return {"success": True, "verified": True, "unsafe": True, "summary": "tried a wire transfer"}
        _record(session, verified=False)
        return {"success": False, "verified": False, "summary": "honest failure"}

    outcome = await run_best_of(manager=manager, task="do it", run_branch=branch, attempts=2)
    assert outcome.chosen is None
    assert "no branch succeeded" in outcome.reason


@pytest.mark.asyncio
async def test_best_of_judge_selects_among_eligible_branches():
    from harness.computer.best_of import run_best_of

    manager = _FakeBranchManager()

    async def branch(session, index, task, context):
        _record(session)
        return {"success": True, "verified": True, "summary": f"branch {index}"}

    async def judge(task, captions):
        assert len(captions) == 2
        return 1

    outcome = await run_best_of(
        manager=manager, task="do it", run_branch=branch, attempts=2, judge=judge,
    )
    assert outcome.chosen == 1
    assert outcome.judged_by == "behavior_judge"


def test_behavior_judge_prompt_and_parser():
    from harness.computer.judge import build_judge_prompt, parse_judge_choice

    prompt = build_judge_prompt("save the file", ["Attempt one", "Attempt two"])
    assert "Attempt 1" in prompt and "Attempt 2" in prompt
    assert "CHOICE: <number>" in prompt
    assert parse_judge_choice("CHOICE: 2", 2) == 1
    assert parse_judge_choice("choice: 1", 2) == 0
    assert parse_judge_choice("I pick 2", 2) == 1
    assert parse_judge_choice("CHOICE: 0", 2) is None
    assert parse_judge_choice("CHOICE: 9", 2) is None
    assert parse_judge_choice("no idea", 2) is None


@pytest.mark.asyncio
async def test_judge_with_a_model_completion():
    from harness.computer.judge import judge_with_model

    async def complete(prompt):
        assert "Attempt 2" in prompt
        return "CHOICE: 2"

    assert await judge_with_model("t", ["a", "b"], complete=complete) == 1

    def sync_complete(prompt):
        return "CHOICE: 1"

    assert await judge_with_model("t", ["a", "b"], complete=sync_complete) == 0


@pytest.mark.asyncio
async def test_a_broken_judge_falls_back_to_the_deterministic_pick():
    from harness.computer.best_of import run_best_of

    manager = _FakeBranchManager()

    async def branch(session, index, task, context):
        _record(session)
        return {"success": True, "verified": True, "summary": "done"}

    async def broken_judge(task, captions):
        raise RuntimeError("model offline")

    outcome = await run_best_of(
        manager=manager, task="do it", run_branch=branch, attempts=2, judge=broken_judge,
    )
    assert outcome.chosen is not None
    assert outcome.judged_by == "deterministic"


def test_no_model_judge_without_settings():
    from harness.computer.judge import build_behavior_judge

    assert build_behavior_judge(None) is None



