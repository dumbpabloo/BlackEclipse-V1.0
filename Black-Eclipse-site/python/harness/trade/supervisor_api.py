"""The supervisor's service-facing entry points.

`trade/service.py` is pinned at its size ratchet, so the wiring lives here as
free functions over a `BrokerService`. That keeps the broker service unchanged
and the supervisor testable without a service subclass.
"""

from __future__ import annotations

from typing import Any, Optional

from .supervisor import SupervisorLoop, TradeSupervisor, interval_from_env


def bind_automations(service: Any, resolver: Any) -> None:
    """Let the supervisor pause bots through the process's automation service."""
    service._automation_resolver = resolver


def supervisor(service: Any) -> TradeSupervisor:
    automations = None
    resolver = getattr(service, "_automation_resolver", None)
    if resolver is not None:
        try:
            automations = resolver()
        except Exception:  # noqa: BLE001 - reporting still works without it
            automations = None
    return TradeSupervisor(service, automations=automations)


def supervisor_report(service: Any) -> dict[str, Any]:
    return supervisor(service).report()


def supervisor_apply(service: Any) -> dict[str, Any]:
    return supervisor(service).apply()


async def start_supervisor_loop(service: Any, interval: Optional[int] = None) -> None:
    seconds = interval_from_env() if interval is None else int(interval)
    loop = getattr(service, "_supervisor_loop", None)
    if seconds <= 0 or loop is not None:
        return
    service._supervisor_loop = SupervisorLoop(supervisor(service), seconds)
    service._supervisor_loop.start()


async def stop_supervisor_loop(service: Any) -> None:
    loop = getattr(service, "_supervisor_loop", None)
    service._supervisor_loop = None
    if loop is not None:
        await loop.stop()


__all__ = [
    "bind_automations",
    "supervisor",
    "supervisor_report",
    "supervisor_apply",
    "start_supervisor_loop",
    "stop_supervisor_loop",
]
