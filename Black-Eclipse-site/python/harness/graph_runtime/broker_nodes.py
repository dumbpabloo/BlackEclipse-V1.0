"""Broker read and risk-reducing nodes (Phase 6).

These are thin adapters over the EXISTING `BrokerService` (the same paths the
chat tools and REST surface use). Reads are honest envelopes: a missing or
logged-out terminal is a named failure, never a fabricated account or price.

Live ORDER submission stays in `trading.broker_order`; it requires an armed live
connection plus a workflow-pinned live mandate. Close/modify/cancel only reduce
or re-scope exposure, so they remain arm-free and mandate-free — an expired arm
must never trap a position.
"""

from __future__ import annotations

from typing import Any

from ..trade.analysis import bar_summary
from ..trade.model import ticket_ref
from .model import ExecutionMode, PortDef, PortType, SideEffect
from .registry import ConfigField, NodeDef, NodeExecContext

_ALL = tuple(ExecutionMode)
_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")


def _service() -> Any:
    from ..trade.bootstrap import get_service  # noqa: PLC0415 - avoids an import cycle

    service = get_service()
    if service is None:
        raise ValueError("broker connectivity is unavailable in this runtime")
    return service


def _ticket_id(context: NodeExecContext, handle: str = "ticket"):
    """A ticket can be an MT5 integer or a REST venue's opaque id."""
    raw = context.inputs.get(handle)
    if raw in (None, ""):
        raw = context.config.get(handle)
    if raw in (None, ""):
        return None
    return ticket_ref(raw)


def _connection(context: NodeExecContext) -> str:
    connection_id = str(context.config.get("connectionId") or "").strip()
    if not connection_id:
        raise ValueError("connectionId is required")
    return connection_id


def _symbol(context: NodeExecContext) -> str:
    raw = context.inputs.get("symbol") or context.config.get("symbol") or ""
    symbol = str(raw).strip().upper()
    if not symbol:
        raise ValueError("a symbol is required")
    return symbol


def _number(context: NodeExecContext, handle: str, config_key: str | None = None) -> float | None:
    raw = context.inputs.get(handle)
    if raw is None and config_key:
        raw = context.config.get(config_key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{handle} must be a number") from exc


def _reason(result: dict[str, Any] | None) -> str:
    """The broker's own explanation, or nothing.

    The service nests the adapter verdict under `result` for close/modify/cancel
    and puts it at the top level for reads, so both are inspected.
    """
    if not isinstance(result, dict):
        return ""
    error = result.get("error")
    if isinstance(error, dict):
        text = str(error.get("reason") or error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        if text:
            return f"{code}: {text}" if code else text
        if code:
            return code
    nested = result.get("result")
    for source in (result, nested if isinstance(nested, dict) else {}):
        reasons = [str(item) for item in (source.get("reasons") or []) if str(item).strip()]
        if reasons:
            return "; ".join(reasons)
        name = str(source.get("retcodeName") or "").strip()
        if name:
            return name
    return ""


def _batch_close(result: dict[str, Any]) -> dict[str, Any]:
    """One verdict for a multi-ticket close, naming every ticket that failed."""
    closed = int(result.get("closed") or 0)
    failures = [
        row for row in (result.get("results") or [])
        if isinstance(row, dict) and not row.get("ok")
    ]
    ok = closed > 0 and not failures
    if ok:
        reason = ""
    elif failures:
        reason = "; ".join(
            f"ticket {row.get('ticket')}: {_reason(row) or 'refused'}" for row in failures
        )
    else:
        reason = _reason(result) or "no position was closed"
    return {
        "result": result,
        "status": "closed" if ok else "rejected",
        "ok": ok,
        "reason": reason,
    }


def _read_output(handle: str, result: dict[str, Any]) -> dict[str, Any]:
    """A read envelope as an honest `ok`/`reason` pair plus the raw payload."""
    ok = bool(result.get("available"))
    return {
        handle: result,
        "status": "ok" if ok else "unavailable",
        "ok": ok,
        "reason": "" if ok else (_reason(result) or "the broker is unavailable"),
    }


def exec_broker_account(context: NodeExecContext) -> dict[str, Any]:
    return _read_output("account", _service().account(_connection(context)))


def exec_broker_quote(context: NodeExecContext) -> dict[str, Any]:
    return _read_output("quote", _service().quote(_connection(context), _symbol(context)))


def exec_broker_positions(context: NodeExecContext) -> dict[str, Any]:
    result = _service().positions(_connection(context))
    ok = bool(result.get("available"))
    rows = result.get("data") if isinstance(result.get("data"), list) else []
    return {
        "positions": rows if ok else [],
        "count": len(rows) if ok else 0,
        "status": "ok" if ok else "unavailable",
        "ok": ok,
        "reason": "" if ok else (_reason(result) or "the broker is unavailable"),
    }


def exec_broker_orders(context: NodeExecContext) -> dict[str, Any]:
    result = _service().orders(_connection(context))
    ok = bool(result.get("available"))
    rows = result.get("data") if isinstance(result.get("data"), list) else []
    return {
        "orders": rows if ok else [],
        "count": len(rows) if ok else 0,
        "status": "ok" if ok else "unavailable",
        "ok": ok,
        "reason": "" if ok else (_reason(result) or "the broker is unavailable"),
    }


def exec_broker_fills(context: NodeExecContext) -> dict[str, Any]:
    """Reconciled broker deals (durable copy). Empty until the first sync."""
    try:
        limit = int(context.config.get("limit", 50) or 50)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be a whole number") from exc
    rows = _service().list_fills(_connection(context), limit=limit)
    return {"fills": rows, "count": len(rows), "status": "ok", "ok": True, "reason": ""}


def exec_order_ledger(context: NodeExecContext) -> dict[str, Any]:
    """Eclipse's own submitted-order ledger, newest first."""
    connection_id = str(context.config.get("connectionId") or "").strip() or None
    try:
        limit = int(context.config.get("limit", 50) or 50)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be a whole number") from exc
    rows = _service().ledger_orders(connection_id, limit=limit)
    return {"orders": rows, "count": len(rows), "status": "ok", "ok": True, "reason": ""}


def exec_broker_bars(context: NodeExecContext) -> dict[str, Any]:
    timeframe = str(context.config.get("timeframe") or "H1")
    try:
        count = int(context.config.get("count", 120))
    except (TypeError, ValueError) as exc:
        raise ValueError("count must be a whole number") from exc
    result = _service().bars(
        _connection(context), _symbol(context), timeframe=timeframe, count=count,
    )
    output = _read_output("bars", result)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    summary = bar_summary(data.get("bars") or [])
    output["summary"] = summary
    output["atr"] = summary.get("atr")
    output["contractSize"] = data.get("contractSize") or 0
    output["tickSize"] = data.get("tickSize") or 0
    return output


def exec_broker_symbol(context: NodeExecContext) -> dict[str, Any]:
    """Static symbol facts (trade mode, contract, volume limits) — no tick needed."""
    result = _service().symbol_info(_connection(context), _symbol(context))
    ok = bool(result.get("available"))
    payload = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "symbol": result,
        "tradeMode": str(payload.get("tradeMode") or ""),
        "status": "ok" if ok else "unavailable",
        "ok": ok,
        "reason": "" if ok else (_reason(result) or "the broker is unavailable"),
    }


def exec_broker_modify_order(context: NodeExecContext) -> dict[str, Any]:
    """Re-price or re-bracket a WORKING order. Risk-reducing: no mandate."""
    ticket = _ticket_id(context)
    if ticket is None:
        raise ValueError("broker modify-order needs a ticket input")
    price = _number(context, "price")
    stop_loss = _number(context, "stopLoss")
    take_profit = _number(context, "takeProfit")
    if price is None and stop_loss is None and take_profit is None:
        raise ValueError("pass a price, a stopLoss and/or a takeProfit to modify")
    result = _service().modify_order(
        _connection(context), ticket,
        price=price, stop_loss=stop_loss, take_profit=take_profit,
    )
    modified = bool(result.get("modified"))
    detail = result.get("result") if isinstance(result.get("result"), dict) else {}
    context.emit(
        f"modify order {ticket}: "
        + (f"stored @ {detail.get('price')}" if modified else f"refused ({_reason(result) or 'not modified'})")
    )
    return {
        "result": result,
        "status": "modified" if modified else "rejected",
        "ok": modified,
        "reason": "" if modified else (_reason(result) or "the broker did not modify it"),
    }


def exec_broker_close(context: NodeExecContext) -> dict[str, Any]:
    service = _service()
    connection_id = _connection(context)
    ticket = _ticket_id(context)
    volume = _number(context, "volume")
    symbol = str(context.inputs.get("symbol") or context.config.get("symbol") or "").strip().upper()
    deviation = int(context.config.get("deviation", 20) or 20)
    if bool(context.config.get("closeAll")):
        return _batch_close(service.close_positions(connection_id, deviation=deviation))
    if ticket is not None:
        result = service.close_position(
            connection_id, ticket, volume=volume, deviation=deviation,
        )
        closed = bool(result.get("closed"))
        return {
            "result": result,
            "status": "closed" if closed else "rejected",
            "ok": closed,
            "reason": "" if closed else (_reason(result) or "the broker did not close it"),
        }
    if symbol:
        positions = service.positions(connection_id)
        rows = positions.get("data") if isinstance(positions.get("data"), list) else []
        tickets = [
            ticket_ref(row.get("ticket") or 0)
            for row in rows
            if str(row.get("symbol") or "").upper() == symbol
        ]
        if not tickets:
            return {
                "result": {"status": "NO_POSITION", "symbol": symbol},
                "status": "no_position",
                "ok": False,
                "reason": f"no open {symbol} position to close",
            }
        return _batch_close(
            service.close_positions(connection_id, tickets=tickets, deviation=deviation)
        )
    raise ValueError("broker close needs a ticket, a symbol, or closeAll = true")


def exec_broker_modify(context: NodeExecContext) -> dict[str, Any]:
    ticket = _ticket_id(context)
    if ticket is None:
        raise ValueError("broker modify needs a ticket input")
    result = _service().modify_position(
        _connection(context), ticket,
        stop_loss=_number(context, "stopLoss"),
        take_profit=_number(context, "takeProfit"),
    )
    modified = bool(result.get("modified")) or bool(result.get("ok"))
    return {
        "result": result,
        "status": "modified" if modified else "rejected",
        "ok": modified,
        "reason": "" if modified else (_reason(result) or "the broker did not modify it"),
    }

def exec_broker_cancel(context: NodeExecContext) -> dict[str, Any]:
    ticket = _ticket_id(context)
    if ticket is None:
        raise ValueError("broker cancel needs a ticket input")
    result = _service().cancel_order(_connection(context), ticket)
    cancelled = bool(result.get("cancelled"))
    return {
        "result": result,
        "status": "cancelled" if cancelled else "rejected",
        "ok": cancelled,
        "reason": "" if cancelled else (_reason(result) or "the broker did not cancel it"),
    }


def build_broker_nodes() -> list[NodeDef]:
    connection = ConfigField("connectionId", "string", "", max_length=80)
    ok_ports = (
        PortDef(id="ok", name="OK", type=PortType.BOOLEAN),
        PortDef(id="reason", name="Reason", type=PortType.STRING),
    )
    return [
        NodeDef(
            type="trading.broker_account", version="1.0.0", label="Broker account",
            category="trading",
            outputs=(
                PortDef(id="account", name="Account", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_account,
            metadata={"docs": "Reads the broker account envelope. Honest UNAVAILABLE, never a fake balance; `ok` is false when the read failed and `reason` names why."},
        ),
        NodeDef(
            type="trading.broker_positions", version="1.0.0", label="Broker positions",
            category="trading",
            outputs=(
                PortDef(id="positions", name="Positions", type=PortType.ARRAY),
                PortDef(id="count", name="Count", type=PortType.NUMBER),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_positions,
            metadata={"docs": "The broker's current positions as rows (ticket, symbol, side, volume, prices, SL/TP, profit). Feed it to a risk check as the `positions` input, or branch on `count`."},
        ),
        NodeDef(
            type="trading.broker_orders", version="1.0.0", label="Broker orders",
            category="trading",
            outputs=(
                PortDef(id="orders", name="Working orders", type=PortType.ARRAY),
                PortDef(id="count", name="Count", type=PortType.NUMBER),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_orders,
            metadata={"docs": "The broker's own working (pending) orders. An empty list with `ok` true means none are working; `ok` false means the read failed."},
        ),
        NodeDef(
            type="trading.broker_fills", version="1.0.0", label="Broker fills",
            category="trading",
            outputs=(
                PortDef(id="fills", name="Fills", type=PortType.ARRAY),
                PortDef(id="count", name="Count", type=PortType.NUMBER),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("limit", "integer", 50, minimum=1, maximum=200),
            ),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_fills,
            metadata={"docs": "Broker deals from Eclipse's durable reconciliation copy (deduped by deal ticket). Empty until the connection has been synced."},
        ),
        NodeDef(
            type="trading.order_ledger", version="1.0.0", label="Order ledger",
            category="trading",
            outputs=(
                PortDef(id="orders", name="Ledger rows", type=PortType.ARRAY),
                PortDef(id="count", name="Count", type=PortType.NUMBER),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(
                ConfigField("connectionId", "string", "", max_length=80),
                ConfigField("limit", "integer", 50, minimum=1, maximum=200),
            ),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_order_ledger,
            metadata={"docs": "Eclipse's own submitted-order rows, newest first, each with status, ticket, and the runId/nodeId that placed it. Leave connectionId empty to read every connection."},
        ),
        NodeDef(
            type="trading.broker_quote", version="1.0.0", label="Broker quote",
            category="trading",
            inputs=(PortDef(id="symbol", name="Symbol", type=PortType.STRING, optional=True),),
            outputs=(
                PortDef(id="quote", name="Quote", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection, ConfigField("symbol", "string", "", max_length=40)),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_quote,
            metadata={"docs": "Bid/ask/spread from the broker (including symbols JDDS may not carry)."},
        ),
        NodeDef(
            type="trading.broker_bars", version="1.0.0", label="Broker bars",
            category="trading",
            inputs=(PortDef(id="symbol", name="Symbol", type=PortType.STRING, optional=True),),
            outputs=(
                PortDef(id="bars", name="Bars", type=PortType.OBJECT),
                PortDef(id="summary", name="Bar summary", type=PortType.OBJECT),
                PortDef(id="atr", name="ATR(14)", type=PortType.NUMBER),
                PortDef(id="contractSize", name="Contract size", type=PortType.NUMBER),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("symbol", "string", "", max_length=40),
                ConfigField("timeframe", "enum", "H1", enum=_TIMEFRAMES),
                ConfigField("count", "integer", 120, minimum=1, maximum=500),
            ),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_bars,
            metadata={"docs": "The broker's own OHLCV series (server time), for symbols JDDS cannot proxy. Also returns the same deterministic ATR(14) / 20-bar range / contract size the chat tool computes, so a graph does not have to recompute them."},
        ),
        NodeDef(
            type="trading.broker_symbol", version="1.0.0", label="Broker symbol",
            category="trading",
            inputs=(PortDef(id="symbol", name="Symbol", type=PortType.STRING, optional=True),),
            outputs=(
                PortDef(id="symbol", name="Symbol facts", type=PortType.OBJECT),
                PortDef(id="tradeMode", name="Trade mode", type=PortType.STRING),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection, ConfigField("symbol", "string", "", max_length=40)),
            capabilities=("trading.broker.read",), side_effects=SideEffect.READ,
            allowed_modes=_ALL, executor=exec_broker_symbol,
            metadata={"docs": "Static symbol facts from the terminal — trade mode, contract size, volume min/max/step, stops and freeze levels, filling mask — with NO tick required, so it answers when the market is closed. Feed `tradeMode` to trading.session_filter. Session calendars are not reported by this adapter."},
        ),
        NodeDef(
            type="trading.broker_modify_order", version="1.0.0", label="Modify order",
            category="trading",
            inputs=(
                PortDef(id="ticket", name="Ticket", type=PortType.TICKET),
                PortDef(id="price", name="New price", type=PortType.NUMBER, optional=True),
                PortDef(id="stopLoss", name="Stop loss", type=PortType.NUMBER, optional=True),
                PortDef(id="takeProfit", name="Take profit", type=PortType.NUMBER, optional=True),
            ),
            outputs=(
                PortDef(id="result", name="Result", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker",), side_effects=SideEffect.TRADING,
            allowed_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            executor=exec_broker_modify_order,
            metadata={"docs": "Re-prices or re-brackets a WORKING (pending) order, keeping its own type/time/expiration, then reads it back (`verified`). Risk-reducing: no arm, approval or mandate."},
        ),
        NodeDef(
            type="trading.broker_close", version="1.0.0", label="Close position",
            category="trading",
            inputs=(
                PortDef(id="ticket", name="Ticket", type=PortType.TICKET, optional=True),
                PortDef(id="symbol", name="Symbol", type=PortType.STRING, optional=True),
                PortDef(id="volume", name="Volume", type=PortType.NUMBER, optional=True),
            ),
            outputs=(
                PortDef(id="result", name="Result", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(
                connection,
                ConfigField("symbol", "string", "", max_length=40),
                ConfigField("closeAll", "boolean", False),
                ConfigField("deviation", "integer", 20, minimum=0, maximum=1_000),
            ),
            capabilities=("trading.broker",), side_effects=SideEffect.TRADING,
            allowed_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            executor=exec_broker_close,
            metadata={"docs": "Risk-reducing: closes one ticket (full/partial), every position in a symbol, or all positions; no arm/approval/mandate."},
        ),
        NodeDef(
            type="trading.broker_modify", version="1.0.0", label="Modify position",
            category="trading",
            inputs=(
                PortDef(id="ticket", name="Ticket", type=PortType.TICKET),
                PortDef(id="stopLoss", name="Stop loss", type=PortType.NUMBER, optional=True),
                PortDef(id="takeProfit", name="Take profit", type=PortType.NUMBER, optional=True),
            ),
            outputs=(
                PortDef(id="result", name="Result", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker",), side_effects=SideEffect.TRADING,
            allowed_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            executor=exec_broker_modify,
            metadata={"docs": "Moves SL/TP on one position. Risk-reducing: no arm/approval/mandate."},
        ),
        NodeDef(
            type="trading.broker_cancel", version="1.0.0", label="Cancel order",
            category="trading",
            inputs=(PortDef(id="ticket", name="Ticket", type=PortType.TICKET),),
            outputs=(
                PortDef(id="result", name="Result", type=PortType.OBJECT),
                PortDef(id="status", name="Status", type=PortType.STRING),
                *ok_ports,
            ),
            config_fields=(connection,),
            capabilities=("trading.broker",), side_effects=SideEffect.TRADING,
            allowed_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            executor=exec_broker_cancel,
            metadata={"docs": "Cancels one working order. Risk-reducing: no arm/approval/mandate."},
        ),
    ]


__all__ = [
    "build_broker_nodes",
    "exec_broker_account",
    "exec_broker_positions",
    "exec_broker_orders",
    "exec_broker_fills",
    "exec_order_ledger",
    "exec_broker_symbol",
    "exec_broker_quote",
    "exec_broker_bars",
    "exec_broker_close",
    "exec_broker_modify",
    "exec_broker_modify_order",
    "exec_broker_cancel",
]
