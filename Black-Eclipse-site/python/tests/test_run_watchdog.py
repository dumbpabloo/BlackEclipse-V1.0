"""Run stall watchdog: no run can stay non-terminal past its model budget."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import tempfile
import time

import pytest

from harness.persistence import AgentMemoryStore, DB_FILENAME
from harness.run_watchdog import (
    APPROVAL_EVENTS,
    find_stalled,
    stall_threshold_s,
    sweep_stalled_runs,
)


def _iso(epoch: float) -> str:
    stamp = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


# ── Pure detector ─────────────────────────────────────────────────────────────


def test_threshold_scales_with_the_agent_model_budget():
    # One model call may run up to the gateway ceiling (300s) even when the agent
    # declares 120s; a shorter budget killed healthy calls mid-flight.
    assert stall_threshold_s("eclipse-agent") == 360.0
    assert stall_threshold_s("code-agent") == 660.0
    assert stall_threshold_s(None) == 360.0  # default 120s, raised to the ceiling


def test_find_stalled_flags_only_silent_runs_and_exempts_approval_waits():
    now = 1_000_000.0
    rows = [
        # Healthy: recent event.
        {"id": "fresh", "agentId": "eclipse-agent", "started": _iso(now - 10), "lastEventAt": _iso(now - 5), "lastEventType": "model.requested"},
        # Stalled: silent for 400s > 180s budget.
        {"id": "old", "agentId": "eclipse-agent", "started": _iso(now - 500), "lastEventAt": _iso(now - 400), "lastEventType": "model.requested"},
        # Waiting on the user's approval decision: exempt.
        {"id": "approval", "agentId": "eclipse-agent", "started": _iso(now - 10_000), "lastEventAt": _iso(now - 10_000), "lastEventType": "approval.requested"},
        # No events at all: age measured from `started`.
        {"id": "never_started", "agentId": "eclipse-agent", "started": _iso(now - 500), "lastEventAt": None, "lastEventType": None},
        # A Code Agent run silent 400s is still healthy (600s budget + 60s).
        {"id": "code_long", "agentId": "code-agent", "started": _iso(now - 400), "lastEventAt": _iso(now - 400), "lastEventType": "model.requested"},
    ]
    stalled = find_stalled(rows, now)
    assert set(stalled) == {"old", "never_started"}
    # A `clarify` question parks the run on the same human-decision gate as an
    # approval, so both are exempt.
    assert APPROVAL_EVENTS == {
        "approval.requested", "user.approval.required", "clarification.requested",
    }


# ── DB sweep ─────────────────────────────────────────────────────────────────


class _StubBus:
    def __init__(self) -> None:
        self.ended: list[str] = []

    def end_stream(self, run_id: str) -> None:
        self.ended.append(run_id)


class _StubService:
    def __init__(self, store: AgentMemoryStore) -> None:
        self.store = store
        self.bus = _StubBus()
        self.cancelled: list[str] = []

    def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True


@pytest.fixture()
def store():
    directory = tempfile.mkdtemp(prefix="be-watchdog-py-")
    s = AgentMemoryStore(os.path.join(directory, DB_FILENAME))
    yield s
    s.close()


def _age_run(store: AgentMemoryStore, run_id: str, seconds: float) -> None:
    old = _iso(time.time() - seconds)
    db = store._db  # noqa: SLF001 - test ages the row on purpose
    db.execute(
        "UPDATE HarnessRun SET createdAt = ?, updatedAt = ?, startedAt = ? WHERE id = ?",
        (old, old, old, run_id),
    )
    db.execute("UPDATE HarnessEvent SET at = ? WHERE runId = ?", (old, run_id))


def test_sweep_fails_a_stalled_run_once_and_leaves_terminal_runs_alone(store):
    store.create_run("run_stuck", "eclipse-agent", {"query": "x"})
    store.update_run("run_stuck", status="running")
    store.append_event("run_stuck", "model.requested", 1, {"step": 1})
    _age_run(store, "run_stuck", 600)

    store.create_run("run_done", "eclipse-agent", {"query": "y"})
    store.update_run("run_done", status="completed", output="ok")
    _age_run(store, "run_done", 600)

    service = _StubService(store)
    stalled = sweep_stalled_runs(service)

    assert stalled == ["run_stuck"]
    stuck = store.get_run("run_stuck")
    assert stuck["status"] == "failed"
    assert stuck["error"]["code"] == "RUN_STALLED"
    assert stuck["finishedAt"]
    events = store.list_events("run_stuck")
    assert events[-1]["type"] == "run.failed"
    assert events[-1]["data"]["code"] == "RUN_STALLED"
    assert events[-1]["seq"] == 2
    assert service.bus.ended == ["run_stuck"]
    assert service.cancelled == ["run_stuck"]
    # Terminal runs untouched; a second sweep is a no-op.
    assert store.get_run("run_done")["status"] == "completed"
    assert sweep_stalled_runs(service) == []


def test_sweep_skips_a_run_that_is_still_emitting_transient_deltas(store):
    """Deltas are not persisted, but they prove the run is alive."""
    from harness import run_activity

    store.create_run("run_live", "eclipse-agent", {"query": "x"})
    store.update_run("run_live", status="running")
    store.append_event("run_live", "model.requested", 1, {"step": 1})
    _age_run(store, "run_live", 600)
    run_activity.touch("run_live")

    service = _StubService(store)
    try:
        assert sweep_stalled_runs(service) == []
        assert store.get_run("run_live")["status"] == "running"
    finally:
        run_activity.forget("run_live")


def test_sweep_preserves_the_last_assistant_text_as_a_partial_answer(store):
    from langchain_core.messages import AIMessage

    from harness.run_lineage import RunLineage

    store.create_run("run_partial", "eclipse-agent", {"query": "x"})
    store.update_run("run_partial", status="running")
    store.append_event("run_partial", "model.requested", 1, {"step": 1})
    RunLineage(store).save_messages("run_partial", [AIMessage(content="Here is what I found so far: 42.")])
    _age_run(store, "run_partial", 600)

    service = _StubService(store)
    assert sweep_stalled_runs(service) == ["run_partial"]

    run = store.get_run("run_partial")
    assert run["status"] == "failed"
    assert run["output"] == "Here is what I found so far: 42."
    assert run["error"]["partial"] is True
    assert run["error"]["partialChars"] == len("Here is what I found so far: 42.")
    types = [e["type"] for e in store.list_events("run_partial")]
    assert "run.partial" in types


def test_sweep_reconciles_a_completed_run_whose_status_write_was_lost(store):
    """The run finished and emitted `run.completed`; only the row write lagged.

    The old watchdog relabelled it RUN_STALLED minutes later, which is why every
    long agent run looked like a timeout failure.
    """
    store.create_run("run_lost_write", "eclipse-agent", {"query": "x"})
    store.update_run("run_lost_write", status="running", startedAt=_iso(time.time() - 600))
    store.append_event("run_lost_write", "run.completed", 1, {"steps": 12})
    # Post-run housekeeping events legitimately follow the terminal event.
    store.append_event("run_lost_write", "session.indexed", 2, {"indexed": 40})
    _age_run(store, "run_lost_write", 600)

    service = _StubService(store)
    assert sweep_stalled_runs(service) == []

    run = store.get_run("run_lost_write")
    assert run["status"] == "completed"
    assert run["error"] is None
    # No second terminal event and no cancel: the run had already finished.
    types = [e["type"] for e in store.list_events("run_lost_write")]
    assert types.count("run.completed") == 1
    assert "run.failed" not in types
    assert service.bus.ended == []
    assert service.cancelled == []
    assert sweep_stalled_runs(service) == []


def test_sweep_reconciles_a_failed_run_and_keeps_its_error(store):
    store.create_run("run_lost_fail", "eclipse-agent", {"query": "x"})
    store.update_run("run_lost_fail", status="running", startedAt=_iso(time.time() - 600))
    store.append_event(
        "run_lost_fail", "run.failed", 1,
        {"error": {"code": "PROVIDER_ERROR", "message": "boom"}},
    )
    _age_run(store, "run_lost_fail", 600)

    service = _StubService(store)
    assert sweep_stalled_runs(service) == []
    run = store.get_run("run_lost_fail")
    assert run["status"] == "failed"
    assert run["error"] == {"code": "PROVIDER_ERROR", "message": "boom"}


def test_sweep_repairs_history_where_a_completed_run_was_marked_RUN_STALLED(store):
    """Self-healing: rows already mislabelled by the old writer are corrected."""
    completed_at = _iso(time.time() - 900)
    failed_at = _iso(time.time() - 600)
    store.create_run("run_old_bug", "eclipse-agent", {"query": "x"})
    store.update_run(
        "run_old_bug", status="failed", output="short partial",
        error={"code": "RUN_STALLED", "message": "no progress"},
        startedAt=_iso(time.time() - 1200), finishedAt=failed_at,
    )
    store.append_event("run_old_bug", "run.completed", 1, {"steps": 9})
    store.append_event("run_old_bug", "run.failed", 2, {"code": "RUN_STALLED"})
    db = store._db
    db.execute("UPDATE HarnessEvent SET at=? WHERE runId=? AND type='run.completed'",
               (completed_at, "run_old_bug"))
    db.execute("UPDATE HarnessEvent SET at=? WHERE runId=? AND type='run.failed'",
               (failed_at, "run_old_bug"))
    db.commit()

    service = _StubService(store)
    assert sweep_stalled_runs(service) == []

    run = store.get_run("run_old_bug")
    assert run["status"] == "completed"
    assert run["error"] is None
    assert run["finishedAt"] == completed_at
    # A second sweep changes nothing (idempotent, and the events are untouched).
    assert sweep_stalled_runs(service) == []
    assert [e["type"] for e in store.list_events("run_old_bug")] == [
        "run.completed", "run.failed",
    ]


def test_a_terminal_event_from_a_previous_attempt_does_not_finalize_a_resume(store):
    """A resume re-marks the row running with a newer `startedAt`."""
    from harness.run_watchdog import terminal_event_of

    now = time.time()
    row = {
        "id": "run_resumed", "agentId": "eclipse-agent",
        "started": _iso(now - 60),
        "terminalType": "run.completed", "terminalAt": _iso(now - 3_600),
    }
    assert terminal_event_of(row) is None

    row["terminalAt"] = _iso(now - 30)
    assert terminal_event_of(row) == "completed"


def test_sweep_skips_a_run_with_a_live_lease(store):
    from harness.run_lease import RunLeaseManager

    store.create_run("run_leased", "eclipse-agent", {"query": "x"})
    store.update_run("run_leased", status="running")
    store.append_event("run_leased", "model.requested", 1, {"step": 1})
    _age_run(store, "run_leased", 600)
    manager = RunLeaseManager(store, runtime_id="rt-live", ttl_ms=60_000)
    assert manager.claim("run_leased") == 1

    service = _StubService(store)
    assert sweep_stalled_runs(service) == []
    assert store.get_run("run_leased")["status"] == "running"

    # Once the lease expires the run is sweepable again.
    store._db.execute("UPDATE RunLease SET expiresAtMs=0 WHERE runId=?", ("run_leased",))
    store._db.commit()
    assert sweep_stalled_runs(service) == ["run_leased"]
    assert store.get_run("run_leased")["status"] == "failed"


def test_find_stalled_accepts_a_live_set():
    now = 1_000_000.0
    rows = [
        {"id": "old", "agentId": "eclipse-agent", "started": _iso(now - 500),
         "lastEventAt": _iso(now - 400), "lastEventType": "model.requested"},
    ]
    assert find_stalled(rows, now, live_run_ids={"old"}) == []
    assert find_stalled(rows, now, live_run_ids=set()) == ["old"]


def test_sweep_never_raises_on_a_broken_store():
    class _Broken:
        class store:  # noqa: N801 - minimal stub
            _db = None

        class bus:  # noqa: N801
            def end_stream(self, run_id: str) -> None:  # noqa: ARG002
                raise AssertionError("must not be reached")

        def cancel(self, run_id: str) -> bool:  # noqa: ARG002
            raise AssertionError("must not be reached")

    assert sweep_stalled_runs(_Broken()) == []
