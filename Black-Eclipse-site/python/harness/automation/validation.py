"""Automation validation + testing (§36, §37).

`validate_automation` catches every class of misconfiguration BEFORE activation
and returns actionable issues (severity + stable code + the field). `test_automation`
is validation plus a dry-run dispatch plan, so a user can see exactly what would
happen without performing any side effect.

Never fabricates success: a referenced workflow/agent/skill/tool that does not
exist is an ERROR, not a warning. Unknown EVENT types are allowed (the event
vocabulary is open-ended by design), but the type must be a well-formed name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .conditions import ConditionError, validate_condition
from .model import (
    AutomationDefinition,
    DELIVERY_ADAPTERS,
    MIN_INTERVAL_S,
    NON_RETRYABLE_ERROR_CLASSES,
)
from .timeutil import TimezoneError, resolve_timezone

ERROR = "error"
WARNING = "warning"

# Action types that require a specific binding.
_ACTION_REQUIRES = {
    "execute_workflow": "workflow_id",
    "call_tool": "tool_name",
    "run_agent": "objective",
    "run_team": "objective",
    "execute_task": None,  # objective OR skill
    "delegate_agent": "objective",
    "send_notification": "notification_template",
    "generate_report": None,
    "run_code": "tool_name",
    "browser_action": "tool_name",
    "create_task": None,
    "custom_action": "tool_name",
    # Required fields live on `action.config` and are checked by `_validate_trading`.
    "run_strategy": None,
}

# Obvious secret material that must never be embedded in a definition (§29).
_SECRET_KEYS = frozenset({
    "apikey", "api_key", "password", "passwd", "secret", "token", "access_token",
    "refresh_token", "private_key", "client_secret", "authorization",
})


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    field: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "field": self.field, "message": self.message}


def _contains_secret(data: Any, path: str = "") -> Optional[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            key_l = str(key).lower()
            if key_l in _SECRET_KEYS and value:
                return f"{path}.{key}".lstrip(".")
            found = _contains_secret(value, f"{path}.{key}".lstrip("."))
            if found:
                return found
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found = _contains_secret(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _validate_trading(
    definition: AutomationDefinition,
    strategy_lookup: Optional[Callable[[str, str], Any]],
) -> list[ValidationIssue]:
    """Trading-bot rules. Mirrored by `src/lib/bots/trading/validate.ts`.

    A draft may be saved mid-setup (so the operator can prepare trading after
    creation); an ACTIVE bot must be complete. That distinction is the difference
    between a recoverable draft and a bot that silently never trades.
    """
    from . import trading_policy  # noqa: PLC0415 - stdlib-only helper module

    issues: list[ValidationIssue] = []
    action = definition.action
    config = action.config if isinstance(action.config, dict) else {}
    authority = definition.execution_authority
    active = definition.state == "active"

    def error(code: str, field: str, message: str) -> None:
        issues.append(ValidationIssue(ERROR, code, field, message))

    def warning(code: str, field: str, message: str) -> None:
        issues.append(ValidationIssue(WARNING, code, field, message))

    strategy_id = str(config.get("strategyId") or "").strip()
    version = str(config.get("strategyVersion") or "").strip()
    mode = str(config.get("mode") or "").strip().lower()
    connection_id = str(config.get("connectionId") or "").strip()
    symbols = trading_policy.normalize_symbols(
        config.get("symbols") or [], limit=config.get("maxSymbols"),
    )

    if not strategy_id:
        error("STRATEGY_ID_REQUIRED", "action.config.strategyId",
              "choose the strategy this bot trades")
    if not version:
        error("STRATEGY_VERSION_REQUIRED", "action.config.strategyVersion",
              "choose the strategy version")
    if strategy_id and version and strategy_lookup is not None:
        try:
            if strategy_lookup(strategy_id, version) is None:
                error("STRATEGY_UNAVAILABLE", "action.config.strategyId",
                      f"strategy {strategy_id!r} version {version!r} does not exist")
        except Exception:  # noqa: BLE001 - an unreadable store is reported
            warning("STRATEGY_UNVERIFIED", "action.config.strategyId",
                    "the strategy store could not be read")

    if mode not in trading_policy.MODES:
        error("INVALID_TRADING_MODE", "action.config.mode",
              "mode must be shadow, demo or live")
        return issues
    if mode != "shadow" and not connection_id:
        error("CONNECTION_REQUIRED", "action.config.connectionId",
              "choose the broker connection for this mode")

    if not symbols:
        error("SYMBOLS_REQUIRED", "action.config.symbols", "add at least one symbol")
    if not trading_policy.timeframe_seconds(config.get("timeframe")):
        error("INVALID_TIMEFRAME", "action.config.timeframe",
              "choose a supported timeframe")
    if authority.require_stop and float(config.get("stopLossPoints") or 0) <= 0:
        error("STOP_REQUIRED", "action.config.stopLossPoints",
              "this bot requires a stop loss, so set a stop distance")

    band = {"conservative": (1, 2), "normal": (2, 4), "aggressive": (3, 8)}
    max_positions, max_orders = band.get(str(authority.appetite), band["normal"])
    if int(config.get("maxOrdersPerDay") or 0) > max_orders:
        error("ORDERS_ABOVE_BAND", "action.config.maxOrdersPerDay",
              f"{authority.appetite} allows at most {max_orders} orders/day")
    if int(authority.max_open_positions or 0) > max_positions:
        error("POSITIONS_ABOVE_BAND", "executionAuthority.maxOpenPositions",
              f"{authority.appetite} allows at most {max_positions} open position(s)")

    tier = str(getattr(authority, "tier", "l0") or "l0")
    dry_run = bool(config.get("dryRun"))
    if mode == "live" and tier != "l2":
        error("BOT_TIER_REQUIRED", "executionAuthority.tier",
              "live execution needs tier L2")
    if mode == "demo" and not dry_run and tier == "l0":
        error("BOT_TIER_REQUIRED", "executionAuthority.tier",
              "tier L0 only proposes; use preview or raise the tier to L1")

    # Misfire catching up on a trading bot would place a late order.
    if definition.execution.misfire != "skip":
        error("MISTRADE_RISK", "execution.misfire",
              "a trading bot must skip a missed fire, never catch up")

    trial_id = str(config.get("trialId") or "").strip()
    if not trial_id:
        message = "prepare trading (create the trial) before enabling this bot"
        if active:
            error("TRADING_NOT_PREPARED", "action.config.trialId", message)
        else:
            warning("TRADING_NOT_PREPARED", "action.config.trialId", message)

    if mode != "shadow" and not dry_run and active:
        request = trading_policy.consent_request_for(config, {
            "maxMarginPerOrder": authority.max_margin_per_order,
            "maxOpenPositions": authority.max_open_positions,
        })
        check = trading_policy.consent_covers(config.get("consent"), request)
        if not check["covered"]:
            code = {"revoked": "CONSENT_REVOKED", "expired": "CONSENT_EXPIRED"}.get(
                check["status"], "CONSENT_REQUIRED")
            error(code, "action.config.consent",
                  check["reason"] or "grant standing trading consent")

    session = config.get("session")
    if isinstance(session, dict) and session:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            ZoneInfo(str(session.get("timezone") or "UTC"))
        except Exception:  # noqa: BLE001
            error("INVALID_TIMEZONE", "action.config.session.timezone",
                  "that timezone is not available on this machine")

    return issues


def _validate_team_action(
    config: dict[str, Any], issues: list[ValidationIssue],
) -> None:
    """Create-time validation for a desk (`action.type == 'run_team'`).

    A desk carries its own `TeamDefinition` in `config.team`; without one the
    action is the Institution preset and needs no team check. Everything here is
    refused at SAVE time so a broken graph is never stored.
    """
    raw = config.get("team")
    if raw is None:
        return  # the Institution preset path

    def error(code: str, field: str, message: str) -> None:
        issues.append(ValidationIssue(ERROR, code, field, message))

    if not isinstance(raw, dict):
        error("INVALID_TEAM", "action.config.team", "config.team must be a team object")
        return
    try:
        from ..team.model import TeamDefinition  # noqa: PLC0415
        from ..team.graph import validate_graph  # noqa: PLC0415
        from ..team.service import ORDER_SUBMIT_TOOLS  # noqa: PLC0415

        team = TeamDefinition(**raw)
    except Exception as exc:  # noqa: BLE001 - a malformed desk is refused, never guessed
        error("INVALID_TEAM", "action.config.team", f"that team is not valid: {exc}")
        return

    # A one-member "team" is a bot with extra steps; the product path is a bot.
    if len(team.members) < 2:
        error("TEAM_TOO_SMALL", "action.config.team.members",
              "a desk needs at least two members (use a bot for a single agent)")
    if not team.subtasks:
        error("TEAM_NO_STAGES", "action.config.team.subtasks",
              "a desk needs at least one stage")
    if not team.policy.delegation_enabled:
        error("TEAM_DELEGATION_DISABLED", "action.config.team.policy.delegationEnabled",
              "delegation must stay enabled: every stage is a delegated run")
    for problem in validate_graph(team.subtasks):
        error("INVALID_TEAM_GRAPH", "action.config.team.subtasks", problem)

    member_ids = {member.agent_id for member in team.members}
    capabilities = {c for member in team.members for c in member.capabilities}
    for subtask in team.subtasks:
        if subtask.assigned_agent_id and subtask.assigned_agent_id not in member_ids:
            error("TEAM_UNASSIGNED_STAGE", f"action.config.team.subtasks.{subtask.id}",
                  f"stage {subtask.id!r} is assigned to {subtask.assigned_agent_id!r}, "
                  "which is not a member")
        elif not subtask.assigned_agent_id and subtask.capability not in capabilities:
            error("TEAM_UNROUTABLE_STAGE", f"action.config.team.subtasks.{subtask.id}",
                  f"stage {subtask.id!r} names no member and no member declares "
                  f"capability {subtask.capability!r}")

    # A team may PROPOSE an order (`order_preview`) but never submit one: it has
    # no pinned mandate and no bot authority.
    for member in team.members:
        forbidden = sorted(ORDER_SUBMIT_TOOLS.intersection(member.tools or []))
        if forbidden:
            error("TEAM_ORDER_SUBMIT_REFUSED", "action.config.team.members",
                  f"member {member.agent_id!r} declares order-submitting tool "
                  f"{', '.join(forbidden)}; a desk proposes (order_preview) and a "
                  "trading bot executes")

    if not team.policy.require_review:
        issues.append(ValidationIssue(
            WARNING, "TEAM_REVIEW_OFF", "action.config.team.policy.requireReview",
            "review is off: conflicting or unsupported claims will not be independently checked",
        ))

    source = config.get("contextAutomationId")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        error("INVALID_LEAD_SOURCE", "action.config.contextAutomationId",
              "contextAutomationId must be the id of an existing bot")


def validate_automation(
    definition: AutomationDefinition,
    *,
    skill_registry: Any = None,
    workflow_store: Any = None,
    known_agents: Optional[set[str]] = None,
    tool_names: Optional[set[str]] = None,
    known_variables: Optional[set[str]] = None,
    strategy_lookup: Optional[Callable[[str, str], Any]] = None,
) -> list[ValidationIssue]:
    """Return every validation issue for a definition (empty ⇒ valid)."""
    issues: list[ValidationIssue] = []
    trigger = definition.trigger
    action = definition.action

    # ── Trigger ──────────────────────────────────────────────────────────────
    try:
        resolve_timezone(trigger.timezone)
    except TimezoneError as exc:
        issues.append(ValidationIssue(ERROR, "INVALID_TIMEZONE", "trigger.timezone", exc.message))

    if trigger.type == "cron":
        from ..jobs.cron import CronError, parse_cron  # noqa: PLC0415

        try:
            parse_cron(trigger.cron or "")
        except CronError as exc:
            issues.append(ValidationIssue(ERROR, "INVALID_CRON", "trigger.cron", exc.message))
    elif trigger.type == "interval":
        if not trigger.interval_seconds or trigger.interval_seconds < MIN_INTERVAL_S:
            issues.append(ValidationIssue(
                ERROR, "INVALID_INTERVAL", "trigger.intervalSeconds",
                f"an interval must be at least {MIN_INTERVAL_S} seconds",
            ))
    elif trigger.type == "one_time":
        from .timeutil import parse_iso  # noqa: PLC0415

        if parse_iso(trigger.run_at) is None:
            issues.append(ValidationIssue(
                ERROR, "INVALID_RUN_AT", "trigger.runAt", "run_at must be an ISO-8601 timestamp",
            ))
    elif trigger.type == "event":
        event_type = trigger.event_type or ""
        if not event_type or "." not in event_type or " " in event_type:
            issues.append(ValidationIssue(
                ERROR, "INVALID_EVENT", "trigger.eventType",
                "an event type must be a dotted name like 'filing.created'",
            ))
    elif trigger.type == "condition":
        if trigger.condition is None:
            issues.append(ValidationIssue(ERROR, "INVALID_CONDITION", "trigger.condition", "a condition trigger needs a condition"))
        else:
            _validate_condition(trigger.condition.expression, known_variables, "trigger.condition.expression", issues)

    # An optional gate condition on any trigger.
    if definition.condition is not None:
        _validate_condition(definition.condition.expression, known_variables, "condition.expression", issues)

    # ── Action ───────────────────────────────────────────────────────────────
    if action.type not in _ACTION_REQUIRES:
        issues.append(ValidationIssue(ERROR, "INVALID_ACTION", "action.type", f"unknown action type {action.type!r}"))
    else:
        required = _ACTION_REQUIRES[action.type]
        if required and not getattr(action, required, None):
            # A Graph Studio workflow is addressed through config.graphWorkflowId
            # (the Python runtime cannot read JDDS); a legacy harness workflow
            # still uses action.workflow_id.
            graph_id = action.config.get("graphWorkflowId") if isinstance(action.config, dict) else None
            if not (action.type == "execute_workflow" and graph_id):
                issues.append(ValidationIssue(
                    ERROR, "INVALID_ACTION", f"action.{required}", f"action {action.type!r} requires {required}",
                ))
        if action.type == "execute_task" and not (action.skill_id or action.objective):
            issues.append(ValidationIssue(
                ERROR, "INVALID_ACTION", "action",
                "an execute_task action needs a skill or an objective",
            ))
        if action.workspace_root and not action.autonomy:
            issues.append(ValidationIssue(
                WARNING, "AUTONOMY_ABSENT", "action.autonomy",
                "a workspace is bound but no autonomy profile is set; code tools stay unavailable",
            ))

    # ── Trading action (run_strategy) ─────────────────────────────────────────
    if action.type == "run_strategy":
        issues.extend(_validate_trading(definition, strategy_lookup))

    # ── Desk (run_team) ───────────────────────────────────────────────────────
    if action.type == "run_team":
        _validate_team_action(action.config if isinstance(action.config, dict) else {}, issues)

    # ── Referenced entities ──────────────────────────────────────────────────
    graph_workflow_id = action.config.get("graphWorkflowId") if isinstance(action.config, dict) else None
    if action.workflow_id and not graph_workflow_id and workflow_store is not None:
        try:
            if workflow_store.get(action.workflow_id) is None:
                issues.append(ValidationIssue(ERROR, "WORKFLOW_NOT_FOUND", "action.workflowId", f"no workflow {action.workflow_id!r}"))
        except Exception:  # noqa: BLE001 - an unreadable store is reported, not fatal
            issues.append(ValidationIssue(WARNING, "WORKFLOW_UNVERIFIED", "action.workflowId", "the workflow store could not be read"))
    if action.skill_id and skill_registry is not None:
        try:
            manifest = skill_registry.manifest(action.skill_id)
            if manifest is None:
                issues.append(ValidationIssue(ERROR, "SKILL_NOT_FOUND", "action.skillId", f"skill {action.skill_id!r} is not installed"))
        except Exception:  # noqa: BLE001
            issues.append(ValidationIssue(WARNING, "SKILL_UNVERIFIED", "action.skillId", "the skill registry could not be read"))
    if action.agent_id and known_agents is not None and action.agent_id not in known_agents:
        issues.append(ValidationIssue(ERROR, "AGENT_NOT_FOUND", "action.agentId", f"no agent {action.agent_id!r}"))
    if action.tool_name and tool_names is not None and action.tool_name not in tool_names:
        issues.append(ValidationIssue(ERROR, "TOOL_NOT_FOUND", "action.toolName", f"no tool {action.tool_name!r}"))

    # ── Delivery ─────────────────────────────────────────────────────────────
    if definition.delivery.adapter not in DELIVERY_ADAPTERS:
        issues.append(ValidationIssue(ERROR, "UNKNOWN_ADAPTER", "delivery.adapter", f"unknown delivery adapter {definition.delivery.adapter!r}"))
    for target in definition.delivery.targets:
        if target.adapter not in DELIVERY_ADAPTERS:
            issues.append(ValidationIssue(ERROR, "UNKNOWN_ADAPTER", "delivery.targets", f"unknown delivery adapter {target.adapter!r}"))

    # ── Retry / resource limits / concurrency ────────────────────────────────
    retry = definition.retry
    overlap = set(retry.retryable_classes) & set(retry.non_retryable_classes)
    if overlap:
        issues.append(ValidationIssue(
            ERROR, "CONFLICTING_RETRY_CLASSES", "retry",
            f"error class {sorted(overlap)[0]!r} is both retryable and non-retryable",
        ))
    if retry.max_attempts > 1:
        non_retryable = set(retry.non_retryable_classes) | NON_RETRYABLE_ERROR_CLASSES
        if retry.retryable_classes and set(retry.retryable_classes) <= non_retryable:
            issues.append(ValidationIssue(
                ERROR, "CONFLICTING_RETRY_CLASSES", "retry.retryableClasses",
                "every retryable class is non-retryable; retries would never happen",
            ))
    limits = definition.limits
    if limits.max_runtime_s is not None and limits.max_runtime_s > definition.execution.max_runtime_s:
        issues.append(ValidationIssue(
            WARNING, "LIMIT_ABOVE_TIMEOUT", "limits.maxRuntimeS",
            "the resource limit exceeds the execution timeout and will never bind",
        ))
    if limits.max_retries is not None and limits.max_retries > retry.max_attempts:
        issues.append(ValidationIssue(
            WARNING, "LIMIT_ABOVE_RETRIES", "limits.maxRetries",
            "the retry limit exceeds max_attempts and will never bind",
        ))
    concurrency = definition.concurrency
    if concurrency.on_conflict == "cancel_previous" and concurrency.max_concurrent_runs > 1:
        issues.append(ValidationIssue(
            ERROR, "CONFLICTING_CONCURRENCY", "concurrency",
            "cancel_previous requires max_concurrent_runs = 1",
        ))

    # ── Permission policy: deny wins; conflicting grants are refused ─────────
    perms = definition.permissions
    both = set(perms.allowed) & set(perms.denied)
    if both:
        issues.append(ValidationIssue(
            ERROR, "CONFLICTING_PERMISSIONS", "permissions",
            f"{sorted(both)[0]!r} is both allowed and denied",
        ))
    if perms.requires_approval and not perms.approval_scope:
        issues.append(ValidationIssue(
            WARNING, "APPROVAL_SCOPE_ABSENT", "permissions.approvalScope",
            "approval is required but no scope label is set",
        ))

    # ── Secrets must be references, never values (§29) ───────────────────────
    for field_name, data in (
        ("action.config", action.config), ("action.toolArgs", action.tool_args),
        ("action.workflowInput", action.workflow_input),
    ):
        found = _contains_secret(data)
        if found:
            issues.append(ValidationIssue(
                ERROR, "EMBEDDED_SECRET", field_name,
                f"{field_name} contains secret-like key {found!r}; store a credential reference instead",
            ))

    return issues


def _validate_condition(expression: str, known_variables: Optional[set[str]], field: str, issues: list[ValidationIssue]) -> None:
    try:
        validate_condition(expression, known_variables=known_variables)
    except ConditionError as exc:
        issues.append(ValidationIssue(ERROR, "INVALID_CONDITION", field, exc.message))


def is_valid(issues: list[ValidationIssue]) -> bool:
    return not any(issue.severity == ERROR for issue in issues)


__all__ = ["ERROR", "WARNING", "ValidationIssue", "is_valid", "validate_automation"]
