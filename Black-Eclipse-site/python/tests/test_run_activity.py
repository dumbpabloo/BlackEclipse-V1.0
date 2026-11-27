"""Per-run activity heartbeat used by the stall watchdog."""

from __future__ import annotations

from harness import run_activity


def setup_function() -> None:
    # The registry is process-global; keep tests independent.
    for run_id in ["r_fresh", "r_old", "r_forget", "r_active", "r_prune"]:
        run_activity.forget(run_id)


def teardown_function() -> None:
    for run_id in ["r_fresh", "r_old", "r_forget", "r_active", "r_prune"]:
        run_activity.forget(run_id)


def test_touch_records_and_reports_age():
    run_activity.touch("r_fresh", at=100.0)
    assert run_activity.last_seen("r_fresh") == 100.0
    assert run_activity.age_s("r_fresh", now=130.0) == 30.0
    assert run_activity.is_fresh("r_fresh", window_s=60, now=130.0) is True
    assert run_activity.is_fresh("r_fresh", window_s=10, now=130.0) is False


def test_unknown_run_is_not_fresh():
    assert run_activity.last_seen("r_old") is None
    assert run_activity.is_fresh("r_old", now=10.0) is False
    assert run_activity.age_s("r_old", now=10.0) is None


def test_forget_removes_the_entry():
    run_activity.touch("r_forget", at=100.0)
    run_activity.forget("r_forget")
    assert run_activity.last_seen("r_forget") is None


def test_active_run_ids_only_include_the_fresh_window():
    run_activity.touch("r_active")
    assert "r_active" in run_activity.active_run_ids()
    assert "r_old" not in run_activity.active_run_ids()
    run_activity.forget("r_active")


def test_prune_drops_entries_older_than_the_bound():
    run_activity.touch("r_prune", at=100.0)
    assert run_activity.prune(older_than_s=10.0, now=200.0) >= 1
    assert run_activity.last_seen("r_prune") is None
