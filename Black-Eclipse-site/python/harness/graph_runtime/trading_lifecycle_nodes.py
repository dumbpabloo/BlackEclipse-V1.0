"""Order lifecycle and reconciliation nodes (Phase 10).

- `trading.await_fill` polls Eclipse's own ledger until an order reaches a
  terminal state or a deadline passes; a timeout is reported, never guessed away.
- `trading.reconcile` refreshes the local mirror from the broker (the same
  `BrokerService.sync_connection` the sync route uses). It never sends an order
  and never changes broker state; it only copies what the broker reports.
- `trading.drift_check` compares the ledger with that mirror and reports what
  does not line up: ledger rows with no matching position/working order/deal,
  broker positions the ledger never recorded, and a mirror that is too old to
  trust. It reports drift; it never repairs it silently.
"""

from __future__ import annotations

import time
from typing import Any

from ..persistence import now_iso
from ..trade.model import ticket_ref
from .model import ExecutionMode, PortDef, PortType, SideEffect
from .registry import ConfigField, NodeDef, NodeExecContext

_PAPER_LIVE = (ExecutionMode.PAPER, ExecutionMode.LIVE)
_ALL = tuple(ExecutionMode)

#: A ledger state that will not change by itself. `placed`/`partial` are still
#: working orders, so awaiting them continues until the deadline.
_TERMINAL = {"filled", "rejected", "cancelled", "expired", "unknown"}
_IN_FLIGHT = {"submitting", "placed", "partial"}


def _service() -> Any:
    from ..trade.bootstrap import get_service  # noqa: PLC0415 - avoids an import cycle

    service = get_service()
    if service is None:
        raise ValueError("broker connectivity is unavailable in this runtime")
    return service


def _connection(context: NodeExecContext) -> str:
    connection_id = str(context.config.get("connectionId") or "").strip()
    if not connection_id:
        raise ValueError("connectionId is required")
    return connection_id


def _number(context: NodeExecContext, handle: str, config_key: str | None = None) -> float | None:
    raw = context.inputs.get(handle)
    if raw is None or raw == "":
        raw = context.config.get(config_key or handle)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{handle} must be a number")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{handle} must be a number") from exc


def _error_text(raw: Any) -> str:
    if isinstance(raw, dict):
        reason = str(raw.get("reason") or raw.get("message") or "").strip()
        code = str(raw.get("code") or "").strip()
        return f"{code}: {reason}" if code and reason else (reason or code)
    return str(raw or "")


def exec_await_fill(context: NodeExecContext) -> dict[str, Any]:
    order_id = str(context.inputs.get("orderId") or context.config.get("orderId") or "").strip()
    if not order_id:
        raise ValueError("an orderId is required (wire broker_order's orderId)")
    service = _service()
    timeout = float(context.config.get("timeoutSeconds", 30) or 30)
    poll = float(context.config.get("pollSeconds", 1.0) or 1.0)
    if timeout <= 0:
        raise ValueError("timeoutSeconds must be greater than zero")
    poll = max(0.05, min(poll, timeout))

    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        try:
            order = service.get_ledger_order(order_id)
        except KeyError:
            return {
                "order": None, "status": "", "filled": False, "timedOut": False,
                "ok": False, "reason": f"no ledger order {order_id!r}",
            }
        status = str(order.get("status") or "")
        if status in _TERMINAL:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        time.sleep(min(poll, remaining))

    filled = status == "filled"
    if filled:
        reason = ""
    elif timed_out:
        reason = f"the order was still {status or 'unknown'} after {timeout:g}s"
    else:
        reason = f"the order ended {status}"
    context.emit(
        f"await {order_id}: {status or 'unknown'}"
        + (" (timed out)" if timed_out else "")
    )
    return {
        "order": order,
        "status": status,
        "filled": filled,
        "timedOut": timed_out,
        "ok": True,
        "reason": reason,
    }


def exec_reconcile(context: NodeExecContext) -> dict[str, Any]:
    if context.test_run:
        raise ValueError(
            "a test run must not write the broker mirror; run this graph as an "
            "operator run to sync"
        )
    result = _service().sync_connection(_connection(context))
    ok = bool(result.get("ok"))
    reason = "" if ok else (_error_text(result.get("error")) or "the sync failed")
    context.emit(
        "reconcile: "
        + (
            f"{result.get('positions')} position(s), {result.get('fillsAdded')} new fill(s)"
            if ok else f"failed ({reason})"
        )
    )
    return {"result": result, "status": "synced" if ok else "failed", "ok": ok, "reason": reason}


def exec_drift_check(context: NodeExecContext) -> dict[str, Any]:
    service = _service()
    connection_id = _connection(context)
    limit = int(context.config.get("limit", 100) or 100)
    max_age = float(context.config.get("maxAgeSeconds", 300) or 0)
    if bool(context.config.get("syncFirst")):
        if context.test_run:
            raise ValueError(
                "a test run must not write the broker mirror; set syncFirst off or "
                "run this graph as an operator run"
            )
        sync = service.sync_connection(connection_id)
        if not sync.get("ok"):
            raise ValueError(
                "the pre-check sync failed: "
                + (_error_text(sync.get("error")) or "unavailable")
            )

    ledger = service.ledger_orders(connection_id, limit=limit)
    orders = service.orders(connection_id)
    if not orders.get("available"):
        raise ValueError(
            "the broker's working orders could not be read: "
            + (_error_text(orders.get("error")) or "unavailable")
        )
    reconciliation = service.reconciliation(connection_id, limit=limit)
    positions = reconciliation.get("positions") or []
    fills = reconciliation.get("fills") or []
    state = reconciliation.get("state") or {}

    working_tickets = {
        ticket_ref(row.get("ticket") or 0)
        for row in (orders.get("data") or [])
        if isinstance(row, dict)
    }
    position_tickets = {
        ticket_ref(row.get("ticket") or 0) for row in positions if isinstance(row, dict)
    }
    fill_tickets = {
        ticket_ref(row.get(key) or 0)
        for row in fills if isinstance(row, dict)
        for key in ("orderTicket", "positionTicket")
    }
    known = working_tickets | position_tickets | fill_tickets

    unmatched = [
        {
            "orderId": row.get("id"),
            "status": row.get("status"),
            "ticket": row.get("ticket"),
            "symbol": (row.get("intent") or {}).get("symbol"),
        }
        for row in ledger
        if str(row.get("status") or "") in _IN_FLIGHT
        and ticket_ref(row.get("ticket") or 0) not in known
    ]
    ledger_tickets = {ticket_ref(row.get("ticket") or 0) for row in ledger}
    untracked = [
        {
            "ticket": row.get("ticket"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "volume": row.get("volume"),
            "magic": row.get("magic"),
        }
        for row in positions
        if isinstance(row, dict) and ticket_ref(row.get("ticket") or 0) not in ledger_tickets
    ]

    synced_at = str(state.get("lastSyncedAt") or "")
    stale = False
    stale_reason = ""
    if synced_at and max_age > 0:
        from datetime import datetime, timezone  # noqa: PLC0415 - local use

        try:
            moment = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - moment).total_seconds()
            stale = age > max_age
            if stale:
                stale_reason = f"the local mirror is {int(age)}s old (max {max_age:g}s)"
        except ValueError:
            stale = False
    elif not synced_at:
        stale = True
        stale_reason = "this connection has never been synced"

    drift = bool(unmatched or untracked)
    reasons = []
    if unmatched:
        reasons.append(f"{len(unmatched)} in-flight ledger order(s) with no broker match")
    if untracked:
        reasons.append(f"{len(untracked)} broker position(s) the ledger never recorded")
    if stale_reason:
        reasons.append(stale_reason)
    context.emit(
        "drift: " + ("clean" if not reasons else "; ".join(reasons))
    )
    return {
        "drift": drift,
        "stale": stale,
        "unmatchedOrders": unmatched,
        "untrackedPositions": untracked,
        "summary": {
            "checkedAt": now_iso(),
            "ledgerRows": len(ledger),
            "brokerPositions": len(positions),
            "workingOrders": len(working_tickets),
            "lastSyncedAt": synced_at or None,
        },
        "ok": True,
        "reason": "; ".join(reasons),
    }


def build_lifecycle_nodes() -> list[NodeDef]:
    connection = ConfigField("connectionId", "string", "", max_length=80)
    return [
        NodeDef(
            type="trading.await_fill", version="1.0.0", label="Await fill",
            category="trading",
            inputs=(
                PortDef(id="orderId", name="Order id", type=PortType.STRING, optional=True),
            ),
            outputs=(
                PortDef(id="order", name="Ledger order", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                PortDef(id="filled", name="Filled", type=PortType.BOOLEAN),
                PortDef(id="timedOut", name="Timed out", type=PortType.BOOLEAN),
                PortDef(id="ok", name="OK", type=PortType.BOOLEAN),
                PortDef(id="reason", name="Reason", type=PortType.STRING),
            ),
            config_fields=(
                ConfigField("orderId", "string", "", max_length=80),
                ConfigField("timeoutSeconds", "number", 30.0, minimum=1.0, maximum=240.0),
                ConfigField("pollSeconds", "number", 1.0, minimum=0.05, maximum=60.0),
            ),
            capabilities=("trading.broker.read",),
            side_effects=SideEffect.READ,
            allowed_modes=_PAPER_LIVE,
            timeout_ms=300_000,
            executor=exec_await_fill,
            metadata={
                "docs": (
                    "Polls Eclipse's own ledger until the order is filled, rejected, "
                    "cancelled, expired or unknown, or the deadline passes. "
                    "`timedOut` is true when it is still working (a limit order that "
                    "has not filled yet) — that is an outcome, not an error. It never "
                    "sends or cancels anything."
                ),
            },
        ),
        NodeDef(
            type="trading.reconcile", version="1.0.0", label="Reconcile",
            category="trading",
            outputs=(
                PortDef(id="result", name="Sync result", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                PortDef(id="ok", name="OK", type=PortType.BOOLEAN),
                PortDef(id="reason", name="Reason", type=PortType.STRING),
            ),
            config_fields=(connection,),
            capabilities=("trading.broker.read",),
            side_effects=SideEffect.READ,
            allowed_modes=_ALL,
            timeout_ms=90_000,
            executor=exec_reconcile,
            metadata={
                "docs": (
                    "Refreshes Eclipse's durable mirror (deals, positions, sync state) "
                    "from the broker — the same path as the sync route. It copies what "
                    "the broker reports; it never sends an order and never edits broker "
                    "state. A failed sync is `ok:false` with the reason and keeps the "
                    "last good mirror."
                ),
            },
        ),
        NodeDef(
            type="trading.drift_check", version="1.0.0", label="Drift check",
            category="trading",
            outputs=(
                PortDef(id="drift", name="Drift found", type=PortType.BOOLEAN),
                PortDef(id="stale", name="Mirror stale", type=PortType.BOOLEAN),
                PortDef(id="unmatchedOrders", name="Unmatched orders", type=PortType.ARRAY),
                PortDef(id="untrackedPositions", name="Untracked positions", type=PortType.ARRAY),
                PortDef(id="summary", name="Summary", type=PortType.OBJECT),
                PortDef(id="ok", name="OK", type=PortType.BOOLEAN),
                PortDef(id="reason", name="Reason", type=PortType.STRING),
            ),
            config_fields=(
                connection,
                ConfigField("limit", "integer", 100, minimum=1, maximum=200),
                ConfigField("maxAgeSeconds", "number", 300.0, minimum=0.0, maximum=86_400.0),
                ConfigField("syncFirst", "boolean", False),
            ),
            capabilities=("trading.broker.read",),
            side_effects=SideEffect.READ,
            allowed_modes=_ALL,
            timeout_ms=90_000,
            executor=exec_drift_check,
            metadata={
                "docs": (
                    "Compares the ledger with the broker mirror: in-flight ledger "
                    "orders that match no position, working order or deal; broker "
                    "positions the ledger never recorded; and how old the mirror is. "
                    "`syncFirst` refreshes the mirror through the normal sync path "
                    "before comparing (the broker stays authoritative) — that is the "
                    "only repair offered. Remaining drift is reported, not rewritten."
                ),
            },
        ),
    ]


__all__ = ["build_lifecycle_nodes", "exec_await_fill", "exec_reconcile", "exec_drift_check"]
