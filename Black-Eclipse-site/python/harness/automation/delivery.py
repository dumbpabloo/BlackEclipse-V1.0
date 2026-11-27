"""Automation delivery (§44).

Reuses the existing in-app bus, artifact directory and messaging vocabulary from
`harness.jobs.delivery` — this is not a second notification subsystem. The message
body is always a template KEY + parameters, never composed prose, so the TS
surface renders it through `src/lib/i18n`.

Delivery NEVER fails the run: an outcome is recorded per target and a failure is
reported honestly (`ok=False` + a reason code).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..jobs.delivery import artifact_dir_for
from .model import AutomationDefinition, DeliveryTarget

STATUS_WORD = {
    "completed": "delivered",
    "failed": "couldn't finish",
    "timed_out": "stopped early",
    "skipped": "skipped",
    "blocked": "not allowed",
    "waiting": "waiting for approval",
}

#: The FIRST line of every delivered report, in plain words (Phase 17's
#: unattended-answer contract). No mechanism vocabulary, no exit codes.
OUTCOME_FIRST_LINE = {
    "completed": "Finished as asked.",
    "failed": "Couldn't finish.",
    "timed_out": "Stopped early.",
    "skipped": "Skipped this time.",
    "blocked": "Couldn't run — not allowed.",
    "waiting": "Waiting for your approval.",
    "approval_required": "Waiting for your approval.",
}

_UNKNOWN_OUTCOME_LINE = "Finished with something worth a look."


def render_run_report(
    definition: AutomationDefinition,
    state: str,
    output: Optional[str],
    *,
    limits: Optional[list[str]] = None,
    pending_approvals: Optional[list[dict[str, Any]]] = None,
) -> str:
    """The text a delivered job report must carry (the unattended contract).

    Rules (asserted by tests, not by convention):
      1. The FIRST line states the outcome in plain words — and when the run left
         an order waiting for the operator's decision, THAT is the outcome, named
         with the exact order and the window it is actually good for.
      2. A run that hit limits names EVERY limit it hit — never a silent short
         report. (The caller passes the limits it recorded; an empty list is a
         complete run.)
      3. A run that produced nothing says so instead of delivering an empty
         report as a success.
      4. No mechanism word reaches the text.
    """
    pending = [p for p in (pending_approvals or []) if p.get("approvalId")]
    if pending:
        lines = []
        for order in pending:
            summary = str(order.get("summary") or "an order").strip()
            window = _window_text(order.get("expiresAt"))
            lines.append(f"AWAITING APPROVAL — {summary}{window}")
            lines.append(f"Approval {order.get('approvalId')} · approve it in this bot's chat.")
    else:
        lines = [OUTCOME_FIRST_LINE.get(str(state or "").strip(), _UNKNOWN_OUTCOME_LINE)]
    for limit in limits or []:
        text = str(limit).strip()
        if text:
            lines.append(f"- {text}")
    body = (output or "").strip()
    if body:
        lines.append("")
        lines.append(body)
    elif str(state or "").strip() == "completed" and not pending:
        lines.append("It produced no result this time.")
    return "\n".join(lines) + "\n"


def _window_text(expires_at: Any) -> str:
    """How long the operator actually has, in plain words.

    A trade approval is short-lived on purpose (a stale market order is worse than
    no order). The report must say so instead of implying the proposal waits
    indefinitely — the operator may be asleep when the bot runs.
    """
    raw = str(expires_at or "").strip()
    if not raw:
        return " (approve it soon — it expires quickly)"
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return " (approve it soon — it expires quickly)"
    remaining = int((expiry - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return " (the approval window has already closed — run the bot again)"
    if remaining < 90:
        return f" (good for {max(1, remaining)} seconds)"
    return f" (good for {remaining // 60} minutes)"


@dataclass
class DeliveryOutcome:
    adapter: str
    destination: Optional[str]
    ok: bool
    reason_code: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter, "destination": self.destination, "ok": self.ok,
            "reasonCode": self.reason_code, "reason": self.reason,
        }


def _message(definition: AutomationDefinition, state: str) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"automationName": definition.name}
    if state != "completed":
        return "automations.delivery.message.trouble", params
    return "automations.delivery.message.done", params


def _write_artifact(
    run_id: str,
    definition: AutomationDefinition,
    state: str,
    output: Optional[str],
    *,
    limits: Optional[list[str]] = None,
    pending_approvals: Optional[list[dict[str, Any]]] = None,
) -> DeliveryOutcome:
    try:
        directory: Path = artifact_dir_for(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.md").write_text(
            render_run_report(
                definition, state, output,
                limits=limits, pending_approvals=pending_approvals,
            ),
            encoding="utf-8",
        )
        (directory / "meta.json").write_text(json.dumps({
            "automationId": definition.id,
            "automationName": definition.name,
            "automationVersion": definition.version,
            "state": state,
            "pendingApprovals": [
                str(p.get("approvalId")) for p in (pending_approvals or [])
            ],
        }, indent=2), encoding="utf-8")
        return DeliveryOutcome(adapter="artifact", destination="report", ok=True)
    except Exception as exc:  # noqa: BLE001 - a write failure is reported honestly
        return DeliveryOutcome(adapter="artifact", destination="report", ok=False,
                               reason_code="DELIVERY_FAILED", reason=str(exc)[:500])


async def deliver_automation(
    definition: AutomationDefinition,
    *,
    execution_id: str,
    run_id: Optional[str],
    state: str,
    output: Optional[str],
    store: Any,
    bus: Any,
    limits: Optional[list[str]] = None,
    pending_approvals: Optional[list[dict[str, Any]]] = None,
    channel_service: Any = None,
) -> list[dict[str, Any]]:
    """Deliver to every configured target; record one outcome per target.

    `limits` names anything the run could not complete (stale source, refused
    check, missing input). The artifact report always carries them — a delivered
    run never hides a limit (the unattended-answer contract).
    """
    if not definition.delivery.wants(state):
        return []
    targets: list[DeliveryTarget] = definition.delivery.effective_targets()
    outcomes: list[DeliveryOutcome] = []
    message_key, message_params = _message(definition, state)
    for target in targets:
        if target.adapter == "in_app":
            # A pending order must be findable in the bot's OWN conversation: the
            # bot id is the thread the Bots workspace opens, and the approval ids
            # let the composer banner (already mounted there) pick them up.
            ok = _publish(bus, "automation.delivered", {
                "automationId": definition.id, "runId": run_id, "executionId": execution_id,
                "status": state, "messageKey": message_key, "messageParams": message_params,
                "botId": definition.id,
                "approvalIds": [
                    str(p.get("approvalId")) for p in (pending_approvals or [])
                ],
            })
            outcomes.append(DeliveryOutcome(adapter="in_app", destination=target.destination, ok=ok,
                                            reason_code=None if ok else "NO_DESTINATION",
                                            reason=None if ok else "the event bus is unavailable"))
        elif target.adapter == "artifact":
            if not run_id:
                outcomes.append(DeliveryOutcome(adapter="artifact", destination=target.destination,
                                                ok=False, reason_code="NO_DESTINATION",
                                                reason="no run id to name the artifact"))
            else:
                outcomes.append(_write_artifact(
                    run_id, definition, state, output,
                    limits=limits, pending_approvals=pending_approvals,
                ))
        elif target.adapter == "messaging":
            destination = (target.destination or "").strip()
            if not destination:
                outcomes.append(DeliveryOutcome(adapter="messaging", destination=None, ok=False,
                                                reason_code="NO_DESTINATION",
                                                reason="no destination is set for this delivery"))
                continue
            # `<kind>:<external chat>` (telegram:123, slack:C123, email:...).
            # This is the REAL send: the channel gateway owns provider transports,
            # so a bot does not grow a second notifier. Without a configured
            # channel (or a malformed destination) the delivery fails honestly.
            kind, _, external_chat = destination.partition(":")
            text = render_run_report(
                definition, state, output,
                limits=limits, pending_approvals=pending_approvals,
            )
            if channel_service is not None and kind and external_chat:
                try:
                    sent = await channel_service.send_outbound(kind, external_chat, text)
                    ok = bool(sent.get("ok"))
                    reason = None if ok else str(sent.get("reason") or "channel send failed")[:500]
                except Exception as exc:  # noqa: BLE001 - a send failure is reported, not raised
                    ok = False
                    reason = str(exc)[:500]
                outcomes.append(DeliveryOutcome(
                    adapter="messaging", destination=destination, ok=ok,
                    reason_code=None if ok else "DELIVERY_FAILED", reason=reason,
                ))
            else:
                # Compatibility: a runtime without the channel layer still
                # publishes the template event, but it is no longer the only way.
                ok = _publish(bus, "automation.message", {
                    "automationId": definition.id, "executionId": execution_id,
                    "destination": destination, "status": state,
                    "messageKey": message_key, "messageParams": message_params,
                })
                outcomes.append(DeliveryOutcome(
                    adapter="messaging", destination=destination, ok=ok,
                    reason_code=None if ok else "NO_DESTINATION",
                    reason=None if ok else "the channel gateway is unavailable",
                ))
        else:
            outcomes.append(DeliveryOutcome(adapter=target.adapter, destination=target.destination,
                                            ok=False, reason_code="UNKNOWN_ADAPTER",
                                            reason=f"unknown adapter {target.adapter!r}"))

    for outcome in outcomes:
        try:
            store.record_delivery(
                execution_id, adapter=outcome.adapter, destination=outcome.destination,
                ok=outcome.ok, reason_code=outcome.reason_code, reason=outcome.reason,
            )
        except Exception:  # noqa: BLE001 - a delivery record failure must not fail the run
            pass
    return [o.to_dict() for o in outcomes]


def _publish(bus: Any, event_type: str, data: dict[str, Any]) -> bool:
    if bus is None:
        return False
    try:
        bus.publish({"type": event_type, "runId": data.get("runId"), "data": data})
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["DeliveryOutcome", "deliver_automation", "render_run_report"]
