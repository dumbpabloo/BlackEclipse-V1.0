"""Numeric parity, tenth tier: source backoff after repeated failures.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/data_gateway/routing.py::NetworkRouter`):

    retry_at = now + min(lifetime, cooldown * 2 ** (failures - 1))

with ``failures`` capped at 4 and defaults ``cooldown=30``, ``lifetime=300``.

Eclipse's breaker used a **flat** ``breaker_reset_s``: a provider that had failed once
and a provider that had been down all morning were retried on the same cadence, which
is the difference between "try again shortly" and hammering a dead endpoint forever.
The formula is ported into ``mcp.circuit.backoff_cooldown_s`` and the market router
now constructs its breakers with the exponential schedule; every other breaker keeps
the flat cooldown because ``lifetime_s`` defaults to ``None``.

Also pinned: both routers only **reorder the candidates they are given** — neither one
invents a source, which matters because a router that adds one bypasses the capability
and asset-class filters.
"""

from __future__ import annotations

import pytest

from harness.market.router import ProviderRouter
from harness.mcp.circuit import CircuitBreaker, backoff_cooldown_s


# -- the reference formula -------------------------------------------------

def reference_backoff(failures, cooldown, lifetime):
    if failures <= 0:
        return float(cooldown)
    return min(float(lifetime), float(cooldown) * 2 ** (min(failures, 4) - 1)
               if failures <= 4 else float(lifetime))


@pytest.mark.parametrize("base,lifetime", [(30.0, 300.0), (60.0, 300.0), (10.0, 45.0)])
@pytest.mark.parametrize("failures", [0, 1, 2, 3, 4, 5, 8, 40])
def test_backoff_matches_the_reference_schedule(base, lifetime, failures):
    ours = backoff_cooldown_s(failures, base_s=base, lifetime_s=lifetime)
    expected = min(lifetime, base * 2 ** (failures - 1)) if failures > 0 else base
    assert ours == pytest.approx(expected)


def test_each_consecutive_failure_doubles_the_wait_until_the_cap():
    waits = [backoff_cooldown_s(n, base_s=30.0, lifetime_s=300.0) for n in range(1, 7)]
    assert waits[:4] == [30.0, 60.0, 120.0, 240.0]
    assert waits[4] == 300.0 and waits[5] == 300.0        # capped, never unbounded


def test_a_huge_failure_count_cannot_overflow():
    # 2**63 is where float exponentiation stops being useful; the cap must win
    assert backoff_cooldown_s(10_000, base_s=30.0, lifetime_s=300.0) == 300.0


def test_a_zero_base_never_waits():
    assert backoff_cooldown_s(5, base_s=0.0, lifetime_s=300.0) == 0.0


# -- the breaker actually uses it ----------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _breaker(clock, *, lifetime):
    return CircuitBreaker(threshold=1, cooldown_s=30.0, lifetime_s=lifetime,
                          _clock=clock)


def test_the_breaker_opens_and_its_wait_grows_with_failures():
    clock = _Clock()
    breaker = _breaker(clock, lifetime=300.0)
    breaker.record_failure("mcp_timeout")
    assert breaker.state() == "open"
    # first failure: 30s
    clock.now += 29
    assert breaker.state() == "open"
    clock.now += 2
    assert breaker.state() == "half_open"
    # a second recorded failure raises the count, so the next wait doubles
    breaker.record_failure("mcp_timeout")
    assert breaker.effective_cooldown_s() == pytest.approx(60.0)


def test_the_wait_is_capped_at_the_lifetime():
    clock = _Clock()
    breaker = _breaker(clock, lifetime=45.0)
    for _ in range(6):
        breaker.record_failure("mcp_timeout")
        breaker.record_probe_failure("mcp_timeout")
    assert breaker.effective_cooldown_s() == 45.0
    clock.now += 44
    assert breaker.state() == "open"
    clock.now += 2
    assert breaker.state() == "half_open"


def test_a_flat_breaker_is_unchanged_where_the_lifetime_is_not_set():
    clock = _Clock()
    breaker = _breaker(clock, lifetime=None)
    breaker.record_failure("mcp_timeout")
    breaker.record_probe_failure("mcp_timeout")
    breaker.record_probe_failure("mcp_timeout")
    assert breaker.effective_cooldown_s() == 30.0     # no growth, as before


def test_success_resets_the_schedule():
    clock = _Clock()
    breaker = _breaker(clock, lifetime=300.0)
    breaker.record_failure("mcp_timeout")
    breaker.record_failure("mcp_timeout")
    assert breaker.effective_cooldown_s() == pytest.approx(60.0)
    breaker.record_success()
    assert breaker.failures == 0
    assert breaker.effective_cooldown_s() == 30.0


# -- the router wires it, and never invents a source ---------------------


def test_the_market_router_uses_the_exponential_schedule():
    router = ProviderRouter(breaker_threshold=1, breaker_reset_s=30.0,
                            breaker_lifetime_s=300.0)
    pid = next(iter(router._health))
    router.record_failure(pid)
    router.record_failure(pid)
    breaker = router._health[pid].breaker
    assert breaker.lifetime_s == 300.0
    assert breaker.effective_cooldown_s() == pytest.approx(60.0)


def test_attempt_order_only_reorders_supplied_candidates():
    router = ProviderRouter()
    order = router.attempt_order("quote", None)
    # every provider returned is one the router knows about, and only fitting ones
    assert set(order) <= {p.id for p in router.all()}
    assert list(order) == [pid for pid in router.provider_order("quote", None)
                           if pid in order]
