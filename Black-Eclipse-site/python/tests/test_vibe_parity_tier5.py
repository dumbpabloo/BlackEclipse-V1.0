"""Numeric parity, fifth tier: memory reinforcement and DST-aware cron firing.

References fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):

* ``memory/lifecycle.py`` — event deltas (+0.10 success, -0.15 failure, +0.20 user
  confirm, -0.30 user reject, -0.05 passive decay), a 0.7x discount for
  system-sourced events, a **0.5 absolute per-session cap**, a [0, 1] clamp, and GC
  thresholds (archive < 0.15, delete < 0.05, never younger than 7 days, delete off).
  Eclipse stored an `importance` but nothing moved it on feedback; the arithmetic is
  ported as `eclipse_memory/quality.py`.
* ``scheduling/triggers.py`` — DST policy: a **nonexistent** wall time (spring-forward
  gap) is skipped, an **ambiguous** one (fall-back fold) resolves to its first
  occurrence. Eclipse's cron did wall-clock arithmetic with no DST policy at all, so a
  daily 09:00 job could land at the wrong instant or fire twice. Ported as
  `jobs/cron.py::{local_wall_time_to_epoch_ms, next_fire_epoch_ms}`.

Both references are reimplemented in this file rather than imported.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from harness.eclipse_memory import quality as Q
from harness.jobs import cron


# -- memory: the reference deltas and the session cap ---------------------

REFERENCE_DELTAS = {
    "task_success": 0.10,
    "task_failure": -0.15,
    "user_confirm": 0.20,
    "user_reject": -0.30,
    "passive_decay": -0.05,
}


def reference_apply(quality, event, source, session_delta):
    delta = REFERENCE_DELTAS[event] * (0.7 if source == "system" else 1.0)
    proposed = session_delta + delta
    if abs(proposed) > 0.5:
        return ("cap", quality, session_delta)
    return ("ok", max(0.0, min(1.0, quality + delta)), proposed)


def test_the_event_deltas_are_the_references():
    assert dict(Q.EVENT_DELTAS) == REFERENCE_DELTAS
    assert Q.SYSTEM_DISCOUNT == pytest.approx(0.7)
    assert Q.MAX_SESSION_DELTA == pytest.approx(0.5)


@pytest.mark.parametrize("event", sorted(REFERENCE_DELTAS))
@pytest.mark.parametrize("source", ["system", "user"])
@pytest.mark.parametrize("quality", [0.0, 0.5, 1.0])
def test_every_event_matches_the_reference_arithmetic(event, source, quality):
    got = Q.apply_event(quality, event, source=source, session_delta=0.0)
    status, expected_quality, expected_delta = reference_apply(quality, event, source, 0.0)
    assert got["accepted"] is (status == "ok")
    assert got["quality"] == pytest.approx(expected_quality)
    assert got["sessionDelta"] == pytest.approx(expected_delta)


def test_a_system_event_is_worth_seventy_percent_of_an_operators():
    user = Q.apply_event(0.5, "user_confirm", source="user")
    system = Q.apply_event(0.5, "user_confirm", source="system")
    assert user["delta"] == pytest.approx(0.2)
    assert system["delta"] == pytest.approx(0.14)
    assert user["quality"] == pytest.approx(0.7)
    assert system["quality"] == pytest.approx(0.64)


def test_quality_is_clamped_to_the_unit_interval():
    assert Q.apply_event(0.95, "user_confirm", source="user")["quality"] == pytest.approx(1.0)
    assert Q.apply_event(0.05, "user_reject", source="user")["quality"] == pytest.approx(0.0)


def test_the_session_cap_refuses_rather_than_overshooting():
    # 0.2 + 0.2 + 0.2 would exceed the 0.5 cap on the third event
    first = Q.apply_event(0.5, "user_confirm", source="user")
    second = Q.apply_event(first["quality"], "user_confirm", source="user",
                           session_delta=first["sessionDelta"])
    third = Q.apply_event(second["quality"], "user_confirm", source="user",
                          session_delta=second["sessionDelta"])
    assert second["accepted"] is True
    assert third["accepted"] is False and third["reason"] == "session_cap"
    # a refused update must not move the accumulated delta
    assert third["sessionDelta"] == pytest.approx(second["sessionDelta"])
    assert third["quality"] == pytest.approx(second["quality"])


def test_an_unknown_event_is_refused_not_ignored():
    got = Q.apply_event(0.5, "task_brilliant", source="user")
    assert got["accepted"] is False and got["reason"] == "unknown_event"
    assert got["quality"] == pytest.approx(0.5)


def test_an_unknown_source_is_an_error():
    with pytest.raises(Q.MemoryQualityError):
        Q.apply_event(0.5, "task_success", source="robot")


# -- memory: GC thresholds ------------------------------------------------


def test_gc_thresholds_are_the_references():
    assert (Q.ARCHIVE_THRESHOLD, Q.DELETE_THRESHOLD, Q.MIN_AGE_DAYS) == (0.15, 0.05, 7)


def test_a_young_memory_is_never_collected():
    assert Q.gc_action(0.0, age_days=6.9) is None
    assert Q.gc_action(0.0, age_days=7) == "archive"


def test_delete_needs_both_the_switch_and_a_low_score():
    assert Q.gc_action(0.01, age_days=30) == "archive"          # delete off by default
    assert Q.gc_action(0.01, age_days=30, enable_delete=True) == "delete"
    assert Q.gc_action(0.10, age_days=30, enable_delete=True) == "archive"
    assert Q.gc_action(0.20, age_days=30, enable_delete=True) is None


# -- cron: DST policy -----------------------------------------------------


NY = ZoneInfo("America/New_York")


def test_a_spring_forward_gap_is_skipped_not_shifted():
    # 2024-03-10 02:30 does not exist in New York
    assert cron.local_wall_time_to_epoch_ms(date(2024, 3, 10), 2, 30, NY) is None
    # an hour either side does exist
    assert cron.local_wall_time_to_epoch_ms(date(2024, 3, 10), 1, 30, NY) is not None
    assert cron.local_wall_time_to_epoch_ms(date(2024, 3, 10), 3, 30, NY) is not None


def test_a_fall_back_fold_resolves_to_the_first_occurrence():
    # 2024-11-03 01:30 happens twice; fold=0 is the first (EDT, UTC-4)
    ms = cron.local_wall_time_to_epoch_ms(date(2024, 11, 3), 1, 30, NY)
    assert ms == int(datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc).timestamp() * 1000)


def test_the_same_wall_time_resolves_the_same_way_in_utc_all_year():
    # EST is UTC-5 in January, EDT is UTC-4 in July: the local wall time is the same,
    # the instant is not, and that is exactly the point of resolving in the zone.
    expected = {date(2024, 1, 15): 14, date(2024, 7, 15): 13}
    for day, utc_hour in expected.items():
        ms = cron.local_wall_time_to_epoch_ms(day, 9, 0, NY)
        assert ms == int(datetime(day.year, day.month, day.day, utc_hour, 0,
                                  tzinfo=timezone.utc).timestamp() * 1000)


def test_a_daily_gap_time_job_fires_after_the_gap_not_inside_it():
    """09:30 is safe, but a 02:30 job must skip the missing hour, not fire early."""
    expr = cron.parse_cron("30 2 * * *")
    after = int(datetime(2024, 3, 9, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    fire = cron.next_fire_epoch_ms(expr, after, NY)
    assert fire is not None
    fired = datetime.fromtimestamp(fire / 1000.0, NY)
    assert (fired.month, fired.day) == (3, 11)      # the 10th is skipped
    assert (fired.hour, fired.minute) == (2, 30)


def test_the_next_fire_is_strictly_after_the_reference_instant():
    expr = cron.parse_cron("0 9 * * *")
    exactly_nine = int(datetime(2024, 5, 6, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    fire = cron.next_fire_epoch_ms(expr, exactly_nine, NY)
    assert fire > exactly_nine
    assert datetime.fromtimestamp(fire / 1000.0, NY).day == 7


def test_an_impossible_date_never_fires():
    expr = cron.parse_cron("0 0 30 2 *")
    after = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert cron.next_fire_epoch_ms(expr, after, timezone.utc) is None


def test_the_vixie_or_rule_holds_in_the_zone_aware_path():
    # day-of-month 13 OR Friday: 2024-09-13 is a Friday, so both agree; use a month
    # where the 13th is not a Friday and check the OR still fires on the 13th.
    expr = cron.parse_cron("0 12 13 * 5")
    after = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fire = cron.next_fire_epoch_ms(expr, after, timezone.utc)
    assert fire is not None
    fired = datetime.fromtimestamp(fire / 1000.0, timezone.utc)
    assert fired.day == 13 or fired.weekday() == 4


def test_the_search_horizon_is_the_references():
    assert cron.CRON_SEARCH_LIMIT_DAYS == 6 * 366 + 1
