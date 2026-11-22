"""A minimal, stdlib-only 5-field cron matcher.

The repo's standing rule is *"Do not add dependencies without justification;
prefer the lighter option"* (`AGENTS.md`), and a full cron library is not
justified by 5-field expressions. This module implements exactly the subset a
finance job needs and refuses everything else WITH a specific error:

  * ``*``           any value in the field's range
  * ``N``           one exact value
  * ``a-b``         an inclusive range
  * ``a,b,c``       a comma list of the above
  * ``*/N``         every Nth value (a step over the whole range)
  * ``a-b/N``       a step over a bounded range

Refused, each with its own message:

  * a 6-field (seconds) expression — cron here is minute-granular;
  * an ``@reboot``-style macro;
  * a name alias (``MON``, ``JAN``) — numeric fields only;
  * any field that does not parse.

``next_fire(after)`` is pure: it never reads the clock, so tests inject a
datetime and assert exact results (including a leap day and a DST boundary).
The day-of-month / day-of-week OR rule matches Vixie cron: when BOTH are
restricted, a day matching EITHER fires.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

# The 5 fields and their inclusive ranges, in order.
FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),    # minute
    (0, 23),    # hour
    (1, 31),    # day of month
    (1, 12),    # month
    (0, 6),     # day of week (0 = Sunday)
)

FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

MACRO_RE = "@"


class CronError(ValueError):
    """A specific, operator-readable cron parse error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_field(field: str, index: int) -> tuple[frozenset[int], bool]:
    """Parse one field. Returns ``(allowed_values, is_restricted)``.

    ``is_restricted`` is False only for a bare ``*`` — the signal the day-of-month
    / day-of-week OR rule needs.
    """
    low, high = FIELD_RANGES[index]
    name = FIELD_NAMES[index]
    if field == "":
        raise CronError("EMPTY_FIELD", f"the {name} field is empty")
    if "*" in field and field != "*" and not field.startswith("*/") and "/" not in field:
        raise CronError("INVALID_FIELD", f"the {name} field {field!r} is not valid")

    if field == "*":
        return frozenset(range(low, high + 1)), False

    allowed: set[int] = set()
    for part in field.split(","):
        if part == "":
            raise CronError("INVALID_FIELD", f"the {name} field has an empty list item")
        step = 1
        body = part
        if "/" in part:
            body, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError("INVALID_STEP", f"the {name} field has an invalid step {step_text!r}")
            step = int(step_text)
            if body == "":
                body = "*"
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_text, _, end_text = body.partition("-")
            if not start_text.isdigit() or not end_text.isdigit():
                raise CronError("INVALID_RANGE", f"the {name} field has an invalid range {body!r}")
            start, end = int(start_text), int(end_text)
        else:
            if not body.isdigit():
                # The only way here is a name alias or garbage.
                if body.isalpha():
                    raise CronError(
                        "NAME_NOT_ALLOWED",
                        f"the {name} field uses a name {body!r}; numeric values only",
                    )
                raise CronError("INVALID_FIELD", f"the {name} field {body!r} is not a number")
            start = end = int(body)

        if start < low or end > high or start > end:
            raise CronError(
                "OUT_OF_RANGE",
                f"the {name} field {part!r} is outside {low}-{high}",
            )
        allowed.update(range(start, end + 1, step))

    if not allowed:
        raise CronError("INVALID_FIELD", f"the {name} field {field!r} matches no value")
    return frozenset(allowed), True


@dataclass(frozen=True)
class CronExpression:
    """A validated 5-field expression, with a pure ``next_fire``."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def matches(self, moment: _dt.datetime) -> bool:
        """Whether ``moment`` (minute granularity) satisfies every field.

        Vixie OR rule: the day matches when BOTH day fields are restricted only
        if EITHER matches; when one is a bare ``*`` it imposes no constraint.
        """
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        # Python: Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
        cron_weekday = (moment.weekday() + 1) % 7
        dom_ok = moment.day in self.days
        dow_ok = cron_weekday in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def next_fire(self, after: _dt.datetime) -> Optional[_dt.datetime]:
        """The first minute strictly after ``after`` that matches, or ``None``.

        Pure. Scans minute by minute from ``after + 1 minute`` for at most ~4 years
        (a leap-year cycle plus slack). An expression that can never match (e.g.
        Feb 30) — or one whose next fire is beyond the horizon — returns ``None``
        rather than a phantom far-future instant, so callers treat it as
        "never fires" instead of scheduling a run years out.
        """
        # Drop seconds/microseconds and step to the next whole minute.
        candidate = after.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        limit = candidate + _dt.timedelta(days=366 * 4)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += _dt.timedelta(minutes=1)
        return None


def _count_fields(expression: str) -> int:
    return len(expression.split())


def parse_cron(expression: str) -> CronExpression:
    """Parse a 5-field expression or raise ``CronError`` with a specific code."""
    if expression is None:
        raise CronError("EMPTY_EXPRESSION", "the schedule is empty")
    text = str(expression).strip()
    if not text:
        raise CronError("EMPTY_EXPRESSION", "the schedule is empty")
    if text.startswith(MACRO_RE):
        raise CronError(
            "MACRO_NOT_ALLOWED",
            f"macros like {text!r} are not supported; use a 5-field schedule "
            "(minute hour day-of-month month day-of-week)",
        )
    count = _count_fields(text)
    if count == 6:
        raise CronError(
            "SECONDS_NOT_ALLOWED",
            "a 6-field schedule (with seconds) is not supported; use 5 fields "
            "(minute hour day-of-month month day-of-week)",
        )
    if count != 5:
        raise CronError(
            "FIELD_COUNT",
            f"a schedule needs exactly 5 fields, got {count}: {text!r}",
        )
    fields = text.split()
    parsed = [_parse_field(fields[i], i) for i in range(5)]
    return CronExpression(
        expression=text,
        minutes=parsed[0][0],
        hours=parsed[1][0],
        days=parsed[2][0],
        months=parsed[3][0],
        weekdays=parsed[4][0],
        dom_restricted=parsed[2][1],
        dow_restricted=parsed[4][1],
    )


def is_valid(expression: str) -> bool:
    """Whether ``expression`` parses. Never raises."""
    try:
        parse_cron(expression)
        return True
    except CronError:
        return False


# ── DST-aware instant resolution ───────────────────────────────────────────
#
# `next_fire` above does wall-clock arithmetic on a naive datetime, which is the
# right thing for a UTC schedule and the wrong thing across a DST transition. The
# policy here is Vibe-Trading's (`scheduling/triggers.py::
# local_wall_time_to_epoch_ms`), and both halves matter:
#
#   * a NONEXISTENT wall time (the spring-forward gap) resolves to ``None`` and is
#     skipped — ZoneInfo would otherwise silently map it past the transition
#     (PEP 495) and run it at an hour nobody asked for;
#   * an AMBIGUOUS wall time (the fall-back fold) resolves with ``fold=0``, the
#     first occurrence, so it runs exactly once rather than twice.

#: Days searched before declaring a cron expression never fires (~4 years, the
#: reference's ``CRON_SEARCH_LIMIT_DAYS``: a leap day plus DST slack).
CRON_SEARCH_LIMIT_DAYS = 6 * 366 + 1


def local_wall_time_to_epoch_ms(
    day: _dt.date, hour: int, minute: int, zone: _dt.tzinfo
) -> Optional[int]:
    """Resolve one local wall time to a UTC epoch-ms instant, or ``None`` in a gap."""
    local = _dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    as_utc = local.astimezone(_dt.timezone.utc)
    round_trip = as_utc.astimezone(zone).replace(tzinfo=None)
    if round_trip != local.replace(tzinfo=None):
        return None                    # this wall time does not exist here (DST gap)
    return int(as_utc.timestamp() * 1000)


def next_fire_epoch_ms(
    expression: CronExpression, after_ms: int, zone: _dt.tzinfo
) -> Optional[int]:
    """First matching instant strictly after ``after_ms``, evaluated in ``zone``.

    Days are walked on the zone's own calendar, so the weekday test follows the
    authoring wall clock rather than UTC. A fire time that falls in a DST gap is
    skipped instead of being moved; ``None`` means "never fires" within the search
    horizon, which is how an impossible date (Feb 30) is reported.
    """
    start = _dt.datetime.fromtimestamp(after_ms / 1000.0, zone).date()
    for offset in range(CRON_SEARCH_LIMIT_DAYS):
        candidate_day = start + _dt.timedelta(days=offset)
        if candidate_day.month not in expression.months:
            continue
        cron_dow = (candidate_day.weekday() + 1) % 7
        dom_ok = candidate_day.day in expression.days
        dow_ok = cron_dow in expression.weekdays
        if expression.dom_restricted and expression.dow_restricted:
            day_ok = dom_ok or dow_ok            # Vixie OR rule
        else:
            day_ok = dom_ok and dow_ok           # an unconstrained field is a full set
        if not day_ok:
            continue
        for hour in sorted(expression.hours):
            for minute in sorted(expression.minutes):
                fire = local_wall_time_to_epoch_ms(candidate_day, hour, minute, zone)
                if fire is not None and fire > after_ms:
                    return fire
    return None


__all__ = [
    "CRON_SEARCH_LIMIT_DAYS",
    "CronError",
    "CronExpression",
    "FIELD_NAMES",
    "FIELD_RANGES",
    "is_valid",
    "local_wall_time_to_epoch_ms",
    "next_fire_epoch_ms",
    "parse_cron",
]
