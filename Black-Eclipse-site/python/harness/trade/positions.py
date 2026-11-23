"""Position lifecycle: close (full or partial) and modify brackets.

These are the risk-reducing verbs. They live in their own module because the
broker service is near its size ceiling and because the rule is simple enough to
state in one place:

  * the connection and the account behind it must agree (demo/demo, live/real),
    or the call is refused — a live position is never closed through a demo path;
  * no mandate, no approval and no arm — an exit must always be possible, even
    after a live arm has expired;
  * the broker's own position is read before and after the send, so the returned
    `verified` flag is evidence, not optimism.

`close_positions` is the "step out" path and reports a per-ticket result instead
of pretending a partial failure succeeded.
"""

from __future__ import annotations

from typing import Any, Optional

from .adapter import AdapterUnavailable
from .model import TradeRefused, ticket_ref


def _execution_adapter(service: Any, connection_id: str, action: str) -> tuple[Any, Any]:
    """The connection and an OPEN adapter, with matching demo/live modes.

    Closing or moving a bracket reduces exposure, so it deliberately does NOT
    require a live arm: an expired arm must never trap an operator in a live
    position. The mode check is still absolute — a live connection can only act
    on a real account, and a demo connection only on a demo account.
    """
    connection = service._connection(connection_id)
    expected = "real" if connection.mode.value == "live" else "demo"
    adapter = service._trading_adapter(connection)
    try:
        account_mode = str(adapter.account().get("mode") or "unknown")
    except AdapterUnavailable as exc:
        _close(adapter)
        raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    except Exception as exc:  # noqa: BLE001
        _close(adapter)
        raise TradeRefused(
            "ADAPTER_ERROR", f"the broker could not be read: {exc}", status_code=503
        ) from exc
    if account_mode != expected:
        _close(adapter)
        raise TradeRefused(
            "LIVE_ACCOUNT_REQUIRED" if expected == "real" else "DEMO_ACCOUNT_REQUIRED",
            f"the connected account is {account_mode}; a {connection.mode.value} "
            f"connection can only {action} a {expected} position",
            status_code=403,
        )
    return connection, adapter


def _close(adapter: Any) -> None:
    try:
        adapter.close()
    except Exception:  # noqa: BLE001
        pass


def close_position(
    service: Any, connection_id: str, ticket: Any, *,
    deviation: int = 20, volume: Optional[float] = None,
) -> dict[str, Any]:
    """Close all or part of ONE position. No approval, no mandate."""
    connection, adapter = _execution_adapter(service, connection_id, "closing")
    try:
        try:
            result = adapter.close_position(
                ticket_ref(ticket), deviation=int(deviation or 0), volume=volume,
            )
        except AdapterUnavailable as exc:
            raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    finally:
        _close(adapter)
    service.connections.record_event(
        connection.id,
        "close",
        "ok" if result.get("ok") else "rejected",
        {"ticket": ticket_ref(ticket), "retcode": result.get("retcode"),
         "deal": result.get("deal"), "partial": result.get("partial"),
         "verified": result.get("verified")},
    )
    return {
        "available": True,
        "closed": bool(result.get("ok")),
        "connectionId": connection.id,
        "result": result,
    }


def close_positions(
    service: Any, connection_id: str, *, tickets: Optional[list[Any]] = None,
    deviation: int = 20,
) -> dict[str, Any]:
    """Close every open position, or only the named tickets."""
    connection, adapter = _execution_adapter(service, connection_id, "closing")
    try:
        try:
            rows = adapter.positions()
        except AdapterUnavailable as exc:
            raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    finally:
        _close(adapter)
    wanted = {ticket_ref(ticket) for ticket in (tickets or [])}
    if connection.mode.value == "live" and not wanted:
        # A live "close all" means every position ECLIPSE owns, not every manual
        # position on the account. The magic is the ownership boundary the kill
        # switch already uses; without it, refuse rather than guess.
        magic = service._magic(connection)
        if magic <= 0:
            raise TradeRefused(
                "MAGIC_REQUIRED",
                "set a non-zero magic before closing all live positions; it scopes "
                "the action to Eclipse's own orders",
                status_code=400,
            )
        targets = [
            ticket_ref(row.get("ticket") or 0)
            for row in rows
            if int(row.get("magic") or 0) == magic
        ]
    else:
        targets = [
            ticket_ref(row.get("ticket") or 0)
            for row in rows
            if not wanted or ticket_ref(row.get("ticket") or 0) in wanted
        ]
    results: list[dict[str, Any]] = []
    for ticket in targets:
        try:
            outcome = close_position(service, connection_id, ticket, deviation=deviation)
            results.append(outcome.get("result") or {})
        except TradeRefused as exc:
            results.append({
                "ok": False, "ticket": ticket,
                "retcodeName": exc.code, "reasons": [exc.message],
            })
    if wanted:
        found = set(targets)
        for ticket in sorted(wanted - found, key=str):
            results.append({
                "ok": False, "ticket": ticket,
                "retcodeName": "POSITION_NOT_FOUND",
                "reasons": [f"no open position with ticket {ticket}"],
            })
    closed = sum(1 for result in results if result.get("ok"))
    return {
        "available": True,
        "connectionId": connection.id,
        "closed": closed,
        "failed": len(results) - closed,
        "requested": len(wanted) if wanted else len(targets) if connection.mode.value == "live" else len(rows),
        "results": results,
    }


def modify_position(
    service: Any, connection_id: str, ticket: Any, *,
    stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
) -> dict[str, Any]:
    """Move a position's stop and/or target. No approval, no mandate.

    A wider stop can increase risk, so the tool text warns; the broker still enforces
    its own stops distance and the result is verified against the stored bracket.
    """
    if stop_loss is None and take_profit is None:
        raise TradeRefused(
            "INVALID_STOPS", "pass stopLoss and/or takeProfit", status_code=400
        )
    connection, adapter = _execution_adapter(service, connection_id, "modifying")
    try:
        try:
            result = adapter.modify_position(
                ticket_ref(ticket), stop_loss=stop_loss, take_profit=take_profit,
            )
        except AdapterUnavailable as exc:
            raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    finally:
        _close(adapter)
    service.connections.record_event(
        connection.id,
        "modify",
        "ok" if result.get("ok") else "rejected",
        {"ticket": ticket_ref(ticket), "retcode": result.get("retcode"),
         "stopLoss": result.get("stopLoss"),
         "takeProfit": result.get("takeProfit"),
         "verified": result.get("verified")},
    )
    return {
        "available": True,
        "modified": bool(result.get("ok")),
        "connectionId": connection.id,
        "result": result,
    }


def modify_order(
    service: Any, connection_id: str, ticket: Any, *,
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> dict[str, Any]:
    """Change a WORKING order's price/bracket. Risk-reducing: no approval.

    Like `modify_position`, re-pricing or re-bracketing a pending order can only
    reduce or re-scope exposure, so it needs no mandate, approval or live arm —
    an expired arm must never leave a stale order unpriced. The order's own
    ticket is the join key, the connection and account modes must agree, and the
    adapter reads the order back before claiming it stored the change.
    """
    if price is None and stop_loss is None and take_profit is None:
        raise TradeRefused(
            "INVALID_REQUEST", "pass price, stopLoss and/or takeProfit", status_code=400
        )
    connection, adapter = _execution_adapter(service, connection_id, "modifying")
    try:
        try:
            result = adapter.modify_order(
                ticket_ref(ticket), price=price,
                stop_loss=stop_loss, take_profit=take_profit,
            )
        except AdapterUnavailable as exc:
            raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    finally:
        _close(adapter)
    service.connections.record_event(
        connection.id,
        "modify-order",
        "ok" if result.get("ok") else "rejected",
        {"ticket": ticket_ref(ticket), "retcode": result.get("retcode"),
         "price": result.get("price"), "verified": result.get("verified")},
    )
    return {
        "available": True,
        "modified": bool(result.get("ok")),
        "connectionId": connection.id,
        "result": result,
    }


__all__ = ["close_position", "close_positions", "modify_position", "modify_order"]
