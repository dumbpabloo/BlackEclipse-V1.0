"""Artifact health: the decay state machine.

A strategy or factor is not pass/fail. It is *active*, then drifts through
*monitoring* and *decayed* to *disabled*, and the drift is a count of consecutive
weak readings rather than one bad day. This is a transcription of the reference
harness's `src/strategy_store/decay.py` (Vibe-Trading, MIT), including its
thresholds and its transition counts — not a re-invention.

The rules, in the reference's own terms:

* **classification** — a value is HEALTHY at or above ``healthy``, WARNING at or
  above ``warning``, DECAYED at or above ``decayed``, else CRITICAL. The
  comparisons are ``>=`` on purpose: a value exactly on the boundary is not a
  failure.
* **aggregation** — the WEAKEST signal across the metrics provided wins, so a
  healthy IC cannot hide a collapsed Sharpe.
* **transitions** — ``active -> monitoring`` needs 3 consecutive non-healthy
  readings; ``monitoring -> decayed`` needs 2 consecutive DECAYED-or-worse;
  ``monitoring -> active`` recovers on a SINGLE healthy reading;
  ``decayed -> disabled`` needs 3 consecutive CRITICAL readings.

Pure logic: no I/O, no clock, no database. That is what makes it testable and what
lets a caller persist the status however it likes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = [
    "ACTIVE",
    "CRITICAL",
    "DECAYED",
    "DISABLED",
    "HEALTHY",
    "MONITORING",
    "SIGNAL_ORDER",
    "STATUSES",
    "DecayError",
    "DecayThresholds",
    "classify",
    "evaluate_decay",
    "should_transition",
    "worst",
]

# Decay signals, ordered worst -> best (the reference's `_SIGNAL_ORDER`).
SIGNAL_ORDER = ("CRITICAL", "DECAYED", "WARNING", "HEALTHY")

# Artifact statuses.
ACTIVE = "ACTIVE"
MONITORING = "MONITORING"
DECAYED = "DECAYED"
DISABLED = "DISABLED"
STATUSES = (ACTIVE, MONITORING, DECAYED, DISABLED)

HEALTHY = "HEALTHY"
WARNING = "WARNING"
CRITICAL = "CRITICAL"


class DecayError(ValueError):
    """An unknown signal or status was supplied."""


@dataclass(frozen=True)
class DecayThresholds:
    """The reference's defaults, unchanged. Override only with a stated reason."""

    ic_ratio_healthy: float = 0.7
    ic_ratio_warning: float = 0.5
    ic_ratio_decayed: float = 0.3
    ir_healthy: float = 1.0
    ir_warning: float = 0.5
    ir_decayed: float = 0.1
    ic_pos_ratio_healthy: float = 0.55
    ic_pos_ratio_warning: float = 0.45
    ic_pos_ratio_decayed: float = 0.35
    sharpe_healthy: float = 1.0
    sharpe_warning: float = 0.5
    sharpe_decayed: float = 0.0
    warnings_for_monitoring: int = 3
    warnings_for_decayed: int = 2
    critical_for_disabled: int = 3


def _check_signal(signal: str) -> str:
    if signal not in SIGNAL_ORDER:
        raise DecayError(f"unknown decay signal {signal!r}")
    return signal


def worst(a: str, b: str) -> str:
    """The weaker of two signals (the reference's ``_worst``)."""
    _check_signal(a)
    _check_signal(b)
    return a if SIGNAL_ORDER.index(a) <= SIGNAL_ORDER.index(b) else b


def classify(value: float, healthy: float, warning: float, decayed: float) -> str:
    """Classify one metric against three descending thresholds."""
    if value >= healthy:
        return HEALTHY
    if value >= warning:
        return WARNING
    if value >= decayed:
        return DECAYED
    return CRITICAL


def evaluate_decay(
    *,
    ic_ratio: Optional[float] = None,
    ir: Optional[float] = None,
    ic_positive_ratio: Optional[float] = None,
    sharpe: Optional[float] = None,
    thresholds: Optional[DecayThresholds] = None,
) -> str:
    """The weakest signal across the metrics supplied. No metrics -> HEALTHY."""
    t = thresholds or DecayThresholds()
    signals: list[str] = []
    if ic_ratio is not None:
        signals.append(classify(ic_ratio, t.ic_ratio_healthy, t.ic_ratio_warning,
                                t.ic_ratio_decayed))
    if ir is not None:
        signals.append(classify(ir, t.ir_healthy, t.ir_warning, t.ir_decayed))
    if ic_positive_ratio is not None:
        signals.append(classify(ic_positive_ratio, t.ic_pos_ratio_healthy,
                                t.ic_pos_ratio_warning, t.ic_pos_ratio_decayed))
    if sharpe is not None:
        signals.append(classify(sharpe, t.sharpe_healthy, t.sharpe_warning,
                                t.sharpe_decayed))
    if not signals:
        return HEALTHY
    result = signals[0]
    for signal in signals[1:]:
        result = worst(result, signal)
    return result


def _tail(signals: Sequence[str], needed: int) -> Optional[list[str]]:
    _check_signal(signals[-1])
    if len(signals) < needed or needed <= 0:
        return None
    return list(signals[-needed:])


def should_transition(
    current_status: str,
    consecutive_signals: Sequence[str],
    thresholds: Optional[DecayThresholds] = None,
) -> Optional[str]:
    """The next status, or ``None`` when the evidence is not yet sufficient.

    ``consecutive_signals`` is oldest-first with the newest reading last.
    """
    if current_status not in STATUSES:
        raise DecayError(f"unknown artifact status {current_status!r}")
    t = thresholds or DecayThresholds()
    if not consecutive_signals:
        return None

    if current_status == ACTIVE:
        tail = _tail(consecutive_signals, t.warnings_for_monitoring)
        if tail and all(signal != HEALTHY for signal in tail):
            return MONITORING
        return None

    if current_status == MONITORING:
        if consecutive_signals[-1] == HEALTHY:
            return ACTIVE          # recovery on one reading, as in the reference
        tail = _tail(consecutive_signals, t.warnings_for_decayed)
        if tail and all(signal in (DECAYED, CRITICAL) for signal in tail):
            return DECAYED
        return None

    if current_status == DECAYED:
        tail = _tail(consecutive_signals, t.critical_for_disabled)
        if tail and all(signal == CRITICAL for signal in tail):
            return DISABLED
        return None
    return None
