"""Evaluation engine + OSWorld adapter tests, and a measured local pass@1.

The OSWorld tests use a fake environment (no VM): they prove the adapter maps
the driver contract onto `env.step`/`env.controller` and that the score comes
from the environment, not the agent. The live suite at the bottom runs six real
tasks against an owned Win32 window with the real Windows driver and reports a
pass@1 number.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest

from harness.computer.eval import EvalTask, run_suite


class _FakeController:
    def __init__(self) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (120, 60), (5, 5, 5)).save(buffer, format="PNG")
        self._png = buffer.getvalue()

    def get_vm_screen_size(self):
        return {"width": 120, "height": 60}

    def get_screenshot(self):
        return self._png

    def get_open_windows(self):
        return [{"title": "Fake OSWorld", "rect": {"x": 0, "y": 0, "width": 120, "height": 60}}]


class _FakeEnv:
    def __init__(self) -> None:
        self.controller = _FakeController()
        self.codes: list[str] = []
        self.evaluated = 0

    def step(self, code, pause=0.0):
        self.codes.append(code)
        return {"ok": True}

    def evaluate(self):
        self.evaluated += 1
        return 1.0


def test_osworld_driver_maps_every_input_to_pyautogui():
    from harness.computer.drivers.osworld import OSWorldDriver

    env = _FakeEnv()
    driver = OSWorldDriver(env)

    async def run():
        frame = await driver.capture()
        assert frame.width == 120 and frame.height == 60
        assert frame.method == "osworld"
        assert frame.png_bytes
        await driver.click(10, 20, count=2)
        await driver.type_text("hello")
        await driver.key(["ctrl", "s"])
        await driver.key(["enter"])
        await driver.scroll(dy=-3, at=(5, 5))
        await driver.drag(1, 2, 30, 40)
        await driver.key_down("shift")
        await driver.key_up("shift")
        return driver

    driver = asyncio.run(run())
    joined = "\n".join(env.codes)
    assert "pyautogui.click(10, 20" in joined
    assert "pyautogui.write('hello'" in joined
    assert "pyautogui.hotkey('ctrl', 's')" in joined
    assert "pyautogui.press('enter')" in joined
    assert "pyautogui.dragTo(30, 40" in joined
    assert "pyautogui.keyDown('shift')" in joined
    assert driver.list_displays()[0].bounds.width == 120
    assert driver.list_windows()[0].title == "Fake OSWorld"
    assert driver.capabilities().screen_capture is True
    assert driver.accessibility_tree() is None


@pytest.mark.asyncio
async def test_osworld_episode_scores_from_the_environment(tmp_path):
    from harness.computer.config import ComputerConfig
    from harness.computer.eval.osworld import run_osworld_episode

    env = _FakeEnv()
    cfg = ComputerConfig(
        screenshots_dir=tmp_path / "shots", snapshots_dir=tmp_path,
        recordings_dir=tmp_path / "rec",
    )

    async def agent(session, prompt):
        assert prompt == "do the thing"
        return "I claim success"

    result = await run_osworld_episode(
        env=env, prompt="do the thing", run_agent=agent, config=cfg,
    )
    assert env.evaluated == 1
    assert result["success"] is True
    assert result["score"] == 1.0


@pytest.mark.asyncio
async def test_a_crashing_agent_does_not_fake_a_score(tmp_path):
    from harness.computer.config import ComputerConfig
    from harness.computer.eval.osworld import run_osworld_episode

    env = _FakeEnv()
    cfg = ComputerConfig(
        screenshots_dir=tmp_path / "shots", snapshots_dir=tmp_path,
        recordings_dir=tmp_path / "rec",
    )

    async def agent(session, prompt):
        raise RuntimeError("model unavailable")

    result = await run_osworld_episode(env=env, prompt="p", run_agent=agent, config=cfg)
    assert result["agentError"].startswith("RuntimeError")
    assert result["agentCrashed"] is True
    # The environment still scored the final state, but a crashed agent can never
    # be reported as a successful episode.
    assert result["score"] == 1.0
    assert result["success"] is False


@pytest.mark.asyncio
async def test_a_clean_agent_with_an_environment_pass_is_success(tmp_path):
    from harness.computer.config import ComputerConfig
    from harness.computer.eval.osworld import run_osworld_episode

    env = _FakeEnv()
    cfg = ComputerConfig(
        screenshots_dir=tmp_path / "shots", snapshots_dir=tmp_path,
        recordings_dir=tmp_path / "rec",
    )

    async def agent(session, prompt):
        return "done"

    result = await run_osworld_episode(env=env, prompt="p", run_agent=agent, config=cfg)
    assert result["agentCrashed"] is False
    assert result["success"] is True
    assert result["score"] == 1.0


@pytest.mark.asyncio
async def test_a_clean_agent_with_a_zero_score_is_not_success(tmp_path):
    from harness.computer.config import ComputerConfig
    from harness.computer.eval.osworld import run_osworld_episode

    env = _FakeEnv()
    env.evaluate = lambda: 0.0  # type: ignore[assignment]
    cfg = ComputerConfig(
        screenshots_dir=tmp_path / "shots", snapshots_dir=tmp_path,
        recordings_dir=tmp_path / "rec",
    )

    async def agent(session, prompt):
        return "done"

    result = await run_osworld_episode(env=env, prompt="p", run_agent=agent, config=cfg)
    assert result["agentCrashed"] is False
    assert result["success"] is False
    assert result["score"] == 0.0



@pytest.mark.asyncio
async def test_suite_runner_scores_pass_at_1():
    async def ok():
        return True

    async def bad():
        return False

    async def boom():
        raise RuntimeError("nope")

    report = await run_suite([
        EvalTask("ok", ok), EvalTask("bad", bad), EvalTask("boom", boom),
    ], suite="unit")
    assert report.total == 3 and report.passed == 1
    assert report.pass_at_1 == pytest.approx(1 / 3)
    assert "pass@1 = 1/3" in report.report()


@pytest.mark.asyncio
async def test_branch_runner_is_honest_without_model_settings():
    from types import SimpleNamespace

    from harness.computer.runner import install_branch_runner

    service = SimpleNamespace(
        computer=SimpleNamespace(branch_runner=None),
        registry=None, store=None, bus=None,
        server_bridge=lambda: None, _run_kwargs=lambda: {},
    )
    install_branch_runner(service)
    assert callable(service.computer.branch_runner)
    fake_session = SimpleNamespace(run_id="r1", id="s1", action_history=[])
    result = await service.computer.branch_runner(fake_session, 0, "task", {})
    assert result["success"] is False
    assert "model settings" in result["error"]


# ── the live local suite (real Windows driver, owned window) ─────────────────

def _ui_automation_available() -> bool:
    try:
        from concurrent.futures import ThreadPoolExecutor

        def probe():
            import uiautomation as automation  # type: ignore[import-not-found]

            root = automation.GetRootControl()
            root.GetChildren()
            return True

        with ThreadPoolExecutor(max_workers=1) as pool:
            return bool(pool.submit(probe).result(timeout=30))
    except Exception:  # noqa: BLE001
        return False


LIVE = pytest.mark.skipif(not _ui_automation_available(), reason="UI Automation unavailable")


@pytest.mark.asyncio
@LIVE
async def test_local_windows_suite_publishes_a_pass_at_1(tmp_path):
    from tests.test_computer_demos import TARGET, _manager

    from harness.computer.eval import LocalWindowsSuite, run_suite

    manager = await _manager()
    suite = LocalWindowsSuite(manager, TARGET, tmp_path)
    try:
        await suite.launch()
        report = await run_suite(suite.tasks(), suite="windows-core")
        print("\n" + report.report())
        assert report.pass_at_1 >= 0.75, report.report()
    finally:
        await suite.close()
        await manager.stop()
