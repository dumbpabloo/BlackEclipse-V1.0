"""The terminal HTTP router — transport seam + a real end-to-end round trip."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.persistence import AgentMemoryStore
from harness.terminal.manager import TerminalManager
from harness.terminal.store import TerminalStore


def _echo(msg: str) -> str:
    return f"Write-Output '{msg}'" if os.name == "nt" else f"printf '{msg}\\n'"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(project))
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    manager = TerminalManager(store=TerminalStore(store))

    from harness.routers import terminal as terminal_router

    class _Stub:
        terminal = manager

    monkeypatch.setattr(terminal_router, "service", _Stub(), raising=False)
    application = FastAPI()
    application.include_router(terminal_router.router)
    yield application, manager
    manager.close_all()
    store.close()


def test_capabilities_and_ports_are_reachable(app):
    application, _ = app
    with TestClient(application) as client:
        caps = client.get("/terminal/capabilities").json()
        assert caps["success"] and "local" in caps["capabilities"]["runtimes"]
        ports = client.get("/terminal/ports").json()
        assert ports["success"] and isinstance(ports["ports"], list)
        system = client.get("/terminal/system").json()
        assert system["success"] and system["platform"]


def test_execute_round_trip(app):
    application, manager = app
    with TestClient(application) as client:
        res = client.post("/terminal/execute", json={"command": _echo("api_ok"), "timeoutMs": 30_000})
        assert res.status_code == 200, res.text
        result = res.json()["result"]
        assert result["status"] == "ok"
        assert "api_ok" in result["stdout"]
        assert manager._store.list_commands()


def test_session_lifecycle_over_http(app):
    application, _ = app
    with TestClient(application) as client:
        opened = client.post("/terminal/sessions", json={}).json()["session"]
        sid = opened["sessionId"]
        assert client.get(f"/terminal/sessions/{sid}").status_code == 200
        ran = client.post(f"/terminal/sessions/{sid}/run", json={"command": _echo("http_shell")})
        assert ran.status_code == 200
        listed = client.get("/terminal/sessions").json()["sessions"]
        assert any(s["sessionId"] == sid for s in listed)
        assert client.delete(f"/terminal/sessions/{sid}").status_code == 200
        assert client.get(f"/terminal/sessions/{sid}").status_code == 404


def test_process_lifecycle_over_http(app):
    application, _ = app
    with TestClient(application) as client:
        started = client.post("/terminal/processes", json={"command": _echo("http_bg")}).json()["process"]
        pid = started["processId"]
        # Wait for exit.
        for _ in range(100):
            info = client.get(f"/terminal/processes/{pid}").json()["process"]
            if info["state"] in ("EXITED", "CRASHED", "FAILED"):
                break
            time.sleep(0.05)
        out = client.get(f"/terminal/processes/{pid}/output", params={"tail": 20}).json()["output"]
        assert "http_bg" in out["text"]
        tree = client.get(f"/terminal/processes/{pid}/tree").json()["tree"]
        assert tree["processId"] == pid


def test_missing_resources_map_to_404(app):
    application, _ = app
    with TestClient(application) as client:
        assert client.get("/terminal/processes/nope").status_code == 404
        assert client.get("/terminal/sessions/nope").status_code == 404
        assert client.post("/terminal/processes/nope/kill").status_code == 404


def test_unknown_runtime_is_refused_on_session_open(app):
    application, _ = app
    with TestClient(application) as client:
        res = client.post("/terminal/sessions", json={"runtime": "docker"})
        assert res.status_code == 503


def test_ui_owner_reattaches_to_the_same_session(app):
    application, _ = app
    with TestClient(application) as client:
        first = client.post("/terminal/sessions", json={"owner": "term-ui-1"}).json()["session"]
        second = client.post("/terminal/sessions", json={"owner": "term-ui-1"}).json()["session"]
        assert first["sessionId"] == second["sessionId"]
        # A different owner is a different shell.
        other = client.post("/terminal/sessions", json={"owner": "term-ui-2"}).json()["session"]
        assert other["sessionId"] != first["sessionId"]


def test_raw_input_and_stream_round_trip(app):
    application, manager = app
    with TestClient(application) as client:
        opened = client.post("/terminal/sessions", json={"owner": "term-ui-raw"}).json()["session"]
        sid = opened["sessionId"]
        # Wait for the shell prompt, then send raw keystrokes like xterm.onData.
        # Full-suite load can make shell startup slow; the deadline is generous so
        # this remains a round-trip test, not a shell-startup race.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ">" in manager.read_shell_output(sid, tail=50).get("text", "") or opened["pty"] is False:
                break
            time.sleep(0.2)
        res = client.post(f"/terminal/sessions/{sid}/raw", json={"data": "echo raw_ok\r"})
        assert res.status_code == 200
        got = ""
        cursor = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "raw_ok" not in got:
            body = client.get(f"/terminal/sessions/{sid}/stream", params={"since": cursor, "timeout": 2}).json()
            cursor = body["nextSequence"]
            got += body["text"]
        assert "raw_ok" in got, got
        assert body["running"] is True


def test_resources_route_reports_limits(app):
    application, _ = app
    with TestClient(application) as client:
        body = client.get("/terminal/resources").json()
        assert body["success"] is True
        assert "limits" in body and "maxProcesses" in body["limits"]
        assert body["system"]["cpuCount"] >= 1


def test_unavailable_runtime_is_refused_on_execute(app):
    application, _ = app
    with TestClient(application) as client:
        res = client.post("/terminal/execute", json={"command": "echo x", "runtime": "docker"})
        assert res.status_code == 200
        assert res.json()["result"]["status"] == "unavailable"
