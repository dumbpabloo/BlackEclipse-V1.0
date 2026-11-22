"""The ONE trade authorization seam (P1, demo).

    policy → money guardrails → single-use approval → broker submit

Nothing else may call `order_send`. The approval digest covers the exact order
(connection, symbol, side, type, volume, price, SL/TP, deviation, TIF, magic,
comment), so an operator decision can never authorize a different payload.

P1 is demo-only: the service refuses a live connection before this module runs,
and the broker's own account mode is re-checked as a second, independent gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .model import BrokerConnection, OrderIntent, TradeRefused

APPROVAL_KIND = "trade_order"
APPROVAL_EFFECT = "external_action"
APPROVAL_TTL_SECONDS = 300


def money_intent_for(
    connection: BrokerConnection, intent: OrderIntent, preview: dict[str, Any],
) -> Any:
    """The money-guardrail shape of this order (not a payment, but money-moving)."""
    from ..finance_guardrails.models import MoneyIntent  # noqa: PLC0415

    account = preview.get("account") or {}
    return MoneyIntent(
        action="place_order",
        tool="broker_order",
        channel="native",
        amount=preview.get("notional"),
        currency=str(account.get("currency") or "USD"),
        instrument=str(preview.get("symbol") or intent.symbol),
        account=connection.id,
        source_text=(
            f"{intent.side} {intent.volume:g} {intent.symbol} {intent.order_type} "
            f"on {connection.label}"
        ),
        args=intent.to_dict(),
    )


def evaluate_guardrails(service: Any, money_intent: Any, *, record: bool) -> Any:
    """Evaluate (preview) or evaluate-and-record (submit) the money guardrails."""
    if service is None:
        raise TradeRefused(
            "GUARDRAILS_UNAVAILABLE",
            "the money guardrails are not available; refusing to trade without them",
            status_code=503,
        )
    return service.evaluate_and_record(money_intent) if record else service.evaluate(money_intent)


def order_summary(
    connection: BrokerConnection, intent: OrderIntent, preview: dict[str, Any],
) -> str:
    bits = [
        f"{intent.side.upper()} {intent.volume:g} {preview.get('symbol') or intent.symbol}",
        f"{intent.order_type}",
    ]
    if intent.price is not None:
        bits.append(f"@ {intent.price:g}")
    bits.append(f"· {connection.label} · {connection.mode.value.upper()}")
    margin = preview.get("margin")
    if isinstance(margin, (int, float)):
        bits.append(f"· margin {margin:,.2f}")
    if intent.stop_loss is not None:
        bits.append(f"· SL {intent.stop_loss:g}")
    if intent.take_profit is not None:
        bits.append(f"· TP {intent.take_profit:g}")
    if preview.get("ok"):
        bits.append("· broker check ok")
    return " ".join(bits)


def request_approval(
    approvals: Any,
    connection: BrokerConnection,
    intent: OrderIntent,
    preview: dict[str, Any],
    guardrail: Any,
    *,
    risk: str = "high",
    run_id: str = "",
    risk_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create (or reuse) the single-use approval for this exact order.

    `run_id` binds the row to the agent run that asked for it, so an unattended
    automation can find its OWN pending orders when it reports. Without it the
    row is anonymous and a scheduled bot's proposal is invisible to delivery.
    """
    if approvals is None:
        raise TradeRefused(
            "APPROVALS_UNAVAILABLE",
            "the approval store is not available; refusing to trade",
            status_code=503,
        )
    affected = {
        "order": intent.to_dict(),
        "preview": {
            "symbol": preview.get("symbol"),
            "volume": preview.get("volume"),
            "margin": preview.get("margin"),
            "spreadPoints": preview.get("spreadPoints"),
            "retcode": preview.get("retcode"),
            # The order's own max loss / % of equity / reward:risk, computed from
            # the broker's numbers. The approval card renders it, so the operator
            # approves a sized risk, not just a symbol and a volume.
            "riskSummary": risk_summary,
        },
        "guardrail": {
            "decision": getattr(guardrail, "decision", None),
            "rules": list(getattr(guardrail, "rules", []) or []),
            "reasons": list(getattr(guardrail, "reasons", []) or []),
        },
    }
    try:
        return approvals.request_kind(
            kind=APPROVAL_KIND,
            request_key=intent.request_key,
            digest=intent.digest,
            effect=APPROVAL_EFFECT,
            run_id=run_id or None,
            action_summary=order_summary(connection, intent, preview),
            affected_data=affected,
            risk=risk,
            ttl_seconds=APPROVAL_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise TradeRefused(
            "APPROVAL_REQUEST_FAILED",
            f"the approval request could not be created: {exc}",
            status_code=503,
        ) from exc


def verify_approval(
    approvals: Any, approval_id: str, intent: OrderIntent,
) -> dict[str, Any]:
    """The approval must be approved, unexpired and bound to THIS exact order."""
    if approvals is None:
        raise TradeRefused("APPROVALS_UNAVAILABLE", "the approval store is unavailable", status_code=503)
    row = approvals.get(str(approval_id or ""))
    if not row:
        raise TradeRefused("APPROVAL_NOT_FOUND", "that approval does not exist", status_code=404)
    if row.get("kind") != APPROVAL_KIND:
        raise TradeRefused("APPROVAL_NOT_FOUND", "that is not a trade approval", status_code=404)
    if row.get("serverId") != intent.request_key:
        raise TradeRefused(
            "APPROVAL_CONNECTION_MISMATCH",
            "the approval belongs to a different broker connection",
            status_code=409,
        )
    if row.get("argsDigest") != intent.digest:
        raise TradeRefused(
            "APPROVAL_ORDER_MISMATCH",
            "the approval was for a different order; re-preview this exact order",
            status_code=409,
        )
    status = str(row.get("status") or "")
    if status != "approved":
        raise TradeRefused(
            "APPROVAL_NOT_APPROVED",
            f"this order approval is {status or 'not approved'}; approve it first",
            status_code=409,
        )
    return row


def consume_approval(approvals: Any, approval_id: str, intent: OrderIntent) -> dict[str, Any]:
    """Claim the approval exactly once. A second submit must be re-approved."""
    claimed: Optional[dict[str, Any]]
    try:
        claimed = approvals.consume_kind(
            kind=APPROVAL_KIND,
            request_key=intent.request_key,
            digest=intent.digest,
        )
    except Exception as exc:  # noqa: BLE001
        raise TradeRefused(
            "APPROVAL_CONSUME_FAILED",
            f"the approval could not be claimed: {exc}",
            status_code=503,
        ) from exc
    if not claimed:
        raise TradeRefused(
            "APPROVAL_USED",
            "this approval is expired or was already used; re-preview the order",
            status_code=409,
        )
    if claimed.get("id") != approval_id:
        raise TradeRefused(
            "APPROVAL_RACE",
            "another request claimed a different approval for this order; re-preview",
            status_code=409,
        )
    return claimed


def approval_view(row: dict[str, Any]) -> dict[str, Any]:
    """A pending/decided approval plus what the chat card needs to submit it.

    The stored row carries the exact order in `affectedData` and the connection
    id in `serverId` (`trade:<connectionId>`). Exposing those lets an operator
    approve and submit from the chat composer without re-typing the order into
    another surface. Nothing here authorizes anything: `submit_order` re-verifies
    the digest, the mode and the phrase.
    """
    view = {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "actionSummary": row.get("actionSummary"),
        "risk": row.get("risk"),
        "expiresAt": row.get("expiresAt"),
        "createdAt": row.get("createdAt"),
        # The run that requested this order. A scheduled bot uses it to report
        # its OWN pending orders; it also lets an operator trace a position to a
        # run (P4 traceability).
        "requestedByRunId": row.get("requestedByRunId"),
    }
    affected: dict[str, Any] = {}
    raw = row.get("affectedData")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            # Older rows double-encoded the payload (json.dumps of a string).
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if isinstance(parsed, dict):
                affected = parsed
        except (TypeError, ValueError):
            affected = {}
    order = affected.get("order")
    if isinstance(order, dict):
        view["intent"] = order
    server = str(row.get("serverId") or "")
    if server.startswith("trade:"):
        view["connectionId"] = server.removeprefix("trade:")
    preview = affected.get("preview")
    if isinstance(preview, dict):
        view["preview"] = preview
    return view


def cooling_remaining(approval_row: dict[str, Any], guardrail: Any) -> int:
    """Seconds left on the guardrail's cooling-off, measured from the approval.

    The operator may decide quickly; the guardrail's cooling-off still has to
    elapse before the send. Anchoring to the approval's creation avoids a client
    resetting the clock by re-previewing (a pending row is reused).
    """
    seconds = int(getattr(guardrail, "cooling_off_seconds", 0) or 0)
    if seconds <= 0:
        return 0
    raw = str((approval_row or {}).get("createdAt") or "")
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return seconds
    elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    return max(0, int(seconds - elapsed))


__all__ = [
    "APPROVAL_KIND",
    "approval_view",
    "consume_approval",
    "cooling_remaining",
    "evaluate_guardrails",
    "money_intent_for",
    "order_summary",
    "request_approval",
    "verify_approval",
]
