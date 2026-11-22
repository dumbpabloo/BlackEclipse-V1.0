"""MCP circuit breaker (continuation) — stop futile retries on a broken server.

`RetryBudget` covers connect/reconnect. `maxCallsPerRun` bounds call *volume*.
Neither stops a server that authenticates its transport and then fails every
call: it is re-attempted on every turn, burning latency and tokens, and the user
sees the same error with no signal the runtime noticed.

This module owns the breaker *policy* as pure, testable functions and a small
`CircuitBreaker` state machine:

  * CLOSED     — normal. Failures accumulate.
  * OPEN       — the failure threshold was reached. Calls refuse immediately and
                 honestly (no transport work) until the cooldown elapses.
  * HALF_OPEN  — after the cooldown, exactly ONE probe call is allowed. Success
                 resets to CLOSED; failure re-arms the cooldown.

Which failures count (stated exactly): failures that *reached the transport* —
connection refused, timeout, transport/session error, and a provider-side error
(the server answered but rejected the call). **Not** counted: a denied/absent
approval, a caller-side validation error, an MCP call budget refusal, or a call
blocked by this breaker. Those are operator/caller behaviour, not server health,
and counting them would trip the breaker on the operator.

Pure helpers (`breaker_state`, `should_probe`) are testable without a live
process, matching `lifecycle.should_recycle` / `keepalive_due`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Named thresholds — not magic numbers.
FAILURE_THRESHOLD = 3
COOLDOWN_S = 60.0

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Error classes that reach the transport and therefore count toward the breaker.
# (Stated for the docstring / tests; the manager classifies before calling
# `record_failure`.)
TRANSPORT_FAILURE_CODES = frozenset({
    "mcp_timeout",
    "mcp_call_failed",
    "mcp_session_expired",
    "mcp_protocol_error",
    "mcp_closed",
    "mcp_startup_failed",
    "mcp_tool_provider_error",
    "mcp_server_unavailable",
})


def backoff_cooldown_s(failures: int, *, base_s: float, lifetime_s: float) -> float:
    """Exponential per-failure cooldown, capped at ``lifetime_s``.

    Parity with Vibe-Trading's `data_gateway/routing.py::NetworkRouter.record`:

        retry_at = now + min(lifetime, cooldown * 2 ** (failures - 1))

    A flat cooldown retries a dead source at the same cadence forever; doubling per
    consecutive failure — and capping at ``lifetime`` so it never waits longer than a
    healthy observation stays fresh — is the difference between "try again in a
    minute" and "this source has been down for an hour".

    ``failures <= 0`` returns the base cooldown: a breaker that has not failed should
    not wait longer than one interval.
    """
    base = max(0.0, float(base_s))
    lifetime = max(base, float(lifetime_s))
    if failures <= 0:
        return base
    exponent = min(int(failures) - 1, 62)      # 2**63 overflows a float
    return min(lifetime, base * (2.0 ** exponent))


def breaker_state(
    *, failures: int, opened_at: Optional[float], now: float, threshold: int = FAILURE_THRESHOLD,
    cooldown_s: float = COOLDOWN_S,
) -> str:
    """The breaker state for the given counters. Pure."""
    if failures < threshold:
        return CLOSED
    if opened_at is None:
        return OPEN
    if now - opened_at >= cooldown_s:
        return HALF_OPEN
    return OPEN


def should_probe(
    *, failures: int, opened_at: Optional[float], now: float, probe_in_flight: bool, threshold: int = FAILURE_THRESHOLD,
    cooldown_s: float = COOLDOWN_S,
) -> bool:
    """Whether the next call is the single HALF_OPEN probe. Pure."""
    if probe_in_flight:
        return False
    return breaker_state(
        failures=failures, opened_at=opened_at, now=now, threshold=threshold, cooldown_s=cooldown_s
    ) == HALF_OPEN


@dataclass
class CircuitBreaker:
    """A per-server failure breaker. Timestamps use a monotonic clock."""

    threshold: int = FAILURE_THRESHOLD
    cooldown_s: float = COOLDOWN_S
    #: When set, ``cooldown_s`` is the BASE of an exponential backoff capped at this
    #: many seconds (see ``backoff_cooldown_s``). ``None`` keeps the flat cooldown,
    #: which is what every existing caller gets.
    lifetime_s: Optional[float] = None
    failures: int = 0
    opened_at: Optional[float] = None
    last_error_code: Optional[str] = None
    probe_in_flight: bool = False
    tripped_count: int = 0
    last_tripped_at: Optional[str] = None
    _clock: object = field(default_factory=lambda: time.monotonic)

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def effective_cooldown_s(self) -> float:
        """The cooldown that applies at the current failure count."""
        if self.lifetime_s is None:
            return self.cooldown_s
        return backoff_cooldown_s(
            self.failures, base_s=self.cooldown_s, lifetime_s=self.lifetime_s
        )

    def state(self) -> str:
        return breaker_state(
            failures=self.failures, opened_at=self.opened_at, now=self._now(),
            threshold=self.threshold, cooldown_s=self.effective_cooldown_s(),
        )

    def allow(self) -> bool:
        """Whether a call may proceed now (and claim the probe slot if HALF_OPEN).

        Sets `probe_in_flight` when it grants the single half-open probe.
        """
        state = self.state()
        if state == CLOSED:
            return True
        if state == OPEN:
            return False
        # HALF_OPEN: exactly one probe at a time.
        if self.probe_in_flight:
            return False
        self.probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.last_error_code = None
        self.probe_in_flight = False

    def record_failure(self, code: Optional[str], *, now_iso: Optional[str] = None) -> None:
        """Count a transport failure; open the breaker at the threshold."""
        self.failures += 1
        self.last_error_code = code or self.last_error_code
        self.probe_in_flight = False
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = self._now()
            self.tripped_count += 1
            self.last_tripped_at = now_iso

    def record_probe_failure(self, code: Optional[str], *, now_iso: Optional[str] = None) -> None:
        """A failed HALF_OPEN probe re-arms the cooldown from now."""
        self.failures = max(self.failures, self.threshold)
        self.last_error_code = code or self.last_error_code
        self.opened_at = self._now()
        self.probe_in_flight = False
        self.last_tripped_at = now_iso

    def reset(self) -> None:
        """Operator reset: clear everything back to CLOSED."""
        self.failures = 0
        self.opened_at = None
        self.last_error_code = None
        self.probe_in_flight = False

    def retry_after_s(self) -> Optional[float]:
        """Seconds until a HALF_OPEN probe is allowed, or None when not OPEN."""
        if self.state() != OPEN or self.opened_at is None:
            return None
        return max(0.0, self.cooldown_s - (self._now() - self.opened_at))

    def public(self) -> dict[str, object]:
        return {
            "state": self.state(),
            "failures": self.failures,
            "threshold": self.threshold,
            "lastErrorCode": self.last_error_code,
            "retryAfterSeconds": self.retry_after_s(),
            "trippedCount": self.tripped_count,
            "lastTrippedAt": self.last_tripped_at,
        }


__all__ = [
    "CircuitBreaker",
    "breaker_state",
    "should_probe",
    "FAILURE_THRESHOLD",
    "COOLDOWN_S",
    "CLOSED",
    "OPEN",
    "HALF_OPEN",
    "TRANSPORT_FAILURE_CODES",
]
