"""Run supervision: leases survive transient locks and lost runs settle honestly."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

from harness.persistence import AgentMemoryStore, DB_FILENAME
from harness.run_lease import RunLeaseManager, _manager_for
from harness.run_supervision import (
    guard_background_task,
    settle_lost_run,
    settle_unfinished_run,
)


@pytest.fixture()
def store():
    directory = tempfile.mkdtemp(prefix="be-supervision-py-")
    s = AgentMemoryStore(os.path.join(directory, DB_FILENAME))
    yield s
    s.close()


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.ended: list[str] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)

    def end_stream(self, run_id: str) -> None:
        self.ended.append(run_id)


def _manager(store, runtime_id: str = "rt-a") -> RunLeaseManager:
    return RunLeaseManager(store, runtime_id=runtime_id, environment="prod", ttl_ms=60_000)


def _claim(store, run_id: str = "run_sup", runtime_id: str = "rt-a") -> RunLeaseManager:
    store.create_run(run_id, "eclipse-agent", {"query": "x"})
    store.update_run(run_id, status="running")
    manager = _manager(store, runtime_id)
    assert manager.claim(run_id) == 1
    return manager


def test_one_manager_is_reused_for_the_same_store(store):
    # RunLeaseGuard is built per run; rebuilding the manager re-ran the schema
    # DDL and rewrote RuntimeInstance on every chat start.
    first = _manager_for(store)
    second = _manager_for(store)
    assert first is second


def test_a_transient_heartbeat_failure_does_not_lose_the_lease(store, monkeypatch):
    manager = _claim(store)

    def _locked(_self):  # noqa: ANN001
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RunLeaseManager, "_db", property(_locked))
    # "error" (not "fenced"): the heartbeat thread must keep retrying. The old
    # code returned False for any exception and permanently orphaned the run.
    assert manager.heartbeat_result("run_sup", 1) == "error"
    assert manager.heartbeat("run_sup", 1) is True


def test_heartbeat_reports_fenced_for_a_foreign_owner(store):
    _claim(store, runtime_id="rt-a")
    other = _manager(store, "rt-b")
    assert other.heartbeat_result("run_sup", 1) == "fenced"
    assert other.heartbeat("run_sup", 1) is False


def test_settle_writes_terminal_when_no_live_owner(store):
    manager = _claim(store)
    manager.release("run_sup", 1)
    assert settle_unfinished_run(
        store, "run_sup", status="failed",
        code="RUN_ORPHANED", message="runtime replaced",
    ) is True
    run = store.get_run("run_sup")
    assert run["status"] == "failed"
    assert run["error"]["code"] == "RUN_ORPHANED"
    assert run["finishedAt"]


def test_settle_never_overwrites_another_live_runtime(store):
    _claim(store, runtime_id="rt-a")
    assert settle_unfinished_run(
        store, "run_sup", status="failed",
        code="RUN_LEASE_LOST", message="stale worker", runtime_id="rt-b",
    ) is False
    assert store.get_run("run_sup")["status"] == "running"


def test_settle_never_overwrites_an_already_terminal_row(store):
    manager = _claim(store)
    manager.release("run_sup", 1)
    store.update_run("run_sup", status="completed", output="done")
    assert settle_unfinished_run(
        store, "run_sup", status="failed", code="RUN_CRASHED", message="late",
    ) is False
    assert store.get_run("run_sup")["status"] == "completed"


@pytest.mark.asyncio
async def test_guard_settles_a_crashed_background_task(store):
    bus = _Bus()
    store.create_run("run_crash", "eclipse-agent", {"query": "x"})
    store.update_run("run_crash", status="running")

    async def _work() -> None:
        raise RuntimeError("boom")

    await guard_background_task(_work(), store, bus, "run_crash", runtime_id="rt-a")
    run = store.get_run("run_crash")
    assert run["status"] == "failed"
    assert run["error"]["code"] == "RUN_CRASHED"
    assert bus.ended == ["run_crash"]
    # A durable terminal event is written so a reconnecting stream closes.
    types = [e["type"] for e in bus.events]
    assert "run.failed" in types


@pytest.mark.asyncio
async def test_guard_settles_a_cancelled_background_task(store):
    bus = _Bus()
    store.create_run("run_cancel", "eclipse-agent", {"query": "x"})
    store.update_run("run_cancel", status="running")

    async def _work() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await guard_background_task(_work(), store, bus, "run_cancel", runtime_id="rt-a")
    assert store.get_run("run_cancel")["status"] == "cancelled"
    assert bus.ended == ["run_cancel"]


@pytest.mark.asyncio
async def test_guard_does_not_touch_a_settled_run(store):
    bus = _Bus()
    store.create_run("run_ok", "eclipse-agent", {"query": "x"})
    store.update_run("run_ok", status="completed", output="done")

    async def _work() -> None:
        return None

    await guard_background_task(_work(), store, bus, "run_ok")
    assert store.get_run("run_ok")["status"] == "completed"
    assert bus.ended == []


@pytest.mark.asyncio
async def test_settle_lost_run_emits_the_reason(store):
    bus = _Bus()
    manager = _claim(store)
    manager.release("run_sup", 1)
    from harness.events import RunEmitter

    wrote = await settle_lost_run(store, "run_sup", "rt-a", RunEmitter("run_sup", store, bus))
    assert wrote is True
    assert store.get_run("run_sup")["error"]["code"] == "RUN_LEASE_LOST"
    assert any(e["type"] == "run.failed" for e in bus.events)


def test_enforce_instance_lock_is_a_noop_without_the_launcher_flag(monkeypatch):
    # Tests and embedded TestClient uses import harness.main repeatedly; the
    # boot guard must only run for a front-door launch.
    from harness.runtime_lock import enforce_instance_lock

    monkeypatch.delenv("BE_ENFORCE_SINGLE_RUNTIME", raising=False)
    enforce_instance_lock()  # would raise SystemExit if it enforced


def test_only_one_runtime_process_can_own_a_port(tmp_path, monkeypatch):
    # Two runtimes on one DB fight for SQLite and the second boot's orphan
    # reconciliation can touch the first one's live runs. main()/import takes
    # this lock BEFORE uvicorn runs the lifespan.
    import subprocess
    import sys

    import harness
    from harness.runtime_lock import acquire_instance_lock, release_instance_lock

    monkeypatch.setenv("BE_RUNTIME_DIR", str(tmp_path))
    first = acquire_instance_lock(19_876)
    assert first is not None
    # Idempotent within one process (import + main both call it).
    assert acquire_instance_lock(19_876) is first
    try:
        # The pytest process may run from the repo root, where `python/` is not
        # on sys.path. Point the child at the directory that CONTAINS `harness`.
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(harness.__file__)))
        code = (
            "import sys\n"
            f"sys.path.insert(0, {package_root!r})\n"
            "from harness.runtime_lock import acquire_instance_lock\n"
            "print('held' if acquire_instance_lock(19876) is None else 'acquired')\n"
        )
        env = {
            **os.environ,
            "BE_RUNTIME_DIR": str(tmp_path),
            "PYTHONPATH": package_root + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60,
        )
        assert out.stdout.strip() == "held", out.stderr
    finally:
        release_instance_lock(19_876)
    again = acquire_instance_lock(19_876)
    assert again is not None
    release_instance_lock(19_876)

