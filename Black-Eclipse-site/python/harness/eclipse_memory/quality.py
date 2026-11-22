"""Reinforcement and garbage-collection arithmetic for the memory store.

A memory's importance should move when it is used and when it is confirmed, and it
should be revisited when it has not been touched in a week. This module is the pure
part of that: no I/O, no clock, no store — the caller supplies the current quality
and the session's running delta.

Ported from Vibe-Trading's `memory/lifecycle.py` (MIT), whose numbers are the
contract:

  * **event deltas** — task_success +0.10, task_failure -0.15, user_confirm +0.20,
    user_reject -0.30, passive_decay -0.05;
  * **system events are discounted** to 0.7x a user event: the system saying "that
    went well" is weaker evidence than the operator saying it;
  * **a per-memory per-session cap of 0.5 absolute** — without it a loop of
    successes could walk a wrong memory to 1.0 in one turn;
  * **quality is clamped to [0, 1]** after every accepted update;
  * **GC thresholds** — archive below 0.15, delete below 0.05, never before 7 days
    old, delete disabled by default (archive is reversible, deletion is not).

Eclipse stored `importance` but nothing moved it on feedback; these functions are
that missing step, and they are pure so the arithmetic is testable without a store.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "ARCHIVE_THRESHOLD",
    "DELETE_THRESHOLD",
    "EVENT_DELTAS",
    "MAX_MEMORY_COUNT",
    "MAX_SESSION_DELTA",
    "MIN_AGE_DAYS",
    "SYSTEM_DISCOUNT",
    "MemoryQualityError",
    "apply_event",
    "gc_action",
]

#: How much each kind of feedback moves a memory's quality.
EVENT_DELTAS: dict[str, float] = {
    "task_success": +0.10,
    "task_failure": -0.15,
    "user_confirm": +0.20,
    "user_reject": -0.30,
    "passive_decay": -0.05,
}

#: A system-sourced observation counts for less than an operator's.
SYSTEM_DISCOUNT = 0.7

#: Absolute size of the cumulative change one session may make to one memory.
MAX_SESSION_DELTA = 0.5

#: GC policy.
ARCHIVE_THRESHOLD = 0.15
DELETE_THRESHOLD = 0.05
MIN_AGE_DAYS = 7
MAX_MEMORY_COUNT = 500


class MemoryQualityError(ValueError):
    """An unknown event or source was supplied."""


def apply_event(
    quality: float,
    event: str,
    *,
    source: str = "system",
    session_delta: float = 0.0,
) -> dict[str, Any]:
    """Apply one feedback event and return the decision, never a bare number.

    ``accepted`` is the field that matters: a rejected update must not be applied
    by the caller, and the reason says why (``unknown_event``, ``session_cap``).
    A rejected update leaves ``sessionDelta`` unchanged, so the cap cannot be
    walked past by retrying.
    """
    if event not in EVENT_DELTAS:
        return {
            "accepted": False,
            "quality": float(quality),
            "sessionDelta": float(session_delta),
            "delta": 0.0,
            "reason": "unknown_event",
        }
    if source not in ("system", "user"):
        raise MemoryQualityError(f"unknown source {source!r}; expected 'system' or 'user'")

    delta = EVENT_DELTAS[event] * (SYSTEM_DISCOUNT if source == "system" else 1.0)
    proposed = float(session_delta) + delta
    if abs(proposed) > MAX_SESSION_DELTA:
        return {
            "accepted": False,
            "quality": float(quality),
            "sessionDelta": float(session_delta),
            "delta": delta,
            "reason": "session_cap",
        }
    return {
        "accepted": True,
        "quality": max(0.0, min(1.0, float(quality) + delta)),
        "sessionDelta": proposed,
        "delta": delta,
        "reason": "ok",
    }


def gc_action(
    importance: float,
    age_days: float,
    *,
    enable_delete: bool = False,
) -> Optional[str]:
    """``"archive"``, ``"delete"`` or ``None`` for one memory.

    Delete is opt-in twice: it needs ``enable_delete`` AND an importance below the
    delete threshold. With the default the worst outcome is an archive, which an
    operator can undo — the reference ships delete disabled for the same reason.
    """
    if age_days < MIN_AGE_DAYS:
        return None
    if enable_delete and importance < DELETE_THRESHOLD:
        return "delete"
    if importance < ARCHIVE_THRESHOLD:
        return "archive"
    return None
