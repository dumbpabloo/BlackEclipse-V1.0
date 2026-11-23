"""C6: the executors themselves honour the configured sandbox profile."""

from __future__ import annotations

import sys

from harness.persistence import AgentMemoryStore
from harness.security import profiles
from harness.terminal.manager import TerminalManager
from harness.terminal.store import TerminalStore

LOCAL_CAPS = {
    "processIsolation": True,
    "filesystemIsolation": False,
    "networkIsolation": False,
    "secretIsolation": True,
}
FULL_CAPS = {"processIsolation": True}


class FakeSandbox:
    name = "local"

    def __init__(self, caps: dict) -> None:
        self._caps = caps

    def available(self) -> bool:
        return True

    def capabilities(self) -> dict:
        return {"backend": self.name, **self._caps}


def _manager(tmp_path) -> TerminalManager:
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    return TerminalManager(store=TerminalStore(store))


def test_foreground_execution_refuses_an_unenforceable_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACK_ECLIPSE_SANDBOX_PROFILE", "read-only")
    monkeypatch.setattr(profiles, "get_sandbox", lambda: FakeSandbox(LOCAL_CAPS))
    manager = _manager(tmp_path)
    events: list[tuple[str, dict]] = []
    manager._on_event = lambda event, data: events.append((event, data))

    result = manager.execute_command(command="echo hi", cwd=str(tmp_path))

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.type == "SANDBOX_UNAVAILABLE"
    assert "read-only" in result.error.message
    assert any(event == "sandbox.profile.blocked" for event, _ in events)
    manager.close_all()


def test_background_processes_refuse_an_unenforceable_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACK_ECLIPSE_SANDBOX_PROFILE", "workspace-write")
    monkeypatch.setattr(profiles, "get_sandbox", lambda: FakeSandbox(LOCAL_CAPS))
    manager = _manager(tmp_path)

    result = manager.processes.start_process(
        argv=[sys.executable, "-c", "print(1)"],
        cwd=str(tmp_path), env={}, command="print(1)",
    )

    assert result["state"] == "FAILED"
    assert result["error"]["type"] == "SANDBOX_UNAVAILABLE"
    assert "workspace-write" in result["error"]["message"]
    manager.close_all()


def test_an_enforceable_profile_does_not_block_the_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACK_ECLIPSE_SANDBOX_PROFILE", "full")
    monkeypatch.setattr(profiles, "get_sandbox", lambda: FakeSandbox(FULL_CAPS))
    manager = _manager(tmp_path)

    result = manager.execute_command(command="echo c6-ok", cwd=str(tmp_path))
    try:
        assert result.status == "ok", result.error
        assert "c6-ok" in result.stdout
    finally:
        manager.close_all()


def test_computer_use_refuses_an_unenforceable_profile(monkeypatch):
    import asyncio

    from harness.computer import tools as computer_tools
    from harness.tools import ToolContext

    monkeypatch.setenv("BLACK_ECLIPSE_SANDBOX_PROFILE", "read-only")
    monkeypatch.setattr(profiles, "get_sandbox", lambda: FakeSandbox(LOCAL_CAPS))
    monkeypatch.setattr(computer_tools, "_session_of", lambda args, ctx=None: (object(), None))
    monkeypatch.setattr(computer_tools, "_core", lambda session: object())

    ctx = ToolContext(run_id="r1", log=lambda *_: None)
    text, data = asyncio.run(
        computer_tools._computer_use({"session": "s", "action": "click"}, ctx)
    )
    assert data["code"] == "SANDBOX_UNAVAILABLE"
    assert "read-only" in data["reason"]
    assert text.startswith("UNAVAILABLE")
