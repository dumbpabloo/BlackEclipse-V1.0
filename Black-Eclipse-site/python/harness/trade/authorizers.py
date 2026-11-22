"""Effect authorizer for the ONE agent trade-submission tool.

The generic effect gate requires an approval for every non-read tool. An
unattended mandate is a different, stronger authorization: an operator created it
with symbols, a margin cap, an order count and an expiry. This wrapper admits
`order_submit` only when an active mandate covers that connection and symbol, and
the connection's MODE is one this run may use:

  * DEMO — any run, under a demo mandate (the rehearsal path);
  * LIVE — only a bot run (`conversationId = bot-<id>`), where
    `trade/authority.py` additionally requires tier L2, the current objective
    version, the position/cooldown limits and the mandate's bot pin.

There is no live path from an ordinary chat run. A manual live trade goes through
`order_preview` → the operator's approval card → the submit route (typing the LIVE
phrase); a Graph Studio `trading.*` node executes its own workflow-pinned mandate
without passing through this authorizer. Live is refused here by MODE, not by a
sentence in a prompt.
"""

from __future__ import annotations

from typing import Any


def wrap_trade_authorizer(inner: Any) -> Any:
    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if name == "risk_supervisor_apply":
            # A stand-down only ever REDUCES trading: it pauses future triggers
            # and cannot open exposure, close a position or move money. Like
            # close/modify it needs no approval card, and the periodic supervisor
            # sweep calls the same service path without a run at all. Admission is
            # not authority: the handler still requires a bound trade service.
            return True
        if name in ("position_close", "position_modify"):
            # Closing or moving a bracket reduces (or re-scopes) exposure: like
            # cancelling a working order it needs no mandate and no approval card.
            # The handler resolves the single connection and the service refuses
            # a live connection or a real account itself.
            if name == "position_modify" and (
                args.get("stopLoss") is None and args.get("takeProfit") is None
            ):
                return False
            return (
                args.get("ticket") is not None
                or bool(args.get("all"))
                or bool(args.get("tickets"))
                or bool(args.get("symbol"))
            )
        if name != "order_submit":
            return bool(await inner(tool, args, ctx))
        from .bootstrap import get_service  # noqa: PLC0415

        service = get_service()
        if service is None:
            return False
        connection_id = str(args.get("connectionId") or "").strip()
        mandate_id = str(args.get("mandateId") or "").strip()
        intent = args.get("intent") or {}
        symbol = str((intent or {}).get("symbol") or "").strip()
        if not symbol:
            return False
        try:
            connections = service.list_connections()
        except Exception:  # noqa: BLE001 - fail closed
            return False
        # A remembered id from another profile/DB is not a connection here; it
        # would make the mandate list look empty. Drop it and resolve normally.
        if connection_id and connection_id not in {
            str(item.get("id")) for item in connections
        }:
            connection_id = ""
        if not connection_id:
            if len(connections) != 1:
                return False
            connection_id = str(connections[0].get("id") or "").strip()
        if not connection_id:
            return False
        # The connection's mode decides who may reach it. DEMO is the ordinary
        # unattended path. LIVE is admitted ONLY from a bot run (P5), where
        # `trade/authority.py` checks the bot's tier, the objective version and
        # every limit; a workflow-pinned mandate still cannot be reached from
        # chat, and an unknown mode is refused. Fail closed, never guess.
        mode = ""
        for item in connections:
            if str(item.get("id") or "") == connection_id:
                mode = str(item.get("mode") or "")
                break
        if mode == "live":
            conversation = str(getattr(ctx, "conversation_id", "") or "")
            if not conversation.startswith("bot-"):
                return False
        elif mode != "demo":
            return False
        if not mandate_id:
            try:
                active = service.list_mandates(connection_id, active_only=True)
            except Exception:  # noqa: BLE001 - fail closed
                return False
            if len(active) == 1:
                mandate_id = str(active[0].get("id") or "").strip()
            else:
                wanted = symbol.upper()
                matches = [
                    mandate for mandate in active
                    if any(
                        str(item).strip().upper() in ("*", wanted)
                        for item in (mandate.get("symbols") or [])
                    )
                ]
                if len(matches) == 1:
                    mandate_id = str(matches[0].get("id") or "").strip()
            if not mandate_id:
                return False
        try:
            return service.mandates.covers(
                mandate_id, connection_id=connection_id, symbol=symbol, mode=mode
            )
        except TypeError:
            # A mandate store that predates the mode check: refuse rather than
            # silently dropping the guard.
            return False
        except Exception:  # noqa: BLE001 - fail closed
            return False

    return authorize


__all__ = ["wrap_trade_authorizer"]
