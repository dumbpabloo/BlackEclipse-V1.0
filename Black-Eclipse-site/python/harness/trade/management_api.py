"""The stop manager's service-facing sweep and opt-in background loop.

Like the supervisor's API, this keeps the wiring out of `trade/service.py` (which
is pinned at a size ratchet). The sweep reads each managed connection's
`config.management`, fetches the broker's own quote/bars per position, and applies
ONLY risk-reducing actions through the existing `modify_position` / `close_position`
paths. It never opens, adds to or hedges exposure.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .management import ManagementPolicy, PositionManager

log = logging.getLogger("harness.trade.management")


class ManagementSweeper:
    """One best-effort pass over every connection with a management policy."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._managers: dict[str, PositionManager] = {}

    def _manager(self, connection_id: str, policy: ManagementPolicy) -> PositionManager:
        manager = self._managers.get(connection_id)
        if manager is None:
            manager = PositionManager(self.service, policy)
            self._managers[connection_id] = manager
        else:
            manager.policy = policy
        return manager

    def sweep(self, now: Optional[float] = None) -> dict[str, Any]:
        connections = self.service.connections.list()
        managed_ids: set[str] = set()
        reports: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for connection in connections:
            policy = ManagementPolicy.from_config(
                (getattr(connection, "config", None) or {}).get("management"),
            )
            if policy.mode == "off":
                continue
            managed_ids.add(connection.id)
            try:
                result = self.service.positions(connection.id)
            except Exception as exc:  # noqa: BLE001 - one unreadable account must not stop the sweep
                errors.append({"connectionId": connection.id, "error": str(exc)})
                continue
            rows = result.get("data") if result.get("available") else []
            if not isinstance(rows, list):
                rows = []

            manager = self._manager(connection.id, policy)
            quotes: dict[str, Any] = {}
            bar_cache: dict[str, Any] = {}
            for position in rows:
                if not isinstance(position, dict):
                    continue
                symbol = str(position.get("symbol") or "").strip()
                if not symbol:
                    continue
                try:
                    if symbol not in quotes:
                        envelope = self.service.quote(connection.id, symbol)
                        quotes[symbol] = envelope.get("data") if envelope.get("available") else None
                    bars = None
                    if policy.mode == "atr_trail":
                        if symbol not in bar_cache:
                            envelope = self.service.bars(
                                connection.id, symbol, timeframe="H1",
                                count=max(40, policy.atr_period * 3),
                            )
                            data = envelope.get("data") if envelope.get("available") else None
                            bar_cache[symbol] = (
                                data.get("bars") if isinstance(data, dict) else None
                            )
                        bars = bar_cache[symbol]
                    report = manager.manage(
                        connection.id, [position], quote=quotes[symbol],
                        bars=bars, now=now,
                    )
                    reports.append(report)
                except Exception as exc:  # noqa: BLE001 - report, never crash the sweep
                    errors.append({
                        "connectionId": connection.id,
                        "ticket": str(position.get("ticket")),
                        "error": str(exc),
                    })

        # Drop state for connections that are no longer managed.
        for connection_id in list(self._managers):
            if connection_id not in managed_ids:
                del self._managers[connection_id]

        return {
            "connections": len(managed_ids),
            "positions": sum(report.get("positions", 0) for report in reports),
            "managed": sum(report.get("managed", 0) for report in reports),
            "reports": reports,
            "errors": errors,
        }


class ManagementLoop:
    """One periodic sweep. Opt-in via TRADING_MANAGEMENT_SECONDS (default 0)."""

    def __init__(self, service: Any, interval_seconds: int) -> None:
        self.service = service
        self.interval = max(30, int(interval_seconds))
        self.sweeper = ManagementSweeper(service)
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.to_thread(self.sweeper.sweep)
            except Exception:  # noqa: BLE001 - a loop must outlive one bad pass
                log.warning("trading management pass failed", exc_info=True)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return

    def start(self) -> None:
        if self._task is not None or self._stopped:
            return
        self._task = asyncio.create_task(self._run(), name="trading-management")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def interval_from_env() -> int:
    try:
        return max(0, int(os.environ.get("TRADING_MANAGEMENT_SECONDS", "0") or 0))
    except (TypeError, ValueError):
        return 0


async def start_management_loop(service: Any, interval: Optional[int] = None) -> None:
    seconds = interval_from_env() if interval is None else int(interval)
    loop = getattr(service, "_management_loop", None)
    if seconds <= 0 or loop is not None:
        return
    service._management_loop = ManagementLoop(service, seconds)
    service._management_loop.start()


async def stop_management_loop(service: Any) -> None:
    loop = getattr(service, "_management_loop", None)
    service._management_loop = None
    if loop is not None:
        await loop.stop()


def management_report(service: Any) -> dict[str, Any]:
    """A read-only sweep an operator or tool can request on demand."""
    return ManagementSweeper(service).sweep()


__all__ = [
    "ManagementLoop",
    "ManagementSweeper",
    "interval_from_env",
    "management_report",
    "start_management_loop",
    "stop_management_loop",
]
