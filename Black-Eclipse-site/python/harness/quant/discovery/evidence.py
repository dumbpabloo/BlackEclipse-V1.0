"""Evidence arithmetic: is a strategy's evidence recent, wide enough, and viable?

A "validated" strategy is a claim about a specific window. Two things make such a
claim stale without anyone lying: the window is old (the market it describes has
moved), or the window is short (one quarter cannot distinguish skill from luck).
Both are arithmetic, so both live here, free of I/O and of any model.

Ported from Vibe-Trading's `strategy_discovery/models.py` (MIT). The constants are
that module's single source of truth, and three details are load-bearing:

* **a window end is the LAST day of its end month** — a range ending ``2024-06``
  covers through 30 June, so age is measured from the end of the month, not its
  first day;
* **an exact threshold belongs to the STRICTER side** — ``age >= 180`` is stale,
  ``age >= 90`` is aging, otherwise fresh; and a window that cannot be dated is
  **stale fail-closed**, because evidence nobody can date cannot vouch for anything;
* **the gating age is the EVIDENCE window's age, never ``last_verified``** — a
  scheduled refresh of unchanged artifacts would otherwise keep a dead row "fresh"
  forever. ``staleness_days`` is reported beside the verdict and never gates.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "BORDERLINE_BREAKEVEN_BPS",
    "BORDERLINE_COVERAGE_BUFFER_DAYS",
    "BORDERLINE_TRADE_BUFFER",
    "COST_SENSITIVE_BREAKEVEN_BPS",
    "DECAY_AGING_EVIDENCE_AGE_DAYS",
    "DECAY_STALE_EVIDENCE_AGE_DAYS",
    "MIN_COVERAGE_DAYS",
    "MIN_TRADES",
    "QUALITY_ADEQUATE",
    "QUALITY_INSUFFICIENT",
    "QUALITY_MARGINAL",
    "QUALITY_ORDER",
    "breakeven_fee_bps",
    "build_warnings",
    "classify_decay",
    "classify_quality",
    "coverage_days_from_ranges",
    "decay_warning",
    "evidence_age_days",
    "is_borderline",
    "staleness_days",
]

QUALITY_ADEQUATE = "adequate"
QUALITY_MARGINAL = "marginal"
QUALITY_INSUFFICIENT = "insufficient"
QUALITY_ORDER = {QUALITY_ADEQUATE: 2, QUALITY_MARGINAL: 1, QUALITY_INSUFFICIENT: 0}

#: Below this many trades in a regime the evidence cannot exceed "insufficient".
MIN_TRADES = 10
#: Below this many calendar days of coverage it is capped at "marginal".
MIN_COVERAGE_DAYS = 730
#: A breakeven fee below this many basis points is too cost-sensitive to recommend
#: without an explicit cost check.
COST_SENSITIVE_BREAKEVEN_BPS = 5.0

#: A row that passes the hard thresholds but sits inside these buffered bands is
#: reported "borderline" so a caller cites the raw figures, not the verdict.
BORDERLINE_TRADE_BUFFER = 5
BORDERLINE_BREAKEVEN_BPS = 10.0
BORDERLINE_COVERAGE_BUFFER_DAYS = 365

DECAY_FRESH = "fresh"
DECAY_AGING = "aging"
DECAY_STALE = "stale"
DECAY_AGING_EVIDENCE_AGE_DAYS = 90
DECAY_STALE_EVIDENCE_AGE_DAYS = 180

_MONTH_RE = re.compile(r"(\d{4})-(\d{1,2})")


def _month_first(token: str) -> Optional[date]:
    match = _MONTH_RE.fullmatch(str(token).strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _month_end(start: date) -> date:
    if start.month == 12:
        return date(start.year, 12, 31)
    return date(start.year, start.month + 1, 1) - timedelta(days=1)


def _ranges(date_ranges: Iterable[Any]) -> Iterable[tuple[date, date]]:
    for entry in date_ranges or ():
        if not isinstance(entry, str):
            continue
        pieces = re.split(r"\s+to\s+", entry.strip(), flags=re.IGNORECASE)
        if len(pieces) != 2:
            continue
        start = _month_first(pieces[0])
        end_start = _month_first(pieces[1])
        if start is None or end_start is None:
            continue
        yield start, _month_end(end_start)


def coverage_days_from_ranges(date_ranges: Iterable[Any]) -> int:
    """Span from the earliest start to the latest end, in days (0 when undatable)."""
    starts: list[date] = []
    ends: list[date] = []
    for start, end in _ranges(date_ranges):
        starts.append(start)
        ends.append(end)
    if not starts or not ends:
        return 0
    return max(0, (max(ends) - min(starts)).days)


def evidence_window_end(date_ranges: Iterable[Any]) -> Optional[date]:
    ends = [end for _start, end in _ranges(date_ranges)]
    return max(ends) if ends else None


def evidence_age_days(date_ranges: Iterable[Any], today: date) -> Optional[int]:
    """Age of the latest window end, clamped at 0; ``None`` when undatable."""
    end = evidence_window_end(date_ranges)
    if end is None:
        return None
    return max(0, (today - end).days)


def staleness_days(last_verified: str, today: date) -> Optional[int]:
    """Days since verification. Reported, never used for gating."""
    text = str(last_verified or "").strip()
    if not text:
        return None
    try:
        verified = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return max(0, (today - verified).days)


def classify_decay(
    date_ranges: Iterable[Any],
    today: date,
    *,
    aging_days: int = DECAY_AGING_EVIDENCE_AGE_DAYS,
    stale_days: int = DECAY_STALE_EVIDENCE_AGE_DAYS,
) -> str:
    """``stale`` / ``aging`` / ``fresh`` from the window's age; undatable = stale."""
    age = evidence_age_days(date_ranges, today)
    if age is None:
        return DECAY_STALE
    if age >= stale_days:
        return DECAY_STALE
    if age >= aging_days:
        return DECAY_AGING
    return DECAY_FRESH


def decay_warning(status: str, age: Optional[int]) -> Optional[str]:
    """A stable, prefix-token warning for a non-fresh verdict, else ``None``."""
    if status == DECAY_STALE:
        if age is None:
            return (
                "stale-evidence: evidence window end is missing or unparseable, "
                "treated as stale fail-closed — re-backtest over a recent window "
                "and refresh before relying on this row"
            )
        return (
            f"stale-evidence: evidence window ended {age} days ago "
            f"(>= {DECAY_STALE_EVIDENCE_AGE_DAYS}-day staleness threshold) — "
            "excluded from default recommendations; re-backtest over a recent "
            "window and refresh to reinstate"
        )
    if status == DECAY_AGING:
        return (
            f"aged-evidence: evidence window ended {age} days ago "
            f"(>= {DECAY_AGING_EVIDENCE_AGE_DAYS}-day aging threshold) — still "
            "queryable, but verify with a fresh backtest before relying on this row"
        )
    return None


def classify_quality(trades_in_regime: int, coverage_days: int) -> str:
    """The quality ladder: trades first, then coverage."""
    if int(trades_in_regime) < MIN_TRADES:
        return QUALITY_INSUFFICIENT
    if int(coverage_days) < MIN_COVERAGE_DAYS:
        return QUALITY_MARGINAL
    return QUALITY_ADEQUATE


def breakeven_fee_bps(
    gross_return: float, trades: int, position_size: Optional[float] = None
) -> Optional[float]:
    """Sizing-corrected breakeven fee: ``ln(1+r) / (2·trades·size) · 10_000``.

    ``None`` for unusable inputs (non-finite, no trades, a return at or below −100%,
    or a non-positive size) rather than a meaningless infinity.
    """
    size = 1.0 if position_size is None else position_size
    try:
        gross = float(gross_return)
        count = float(trades)
        position = float(size)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(gross) and math.isfinite(count) and math.isfinite(position)):
        return None
    if count <= 0 or gross <= -1.0 or position <= 0:
        return None
    return math.log(1.0 + gross) / (2.0 * count * position) * 10_000.0


def is_borderline(
    trades_in_regime: int, breakeven: Optional[float], coverage_days: int
) -> bool:
    """Whether a passing row sits inside a buffered band around a threshold."""
    if int(trades_in_regime) < MIN_TRADES + BORDERLINE_TRADE_BUFFER:
        return True
    if breakeven is not None and breakeven < BORDERLINE_BREAKEVEN_BPS:
        return True
    if int(coverage_days) < MIN_COVERAGE_DAYS + BORDERLINE_COVERAGE_BUFFER_DAYS:
        return True
    return False


def build_warnings(
    trades_in_regime: int,
    coverage_days: int,
    breakeven: Optional[float],
    quality: str,
    cost_sensitive: bool,
) -> tuple[str, ...]:
    """Stable prefix-token warnings for the conditions that actually hold."""
    out: list[str] = []
    if int(trades_in_regime) < MIN_TRADES:
        out.append(
            f"insufficient-trades: {trades_in_regime} < {MIN_TRADES} trades in regime"
        )
    if int(coverage_days) < MIN_COVERAGE_DAYS:
        out.append(
            f"short-coverage: {coverage_days} days < {MIN_COVERAGE_DAYS} day window"
        )
    if cost_sensitive:
        text = (
            f"{breakeven:.2f} bps"
            if isinstance(breakeven, float) and math.isfinite(breakeven)
            else "unknown"
        )
        out.append(
            f"cost-sensitive: breakeven {text} below "
            f"{COST_SENSITIVE_BREAKEVEN_BPS:.1f} bps threshold"
        )
    return tuple(out)
