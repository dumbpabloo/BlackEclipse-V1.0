"""Read-only broker tools.

The agent may see which brokers are configured and read an account, positions,
working orders and a quote. It cannot create, edit or test a connection, and it
cannot place or cancel an order: those are operator routes (and, later, the
approval-gated execution phase).
"""

from __future__ import annotations

from typing import Any

from .analysis import bar_summary
from .authority import bot_version, check_live_order
from .model import TradeRefused

READ = "read"


def _service(ctx: Any) -> Any:
    service = getattr(ctx, "brokers", None)
    if service is None:
        from .bootstrap import get_service  # noqa: PLC0415

        service = get_service()
    if service is None:
        raise RuntimeError("broker connectivity is not available in this run")
    return service


def _bot_from_conversation(ctx: Any) -> str:
    """The automation id when this run IS a bot conversation (`bot-<id>`).

    Bots and group rooms carry their private conversation id on the run, so an
    order placed from a bot can be attributed without asking the model to repeat
    an id it was never given.
    """
    conversation = str(getattr(ctx, "conversation_id", "") or "")
    return conversation[4:] if conversation.startswith("bot-") else ""


def _pick(service: Any, args: dict[str, Any]) -> str:
    explicit = str(args.get("connectionId") or args.get("connection_id") or "").strip()
    if explicit:
        return explicit
    connections = service.connections.list()
    if not connections:
        raise ValueError(
            "no broker connection is configured; add one in Settings › Trading"
        )
    if len(connections) > 1:
        raise ValueError(
            "more than one broker connection exists; pass connectionId "
            f"({', '.join(c.id for c in connections)})"
        )
    return connections[0].id


def _known_connection(service: Any, connection_id: str) -> str:
    """Ignore a connection id that no longer exists.

    A remembered or copied id from another profile/DB is the classic cause of
    "no active mandate" on an account that has one: the id is stale, the mandate
    list for it is empty, and the user is told to create a mandate they already
    own. With a single saved connection the only honest interpretation is that
    id, so a stale one is dropped and resolved normally.
    """
    if not connection_id:
        return ""
    try:
        known = {str(item.get("id")) for item in service.list_connections()}
    except Exception:  # noqa: BLE001 - a read failure must not silently rewrite
        return connection_id
    return connection_id if connection_id in known else ""


def _unavailable_text(result: dict[str, Any]) -> str:
    error = result.get("error") or {}
    return (
        f"Broker unavailable ({error.get('code') or 'UNAVAILABLE'}): "
        f"{error.get('message') or 'the broker did not answer'}"
    )


async def broker_connections(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    kinds = service.kinds()
    connections = service.list_connections()
    lines = ["Broker adapters:"]
    for kind in kinds:
        state = "available" if kind.get("available") else f"unavailable — {kind.get('unavailableReason')}"
        execution = (
            f"execution={','.join(kind.get('executionModes') or [])}"
            if kind.get("execution") else "execution=no"
        )
        lines.append(
            f"- {kind['label']} ({kind['id']}): {state}; "
            f"auth={kind['auth']}; {execution}"
        )
    lines.append("")
    if connections:
        lines.append("Saved connections:")
        for connection in connections:
            lines.append(
                f"- {connection['label']} [{connection['id']}] "
                f"{connection['kind']}/{connection['mode']} — {connection['status']}"
                + (f" ({connection['statusReason']})" if connection.get("statusReason") else "")
            )
    else:
        lines.append("No broker connection is configured yet.")
    return "\n".join(lines), {"kinds": kinds, "connections": connections}


async def broker_account(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    result = service.account(_pick(service, args))
    if not result.get("available"):
        return _unavailable_text(result), result
    account = result["data"]
    return (
        f"{account['company']} · {account['server']} · {account['mode'].upper()} "
        f"account {account['login']}\n"
        f"Balance {account['balance']:,.2f} {account['currency']} · "
        f"Equity {account['equity']:,.2f} · Free margin {account['marginFree']:,.2f} · "
        f"Leverage 1:{account['leverage']}\n"
        f"Margin mode {account['marginMode']} · "
        f"trading {'allowed' if account['tradeAllowed'] else 'disabled'} · "
        f"expert {'allowed' if account['expertAllowed'] else 'disabled'}",
        result,
    )


async def broker_positions(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    result = service.positions(_pick(service, args))
    if not result.get("available"):
        return _unavailable_text(result), result
    rows = result["data"]
    if not rows:
        return "No open positions.", result
    lines = [f"{len(rows)} open position(s):"]
    for row in rows:
        stop = f"{row['stopLoss']:g}" if row.get("stopLoss") is not None else "—"
        target = f"{row['takeProfit']:g}" if row.get("takeProfit") is not None else "—"
        lines.append(
            f"- #{row.get('ticket')} {row['symbol']} {row['side']} {row['volume']:g} "
            f"@ {row['openPrice']:g} (now {row['currentPrice']:g}, SL {stop}, "
            f"TP {target}, P&L {row['profit']:,.2f})"
        )
    return "\n".join(lines), result


async def broker_orders(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    result = service.orders(_pick(service, args))
    if not result.get("available"):
        return _unavailable_text(result), result
    rows = result["data"]
    if not rows:
        return "No working orders.", result
    lines = [f"{len(rows)} working order(s):"]
    for row in rows:
        lines.append(
            f"- #{row['ticket']} {row['symbol']} {row['type']} "
            f"{row['volumeCurrent']:g} @ {row['openPrice']:g} ({row['state']})"
        )
    return "\n".join(lines), result


async def broker_quote(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return "A symbol is required (for example EURUSD).", {"status": "SYMBOL_REQUIRED"}
    result = service.quote(_pick(service, args), symbol)
    if not result.get("available"):
        return _unavailable_text(result), result
    quote = result["data"]
    spread = (
        f"{quote['spreadPoints']:g} pts" if quote.get("spreadPoints") is not None else "unknown"
    )
    digits = int(quote.get("digits") or 5)
    bid = f"{quote['bid']:.{digits}f}"
    ask = f"{quote['ask']:.{digits}f}"
    volume_max = quote.get("volumeMax") or 0
    volume = (
        f"{quote.get('volumeMin', 0):g}–{volume_max:g} (step {quote.get('volumeStep', 0):g})"
        if volume_max
        else "unavailable"
    )
    return (
        f"{quote['symbol']}: bid {bid} / ask {ask} (spread {spread}) from MetaTrader 5\n"
        f"digits {digits} · point {quote.get('point', 0):g} · volume {volume} · "
        f"stops level {quote.get('stopsLevelPoints', 0)} pts · "
        f"server time {quote.get('serverTimeMs', 0)} ms",
        result,
    )


async def broker_symbol_info(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return (
            "A symbol is required (for example EURUSD or XAUUSD).",
            {"status": "SYMBOL_REQUIRED"},
        )
    result = service.symbol_info(_pick(service, args), symbol)
    if not result.get("available"):
        return _unavailable_text(result), result
    facts = result["data"]
    volume = (
        f"{facts.get('volumeMin', 0):g}-{facts.get('volumeMax', 0):g} "
        f"(step {facts.get('volumeStep', 0):g})"
        if facts.get("volumeMax")
        else "unavailable"
    )
    sessions = (
        "reported" if facts.get("sessionsAvailable") else "not reported by the broker"
    )
    tick_value = facts.get("tickValue")
    money = (
        f"{tick_value:g} per tick per lot ({facts.get('currencyProfit') or '?'} "
        f"-> {facts.get('currencyMargin') or 'account'})"
        if tick_value
        else "not reported (sizing falls back to contract size)"
    )
    return (
        f"{facts.get('symbol')}: {facts.get('description') or 'instrument'}\n"
        f"trade mode {facts.get('tradeMode')} - digits {facts.get('digits')} - "
        f"point {facts.get('point', 0):g} - tick {facts.get('tickSize', 0):g}\n"
        f"contract size {facts.get('contractSize', 0):g} - volume {volume}\n"
        f"stops level {facts.get('stopsLevelPoints', 0)} pts - "
        f"spread {facts.get('spreadPoints', 0)} pts\n"
        f"tick value {money}; sessions {sessions}",
        result,
    )


async def trade_plan(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    connection_id = str(args.get("connectionId") or "").strip()
    if not connection_id:
        connection_id = _pick(service, args)
    payload = {
        "symbol": args.get("symbol"),
        "side": args.get("side"),
        "entry": args.get("entry"),
        "stop": args.get("stop"),
        "target": args.get("target"),
        "riskPct": args.get("riskPct"),
    }
    result = service.trade_plan(connection_id, payload)
    if not result.get("available"):
        return _unavailable_text(result), result
    if not result.get("allowed"):
        reason = (result.get("reasons") or ["the plan was refused"])[0]
        return f"Trade plan refused: {reason}", result
    risk = result.get("risk") or {}
    lines = [result.get("summary") or "Trade plan"]
    if result.get("entrySource"):
        lines.append(
            f"Entry source: {result['entrySource']}; "
            f"plan id {result.get('planId')}"
        )
    if risk.get("warnings"):
        lines.extend(f"- {item}" for item in risk["warnings"])
    if risk.get("unmeasured"):
        lines.append("Not measured: " + "; ".join(risk["unmeasured"]))
    lines.append(
        "This is a plan, not an order. Call order_preview with this exact "
        "volume and stop to create the approval card."
    )
    return "\n".join(lines), result


async def risk_supervisor(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    from .supervisor_api import supervisor_report  # noqa: PLC0415

    report = supervisor_report(service)
    portfolio = report["portfolio"]
    equity = portfolio.get("equity")
    margin_pct = portfolio.get("marginPct")
    lines = [
        f"Portfolio: {portfolio['verdict'].upper()} - "
        f"equity {equity if equity is not None else 'unavailable'}, "
        f"margin {margin_pct if margin_pct is not None else 'n/a'}%, "
        f"{portfolio.get('openPositions')} open position(s)"
    ]
    lines.extend(f"- STOP: {reason}" for reason in portfolio.get("reasons") or [])
    lines.extend(f"- watch: {warning}" for warning in portfolio.get("warnings") or [])
    if portfolio.get("unmeasured"):
        lines.append("- unmeasured: " + "; ".join(portfolio["unmeasured"]))
    bots = report.get("bots") or []
    if bots:
        lines.append("Bots:")
        for bot in bots:
            lines.append(
                f"- {bot['automationId']}: {bot['verdict']} - "
                f"{bot['trades']} closed trade(s), realized {bot['realizedPnl']:,.2f}, "
                f"{bot['consecutiveLosses']} consecutive loss(es)"
            )
            lines.extend(f"    {reason}" for reason in bot.get("reasons") or [])
    else:
        lines.append("No bot with a recorded order yet.")
    return "\n".join(lines), report


async def risk_supervisor_apply(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    from .supervisor_api import supervisor_apply  # noqa: PLC0415

    result = supervisor_apply(service)
    paused = result.get("paused") or []
    failed = result.get("failed") or []
    if paused:
        text = "Paused underperforming bot(s): " + ", ".join(paused)
    else:
        text = "No bot needed to be paused."
    if failed:
        text += "\nCould not pause: " + "; ".join(
            f"{item['automationId']} ({item['error']})" for item in failed
        )
    return text, result


async def order_preview(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    connection_id = str(args.get("connectionId") or "").strip()
    if not connection_id:
        connection_id = _pick(service, args)
    intent = args.get("intent")
    if not isinstance(intent, dict):
        return "An `intent` object is required (symbol, side, type, volume, …).", {
            "status": "INVALID_ORDER"
        }
    result = service.preview_order(
        connection_id, intent, run_id=str(getattr(ctx, "run_id", "") or "")
    )
    if result.get("allowed"):
        approval = result.get("approval") or {}
        return (
            "Order preview is valid and awaits an operator decision. "
            f"Approval {approval.get('id')} ({approval.get('status')}).\n"
            f"{approval.get('actionSummary') or ''}\n"
            "No order was sent yet. The operator's approval card submits this exact "
            "order through the normal submit route (no mandate needed); after they "
            "confirm, read `order_ledger` and `broker_positions` to report the fill.",
            result,
        )
    reasons = "; ".join(result.get("reasons") or []) or "the order was refused"
    error = result.get("error") or {}
    code = error.get("code") or "REFUSED"
    return f"Order preview refused ({code}): {reasons}", result


async def broker_reconciliation(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    result = service.reconciliation(_pick(service, args), limit=int(args.get("limit") or 50))
    state = result.get("state") or {}
    positions = result.get("positions") or []
    fills = result.get("fills") or []
    if not state:
        return "No reconciliation has run for this connection yet.", result
    lines = [
        f"Last sync: {state.get('lastSyncedAt')} ({state.get('status')})",
        f"Positions: {len(positions)} · fills stored: {len(fills)} · "
        f"last deal: {state.get('lastDealAt') or 'none'}",
    ]
    if state.get("error"):
        lines.append(f"Error: {state['error']}")
    return "\n".join(lines), result


async def broker_fills(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    fills = service.list_fills(
        _pick(service, args), limit=int(args.get("limit") or 20)
    )
    if not fills:
        return "No broker fills are recorded.", {"fills": []}
    lines = [f"{len(fills)} fill(s), newest first:"]
    for fill in fills:
        lines.append(
            f"- {fill['symbol']} {fill['side']} {fill['entry']} {fill['volume']:g} "
            f"@ {fill['price']:g} (profit {fill['profit']:,.2f}, "
            f"deal {fill['dealTicket']})"
        )
    return "\n".join(lines), {"fills": fills}


async def broker_mandates(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    connection_id = str(args.get("connectionId") or "").strip() or None
    mandates = service.list_mandates(connection_id, active_only=True)
    if not mandates:
        return (
            "No active demo mandate. An operator must create one in "
            "Settings → Broker connection before unattended orders are allowed.",
            {"mandates": []},
        )
    lines = [f"{len(mandates)} active demo mandate(s):"]
    for mandate in mandates:
        lines.append(
            f"- {mandate['id']} · connection {mandate['connectionId']} · "
            f"symbols {', '.join(mandate['symbols'])} · "
            f"{mandate['remainingOrders']}/{mandate['maxOrders']} orders left · "
            f"max margin {mandate['maxMargin']:g} · expires {mandate['expiresAt']}"
        )
    return "\n".join(lines), {"mandates": mandates}


def _select_mandate(
    service: Any, connection_id: str, symbol: str, *, bot_id: str = "",
) -> str:
    """Resolve the one mandate that applies, without requiring an id from the user.

    When `bot_id` is given (an L2 bot run) only a mandate pinned to THAT bot is a
    candidate. A workflow-pinned or unpinned mandate must never authorize a bot's
    unattended order just because it covers the symbol.
    """
    try:
        active = service.list_mandates(connection_id, active_only=True)
    except Exception:  # noqa: BLE001
        return ""
    if bot_id:
        active = [m for m in active if str(m.get("botId") or "") == str(bot_id)]
    if len(active) == 1:
        return str(active[0].get("id") or "")
    wanted = str(symbol or "").upper()
    matches = [
        mandate for mandate in active
        if any(str(item).strip().upper() in ("*", wanted) for item in (mandate.get("symbols") or []))
    ]
    if len(matches) == 1:
        return str(matches[0].get("id") or "")
    return ""


async def broker_bars(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    """The broker's own OHLCV series, with ATR computed deterministically."""
    service = _service(ctx)
    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return "A `symbol` is required (e.g. XAUUSD).", {"status": "INVALID_SYMBOL"}
    connection_id = _known_connection(
        service, str(args.get("connectionId") or "").strip()
    )
    try:
        if not connection_id:
            connection_id = _pick(service, {})
        timeframe = str(args.get("timeframe") or "H1").strip().upper()
        count = int(args.get("count") or 120)
        result = service.bars(connection_id, symbol, timeframe=timeframe, count=count)
    except ValueError as exc:
        return str(exc), {"status": "CONNECTION_REQUIRED"}
    if not result.get("available"):
        return _unavailable_text(result), result
    data = result.get("data") or {}
    bars = data.get("bars") or []
    summary = bar_summary(bars)
    if not bars or summary.get("atr") is None:
        return (
            f"{data.get('symbol') or symbol} {data.get('timeframe') or timeframe}: "
            f"{len(bars)} bars; not enough history for ATR(14).",
            {**result, "summary": summary},
        )
    contract = data.get("contractSize") or 0
    lines = [
        f"{data.get('symbol')} {data.get('timeframe')}: {len(bars)} bars from "
        "MetaTrader 5 (broker server time).",
        f"Last close {summary['lastClose']:g} "
        f"(high {summary['lastHigh']:g} / low {summary['lastLow']:g}), "
        f"change {summary['changePct'] if summary['changePct'] is not None else 'n/a'}%.",
        f"ATR({summary['atrPeriod']}) {summary['atr']:g} over the last "
        f"{summary['lookback']} bars; range "
        f"{summary['lookbackLow']:g}–{summary['lookbackHigh']:g}.",
    ]
    if contract:
        lines.append(
            f"Contract size {contract:g}; a 0.01 lot is {contract / 100:g} units."
        )
    return "\n".join(lines), {**result, "summary": summary}


async def order_submit(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    """Submit a demo order under the active mandate that covers its symbol."""
    service = _service(ctx)
    connection_id = str(args.get("connectionId") or "").strip()
    mandate_id = str(args.get("mandateId") or "").strip()
    intent = args.get("intent")
    if not isinstance(intent, dict):
        return "An `intent` object is required.", {"status": "INVALID_ORDER"}
    connection_id = _known_connection(service, connection_id)
    if not connection_id:
        try:
            connection_id = _pick(service, {})
        except ValueError as exc:
            return str(exc), {"status": "CONNECTION_REQUIRED"}
    # Defence in depth: the authorizer applies the same rule, but this handler must
    # not depend on that wrapper being installed. A DEMO order is authorized by the
    # mandate; a LIVE order additionally needs the bot's own tier (L2), the current
    # objective version and every limit in `trade/authority.py`.
    mode = ""
    try:
        for item in service.list_connections():
            if str(item.get("id") or "") == connection_id:
                mode = str(item.get("mode") or "")
                break
    except Exception:  # noqa: BLE001 - unknown mode is refused below
        mode = ""
    automation_id = _bot_from_conversation(ctx)
    refusal = check_live_order(
        service,
        automation_id=automation_id,
        connection_id=connection_id,
        mode=mode,
        intent=intent,
    )
    if refusal is not None:
        return refusal
    if not mandate_id:
        mandate_id = _select_mandate(
            service, connection_id, str(intent.get("symbol") or ""),
            bot_id=automation_id if mode == "live" else "",
        )
    if not mandate_id:
        active = service.list_mandates(connection_id, active_only=True)
        return (
            "No single active mandate covers this symbol"
            + (" for this bot." if mode == "live" else ".")
            + f" Active mandates: {', '.join(m['id'] for m in active) or 'none'}. "
            "For a one-off manual trade call `order_preview` instead: the "
            "operator's approval card submits that exact order and needs no "
            "mandate. A mandate (Settings → Broker connection) is only for "
            "unattended orders.",
            {"status": "MANDATE_REQUIRED", "mandates": active},
        )
    try:
        version = bot_version(automation_id)
        result = service.submit_order(
            connection_id,
            intent,
            mandate_id=mandate_id,
            run_id=str(getattr(ctx, "run_id", "") or ""),
            automation_id=automation_id,
            automation_version=version,
            bot_id=automation_id if mode == "live" else "",
            bot_version=version if mode == "live" else None,
        )
    except TradeRefused as exc:
        return f"Order refused ({exc.code}): {exc.message}", {
            "status": "REFUSED",
            "error": exc.to_dict(),
        }
    status = str(result.get("status") or "UNKNOWN")
    ledger = result.get("ledger") or {}
    if status in ("filled", "placed"):
        detail = result.get("result") or {}
        verify = "" if detail.get("verified", True) else (
            " WARNING: the broker's stored bracket does not match the request — "
            f"{detail.get('verifyNote') or 'verify the position manually'}."
        )
        return (
            f"Order {status}. Ticket {ledger.get('ticket') or 'n/a'}, "
            f"order {ledger.get('id') or result.get('orderId') or 'n/a'}.{verify}",
            result,
        )
    reasons = "; ".join(result.get("reasons") or []) or status
    return f"Order not sent ({status}): {reasons}", result


async def position_close(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    """Close one position, or every open one, without an approval.

    Closing reduces exposure, so it follows the cancel rule: no mandate, no
    approval and no live arm. The service still requires the account mode to
    match the connection (demo/demo, live/real).
    """
    service = _service(ctx)
    connection_id = _known_connection(
        service, str(args.get("connectionId") or "").strip()
    )
    try:
        if not connection_id:
            connection_id = _pick(service, {})
        ticket = args.get("ticket")
        tickets = args.get("tickets")
        close_all = bool(args.get("all"))
        symbol = str(args.get("symbol") or "").strip().upper()
        volume = args.get("volume")
        if ticket is not None:
            result = service.close_position(
                connection_id, int(ticket),
                volume=float(volume) if volume is not None else None,
            )
            closed = bool(result.get("closed"))
            detail = result.get("result") or {}
            if closed:
                suffix = (
                    f" Partial close; {detail.get('remainingVolume'):g} still open."
                    if detail.get("partial") else ""
                )
                verify = "" if detail.get("verified") else (
                    " WARNING: the broker's position did not read back as expected."
                )
                return (
                    f"Closed position {ticket} ({detail.get('symbol', '?')} "
                    f"{detail.get('volume', '?')} at {detail.get('price', '?')})."
                    f"{suffix}{verify}",
                    result,
                )
            reasons = "; ".join(detail.get("reasons") or []) or detail.get("retcodeName")
            return f"Position {ticket} not closed: {reasons}", result
        if symbol:
            positions = service.positions(connection_id)
            rows = (positions.get("data") or []) if positions.get("available") else []
            wanted = [
                int(row.get("ticket") or 0)
                for row in rows
                if str(row.get("symbol") or "").upper() == symbol
            ]
            if not wanted:
                return f"No open {symbol} position to close.", {
                    "status": "NO_POSITION", "symbol": symbol
                }
            result = service.close_positions(connection_id, tickets=wanted)
        elif close_all or tickets:
            wanted = [int(item) for item in tickets] if isinstance(tickets, list) else None
            result = service.close_positions(connection_id, tickets=wanted)
        else:
            return (
                'Pass `ticket` to close one position, `symbol` to close that '
                'instrument, or `all: true` to close every open position.',
                {"status": "INVALID_ARGUMENTS"},
            )
    except ValueError as exc:
        return str(exc), {"status": "CONNECTION_REQUIRED"}
    except TradeRefused as exc:
        return f"Close refused ({exc.code}): {exc.message}", {
            "status": "REFUSED",
            "error": exc.to_dict(),
        }
    closed = int(result.get("closed") or 0)
    failed = int(result.get("failed") or 0)
    if not result.get("requested"):
        return "No open positions to close.", result
    summary = f"Closed {closed} of {result.get('requested')} position(s)"
    if failed:
        summary += f"; {failed} failed"
    return summary + ".", result


async def position_modify(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    """Move a position's stop and/or target (no mandate, approval or arm)."""
    service = _service(ctx)
    ticket = args.get("ticket")
    stop_loss = args.get("stopLoss")
    take_profit = args.get("takeProfit")
    if ticket is None:
        return "Pass `ticket` for the position to modify.", {"status": "INVALID_ARGUMENTS"}
    if stop_loss is None and take_profit is None:
        return (
            "Pass `stopLoss` and/or `takeProfit` (a number, or 0 to remove it).",
            {"status": "INVALID_STOPS"},
        )
    connection_id = _known_connection(
        service, str(args.get("connectionId") or "").strip()
    )
    try:
        if not connection_id:
            connection_id = _pick(service, {})
        result = service.modify_position(
            connection_id, int(ticket),
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            take_profit=float(take_profit) if take_profit is not None else None,
        )
    except ValueError as exc:
        return str(exc), {"status": "CONNECTION_REQUIRED"}
    except TradeRefused as exc:
        return f"Modify refused ({exc.code}): {exc.message}", {
            "status": "REFUSED",
            "error": exc.to_dict(),
        }
    detail = result.get("result") or {}
    if result.get("modified"):
        verify = "" if detail.get("verified") else (
            " WARNING: the broker accepted the request but the stored bracket "
            "did not read back as requested."
        )
        return (
            f"Position {ticket} updated: stop loss "
            f"{detail.get('stopLoss', 'n/a')}, take profit "
            f"{detail.get('takeProfit', 'n/a')}.{verify}",
            result,
        )
    reasons = "; ".join(detail.get("reasons") or []) or detail.get("retcodeName")
    return f"Position {ticket} not modified: {reasons}", result


async def order_ledger(args: dict[str, Any], ctx: Any) -> tuple[str, Any]:
    service = _service(ctx)
    connection_id = str(args.get("connectionId") or "").strip() or None
    # A bot asking about itself: `conversation_id` is `bot-<automationId>`, so it
    # can answer "what have I actually placed?" without being told its own id.
    automation_id = str(args.get("automationId") or "").strip() or _bot_from_conversation(ctx)
    orders = service.ledger_orders(
        connection_id, limit=int(args.get("limit") or 20),
        automation_id=automation_id or None,
    )
    if not orders:
        return "No submitted broker orders are recorded.", {"orders": []}
    lines = [f"{len(orders)} submitted order(s), newest first:"]
    for order in orders:
        intent = order.get("intent") or {}
        owner = ""
        if order.get("automationId"):
            version = order.get("automationVersion")
            owner = f" · bot {order['automationId']}" + (f"@v{version}" if version else "")
        lines.append(
            f"- {order['id']} {intent.get('side', '?')} {intent.get('volume', '?')} "
            f"{intent.get('symbol', '?')} {order['status']}"
            + (f" ticket {order['ticket']}" if order.get("ticket") else "")
            + owner
        )
    return "\n".join(lines), {"orders": orders}


def register_broker_tools(registry: Any) -> None:
    from ..tools import ToolDefinition

    def _def(
        name: str,
        description: str,
        handler: Any,
        schema: dict[str, Any],
        *,
        permission: str = READ,
        risk: str = "none",
        effect: str | None = None,
    ) -> None:
        registry.register(ToolDefinition(
            name=name,
            description=description,
            input_schema=schema,
            handler=handler,
            permission=permission,
            risk=risk,
            effect=effect,
        ))

    connection_id = {"type": "string", "description": "Saved connection id (optional when there is exactly one)."}
    _def(
        "broker_connections",
        "List the broker adapters Eclipse knows and the saved connections with "
        "their last known status. Read-only.",
        broker_connections,
        {"type": "object", "properties": {}},
    )
    _def(
        "broker_account",
        "Read the connected broker account (balance, equity, margin, leverage, "
        "demo/real mode). Read-only.",
        broker_account,
        {"type": "object", "properties": {"connectionId": connection_id}},
    )
    _def(
        "broker_positions",
        "Read open broker positions. Read-only; never invents a position.",
        broker_positions,
        {"type": "object", "properties": {"connectionId": connection_id}},
    )
    _def(
        "broker_orders",
        "Read working (pending) broker orders. Read-only.",
        broker_orders,
        {"type": "object", "properties": {"connectionId": connection_id}},
    )
    _def(
        "broker_quote",
        "Read a live bid/ask quote from the connected broker feed.",
        broker_quote,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "symbol": {"type": "string"},
            },
            "required": ["symbol"],
        },
    )
    _def(
        "broker_symbol_info",
        "Read the broker's own static facts for one instrument: contract size, "
        "volume minimum/step/maximum, tick size and tick value in the account "
        "currency, stops level, trade mode and sessions. Read-only. Call this "
        "before sizing a trade - never assume a contract size.",
        broker_symbol_info,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "symbol": {"type": "string"},
            },
            "required": ["symbol"],
        },
    )
    _def(
        "trade_plan",
        "Turn a trade idea into a sized, risk-checked plan: read the account "
        "equity and the symbol's own limits, place the entry at the operator's "
        "price or the broker's quote, and compute the largest volume whose "
        "stop-out stays inside `riskPct` of equity (default 1%). Also checks the "
        "account fit (open positions, concentration, margin, spread) and returns "
        "max loss, % of equity and reward:risk. Sends nothing and creates no "
        "approval. A refusal (minimum lot too large, stop too tight) names the "
        "exact correction. Use its volume and stop in `order_preview`.",
        trade_plan,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "entry": {"type": "number"},
                "stop": {"type": "number"},
                "target": {"type": "number"},
                "riskPct": {"type": "number"},
            },
            "required": ["symbol", "side", "stop"],
        },
    )
    _def(
        "risk_supervisor",
        "Read the account-level trading supervisor: total margin vs equity, "
        "open positions, equity loss since the day/week open, and each bot's "
        "realized result after costs. `block` means no new entries. Read-only.",
        risk_supervisor,
        {"type": "object", "properties": {}},
    )
    _def(
        "order_preview",
        "Validate an order (demo or live) against broker constraints, the "
        "connection's limits and the money guardrails, then request a single-use "
        "operator approval. On a live connection the operator confirms the "
        "approval card in chat and types the LIVE phrase; this tool NEVER sends "
        "an order. For a new entry, call `broker_symbol_info` and `trade_plan` "
        "first; never guess a volume.",
        order_preview,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "intent": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "type": {"type": "string", "enum": ["market", "limit", "stop"]},
                        "volume": {"type": "number"},
                        "price": {"type": "number"},
                        "stopLoss": {"type": "number"},
                        "takeProfit": {"type": "number"},
                        "deviationPoints": {"type": "integer"},
                        "timeInForce": {"type": "string", "enum": ["gtc", "day"]},
                        "comment": {"type": "string"},
                        "magic": {"type": "integer"},
                    },
                    "required": ["symbol", "side", "type", "volume"],
                },
            },
            "required": ["intent"],
        },
    )
    _def(
        "order_ledger",
        "Read the local ledger of orders Eclipse submitted to a broker. This is an "
        "audit record; the broker remains the source of truth for fills. Each row "
        "names the run and, for a bot, the bot id and the version of its objective "
        "that placed it. Pass `automationId` to see one bot's orders; a bot asking "
        "about itself may omit it.",
        order_ledger,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "automationId": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    )
    _def(
        "broker_reconciliation",
        "Read the last reconciled fills, positions snapshot and sync state for a "
        "broker connection. Read-only.",
        broker_reconciliation,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "limit": {"type": "integer"},
            },
        },
    )
    _def(
        "broker_fills",
        "Read the broker deals Eclipse has durably recorded (deduped by deal ticket). "
        "Read-only; the broker remains the source of truth.",
        broker_fills,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "limit": {"type": "integer"},
            },
        },
    )
    _def(
        "broker_mandates",
        "List active demo mandates (symbols, remaining orders, margin cap, expiry). "
        "A mandate is the operator's authorization for unattended demo orders.",
        broker_mandates,
        {"type": "object", "properties": {"connectionId": connection_id}},
    )
    _def(
        "broker_bars",
        "Read the broker's OWN OHLCV bars for a symbol (server time) and the "
        "deterministic ATR(14) / 20-bar range computed from them. Use this for "
        "any instrument the stored market history does not cover (XAUUSD): it is "
        "the accurate series for a bracket, unlike a proxy ETF. Read-only.",
        broker_bars,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "symbol": {"type": "string"},
                "timeframe": {
                    "type": "string",
                    "enum": ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"],
                },
                "count": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["symbol"],
        },
    )
    _def(
        "order_submit",
        "Submit ONE order under an active operator-created mandate. REQUIRED "
        "arguments: connectionId and intent {symbol, side, type, volume, stopLoss?, "
        "takeProfit?}. `connectionId` and `mandateId` are optional: the one saved "
        "connection and the one active mandate covering the symbol are resolved for "
        "you. Only an ambiguous multi-mandate symbol needs an id from broker_mandates. "
        "This call performs "
        "its own preview and submission; do not call order_preview first and never call "
        "it with empty arguments. "
        "DEMO connections submit under a demo mandate. A LIVE order is only possible "
        "from a BOT run whose definition is at tier L2 and whose live mandate is "
        "pinned to that bot and its current objective version; every other run, and "
        "every workflow-pinned or unpinned mandate, is refused. In an ordinary chat, "
        "call `order_preview` instead and the operator confirms the approval card, "
        "typing the LIVE phrase.",
        order_submit,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "mandateId": {
                    "type": "string",
                    "description": "Active demo mandate id from broker_mandates.",
                },
                "intent": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "type": {"type": "string", "enum": ["market", "limit", "stop"]},
                        "volume": {"type": "number"},
                        "price": {"type": "number"},
                        "stopLoss": {"type": "number"},
                        "takeProfit": {"type": "number"},
                        "deviationPoints": {"type": "integer"},
                        "timeInForce": {"type": "string", "enum": ["gtc", "day"]},
                        "comment": {"type": "string"},
                        "magic": {"type": "integer"},
                    },
                    "required": ["symbol", "side", "type", "volume"],
                },
            },
            "required": ["intent"],
        },
        permission=READ,
        risk="high",
        effect="trading",
    )


    _def(
        "position_close",
        "Close an open position (risk-reducing, so no approval, mandate or live arm "
        "is needed). Pass `ticket` for one position, or `all: true` to step out of "
        "every open position; `connectionId` resolves on its own when there is one "
        "connection. Works on a demo or a live connection; the account mode must "
        "match the connection. Returns the broker's per-ticket result.",
        position_close,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "ticket": {"type": "integer"},
                "tickets": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Close these tickets instead of every position.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Close every open position in this instrument.",
                },
                "volume": {
                    "type": "number",
                    "description": "Partial close size; omit to close the whole ticket.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Close every open position on the connection.",
                },
                "deviation": {"type": "integer"},
            },
        },
        permission=READ,
        risk="medium",
        effect="trading",
    )
    _def(
        "position_modify",
        "Move a position's stop loss and/or take profit (risk-reducing; no "
        "approval, mandate or live arm). Pass `ticket` and `stopLoss`/`takeProfit`; "
        "0 removes a level. Works on a demo or a live connection; the account mode "
        "must match. The result is verified against the broker's stored bracket. A "
        "wider stop increases risk — say so when you do it.",
        position_modify,
        {
            "type": "object",
            "properties": {
                "connectionId": connection_id,
                "ticket": {"type": "integer"},
                "stopLoss": {"type": "number"},
                "takeProfit": {"type": "number"},
            },
            "required": ["ticket"],
        },
        permission=READ,
        risk="medium",
        effect="trading",
    )


def register_supervisor_tools(registry: Any) -> None:
    """The supervisor's STAND-DOWN tool, kept out of the read-only broker set.

    Every broker tool is read-only by contract (`test_broker_layer`); pausing a
    bot is a state change, so it is registered here and marked write/high. The
    PERIODIC sweep uses the service directly and needs no tool at all.
    """
    from harness.tools import ToolDefinition  # noqa: PLC0415

    registry.register(ToolDefinition(
        name="risk_supervisor_apply",
        description=(
            "Run the supervisor's stand-down: pause every bot that has taken "
            "enough real fills and is still unprofitable after costs, or is on a "
            "losing streak. It pauses future triggers and never closes positions."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=risk_supervisor_apply,
        permission="write",
        risk="high",
    ))


__all__ = [
    "BROKER_TOOLS",
    "register_supervisor_tools",
    "broker_account",
    "broker_bars",
    "broker_connections",
    "broker_fills",
    "broker_mandates",
    "broker_orders",
    "broker_positions",
    "broker_quote",
    "broker_symbol_info",
    "broker_reconciliation",
    "order_ledger",
    "order_preview",
    "trade_plan",
    "risk_supervisor",
    "risk_supervisor_apply",
    "order_submit",
    "position_close",
    "position_modify",
    "register_broker_tools",
]

#: The canonical broker allowlist (imported by `agents.py`). `order_submit` opens
#: exposure under an active demo mandate; `position_close` only reduces exposure.
BROKER_TOOLS = [
    "broker_connections",
    "broker_account",
    "broker_positions",
    "broker_orders",
    "broker_quote",
    "broker_symbol_info",
    "broker_bars",
    "trade_plan",
    "risk_supervisor",
    "risk_supervisor_apply",
    "order_preview",
    "order_submit",
    "position_close",
    "position_modify",
    "broker_mandates",
    "order_ledger",
    "broker_reconciliation",
    "broker_fills",
]
