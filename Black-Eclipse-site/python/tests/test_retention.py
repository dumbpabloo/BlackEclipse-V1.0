"""Retention: prune terminal runs' child rows, never live work or run history."""

from __future__ import annotations

import datetime as dt
import os
import tempfile

import pytest

from harness.persistence import AgentMemoryStore, DB_FILENAME
from harness.retention import apply, plan


@pytest.fixture()
def store():
    directory = tempfile.mkdtemp(prefix="be-retention-py-")
    s = AgentMemoryStore(os.path.join(directory, DB_FILENAME))
    yield s
    s.close()


def _iso(days_ago: int) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _add_run(store, run_id: str, status: str, *, days_ago: int) -> None:
    stamp = _iso(days_ago)
    store.create_run(run_id, "eclipse-agent", {"query": "x"})
    store.update_run(run_id, status=status, finishedAt=stamp)
    store.append_event(run_id, "model.requested", 1, {"step": 1})
    store.append_event(run_id, "run.completed", 2, {"ok": True})
    db = store._db  # noqa: SLF001 - test ages rows on purpose
    db.execute(
        "UPDATE HarnessRun SET createdAt=?, updatedAt=?, startedAt=? WHERE id=?",
        (stamp, stamp, stamp, run_id),
    )
    db.execute("UPDATE HarnessEvent SET at=? WHERE runId=?", (stamp, run_id))
    db.commit()


def test_plan_counts_only_old_terminal_runs(store):
    _add_run(store, "run_old", "completed", days_ago=40)
    _add_run(store, "run_new", "completed", days_ago=2)
    _add_run(store, "run_live", "running", days_ago=40)

    result = plan(store, older_than_days=30)
    assert result["terminalRuns"] == 1
    assert result["rows"]["HarnessEvent"] == 2


def test_apply_preserves_live_runs_and_run_history(store):
    _add_run(store, "run_old", "completed", days_ago=40)
    _add_run(store, "run_new", "completed", days_ago=2)
    _add_run(store, "run_live", "running", days_ago=40)

    result = apply(store, older_than_days=30)

    assert result["deleted"]["HarnessEvent"] == 2
    # The run ROW stays: history remains visible, only child bulk is pruned.
    assert store.get_run("run_old") is not None
    assert store.get_run("run_new") is not None
    assert store.get_run("run_live") is not None
    # New terminal and in-flight runs keep their traces.
    assert len(store.list_events("run_new")) == 2
    assert len(store.list_events("run_live")) == 2
    assert store.list_events("run_old") == []


def test_apply_is_idempotent(store):
    _add_run(store, "run_old", "completed", days_ago=40)
    apply(store, older_than_days=30)
    second = apply(store, older_than_days=30)
    assert second["deleted"]["HarnessEvent"] == 0
