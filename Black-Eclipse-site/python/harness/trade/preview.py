"""Order preview: validate, price-check and (optionally) request an approval.

Extracted from `trade/service.py` to keep that file inside the size ceiling. The
behavior is unchanged: the broker's own `order_check`, the connection policy, the
money guardrails and (optionally) a typed-confirmation phrase. Nothing is sent.

`record_approval=False` is the graph dry-run path: the SAME validation without an
approval row, so previewing a workflow never leaves approval cards behind.
"""

from __future__ import annotations

from typing import Any

from .adapter import AdapterUnavailable
from .authorizer import evaluate_guardrails, money_intent_for, request_approval
from .model import OrderIntent, TradeRefused
from .policy import TradePolicy
from .risk import assess_trade_risk, limits_from_connection


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _risk_summary(connection: Any, intent: OrderIntent, preview: dict[str, Any]) -> dict[str, Any] | None:
    """The order's own risk, from the broker's facts already in the preview.

    Uses only numbers the adapter returned (`price`, `stopLoss`, `volume`,
    `tickValue`, `margin`, `account`, `spreadPoints`) - no second round-trip and
    no estimate. Returns None when the order has no stop or the broker gave no
    basis, so the card shows "not measured" rather than a zero.
    """
    stop = _num(preview.get("stopLoss")) or _num(intent.stop_loss)
    price = _num(preview.get("price")) or _num(intent.price)
    volume = _num(preview.get("volume")) or _num(intent.volume)
    if stop <= 0 or price <= 0 or volume <= 0:
        return None
    distance = abs(price - stop)
    if distance <= 0:
        return None
    tick_size = _num(preview.get("tickSize"))
    tick_value = _num(preview.get("tickValue"))
    contract = _num(preview.get("contractSize"))
    if tick_size > 0 and tick_value > 0:
        loss = volume * (distance / tick_size) * tick_value
        basis = "tick-value"
    elif contract > 0:
        loss = volume * distance * contract
        basis = "contract-size"
    else:
        return None
    account = dict(preview.get("account") or {})
    equity = _num(account.get("equity")) or _num(account.get("balance"))
    if equity <= 0:
        return None
    risk_pct = loss / equity * 100.0
    limits = limits_from_connection(getattr(connection, "config", None))
    risk = assess_trade_risk(
        account=account,
        positions=[],
        symbol=str(preview.get("symbol") or intent.symbol),
        planned_risk=loss,
        risk_pct=risk_pct,
        estimated_margin=_num(preview.get("margin")) or None,
        spread_points=_num(preview.get("spreadPoints")) or None,
        point=_num(preview.get("point")),
        tick_value=tick_value,
        tick_size=tick_size,
        distance=distance,
        limits=limits,
    )
    take_profit = _num(preview.get("takeProfit")) or _num(intent.take_profit)
    summary = {
        "maxLoss": round(loss, 2),
        "riskPctOfEquity": round(risk_pct, 4),
        "basis": basis,
        "equity": round(equity, 2),
        "currency": account.get("currency") or None,
        "verdict": risk["verdict"],
        "warnings": list(risk["warnings"]),
        "reasons": list(risk["reasons"]),
        "spreadCost": risk["spreadCost"],
        "spreadPctOfRisk": risk["spreadPctOfRisk"],
        "marginUsedPct": (
            round((_num(account.get("margin")) + _num(preview.get("margin"))) / equity * 100.0, 1)
            if _num(preview.get("margin")) else None
        ),
    }
    if take_profit > 0:
        summary["rewardRisk"] = round(abs(take_profit - price) / distance, 2)
    return summary


def preview_order(
    service: Any,
    connection_id: str,
    payload: dict[str, Any],
    *,
    record_approval: bool = True,
    run_id: str = "",
) -> dict[str, Any]:
    """The preview implementation; `BrokerService.preview_order` delegates here."""
    try:
        connection, execution_mode = service._execution_connection(connection_id)
    except TradeRefused as exc:
        return {
            "available": True,
            "allowed": False,
            "status": "REFUSED",
            "mode": None,
            "connectionId": connection_id,
            "approval": None,
            "error": exc.to_dict(),
            "reasons": [exc.message],
        }
    intent = OrderIntent.from_payload(
        payload, connection_id=connection.id, magic_default=service._magic(connection)
    )
    adapter = service._trading_adapter(connection)
    try:
        preview = adapter.order_preview(intent)
    except AdapterUnavailable as exc:
        raise TradeRefused(exc.code, exc.reason, status_code=503) from exc
    except TradeRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TradeRefused(
            "ADAPTER_ERROR", f"{connection.kind} adapter failed: {exc}", status_code=502
        ) from exc
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        service._assert_account_mode(preview, execution_mode)
    except TradeRefused as exc:
        service.connections.record_event(
            connection.id, "preview", "refused",
            {"digest": intent.digest, "code": exc.code},
        )
        return {
            "available": True,
            "allowed": False,
            "status": "REFUSED",
            "mode": execution_mode,
            "connectionId": connection.id,
            "intent": intent.to_dict(),
            "digest": intent.digest,
            "preview": preview,
            "policy": None,
            "guardrail": None,
            "approval": None,
            "requiresTypedConfirmation": False,
            "confirmPhrase": None,
            "error": exc.to_dict(),
            "reasons": [exc.message],
        }
    policy = TradePolicy.from_connection(connection).evaluate(preview)
    money_intent = money_intent_for(connection, intent, preview)
    verdict = evaluate_guardrails(service.guardrails, money_intent, record=False)
    reasons = (
        list(preview.get("reasons") or [])
        + list(policy.get("reasons") or [])
        + list(getattr(verdict, "reasons", []) or [])
    )
    allowed = (
        bool(preview.get("ok"))
        and bool(policy.get("allowed"))
        and str(verdict.decision) not in ("deny", "escalate")
    )
    if execution_mode == "live":
        requires_typed = True
        confirm_phrase = (
            f"LIVE {intent.side.upper()} "
            f"{float(preview.get('volume') or intent.volume):g} "
            f"{preview.get('symbol') or intent.symbol}"
        )
    else:
        requires_typed = bool(verdict.requires_typed_confirmation)
        confirm_phrase = str(verdict.confirm_phrase) if requires_typed else None
    risk_summary = _risk_summary(connection, intent, preview)
    if risk_summary and risk_summary.get("verdict") == "block":
        # An order that breaches the account's own risk limits is refused BEFORE
        # an approval card exists. The operator gets the reason, not a card they
        # should not approve.
        allowed = False
        reasons.extend(risk_summary.get("reasons") or [])
    approval = None
    if allowed and record_approval:
        approval = request_approval(
            service.approvals,
            connection,
            intent,
            preview,
            verdict,
            risk="critical" if execution_mode == "live" else "high",
            run_id=run_id,
            risk_summary=risk_summary,
        )
        if str(approval.get("status")) == "denied":
            allowed = False
            reasons.append(
                "this exact order was denied; change the order to request a new decision"
            )
    service.connections.record_event(
        connection.id,
        "preview",
        "allowed" if allowed else "refused",
        {"digest": intent.digest, "rules": policy.get("rules") or []},
    )
    return {
        "available": True,
        "allowed": allowed,
        "mode": execution_mode,
        "connectionId": connection.id,
        "intent": intent.to_dict(),
        "digest": intent.digest,
        "preview": preview,
        "policy": policy,
        "guardrail": verdict.to_dict(),
        "riskSummary": risk_summary,
        "approval": service._public_approval(approval),
        "requiresTypedConfirmation": requires_typed,
        "confirmPhrase": confirm_phrase,
        "reasons": reasons,
    }


__all__ = ["preview_order"]
