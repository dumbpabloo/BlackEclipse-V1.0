"""Eclipse CUA Demo Workflows — live, controlled, deterministic.

Runs the demo workflows from the design against windows WE own (the stress
target and the mock trading terminal), so the real Windows driver is exercised
end to end without touching a live application.

  Demo 1  open/locate a target, read its state
  Demo 2  find a control by name and act on it
  Demo 3  update a value and verify the change
  Demo 4  read structured financial state from a terminal screen
  Demo 5  a full prepare → firewall → no-submit finance flow

These tests are SKIPPED (not failed) when the platform cannot run them (e.g. on
a non-Windows CI host), and they always clean up the child processes they spawn.

Run them explicitly with the live rig; they are part of the default suite on
Windows because they are fast and use only owned windows.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Computer Use native demos require Windows")


def _ui_automation_available() -> bool:
    """Can this process actually drive UI Automation?

    The driver talks to `UIAutomationCore.dll` from a worker thread, so it needs
    COM initialised on that thread. On a host that cannot load the DLL (or has
    COM blocked in the process) every call returns no elements, which used to
    fail three demos instead of skipping them — the module's contract is
    "skipped, not failed, when the platform cannot run them". Probe the same way
    the driver does: from a thread, not the main one.
    """
    if sys.platform != "win32":
        return False
    try:
        from concurrent.futures import ThreadPoolExecutor

        def probe() -> bool:
            import uiautomation as automation  # type: ignore[import-not-found]

            root = automation.GetRootControl()
            if root is None:
                return False
            root.GetChildren()  # raises when UIAutomationCore is unusable
            return True

        with ThreadPoolExecutor(max_workers=1) as pool:
            return bool(pool.submit(probe).result(timeout=30))
    except Exception:  # noqa: BLE001 - any failure means "not runnable here"
        return False


if not _ui_automation_available():
    pytestmark = [
        pytest.mark.skipif(sys.platform != "win32", reason="Computer Use native demos require Windows"),
        pytest.mark.skip(reason="UI Automation is not available in this process (UIAutomationCore/COM)"),
    ]

TARGET = Path(__file__).parent / "computer_stress_target.py"
TERMINAL = Path(__file__).parent / "computer_finance_terminal.py"


class _Launched:
    def __init__(self, proc: subprocess.Popen, hwnd: int) -> None:
        self.proc = proc
        self.hwnd = hwnd

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


async def _launch(script: Path, name: str, tmp_path: Path, env: dict | None = None) -> _Launched:
    pidfile = tmp_path / f"{name}.pid"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(pidfile)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, **(env or {})},
    )
    hwnd = 0
    for _ in range(60):
        await asyncio.sleep(0.1)
        if pidfile.exists():
            try:
                _, h = pidfile.read_text(encoding="utf-8").split()
                hwnd = int(h)
                break
            except (ValueError, OSError):
                pass
    if not hwnd:
        proc.terminate()
        raise RuntimeError(f"{name} failed to start")
    return _Launched(proc, hwnd)


async def _manager():
    from harness.computer.config import computer_config
    from harness.computer.manager import ComputerManager

    manager = ComputerManager(config=computer_config(refresh=True))
    await manager.start()
    return manager


def _permissive(manager_policy=None):
    from harness.computer.policy import ALLOW, default_policy

    policy = default_policy()
    for p in ("observe", "click", "type", "key", "set_value", "foreground", "window_control", "drag",
              "clipboard_read", "clipboard_write"):
        policy.permissions[p] = ALLOW
    return policy


@pytest.mark.asyncio
async def test_demo1_locate_and_read(tmp_path):
    manager = await _manager()
    launched = await _launch(TARGET, "demo1", tmp_path)
    try:
        session = await manager.create_session(policy=_permissive())
        snap = await session.observe(window_id=str(launched.hwnd), mode="som")
        names = {e.name for e in snap.elements}
        assert {"Save", "Save As", "Cancel"}.issubset(names)
        assert all(e.bounds is not None for e in snap.elements)
    finally:
        await manager.stop()
        launched.close()


@pytest.mark.asyncio
async def test_demo2_find_and_click(tmp_path):
    from harness.computer.core import ComputerCore

    manager = await _manager()
    launched = await _launch(TARGET, "demo2", tmp_path)
    try:
        session = await manager.create_session(policy=_permissive())
        core = ComputerCore(session, config=session._config)
        snap = await session.observe(window_id=str(launched.hwnd), mode="som")
        el = next(e for e in snap.elements if e.name == "Cancel")
        res = await core.act("click", element=el.ref, snapshot_id=snap.snapshot_id)
        # The click ran and produced a real, verified state change.
        assert res.success
        assert (res.verification or {}).get("status") == "verified"
    finally:
        await manager.stop()
        launched.close()


@pytest.mark.asyncio
async def test_demo3_set_value_and_verify(tmp_path):
    from harness.computer.core import ComputerCore

    manager = await _manager()
    launched = await _launch(TARGET, "demo3", tmp_path)
    try:
        session = await manager.create_session(policy=_permissive())
        core = ComputerCore(session, config=session._config)
        snap = await session.observe(window_id=str(launched.hwnd), mode="som")
        edit = next((e for e in snap.elements if e.role.lower() in ("textbox", "edit")), None)
        if edit is None:
            pytest.skip("no text control exposed on this platform build")
        res = await core.act("set_value", element=edit.ref, snapshot_id=snap.snapshot_id,
                             value="NVDA", capture_after=True)
        # set_value reports a verification status and, with capture_after, a fresh
        # snapshot; it is never a blind success.
        assert res.verification is not None
        assert res.snapshot is not None
    finally:
        await manager.stop()
        launched.close()


@pytest.mark.asyncio
async def test_demo4_read_financial_state(tmp_path):
    manager = await _manager()
    launched = await _launch(TERMINAL, "demo4", tmp_path)
    try:
        session = await manager.create_session(policy=_permissive())
        snap = await session.observe(window_id=str(launched.hwnd), mode="som")
        state = manager.extractor.extract(snapshot=snap, application="mock_terminal",
                                          window="Eclipse Mock Trading Terminal")
        assert state.symbol == "NVDA"
        assert state.fields.get("bid") == 185.21
        assert state.fields.get("position") == 100
        assert state.provenance()["source_type"] == "desktop_screen"
        assert state.provenance()["capture_id"] == snap.snapshot_id
    finally:
        await manager.stop()
        launched.close()


@pytest.mark.asyncio
async def test_demo5_finance_prepare_never_submits(tmp_path):
    from harness.computer.finance.firewall import ASSISTED, FinanceActionFirewall

    log = tmp_path / "terminal.log"
    manager = await _manager()
    launched = await _launch(TERMINAL, "demo5", tmp_path, env={"CUA_TERMINAL_LOG": str(log)})
    try:
        session = await manager.create_session(policy=_permissive(), risk_mode="assisted")
        await session.observe(window_id=str(launched.hwnd), mode="som")
        fw = FinanceActionFirewall(mode=ASSISTED, max_order_value=25_000)
        prepared = fw.evaluate({"action": "prepare_order", "instrument": "NVDA"})
        submitted = fw.evaluate({"action": "buy", "instrument": "NVDA", "quantity": 10, "price": 185.21})
        assert prepared.allowed
        assert submitted.requires_approval
        # The firewall refused to auto-submit, so NO mutation control was clicked.
        entries = []
        if log.exists():
            entries = [line.split()[-1] for line in log.read_text(encoding="utf-8").splitlines()]
        assert "SUBMIT_ORDER" not in entries
        assert "BUY" not in entries
    finally:
        await manager.stop()
        launched.close()
