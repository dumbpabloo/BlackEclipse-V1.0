"""Broker reconciliation: fills, positions and order-state drift.

The broker is authoritative. This module reads its history and writes a durable
local copy so the UI and the ledger can answer "what actually happened?" without
pretending a read-on-demand is a source of truth.

`BrokerReconciler.sync()` is safe to call repeatedly: fills are deduped by
`(connectionId, dealTicket)`, positions are replaced as a snapshot, and ledger
orders are updated only when the broker's state is definitive. A failure records
an error state and never fabricates success.

The optional `ReconcilerLoop` is OFF by default (`TRADING_RECONCILE_SECONDS=0`):
background IPC to a desktop terminal must be opted into, not assumed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from typing import Any, Optional

from .adapter import AdapterUnavailable, BrokerAdapter, TradingAdapter
from .model import ticket_ref
from .registry import make_adapter

log = logging.getLogger("harness.trade.reconcile")

_OPEN_LEDGER_STATES = {"submitting", "unknown", "placed", "partial"}
_STATE_TO_LEDGER = {
    "filled": "filled",
    "done": "filled",
    "closed": "filled",
    "partial": "partial",
    "partially_filled": "partial",
    "partially-filled": "partial",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "rejected": "rejected",
    "expired": "expired",
    "placed": "placed",
    "started": "placed",
    "new": "placed",
    "open": "placed",
    "working": "placed",
    "accepted": "placed",
    "pending_new": "placed",
}


class BrokerReconciler:
    def __init__(self, brokers: Any) -> None:
        self.brokers = brokers
        self.store = brokers.connections

    def sync(self, connection_id: str, *, history_days: Optional[int] = None) -> dict[str, Any]:
        """Refresh the mirror.

        `history_days` asks for an explicit backfill window instead of the
        advisory incremental cursor. It is read-only against the broker and
        deduped by `(connectionId, dealTicket)`, so re-running is safe.
        """
        connection = self.store.get(connection_id)
        if connection is None:
            raise KeyError(f"no broker connection {connection_id!r}")
        try:
            adapter = make_adapter(connection.kind, connection.config)
        except Exception as exc:  # noqa: BLE001 - unknown kind is an honest error
            self.store.set_sync_state(connection.id, status="error", error=str(exc))
            return {"ok": False, "connectionId": connection.id, "error": str(exc)}
        try:
            positions = adapter.positions()
            working = adapter.orders()
            if not isinstance(adapter, BrokerAdapter):
                fills = []
            elif history_days is not None:
                fills = adapter.deals_window(int(history_days))
            else:
                fills = adapter.deals_since(self.store.last_deal_at(connection.id))
            added = self.store.save_fills(connection.id, fills)
            self.store.replace_positions(connection.id, positions)
            updated = self._reconcile_ledger(adapter, connection.id, working)
            last_deal = max(
                [int(fill.get("dealtAt") or 0) for fill in fills] + [0]
            ) or self.store.last_deal_at(connection.id)
            self.store.set_sync_state(
                connection.id,
                status="ok",
                positions_count=len(positions),
                fills_added=added,
                last_deal_at=last_deal,
            )
            self.store.record_event(
                connection.id, "sync", "ok",
                {"fillsAdded": added, "positions": len(positions), "ordersUpdated": updated},
            )
            return {
                "ok": True,
                "connectionId": connection.id,
                "positions": len(positions),
                "workingOrders": len(working),
                "fillsAdded": added,
                "ordersUpdated": updated,
                "lastDealAt": last_deal,
            }
        except AdapterUnavailable as exc:
            self.store.set_sync_state(connection.id, status="unavailable", error=exc.reason)
            self.store.record_event(connection.id, "sync", "unavailable", exc.to_dict())
            return {
                "ok": False,
                "connectionId": connection.id,
                "error": exc.to_dict(),
            }
        except Exception as exc:  # noqa: BLE001 - a failed sync is not a fake success
            self.store.set_sync_state(connection.id, status="error", error=str(exc))
            self.store.record_event(connection.id, "sync", "error", {"error": str(exc)})
            return {"ok": False, "connectionId": connection.id, "error": str(exc)}
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass

    def _reconcile_ledger(self, adapter: Any, connection_id: str, working: list[dict[str, Any]]) -> int:
        """Update in-flight ledger orders from broker history; never guesses."""
        if not isinstance(adapter, TradingAdapter):
            return 0
        working_tickets = {ticket_ref(row.get("ticket") or 0) for row in working}
        updated = 0
        for order in self.store.list_orders(connection_id, limit=200):
            if order.get("status") not in _OPEN_LEDGER_STATES:
                continue
            ticket = ticket_ref(order.get("ticket") or 0)
            if ticket in ("", "0", 0):
                continue
            # A still-working order is already in the broker's working set.
            if ticket in working_tickets:
                if order.get("status") != "placed":
                    self.store.update_order(order["id"], status="placed")
                    updated += 1
                continue
            try:
                state = adapter.order_status(ticket)
            except AdapterUnavailable:
                # A venue that cannot answer for this id is not evidence that
                # the order is gone; leave the ledger row untouched.
                continue
            if not state.get("found"):
                continue
            target = _STATE_TO_LEDGER.get(
                str(state.get("stateName") or "").strip().lower(), None
            )
            if target and target != order.get("status"):
                self.store.update_order(order["id"], status=target)
                updated += 1
        return updated

    def sync_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for connection in self.store.list():
            if connection.status not in ("connected", "unavailable"):
                continue
            results.append(self.sync(connection.id))
        return results


class ReconcilerLoop:
    """One best-effort background task. Opt-in via TRADING_RECONCILE_SECONDS."""

    def __init__(self, reconciler: BrokerReconciler, interval_seconds: int) -> None:
        self.reconciler = reconciler
        self.interval = max(15, int(interval_seconds))
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.to_thread(self.reconciler.sync_all)
            except Exception:  # noqa: BLE001 - a loop must outlive one bad sync
                log.warning("broker reconciliation pass failed", exc_info=True)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return

    def start(self) -> None:
        if self._task is not None or self._stopped:
            return
        self._task = asyncio.create_task(self._run(), name="broker-reconciler")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def interval_from_env() -> int:
    try:
        return max(0, int(os.environ.get("TRADING_RECONCILE_SECONDS", "0") or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["BrokerReconciler", "ReconcilerLoop", "interval_from_env"]
