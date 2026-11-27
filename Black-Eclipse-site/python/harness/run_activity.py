"""Per-run activity heartbeat used by the stall watchdog.

Durable events are the run's trace, but token deltas are deliberately transient
(writing every fragment would bloat the store). That made a healthy model call
that stopped producing durable events for a while look identical to a hang, and
the watchdog could only rely on its 6-minute silence budget.

`RunEmitter` touches this registry on every event — including transient deltas —
so the watchdog can distinguish:

  * alive and emitting (skip), and
  * silent with no event of any kind (eligible for RUN_STALLED).

The registry is process-local and in-memory: a run owned by another process is
protected by its lease, not by this map. `find_stalled` keeps the lease skip.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

#: Activity fresher than this is treated as "the run is alive right now".
#: Comfortably below the watchdog sweep interval and far below its 360 s budget.
ACTIVITY_FRESH_S = 90.0

_lock = threading.Lock()
_last_seen: dict[str, float] = {}


def touch(run_id: Optional[str], *, at: Optional[float] = None) -> None:
    """Record that `run_id` produced an event now."""
    if not run_id:
        return
    with _lock:
        _last_seen[str(run_id)] = time.monotonic() if at is None else float(at)


def last_seen(run_id: str) -> Optional[float]:
    with _lock:
        return _last_seen.get(str(run_id))


def age_s(run_id: str, *, now: Optional[float] = None) -> Optional[float]:
    seen = last_seen(run_id)
    if seen is None:
        return None
    current = time.monotonic() if now is None else float(now)
    return max(0.0, current - seen)


def is_fresh(
    run_id: str,
    *,
    window_s: float = ACTIVITY_FRESH_S,
    now: Optional[float] = None,
) -> bool:
    age = age_s(run_id, now=now)
    return age is not None and age <= window_s


def active_run_ids(*, window_s: float = ACTIVITY_FRESH_S) -> set[str]:
    """Runs touched inside the freshness window (for the watchdog sweep)."""
    now = time.monotonic()
    with _lock:
        return {rid for rid, seen in _last_seen.items() if now - seen <= window_s}


def forget(run_id: Optional[str]) -> None:
    if not run_id:
        return
    with _lock:
        _last_seen.pop(str(run_id), None)


def prune(*, older_than_s: float = 3600.0, now: Optional[float] = None) -> int:
    """Drop dead entries so a long-lived process cannot grow this map forever."""
    current = time.monotonic() if now is None else float(now)
    with _lock:
        stale = [rid for rid, seen in _last_seen.items() if current - seen > older_than_s]
        for rid in stale:
            _last_seen.pop(rid, None)
    return len(stale)


__all__ = [
    "ACTIVITY_FRESH_S",
    "active_run_ids",
    "age_s",
    "forget",
    "is_fresh",
    "last_seen",
    "prune",
    "touch",
]
