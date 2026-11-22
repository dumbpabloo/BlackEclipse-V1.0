"""Provider routing — capability-aware selection with health + circuit breaking.

The gateway asks the router "which providers, in what order?" for a capability +
instrument, and reports each attempt's success/failure back. Selection combines:

  * configuration priority (per capability, from `config`),
  * declared capability + asset-class fit (from `providers`),
  * per-provider circuit-breaker state (a tripped provider is skipped),
  * operator-disabled providers.

It reuses `mcp.circuit.CircuitBreaker` verbatim — there is no second breaker.
The router executes nothing; it is pure selection + bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..mcp.circuit import CircuitBreaker, backoff_cooldown_s
from .config import get_config
from .instruments import CanonicalInstrument
from .providers import ProviderDescriptor, default_providers


class ProbeBudget:
    """One half-open recovery probe per (scope, provider) per cooldown.

    Scope is the routing intent (for example ``"quote:crypto"``), not the whole
    process: a provider that is broken for crypto may still be healthy for US
    equities, and one scope's failure must not spend another scope's probe.

    Cross-scope evidence: a recent SUCCESS for the same provider in a DIFFERENT
    scope makes this scope probe-ready earlier. It never clears this scope's
    failures and never bypasses the one-probe-per-cooldown budget — the provider
    still has to prove itself for the scope that is actually being routed.
    """

    def __init__(self, cooldown_s: float, clock=None) -> None:
        import time  # noqa: PLC0415 - the default is only paid when used

        self.cooldown_s = float(cooldown_s)
        self._clock = clock or time.monotonic
        self._probes: dict[tuple[str, str], float] = {}
        self._successes: dict[tuple[str, str], float] = {}

    def claim(self, scope: str, provider: str) -> bool:
        """Whether this caller may spend the scope's single probe now."""
        now = self._clock()
        key = (scope, provider)
        last = self._probes.get(key)
        if last is not None and now - last < self.cooldown_s:
            return False
        self._probes[key] = now
        return True

    def record_success(self, scope: str, provider: str) -> None:
        self._successes[(scope, provider)] = self._clock()

    def record_failure(self, scope: str, provider: str) -> None:
        # A failure keeps the probe timestamp (the cooldown started at the claim)
        # and grants no cross-scope credit.
        self._successes.pop((scope, provider), None)

    def evidence_elsewhere(self, scope: str, provider: str) -> bool:
        """A recent success for `provider` in another scope."""
        now = self._clock()
        return any(
            other_scope != scope
            and other_provider == provider
            and now - at <= self.cooldown_s
            for (other_scope, other_provider), at in self._successes.items()
        )

    def snapshot(self) -> dict[str, float]:
        return {f"{scope}|{provider}": at for (scope, provider), at in self._probes.items()}


@dataclass
class ProviderHealth:
    provider: str
    breaker: CircuitBreaker
    successes: int = 0
    failures: int = 0
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "successes": self.successes,
            "failures": self.failures,
            "lastSuccessAt": self.last_success_at,
            "lastFailureAt": self.last_failure_at,
            "breaker": self.breaker.public(),
        }


class ProviderRouter:
    """Deterministic provider selection + health tracking."""

    def __init__(
        self,
        providers: Optional[list[ProviderDescriptor]] = None,
        *,
        breaker_threshold: Optional[int] = None,
        breaker_reset_s: Optional[float] = None,
        breaker_lifetime_s: Optional[float] = None,
    ) -> None:
        self._providers: dict[str, ProviderDescriptor] = {
            p.id: p for p in (providers if providers is not None else default_providers())
        }
        cfg = get_config()
        base = breaker_reset_s if breaker_reset_s is not None else cfg.breaker_reset_s
        lifetime = (
            breaker_lifetime_s if breaker_lifetime_s is not None
            else getattr(cfg, "breaker_lifetime_s", 300.0)
        )
        self._health: dict[str, ProviderHealth] = {
            pid: ProviderHealth(
                provider=pid,
                breaker=CircuitBreaker(
                    threshold=breaker_threshold if breaker_threshold is not None else cfg.breaker_fail_threshold,
                    cooldown_s=base,
                    # Per-provider failures back off exponentially up to the lifetime,
                    # so a provider that has been down for an hour is not retried at
                    # the same cadence as one that failed once.
                    lifetime_s=lifetime,
                ),
            )
            for pid in self._providers
        }
        self._probes = ProbeBudget(cooldown_s=base)

    def get(self, provider: str) -> Optional[ProviderDescriptor]:
        return self._providers.get(provider)

    def all(self) -> list[ProviderDescriptor]:
        return list(self._providers.values())

    def provider_order(self, capability: str, instrument: Optional[CanonicalInstrument]) -> tuple[str, ...]:
        """Ordered, capability-fitting, non-disabled providers for a request.

        Only providers that declare the capability AND fit the instrument's
        asset class are returned; configured priority orders them.
        """
        asset_type = instrument.asset_type if instrument else None
        configured = get_config().provider_order(capability)
        # Providers declared for the capability but not in config priority are
        # appended so a newly-added provider is still routable.
        ordered = list(configured)
        for pid, descriptor in self._providers.items():
            if pid not in ordered and descriptor.implemented and descriptor.supports(capability, asset_type):
                ordered.append(pid)
        out: list[str] = []
        for pid in ordered:
            descriptor = self._providers.get(pid)
            if descriptor is None or not descriptor.implemented:
                continue
            if not descriptor.supports(capability, asset_type):
                continue
            out.append(pid)
        return tuple(out)

    def attempt_order(
        self, capability: str, instrument: Optional[CanonicalInstrument], *, scope: Optional[str] = None,
    ) -> tuple[str, ...]:
        """Providers to TRY now: an open breaker is skipped unless a probe is due.

        `provider_order` is the preference order and never consults health; this
        is the execution order. A provider whose breaker is OPEN rejoins only
        when a recent success for it in ANOTHER scope grants this scope one probe
        (budgeted once per cooldown). That is evidence, not a reset: the breaker
        still counts the failure, and a failed probe re-arms the cooldown.
        """
        order = self.provider_order(capability, instrument)
        if not order:
            return order
        key = scope or f"{capability}:{getattr(instrument, 'asset_type', None) or 'any'}"
        out: list[str] = []
        for pid in order:
            health = self._health.get(pid)
            if health is None:
                continue
            if health.breaker.allow():
                out.append(pid)
                continue
            if (
                health.breaker.state() == "open"
                and self._probes.evidence_elsewhere(key, pid)
                and self._probes.claim(key, pid)
            ):
                out.append(pid)
        return tuple(out)

    def allow(self, provider: str) -> bool:
        health = self._health.get(provider)
        if health is None:
            return False
        return health.breaker.allow()

    def record_success(self, provider: str, *, scope: Optional[str] = None) -> None:
        health = self._health.get(provider)
        if health is None:
            return
        health.successes += 1
        health.last_success_at = _iso_now()
        health.breaker.record_success()
        if scope:
            self._probes.record_success(scope, provider)

    def record_failure(self, provider: str, *, scope: Optional[str] = None) -> None:
        health = self._health.get(provider)
        if health is None:
            return
        health.failures += 1
        health.last_failure_at = _iso_now()
        if scope:
            self._probes.record_failure(scope, provider)
        if health.breaker.state() == "half_open" or health.breaker.probe_in_flight:
            health.breaker.record_probe_failure(None, now_iso=health.last_failure_at)
        else:
            health.breaker.record_failure(None, now_iso=health.last_failure_at)

    def record_rate_limited(self, provider: str, retry_after_s: Optional[float] = None) -> None:
        health = self._health.get(provider)
        if health is None:
            return
        health.failures += 1
        health.last_failure_at = _iso_now()
        # A 429 counts toward the breaker and opens a cooldown immediately.
        health.breaker.record_failure("RATE_LIMITED", now_iso=health.last_failure_at)
        if retry_after_s and retry_after_s > 0:
            health.breaker.opened_at = health.breaker._now() - max(0.0, health.breaker.cooldown_s - retry_after_s)  # noqa: SLF001

    def health_snapshot(self) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self._health.values()]

    def describe(self) -> dict[str, Any]:
        return {
            "providers": [p.to_dict() for p in self._providers.values()],
            "health": self.health_snapshot(),
        }


_default_router: Optional[ProviderRouter] = None


def default_router() -> ProviderRouter:
    """The process-wide router (one health ledger for the whole process)."""
    global _default_router
    if _default_router is None:
        _default_router = ProviderRouter()
    return _default_router


def reset_router() -> None:
    """Rebuild on next read. Tests only."""
    global _default_router
    _default_router = None


def _iso_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


__all__ = ["ProviderHealth", "ProviderRouter", "default_router", "reset_router"]
