"""Process-local runtime state shared by the drain route and the run endpoints.

A restart must not orphan runs. The front door asks the runtime to DRAIN first:
new runs are refused, current runs finish (bounded), and only then is the
process replaced. This module owns the one flag and the bounded wait so
`main.start_run` and the drain route cannot drift apart.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

_draining = False


def is_draining() -> bool:
    return _draining


def begin_draining() -> None:
    global _draining
    _draining = True


def reset_draining() -> None:
    """Test/embedded helper: a fresh process is never draining."""
    global _draining
    _draining = False


async def wait_for_idle(service: Any, timeout_s: float = 30.0) -> dict[str, Any]:
    """Wait until no run is active, or the budget expires (honest counts)."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        active = getattr(service, "_active", {}) or {}
        if not active:
            return {"idle": True, "activeRuns": 0, "waitedMs": 0}
        await asyncio.sleep(0.1)
    active = getattr(service, "_active", {}) or {}
    return {"idle": not active, "activeRuns": len(active), "waitedMs": int(float(timeout_s) * 1000)}


__all__ = ["begin_draining", "is_draining", "reset_draining", "wait_for_idle"]
