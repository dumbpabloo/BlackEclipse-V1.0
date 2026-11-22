"""LLM-facing tools for the Bot / Automation workspace.

These register on the SAME tool registry as every other subsystem, so
validation, permission and effect authorization apply unchanged. They are thin
wrappers over the ONE `AutomationService`, so the chat agent and the Bots UI can
never disagree about what a bot is or what it is doing.

A "bot" is a `run_agent` automation tagged `bot` (monitors are tagged `bot` +
`monitor` too): a durable, scheduled agent that keeps running in the background.
Unrelated automations (jobs, event rules) share the engine but are NOT bots.
Creation persists through the same validated service the UI uses; nothing is
claimed until the store confirms it.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from ..jobs.proposals import TTL_MS as PROPOSAL_TTL_MS, ProposalError, commit, is_system_job, new_proposal
from ..tools import ToolContext, ToolDefinition, ToolRegistry

TOOL_NAMES = (
    "bot_create",
    "bot_list",
    "bot_get",
    "bot_update",
    "bot_run",
    "bot_pause",
    "bot_resume",
    "bot_enable",
    "bot_disable",
    "bot_delete",
    "bot_status",
    "bot_history",
    "bot_inspect_run",
)

_service: Any = None


def set_automation_service(service: Any) -> None:
    global _service
    _service = service


def get_automation_service() -> Any:
    return _service


def _require_service() -> Any:
    if _service is None:
        raise RuntimeError("the automation service is unavailable")
    return _service


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _is_bot(definition: Any) -> bool:
    """A bot is tagged `bot` (or is a monitor); jobs/event rules share the engine
    but must never surface as bots in `bot_list`."""
    tags = list(getattr(definition, "tags", None) or [])
    return "bot" in tags or "monitor" in tags or str(getattr(definition, "id", "")).startswith("mon_")


def _obj(properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _known_agent_ids() -> set[str]:
    """Every installed agent preset id, resolved lazily.

    `harness.agents` imports this module (it owns the `bot_*` allowlist), so the
    import must stay inside the function or the two modules deadlock at import.
    An empty set means the preset registry could not be read; callers then skip
    the check rather than refusing every bot.
    """
    try:
        from ..agents import AGENT_PRESETS  # noqa: PLC0415 - breaks an import cycle
    except Exception:  # noqa: BLE001 - an unreadable registry is not a refusal
        return set()
    return set(AGENT_PRESETS.keys())


def _agent_refusal(agent_id: str, known: set[str]) -> Optional[str]:
    if not known or agent_id in known:
        return None
    return (
        f"Unknown agent preset {agent_id!r}. Known presets: "
        f"{', '.join(sorted(known))}. Nothing was created."
    )


#: Definition-level blocks a bot may set, each already modelled by
#: `AutomationDefinition` (execution/retry/concurrency/limits/delivery/
#: permissions). Passing them through is what closes the gap between a bot the
#: chat agent creates and one the Bots UI creates.
_RUNTIME_BLOCKS = ("execution", "retry", "concurrency", "limits", "delivery", "permissions")


def _runtime_blocks(args: dict[str, Any]) -> dict[str, Any]:
    """The optional definition blocks present in `args`, as camelCase payload."""
    out: dict[str, Any] = {}
    for key in _RUNTIME_BLOCKS:
        value = args.get(key)
        if isinstance(value, dict) and value:
            out[key] = value
    return out


def _authority_schema() -> dict[str, Any]:
    """The bot's execution leash (P5). A new bot is L0; raising it is operator-gated."""
    return {
        "type": "object",
        "description": "How much this bot may do unattended. A NEW bot starts at "
        "l0; raising the tier needs the operator's approval, and l2 needs a scored "
        "track record. Limits apply in addition to the mandate's caps.",
        "properties": {
            "tier": {"type": "string", "enum": ["l0", "l1", "l2"]},
            "appetite": {
                "type": "string", "enum": ["conservative", "normal", "aggressive"],
            },
            "sizingMode": {
                "type": "string", "enum": ["risk_based", "user_fixed"],
            },
            "requireStop": {"type": "boolean"},
            "maxOpenPositions": {"type": "integer", "minimum": 1, "maximum": 50},
            "maxMarginPerOrder": {"type": "number", "minimum": 0},
            "cooldownS": {"type": "integer", "minimum": 0, "maximum": 86400},
        },
    }


def _runtime_block_schema() -> dict[str, Any]:
    """The shared JSON schema for the optional definition blocks."""
    return {
        "execution": {
            "type": "object",
            "description": "Timing policy: maxRuntimeS, timeoutS, misfire "
            "(skip|run_once|catch_up), catchUpMax.",
            "properties": {
                "maxRuntimeS": {"type": "integer", "minimum": 5, "maximum": 86400},
                "timeoutS": {"type": "integer", "minimum": 1, "maximum": 86400},
                "misfire": {"type": "string", "enum": ["skip", "run_once", "catch_up"]},
                "catchUpMax": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        "retry": {
            "type": "object",
            "description": "Retry policy: maxAttempts, backoff knobs.",
            "properties": {
                "maxAttempts": {"type": "integer", "minimum": 1, "maximum": 10},
                "initialBackoffS": {"type": "number", "minimum": 0},
                "backoffMultiplier": {"type": "number", "minimum": 1},
                "maxBackoffS": {"type": "number", "minimum": 0},
                "jitter": {"type": "boolean"},
            },
        },
        "concurrency": {
            "type": "object",
            "description": "Concurrency policy: maxConcurrentRuns, onConflict "
            "(skip_if_running|queue_if_running|cancel_previous).",
            "properties": {
                "maxConcurrentRuns": {"type": "integer", "minimum": 1, "maximum": 50},
                "onConflict": {
                    "type": "string",
                    "enum": ["skip_if_running", "queue_if_running", "cancel_previous"],
                },
            },
        },
        "limits": {
            "type": "object",
            "description": "Resource limits enforced (or honestly reported) at run end.",
            "properties": {
                "maxRuntimeS": {"type": "integer", "minimum": 1, "maximum": 86400},
                "maxTokens": {"type": "integer", "minimum": 0},
                "maxCost": {"type": "number", "minimum": 0},
                "maxToolCalls": {"type": "integer", "minimum": 0},
                "maxParallelTasks": {"type": "integer", "minimum": 1},
                "maxRetries": {"type": "integer", "minimum": 0},
            },
        },
        "delivery": {
            "type": "object",
            "description": "When and how a finished run reaches the operator.",
            "properties": {
                "adapter": {"type": "string", "enum": ["in_app", "artifact", "messaging"]},
                "destination": {"type": "string"},
                "title": {"type": "string"},
                "onSuccess": {"type": "boolean"},
                "onFailure": {"type": "boolean"},
                "onApprovalRequired": {"type": "boolean"},
                "onTimeout": {"type": "boolean"},
            },
        },
        "permissions": {
            "type": "object",
            "description": "Effective permission policy for this bot. Deny wins.",
            "properties": {
                "allowed": {"type": "array", "items": {"type": "string"}},
                "denied": {"type": "array", "items": {"type": "string"}},
                "requiresApproval": {"type": "boolean"},
                "approvalScope": {"type": "string"},
            },
        },
    }


def _build_trigger(args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("triggerType") or "manual")
    timezone = str(args.get("timezone") or "UTC")
    trigger: dict[str, Any] = {"type": kind, "timezone": timezone}
    if kind == "cron":
        trigger["cron"] = str(args.get("cron") or "")
    elif kind == "interval":
        trigger["intervalSeconds"] = int(args.get("intervalSeconds") or 0)
    elif kind == "one_time":
        trigger["runAt"] = args.get("runAt")
    elif kind == "event":
        trigger["eventType"] = str(args.get("eventType") or "")
        raw_filter = args.get("eventFilter")
        trigger["eventFilter"] = dict(raw_filter) if isinstance(raw_filter, dict) else {}
    elif kind == "condition":
        trigger["condition"] = {"expression": str(args.get("conditionExpression") or "")}
    return trigger


#: Trigger kinds that make a bot run BY ITSELF. A manual bot only runs when the
#: user (or `bot_run`) asks, so it needs no schedule approval.
_AUTONOMOUS_TRIGGERS = ("cron", "interval", "one_time", "event", "condition")

#: Pending delete confirmations, keyed by proposal id. In-process and single-use:
#: the reference persists proposals to disk, but a confirmation that outlives the
#: runtime has no scheduler to act on it, so keeping it in memory is the honest scope
#: (stated here rather than implied).
_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _remember_proposal(proposal: dict[str, Any]) -> None:
    with _PENDING_LOCK:
        # Drop anything already past its deadline so the map cannot grow forever.
        now = _now_ms()
        for key in [k for k, v in _PENDING.items() if int(v.get("expiresAt") or 0) < now]:
            _PENDING.pop(key, None)
        _PENDING[str(proposal["proposalId"])] = proposal


def _take_proposal(proposal_id: str) -> Optional[dict[str, Any]]:
    with _PENDING_LOCK:
        return _PENDING.pop(proposal_id, None)

_SCHEDULE_NEEDS_APPROVAL = (
    "This needs your go-ahead first: I've asked to set up (or start) a scheduled "
    "task. Nothing runs on a schedule until you approve it."
)


async def _approve_autonomous_schedule(ctx: ToolContext, summary: str) -> bool:
    """The operator's go-ahead for creating or starting an autonomous schedule.

    Mirrors `workspace_patch_apply`'s single-use flow: a matching approval is
    claimed and consumed; otherwise one is requested, the approval card is
    emitted, and when the run asked to pause we block for the decision and
    re-claim it. Without an approvals backend (tests / zero-config local runs) the
    tool stays admitted — the run's profile and allowlist already gated it; WITH a
    store, an autonomous schedule needs the operator's explicit yes (R3-07).
    """
    approvals = getattr(ctx, "approvals", None)
    if approvals is None:
        return True
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    key = str(getattr(ctx, "run_id", "") or "bot")
    try:
        if approvals.consume_kind(kind="bot_schedule", request_key=key, digest=digest) is not None:
            return True
        pending = approvals.request_kind(
            kind="bot_schedule", request_key=key, digest=digest,
            effect="external_action", run_id=key,
            action_summary=summary, risk="medium",
        )
    except Exception:  # noqa: BLE001 - a store error is a refusal, never a bypass
        return False
    try:
        from ..code.events import emit_code_event  # noqa: PLC0415

        await emit_code_event(ctx, "approval.requested", {
            "approvalId": (pending or {}).get("id"),
            "kind": "bot_schedule",
            "title": "Approve a scheduled task",
            "summary": summary,
            "effect": "external_action",
            "risk": "medium",
        })
    except Exception:  # noqa: BLE001
        pass
    if pending is not None:
        try:
            from ..mcp.approvals import pause_for_decision  # noqa: PLC0415

            if await pause_for_decision(
                approvals, ctx, pending.get("id"),
                lambda: approvals.consume_kind(kind="bot_schedule", request_key=key, digest=digest),
                emitter=getattr(ctx, "emitter", None), digest=digest,
                event_type="bot.schedule.approved",
                approved_payload={"kind": "bot_schedule", "summary": summary},
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


#: The minimum evidence before L2 can even be REQUESTED. The operator still has to
#: approve; this stops the request from being made on no record at all.
L2_MIN_SCORED_DECISIONS = 30
L2_MIN_WINDOW_DAYS = 42


def _bot_track_record(ctx: Any, bot_id: str) -> dict[str, Any]:
    """The bot's scored advisory record, read from its private conversation.

    Best-effort and read-only: an unavailable layer yields `available: False`, and
    the caller treats that as "not enough evidence" rather than as a pass.
    """
    service = getattr(ctx, "advisory_service", None)
    if service is None:
        return {"available": False, "reason": "the advisory ledger is unavailable"}
    try:
        decisions = service.list_decisions(conversation_id=f"bot-{bot_id}", limit=1000)
        outcome_rows = service.store.outcomes_for([d["id"] for d in decisions])
    except Exception:  # noqa: BLE001 - an unreadable record is not evidence
        return {"available": False, "reason": "the advisory ledger could not be read"}
    right = sum(1 for o in outcome_rows if str(o.get("verdict") or "").lower() == "right")
    wrong = sum(1 for o in outcome_rows if str(o.get("verdict") or "").lower() == "wrong")
    scored = right + wrong
    first = min((str(d.get("createdAt") or "") for d in decisions), default="")
    return {
        "available": True,
        "decisions": len(decisions),
        "scored": scored,
        "right": right,
        "wrong": wrong,
        "hitRate": (right / scored) if scored else None,
        "since": first or None,
    }


def _track_record_text(record: dict[str, Any]) -> str:
    if not record.get("available"):
        return f"No scored record is available ({record.get('reason')})."
    scored = int(record.get("scored") or 0)
    if not scored:
        return f"{record.get('decisions', 0)} recorded call(s), none scored yet."
    return (
        f"{scored} scored call(s): {record.get('right')} right, "
        f"{record.get('wrong')} wrong"
        + (f" ({record['hitRate']:.0%} hit rate)" if record.get("hitRate") is not None else "")
    )


async def _approve_execution_authority(
    ctx: ToolContext, bot_id: str, tier: str, record: dict[str, Any],
) -> bool:
    """The operator's explicit go-ahead to raise a bot's execution tier.

    The same single-use, digest-bound shape as a scheduled-task approval: with no
    approval store the request is refused (raising money authority is never a
    default), and with one the operator sees the bot, the tier and its record.
    """
    approvals = getattr(ctx, "approvals", None)
    if approvals is None:
        return False
    import hashlib  # noqa: PLC0415

    summary = (
        f"Grant execution tier {tier.upper()} to bot {bot_id}. "
        f"Track record: {_track_record_text(record)}. "
        + (
            "LIVE orders will be placed without per-order approval, within the "
            "bot's caps, mandate and the kill switch."
            if tier == "l2"
            else "Unattended DEMO orders only; no money at risk."
        )
    )
    digest = hashlib.sha256(f"{bot_id}:{tier}:{summary}".encode("utf-8")).hexdigest()
    key = f"{getattr(ctx, 'run_id', '') or 'bot'}:{bot_id}:{tier}"
    kind = "bot_authority"
    try:
        if approvals.consume_kind(kind=kind, request_key=key, digest=digest) is not None:
            return True
        pending = approvals.request_kind(
            kind=kind, request_key=key, digest=digest,
            effect="external_action", run_id=str(getattr(ctx, "run_id", "") or ""),
            action_summary=summary, risk="critical" if tier == "l2" else "high",
        )
    except Exception:  # noqa: BLE001 - a store error is a refusal, never a bypass
        return False
    try:
        from ..code.events import emit_code_event  # noqa: PLC0415

        await emit_code_event(ctx, "approval.requested", {
            "approvalId": (pending or {}).get("id"),
            "kind": kind,
            "title": f"Grant {tier.upper()} execution to a bot" if tier != "l0" else "Lower a bot's authority",
            "summary": summary,
            "effect": "external_action",
            "risk": "critical" if tier == "l2" else "high",
        })
    except Exception:  # noqa: BLE001
        pass
    if pending is not None:
        try:
            from ..mcp.approvals import pause_for_decision  # noqa: PLC0415

            if await pause_for_decision(
                approvals, ctx, pending.get("id"),
                lambda: approvals.consume_kind(kind=kind, request_key=key, digest=digest),
                emitter=getattr(ctx, "emitter", None), digest=digest,
                event_type="bot.authority.approved",
                approved_payload={"kind": kind, "botId": bot_id, "tier": tier},
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


async def _create(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    objective = str(args.get("objective") or "").strip()
    name = str(args.get("name") or "").strip()
    if not objective:
        return "A bot needs an objective.", {"created": False, "reason": "objective required"}
    agent_id = str(args.get("agentId") or "eclipse-agent")
    known = _known_agent_ids()
    refusal = _agent_refusal(agent_id, known)
    if refusal:
        return refusal, {"created": False, "reason": "unknown_agent", "agentId": agent_id}
    if not name:
        name = objective[:60]
    trigger = _build_trigger(args)
    if trigger.get("type") == "cron" and not str(trigger.get("cron") or "").strip():
        return (
            "A scheduled bot needs a cron expression (for example '0 9 * * *') and an "
            "IANA timezone (for example 'America/New_York'). Nothing was created.",
            {"created": False, "reason": "cron expression required"},
        )
    if trigger.get("type") == "interval" and int(trigger.get("intervalSeconds") or 0) <= 0:
        return (
            "An interval bot needs a positive interval in seconds. Nothing was created.",
            {"created": False, "reason": "intervalSeconds required"},
        )
    if trigger.get("type") in _AUTONOMOUS_TRIGGERS:
        summary = f"Create a scheduled task ({trigger.get('type')}): {objective[:120]}"
        if not await _approve_autonomous_schedule(ctx, summary):
            return _SCHEDULE_NEEDS_APPROVAL, {"created": False, "approved": False}
    action: dict[str, Any] = {
        "type": "run_agent",
        "objective": objective,
        "agentId": agent_id,
    }
    authority = args.get("executionAuthority")
    if isinstance(authority, dict) and str(authority.get("tier") or "l0").lower() != "l0":
        return (
            "A NEW bot starts at L0 (propose only). Create it, let it build a scored "
            "record, then raise its execution tier with `bot_update` — the operator "
            "approves that grant explicitly. Nothing was created.",
            {"created": False, "reason": "tier_requires_existing_bot"},
        )
    # Parity with the Bots UI: a bot may be bound to a workspace (which is what
    # grants code verbs) and carry an autonomy profile. Without a workspace the
    # runtime's default-actor rule still grants no code permission.
    if args.get("workspaceRoot"):
        action["workspaceRoot"] = str(args["workspaceRoot"])
    if args.get("autonomy"):
        action["autonomy"] = str(args["autonomy"])
    payload: dict[str, Any] = {
        "name": name,
        "description": args.get("description") or objective,
        "state": str(args.get("state") or "active"),
        "trigger": trigger,
        "action": action,
        "tags": ["bot"],
        **_runtime_blocks(args),
    }
    try:
        definition = service.create(payload, created_by="agent")
    except Exception as exc:  # noqa: BLE001 - a domain error is reported, not raised
        return f"Could not create the bot: {exc}", {"created": False, "error": str(exc)}
    view = definition.public()
    return _dumps({"created": True, "bot": view}), {"created": True, "bot": view}


async def _list(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bots = [b for b in service.list(
        limit=int(args.get("limit") or 100),
        scope_id=args.get("scopeId"),
        state=args.get("state"),
    ) if _is_bot(b)]
    views = [b.public() for b in bots]
    return _dumps({"count": len(views), "bots": views}), {"bots": views}


async def _get(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    definition = service.get(bot_id)
    if definition is None:
        return f"No bot {bot_id!r}.", {"found": False, "botId": bot_id}
    view = definition.public()
    return _dumps(view), view


async def _update(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    definition = service.get(bot_id)
    if definition is None:
        return f"No bot {bot_id!r}.", {"updated": False, "botId": bot_id}
    trigger_change = str(args.get("triggerType") or "")
    if trigger_change in _AUTONOMOUS_TRIGGERS:
        summary = f"Put an existing task on a schedule ({trigger_change}): {bot_id}"
        if not await _approve_autonomous_schedule(ctx, summary):
            return _SCHEDULE_NEEDS_APPROVAL, {"updated": False, "approved": False, "botId": bot_id}
    patch: dict[str, Any] = {}
    if args.get("name") is not None:
        patch["name"] = str(args["name"])
    if args.get("objective") is not None:
        # Update the typed Action model (not a raw dict) so the service's re-type
        # step never sees an unvalidated field.
        patch["description"] = str(args["objective"])
        patch["action"] = definition.action.model_copy(update={"objective": str(args["objective"])})
    if args.get("agentId") is not None:
        agent_id = str(args["agentId"])
        refusal = _agent_refusal(agent_id, _known_agent_ids())
        if refusal:
            return refusal, {"updated": False, "reason": "unknown_agent", "agentId": agent_id}
        base_action = patch.get("action") or definition.action
        patch["action"] = base_action.model_copy(update={"agent_id": agent_id})
    # The same definition blocks the UI can edit, applied through the typed model.
    action_patch: dict[str, Any] = {}
    if args.get("workspaceRoot") is not None:
        action_patch["workspace_root"] = str(args["workspaceRoot"]) or None
    if args.get("autonomy") is not None:
        action_patch["autonomy"] = str(args["autonomy"]) or None
    if action_patch:
        base_action = patch.get("action") or definition.action
        patch["action"] = base_action.model_copy(update=action_patch)
    authority = args.get("executionAuthority")
    if isinstance(authority, dict) and authority:
        requested = str(authority.get("tier") or "").lower()
        current = str(getattr(definition.execution_authority, "tier", "l0") or "l0").lower()
        if requested in ("l1", "l2") and requested != current:
            record = _bot_track_record(ctx, bot_id)
            if requested == "l2" and (
                not record.get("available")
                or int(record.get("scored") or 0) < L2_MIN_SCORED_DECISIONS
            ):
                return (
                    f"L2 needs a scored record of at least {L2_MIN_SCORED_DECISIONS} "
                    f"calls over {L2_MIN_WINDOW_DAYS} days first. This bot: "
                    f"{_track_record_text(record)}. Nothing was changed.",
                    {"updated": False, "reason": "track_record_insufficient",
                     "record": record},
                )
            if not await _approve_execution_authority(ctx, bot_id, requested, record):
                return (
                    "Raising a bot's execution tier needs your explicit approval; "
                    "I've asked for it and nothing has changed yet.",
                    {"updated": False, "approved": False, "tier": requested},
                )
        patch["executionAuthority"] = authority
    patch.update(_runtime_blocks(args))
    if args.get("triggerType") is not None:
        patch["trigger"] = _build_trigger(args)
    if not patch:
        return "Nothing to update.", {"updated": False, "reason": "no fields"}
    try:
        saved = service.update(bot_id, patch, updated_by="agent")
    except Exception as exc:  # noqa: BLE001
        return f"Could not update the bot: {exc}", {"updated": False, "error": str(exc)}
    view = saved.public()
    return _dumps({"updated": True, "bot": view}), {"updated": True, "bot": view}


async def _run(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    definition = service.get(bot_id)
    if definition is None:
        return f"No bot {bot_id!r}.", {"started": False, "botId": bot_id}
    if definition.state != "active":
        return f"Bot {bot_id!r} is {definition.state}; enable it before running.", {"started": False, "state": definition.state}
    execution_id = await service.run_now(bot_id)
    if execution_id is None:
        return f"Could not start bot {bot_id!r}.", {"started": False}
    return _dumps({"started": True, "executionId": execution_id}), {"started": True, "executionId": execution_id}


async def _delete(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    # A scheduled bot owns a durable side effect, so deleting one takes two steps:
    # a proposal with a 15-minute deadline, then a commit. An expired or superseded
    # confirmation can no longer remove a running schedule, which is the failure mode
    # the reference's proposals exist to prevent.
    if is_system_job(bot_id):
        return (
            f"Bot {bot_id!r} is a scheduler-owned system job and cannot be deleted.",
            {"deleted": False, "reason": "system_job"},
        )
    if not bool(args.get("confirm")):
        proposal = new_proposal("cancel", now_ms=_now_ms(), job_id=bot_id)
        _remember_proposal(proposal)
        minutes = PROPOSAL_TTL_MS // 60_000
        return (
            f"Deleting bot {bot_id!r} is irreversible, so it needs confirmation. "
            f"Call bot_delete again with confirm=true and proposalId="
            f"{proposal['proposalId']} within {minutes} minutes.",
            {
                "deleted": False,
                "needsConfirmation": True,
                "proposalId": proposal["proposalId"],
                "expiresAt": proposal["expiresAt"],
            },
        )
    proposal_id = str(args.get("proposalId") or "")
    proposal = _take_proposal(proposal_id)
    if proposal is None:
        return (
            "That confirmation is unknown or already used. Start again with "
            "bot_delete without confirm to get a fresh proposal.",
            {"deleted": False, "reason": "unknown_proposal", "proposalId": proposal_id},
        )
    if str(proposal.get("jobId")) != bot_id:
        return (
            "That confirmation was issued for a different bot. Nothing was deleted.",
            {"deleted": False, "reason": "proposal_mismatch"},
        )
    try:
        commit(proposal, now_ms=_now_ms(), executor_ready=True, job_exists=True)
    except ProposalError as exc:
        return (f"Nothing was deleted: {exc}", {"deleted": False, "reason": "proposal_rejected"})
    ok = service.delete(bot_id)
    if not ok:
        return f"No bot {bot_id!r}.", {"deleted": False}
    # Privacy: deleting a bot also forgets its PRIVATE conversation memory (the
    # `bot-<id>` bucket), archived rather than hard-deleted. Best-effort — a purge
    # failure never blocks the delete and is reported honestly.
    purged = 0
    memory = getattr(service, "memory", None)
    if memory is not None:
        try:
            purged = len(memory.forget_conversation(f"bot-{bot_id}").get("archived") or [])
        except Exception:  # noqa: BLE001 - a purge failure must not fail the delete
            purged = 0
    return (
        _dumps({"deleted": True, "botId": bot_id, "memoryPurged": purged}),
        {"deleted": True, "botId": bot_id, "memoryPurged": purged},
    )


async def _status(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    result = service.status(bot_id)
    if result is None:
        return f"No bot {bot_id!r}.", {"found": False}
    return _dumps(result), result


async def _history(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    bot_id = str(args.get("botId") or "")
    if service.get(bot_id) is None:
        return f"No bot {bot_id!r}.", {"found": False}
    runs = service.history(bot_id, limit=int(args.get("limit") or 20))
    return _dumps({"count": len(runs), "runs": runs}), {"runs": runs}


async def _inspect_run(args: dict[str, Any], _ctx: ToolContext) -> tuple[str, Any]:
    service = _require_service()
    execution_id = str(args.get("executionId") or "")
    execution = service.get_execution(execution_id)
    if execution is None:
        return f"No run {execution_id!r}.", {"found": False}
    return _dumps(execution), execution


def register_automation_tools(registry: ToolRegistry) -> list[str]:
    """Register the bot tools. Idempotent per name (returns the ones added)."""
    definitions = [
        ToolDefinition(
            name="bot_create",
            description="Create a persistent autonomous bot (a scheduled agent) that runs in the "
            "background even after the browser closes. Set triggerType to manual, one_time, "
            "interval, cron, event or condition; provide objective; optionally name, agentId (any installed "
            "preset), description, state, workspaceRoot, autonomy, and the execution, retry, "
            "concurrency, limits, delivery and permissions blocks the Bots UI also exposes. "
            "A bot gets the SAME tool surface as an interactive turn of its agent.",
            input_schema=_obj({
                "name": {"type": "string"},
                "objective": {"type": "string"},
                "description": {"type": "string"},
                "agentId": {
                    "type": "string",
                    "description": "Any installed agent preset id (e.g. eclipse-agent, "
                    "code-agent). An unknown id is refused.",
                },
                "state": {"type": "string", "enum": ["draft", "active", "paused", "disabled"]},
                "triggerType": {"type": "string", "enum": ["manual", "one_time", "interval", "cron", "event", "condition"]},
                "cron": {"type": "string"}, "intervalSeconds": {"type": "integer"},
                "runAt": {"type": "string"}, "timezone": {"type": "string"},
                "eventType": {"type": "string", "description": "Dotted event name for an event trigger, e.g. 'automation.completed'."},
                "eventFilter": {"type": "object", "description": "Event fields that must match, e.g. {\"automationId\": \"<bot id>\"}."},
                "conditionExpression": {"type": "string", "description": "Deterministic condition for a condition trigger."},
                "workspaceRoot": {
                    "type": "string",
                    "description": "An approved workspace root. Binding one is what grants "
                    "code verbs; without it the run has no code permission.",
                },
                "autonomy": {
                    "type": "string",
                    "description": "The run's autonomy profile (e.g. edit_verify). Grants "
                    "nothing without a workspace.",
                },
                "executionAuthority": _authority_schema(),
                **_runtime_block_schema(),
            }, ["objective"]),
            handler=_create, permission="write", risk="medium", timeout_ms=30_000,
            effect="external_action",
        ),
        ToolDefinition(
            name="bot_list",
            description="List the user's bots (persistent scheduled agents).",
            input_schema=_obj({"state": {"type": "string"}, "scopeId": {"type": "string"}, "limit": {"type": "integer"}}),
            handler=_list, permission="read", risk="none", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_get",
            description="Get one bot's full configuration by id.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_get, permission="read", risk="none", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_update",
            description="Update a bot's name, objective/instructions, schedule, agent preset, "
            "workspace binding, or any of its execution/retry/concurrency/limits/delivery/"
            "permissions blocks.",
            input_schema=_obj({
                "botId": {"type": "string"}, "name": {"type": "string"}, "objective": {"type": "string"},
                "agentId": {"type": "string"},
                "triggerType": {"type": "string", "enum": ["manual", "one_time", "interval", "cron", "event", "condition"]},
                "cron": {"type": "string"}, "intervalSeconds": {"type": "integer"},
                "runAt": {"type": "string"}, "timezone": {"type": "string"},
                "eventType": {"type": "string", "description": "Dotted event name for an event trigger, e.g. 'automation.completed'."},
                "eventFilter": {"type": "object", "description": "Event fields that must match, e.g. {\"automationId\": \"<bot id>\"}."},
                "conditionExpression": {"type": "string", "description": "Deterministic condition for a condition trigger."},
                "workspaceRoot": {"type": "string"}, "autonomy": {"type": "string"},
                "executionAuthority": _authority_schema(),
                **_runtime_block_schema(),
            }, ["botId"]),
            handler=_update, permission="write", risk="medium", timeout_ms=30_000,
            effect="external_action",
        ),
        ToolDefinition(
            name="bot_run",
            description="Run a bot immediately without changing its schedule.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_run, permission="read", risk="low", timeout_ms=30_000,
        ),
        ToolDefinition(
            name="bot_pause", description="Pause a bot so it stops triggering (a running execution is not killed).",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_lifecycle_handler("pause"), permission="read", risk="low", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_resume", description="Resume a paused bot.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_lifecycle_handler("resume"), permission="write", risk="low",
            timeout_ms=15_000, effect="external_action",
        ),
        ToolDefinition(
            name="bot_enable", description="Enable a draft/disabled bot so it becomes active.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_lifecycle_handler("enable"), permission="write", risk="low",
            timeout_ms=15_000, effect="external_action",
        ),
        ToolDefinition(
            name="bot_disable", description="Disable an active bot.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_lifecycle_handler("disable"), permission="read", risk="low", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_delete", description="Delete a bot. Its run history is retained for auditing.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_delete, permission="read", risk="medium", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_status", description="Get a bot's health: next run, last run, success rate, breaker.",
            input_schema=_obj({"botId": {"type": "string"}}, ["botId"]),
            handler=_status, permission="read", risk="none", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_history", description="List a bot's recent runs (executions) with outcomes.",
            input_schema=_obj({"botId": {"type": "string"}, "limit": {"type": "integer"}}, ["botId"]),
            handler=_history, permission="read", risk="none", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="bot_inspect_run", description="Inspect one run: state, outcome, activity timeline and deliveries.",
            input_schema=_obj({"executionId": {"type": "string"}}, ["executionId"]),
            handler=_inspect_run, permission="read", risk="none", timeout_ms=15_000,
        ),
    ]
    added: list[str] = []
    for definition in definitions:
        if registry.get(definition.name) is None:
            registry.register(definition)
            added.append(definition.name)
    return added


def _lifecycle_handler(action: str):
    """Return a lifecycle handler. Kept sync-friendly for the definition table."""
    async def handler(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
        service = _require_service()
        bot_id = str(args.get("botId") or "")
        # Enabling or resuming starts a STORED schedule firing: same go-ahead as
        # creating one. (Pause/disable/delete stop future work and need none.)
        if action in ("enable", "resume"):
            summary = f"{action.title()} a scheduled task: {bot_id}"
            if not await _approve_autonomous_schedule(ctx, summary):
                return _SCHEDULE_NEEDS_APPROVAL, {"ok": False, "approved": False, "botId": bot_id}
        try:
            definition = getattr(service, action)(bot_id, actor="agent")
        except Exception as exc:  # noqa: BLE001
            return f"Could not {action} bot {bot_id!r}: {exc}", {"ok": False, "error": str(exc)}
        view = definition.public()
        data = {"ok": True, "state": view["state"], "bot": view}
        return _dumps(data), data
    return handler


__all__ = [
    "TOOL_NAMES",
    "get_automation_service",
    "register_automation_tools",
    "set_automation_service",
]
