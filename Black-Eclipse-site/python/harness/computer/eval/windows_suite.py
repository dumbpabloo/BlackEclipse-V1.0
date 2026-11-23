"""The local Windows capability suite (real driver, owned window, no VM).

This is the reproducible measurement path when an OSWorld host is not available:
a controlled Win32 window (buttons, an edit, a combo, a label) is driven by the
REAL Windows driver, and each task has a deterministic checker. It measures the
ENGINE (observation, grounding, input, verification, memory, redaction hooks),
not model intelligence — label it that way wherever the number is published.

The task list is shared by the pytest live test and `scripts/cua_benchmark.py`,
so the two can never drift.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..core import ComputerCore
from ..policy import ALLOW, default_policy
from .suite import EvalTask


@dataclass
class LaunchedTarget:
    proc: subprocess.Popen
    hwnd: int

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def permissive_policy() -> Any:
    """The policy the suite's controlled window needs (all local, no money)."""
    policy = default_policy()
    for permission in ("observe", "click", "type", "key", "set_value", "foreground",
                       "window_control", "drag", "scroll", "clipboard_read", "clipboard_write"):
        policy.permissions[permission] = ALLOW
    return policy


async def launch_target(script: Path, workdir: Path, *, name: str = "bench") -> LaunchedTarget:
    pidfile = workdir / f"{name}.pid"
    try:
        pidfile.unlink()
    except OSError:
        pass
    proc = subprocess.Popen(
        [sys.executable, str(script), str(pidfile)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "CUA_TARGET_LOG": str(workdir / f"{name}.log")},
    )
    hwnd = 0
    for _ in range(80):
        await asyncio.sleep(0.1)
        if pidfile.exists():
            try:
                _, handle = pidfile.read_text(encoding="utf-8").split()
                hwnd = int(handle)
                break
            except (ValueError, OSError):
                pass
    if not hwnd:
        proc.terminate()
        raise RuntimeError("the controlled Win32 target failed to start")
    return LaunchedTarget(proc, hwnd)


class LocalWindowsSuite:
    """Owns the target window, a fresh session per task, and the task list."""

    def __init__(self, manager: Any, target_script: Path, workdir: Path) -> None:
        self.manager = manager
        self.target_script = Path(target_script)
        self.workdir = Path(workdir)
        self.target: Optional[LaunchedTarget] = None
        self.session: Any = None
        self.core: Any = None

    async def launch(self) -> None:
        self.target = await launch_target(self.target_script, self.workdir)
        # The suite's window must own the foreground for deterministic keyboard
        # input (a posted click does not give a control real keyboard focus).
        try:
            await self.manager.driver.focus_window(str(self.target.hwnd), foreground=True)
        except Exception:  # noqa: BLE001 - the benchmark still runs without focus
            pass

    async def fresh(self) -> tuple[Any, ComputerCore]:
        if self.session is not None:
            await self.manager.destroy_session(self.session.id)
            self.session = None
        if self.target is None:
            await self.launch()
        self.session = await self.manager.create_session(policy=permissive_policy())
        self.session._config.memory_min_actions = 1  # noqa: SLF001 - deterministic suite
        self.core = ComputerCore(self.session, config=self.session._config)
        # Every task starts with the owned window actually active: the suite
        # measures capabilities, not who happened to own the foreground.
        try:
            await self.core.act("focus_app", window_id=str(self.target.hwnd))
        except Exception:  # noqa: BLE001 - the task can still use UIA
            pass
        await self.session.observe(window_id=str(self.target.hwnd), mode="som")
        return self.session, self.core

    async def close(self) -> None:
        if self.session is not None:
            try:
                await self.manager.destroy_session(self.session.id)
            except Exception:  # noqa: BLE001
                pass
            self.session = None
        if self.target is not None:
            self.target.close()
            self.target = None

    # ── tasks ────────────────────────────────────────────────────────────────

    def tasks(self) -> list[EvalTask]:
        return [
            EvalTask("locate-and-read", self._locate, ["perception"]),
            EvalTask("click-and-verify", self._click, ["input", "verification"]),
            EvalTask("invoke-pattern", self._invoke, ["input", "pattern"]),
            EvalTask("set-value", self._set_value, ["input", "verification"]),
            EvalTask("windows-listing", self._list_windows, ["perception"]),
            EvalTask("wait-for", self._wait_for, ["wait"]),
            EvalTask("clipboard", self._clipboard, ["input"]),
            EvalTask("drag", self._drag, ["input"]),
            EvalTask("press-and-hold", self._press_and_hold, ["input"]),
            EvalTask("scroll-into-view", self._scroll_into_view, ["input"]),
            EvalTask("region-capture", self._region_capture, ["perception"]),
            EvalTask("learned-interface", self._learned_interface, ["memory"]),
        ]

    async def _locate(self) -> bool:
        session, _core = await self.fresh()
        names = {e.name for e in session.latest_snapshot.elements}
        return {"Save", "Save As", "Cancel"}.issubset(names)

    async def _click(self) -> bool:
        session, core = await self.fresh()
        await core.act("focus_app", window_id=str(self.target.hwnd))
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.name == "Save"), None)
        if element is None:
            return False
        result = await core.act("click", element=element.ref, snapshot_id=snap.snapshot_id,
                                capture_after=False)
        if not result.success:
            return False
        fresh = await core.observe(window_id=str(self.target.hwnd), mode="ax")
        return "STATUS=SAVE" in fresh.text

    async def _invoke(self) -> bool:
        session, core = await self.fresh()
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.name == "Cancel"), None)
        if element is None:
            return False
        result = await core.act("invoke", element=element.ref, snapshot_id=snap.snapshot_id,
                                capture_after=False)
        if not result.success:
            return False
        fresh = await core.observe(window_id=str(self.target.hwnd), mode="ax")
        return "STATUS=CANCEL" in fresh.text

    async def _set_value(self) -> bool:
        session, core = await self.fresh()
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.role in ("textbox", "edit")), None)
        if element is None:
            return False
        await core.act("set_value", element=element.ref, snapshot_id=snap.snapshot_id,
                       value="AAPL", capture_after=False)
        fresh = await core.observe(window_id=str(self.target.hwnd), mode="ax")
        return "AAPL" in fresh.text

    async def _list_windows(self) -> bool:
        _session, core = await self.fresh()
        result = await core.act("list_windows")
        windows = (result.evidence[0] or {}).get("items") if result.evidence else []
        return bool(result.success and windows)

    async def _wait_for(self) -> bool:
        session, core = await self.fresh()
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.name == "Save As"), None)
        if element is None:
            return False
        await core.act("click", element=element.ref, snapshot_id=snap.snapshot_id,
                       capture_after=False)
        result = await core.act("wait_for", target="STATUS=SAVEAS", ms=3000)
        return bool(result.success)

    async def _clipboard(self) -> bool:
        ok = await self.manager.driver.set_clipboard("eclipse-bench")
        return bool(ok) and await self.manager.driver.get_clipboard() == "eclipse-bench"

    async def _drag(self) -> bool:
        _session, core = await self.fresh()
        result = await core.act("drag", from_coordinates=[500, 300],
                                to_coordinates=[600, 350], capture_after=False)
        return bool(result.success)

    async def _press_and_hold(self) -> bool:
        _session, core = await self.fresh()
        result = await core.act("press_and_hold", coordinates=[500, 300], ms=120,
                                capture_after=False)
        return bool(result.success)

    async def _scroll_into_view(self) -> bool:
        session, core = await self.fresh()
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.name == "Save"), None)
        if element is None:
            return False
        result = await core.act("scroll_into_view", element=element.ref,
                                snapshot_id=snap.snapshot_id, capture_after=False)
        return bool(result.success)

    async def _region_capture(self) -> bool:
        session, _core = await self.fresh()
        snap = await session.observe(region=[0, 0, 120, 80], mode="vision")
        if snap.bounds is None or snap.bounds.width != 120 or snap.bounds.height != 80:
            return False
        try:
            from PIL import Image  # noqa: PLC0415

            return Image.open(snap.image_path).size == (120, 80)
        except Exception:  # noqa: BLE001
            return False

    async def _learned_interface(self) -> bool:
        session, core = await self.fresh()
        from ..applications.learned import LearnedAdapterBook  # noqa: PLC0415

        session.learned_adapters = LearnedAdapterBook(self.workdir / "learned.json")
        snap = session.latest_snapshot
        element = next((e for e in snap.elements if e.name == "Save"), None)
        if element is None:
            return False
        await core.act("click", element=element.ref, snapshot_id=snap.snapshot_id,
                       capture_after=False)
        await core.act("hotkey", combo="ctrl+s", capture_after=False)
        result = await core.act("done", text="save the file")
        learned = (result.workflow or {}).get("adapter")
        if not learned or not learned.get("validated"):
            return False
        selected = session.adapter_for(app_name=snap.application, title=snap.window_title)
        return str(selected.id).startswith("learned-")


__all__ = ["LaunchedTarget", "LocalWindowsSuite", "launch_target", "permissive_policy"]
