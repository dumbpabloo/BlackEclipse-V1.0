"""The Automation domain model — a first-class, durable domain object.

An automation is DATA with policy, never executable code. It declares a trigger
(WHEN), an optional condition (WHY), and an action (WHAT the existing runtime
should run). It never carries credentials, never widens authority, and never
holds a prompt body that grants anything: the run it dispatches is subject to
exactly the same permission, allowlist and policy checks as a user-initiated run.

Persisted as JSON blobs on `Automation` / `AutomationVersion` rows with the
queryable columns (`state`, `nextRunAt`, `triggerType`, `eventType`) beside them,
mirroring `harness.jobs.model.JobDefinition`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# Accept BOTH snake_case (persistence / Python callers) and camelCase (the TS
# front door), exactly like `schemas.ModelSettings` and `jobs.model`.
_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

# ── Enumerations (the vocabulary is documented; SQLite stores plain strings) ──

LifecycleState = Literal["draft", "validating", "active", "paused", "disabled", "archived"]

TriggerKind = Literal["cron", "interval", "one_time", "event", "condition", "webhook", "manual"]

ActionKind = Literal[
    "execute_task",
    "execute_workflow",
    "run_agent",
    "run_team",
    "delegate_agent",
    "call_tool",
    "create_task",
    "send_notification",
    "generate_report",
    "run_code",
    "browser_action",
    "custom_action",
    # A bot whose brain is a saved quant strategy (strategy trading bot).
    "run_strategy",
    # A bot whose brain is an external ACP agent process (Gemini/Claude Code/…).
    "run_external_agent",
]

# Execution states are DISTINCT from lifecycle states (§6).
ExecutionState = Literal[
    "triggered",
    "queued",
    "running",
    "waiting",
    "retrying",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "blocked",
    "skipped",
]

MisfirePolicy = Literal["skip", "run_once", "catch_up"]
ConcurrencyConflict = Literal["queue_if_running", "skip_if_running", "cancel_previous"]
DeliveryAdapter = Literal["in_app", "artifact", "messaging"]
BreakerState = Literal["closed", "open", "half_open"]
#: The bot autonomy ladder (§ P5). L0 proposes, L1 fills demo, L2 fills live under
#: a bot- and version-pinned mandate.
TradingTier = Literal["l0", "l1", "l2"]
RiskAppetite = Literal["conservative", "normal", "aggressive"]
SizingMode = Literal["risk_based", "user_fixed"]

LIFECYCLE_STATES: tuple[str, ...] = (
    "draft", "validating", "active", "paused", "disabled", "archived",
)
TRIGGER_KINDS: tuple[str, ...] = (
    "cron", "interval", "one_time", "event", "condition", "webhook", "manual",
)
ACTION_KINDS: tuple[str, ...] = (
    "execute_task", "execute_workflow", "run_agent", "run_team", "delegate_agent",
    "call_tool", "create_task", "send_notification", "generate_report", "run_code",
    "browser_action", "custom_action", "run_strategy", "run_external_agent",
)
EXECUTION_STATES: tuple[str, ...] = (
    "triggered", "queued", "running", "waiting", "retrying", "completed", "failed",
    "cancelled", "timed_out", "blocked", "skipped",
)
TERMINAL_EXECUTION_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "skipped", "blocked"}
)
MISFIRE_POLICIES: tuple[str, ...] = ("skip", "run_once", "catch_up")
CONCURRENCY_CONFLICTS: tuple[str, ...] = (
    "queue_if_running", "skip_if_running", "cancel_previous",
)
DELIVERY_ADAPTERS: tuple[str, ...] = ("in_app", "artifact", "messaging")

# Error classes the runtime reports that are NEVER retried (§25): a retry cannot
# fix a permission denial, a bad credential, or a destructive business-rule
# failure, and retrying them is harmful.
NON_RETRYABLE_ERROR_CLASSES: frozenset[str] = frozenset({
    "PERMISSION_DENIED",
    "INVALID_CONFIGURATION",
    "INVALID_CONDITION",
    "INVALID_CREDENTIAL",
    "CREDENTIAL_MISSING",
    "TOOL_FORBIDDEN",
    "TOOL_VALIDATION_FAILED",
    "INVALID_INPUT",
    "WORKFLOW_NOT_FOUND",
    "TASK_NOT_FOUND",
    "AGENT_NOT_FOUND",
    "BUSINESS_RULE_FAILURE",
    "APPROVAL_DENIED",
    "RESOURCE_LIMIT_EXCEEDED",
})
# Classes that ARE retried by default (transient transport/provider faults).
DEFAULT_RETRYABLE_ERROR_CLASSES: frozenset[str] = frozenset({
    "PROVIDER_ERROR",
    "PROVIDER_TIMEOUT",
    "TOOL_TIMEOUT",
    "TOOL_ERROR",
    "NETWORK_ERROR",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "PERSISTENCE_ERROR",
    "QUEUE_UNAVAILABLE",
})

MIN_INTERVAL_S = 30
MAX_INTERVAL_S = 30 * 24 * 3600


class ConditionSpec(BaseModel):
    """A deterministic condition expression (§8).

    `expression` is the canonical DSL text. `edge` selects edge-triggered
    semantics (§9): the automation fires on a FALSE→TRUE transition, never
    continuously while the condition stays true. `variables` is a cached list of
    referenced variable paths, validated on save.
    """

    model_config = _CAMEL

    expression: str = Field(..., min_length=1, max_length=2000)
    edge: bool = True
    variables: list[str] = Field(default_factory=list)


class Trigger(BaseModel):
    """WHEN the automation should be considered (§7)."""

    model_config = _CAMEL

    type: TriggerKind
    cron: Optional[str] = Field(None, max_length=200)
    interval_seconds: Optional[int] = Field(None, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S)
    run_at: Optional[str] = None                 # one_time, ISO-8601
    event_type: Optional[str] = Field(None, max_length=200)
    event_filter: dict[str, Any] = Field(default_factory=dict)
    condition: Optional[ConditionSpec] = None    # condition trigger
    #: Webhook triggers are fired by an external caller through
    #: `/automations/webhook/{id}` with this bearer token. Generated on create;
    #: never inferred from the id.
    webhook_secret: Optional[str] = Field(None, max_length=128)
    timezone: str = "UTC"
    start_at: Optional[str] = None
    end_at: Optional[str] = None

    @model_validator(mode="after")
    def _consistency(self) -> "Trigger":
        if self.type == "cron" and not self.cron:
            raise ValueError("a cron trigger requires a cron expression")
        if self.type == "interval" and self.interval_seconds is None:
            raise ValueError("an interval trigger requires interval_seconds")
        if self.type == "one_time" and not self.run_at:
            raise ValueError("a one-time trigger requires run_at")
        if self.type == "event" and not self.event_type:
            raise ValueError("an event trigger requires event_type")
        if self.type == "condition" and self.condition is None:
            raise ValueError("a condition trigger requires a condition")
        return self


class Action(BaseModel):
    """WHAT the existing runtime should execute (§11)."""

    model_config = _CAMEL

    type: ActionKind
    # Generic runtime bindings (reused, never duplicated).
    objective: Optional[str] = Field(None, max_length=4000)
    agent_id: Optional[str] = Field(None, max_length=200)
    workflow_id: Optional[str] = Field(None, max_length=200)
    workflow_input: dict[str, Any] = Field(default_factory=dict)
    tool_name: Optional[str] = Field(None, max_length=200)
    tool_args: dict[str, Any] = Field(default_factory=dict)
    task_title: Optional[str] = Field(None, max_length=200)
    skill_id: Optional[str] = Field(None, max_length=200)
    skill_version: Optional[str] = Field(None, max_length=100)
    workspace_root: Optional[str] = Field(None, max_length=1024)
    autonomy: Optional[str] = Field(None, max_length=50)
    notification_template: Optional[str] = Field(None, max_length=200)
    # A REFERENCE to a credential in the vault — never a secret value (§29).
    credential_ref: Optional[str] = Field(None, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class ExecutionPolicy(BaseModel):
    """Timing + misfire + timeout (§7.1, §18, §25)."""

    model_config = _CAMEL

    # Same 20-minute floor as one chat turn (`DEFAULT_CHAT_RUN_TIMEOUT_S`): a
    # scheduled bot doing the same research must not be cut five minutes earlier
    # just because it runs in the background. The operator can raise it to a day.
    max_runtime_s: int = Field(1200, ge=5, le=86_400)
    # A fire missed while the app was CLOSED runs once when the app returns —
    # never silently dropped. `skip` (the old default) discarded every scheduled
    # run whenever Eclipse wasn't open at the exact minute, so a daily bot never
    # ran; `run_once` makes a scheduled bot actually produce a result.
    misfire: MisfirePolicy = "run_once"
    catch_up_max: int = Field(1, ge=1, le=100)
    timeout_s: Optional[int] = Field(None, ge=1, le=86_400)


class RetryPolicy(BaseModel):
    """Retry + backoff (§25). Never retries a non-retryable error class."""

    model_config = _CAMEL

    max_attempts: int = Field(1, ge=1, le=10)
    initial_backoff_s: float = Field(5.0, ge=0.0, le=3600.0)
    backoff_multiplier: float = Field(2.0, ge=1.0, le=10.0)
    max_backoff_s: float = Field(300.0, ge=0.0, le=86_400.0)
    jitter: bool = True
    retryable_classes: list[str] = Field(default_factory=list)
    non_retryable_classes: list[str] = Field(default_factory=list)


class ConcurrencyPolicy(BaseModel):
    """Concurrency control (§22)."""

    model_config = _CAMEL

    max_concurrent_runs: int = Field(1, ge=1, le=50)
    on_conflict: ConcurrencyConflict = "skip_if_running"


class ResourceLimits(BaseModel):
    """Runtime-enforced limits (§26). Storing without enforcement is forbidden."""

    model_config = _CAMEL

    max_runtime_s: Optional[int] = Field(None, ge=1, le=86_400)
    max_tokens: Optional[int] = Field(None, ge=1, le=100_000_000)
    max_cost: Optional[float] = Field(None, ge=0.0)
    max_tool_calls: Optional[int] = Field(None, ge=0, le=10_000)
    max_parallel_tasks: Optional[int] = Field(None, ge=1, le=100)
    max_retries: Optional[int] = Field(None, ge=0, le=10)


class PermissionPolicy(BaseModel):
    """Effective permission policy (§27, §28). Deny wins over allow."""

    model_config = _CAMEL

    allowed: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    approval_scope: Optional[str] = Field(None, max_length=200)


class DeliveryTarget(BaseModel):
    model_config = _CAMEL

    destination: str = Field(..., max_length=200)
    adapter: DeliveryAdapter = "in_app"


class DeliveryPolicy(BaseModel):
    """When + how a result reaches the user (§44)."""

    model_config = _CAMEL

    on_success: bool = True
    on_failure: bool = True
    on_approval_required: bool = True
    on_condition_triggered: bool = True
    on_timeout: bool = True
    adapter: DeliveryAdapter = "in_app"
    title: Optional[str] = Field(None, max_length=200)
    destination: Optional[str] = Field(None, max_length=200)
    targets: list[DeliveryTarget] = Field(default_factory=list)

    def effective_targets(self) -> list[DeliveryTarget]:
        if self.targets:
            seen: set[tuple[str, str]] = set()
            out: list[DeliveryTarget] = []
            for target in self.targets:
                key = (target.destination, target.adapter)
                if key not in seen:
                    seen.add(key)
                    out.append(target)
            return out
        return [DeliveryTarget(destination=self.destination or self.adapter, adapter=self.adapter)]

    def wants(self, outcome: str) -> bool:
        """Whether a delivery should happen for a terminal/approval outcome."""
        return {
            "completed": self.on_success,
            "failed": self.on_failure,
            "timed_out": self.on_timeout,
            "approval_required": self.on_approval_required,
            "condition_triggered": self.on_condition_triggered,
        }.get(outcome, False)


class ExecutionAuthority(BaseModel):
    """How much a bot may do on its own (P5's ladder, enforced at submit).

    `tier` is the only thing that decides whether a bot may reach a LIVE broker:
    `l0` proposes, `l1` may fill a demo account unattended, `l2` may fill a live
    account but only under a mandate pinned to this bot AND its current version.
    The remaining fields are the leash. They are stored on the definition, so a
    version bump carries the authority it was granted with.
    """

    model_config = _CAMEL

    tier: TradingTier = "l0"
    #: The band the bot may choose inside for size and frequency. Never outside.
    appetite: RiskAppetite = "normal"
    #: How size is decided. `risk_based` requires a stop; `user_fixed` may not be
    #: resized by the bot.
    sizing_mode: SizingMode = "risk_based"
    require_stop: bool = True
    #: Hard ceilings, enforced in addition to the mandate's own caps.
    max_open_positions: int = Field(2, ge=1, le=50)
    max_margin_per_order: float = Field(0.0, ge=0.0)
    cooldown_s: int = Field(900, ge=0, le=86_400)
    #: The daily loss brake the bot owns, on top of the connection's own policy.
    max_daily_loss_pct: float = Field(3.0, ge=0.0, le=50.0)
    #: Standing consent. When true the operator typed the phrase once and the bot
    #: renews its bounded mandate without asking again (until expiry/revocation).
    auto_mode: bool = False
    auto_renew: bool = True
    #: Failure learning. The bot raises a lesson after N repeated failures and
    #: applies it only with the operator's consent (tighten-only by construction).
    learning_enabled: bool = True
    improve_after_failures: int = Field(3, ge=2, le=10)


class AutomationDefinition(BaseModel):
    """One durable automation definition (lifecycle + version + policy)."""

    model_config = _CAMEL

    id: str
    name: str = Field(..., max_length=160)
    description: Optional[str] = Field(None, max_length=2000)
    version: int = Field(1, ge=1)
    state: LifecycleState = "draft"
    trigger: Trigger
    condition: Optional[ConditionSpec] = None
    action: Action
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    delivery: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    #: What this bot may execute without a human (L0/L1/L2) and its leash.
    execution_authority: ExecutionAuthority = Field(default_factory=ExecutionAuthority)
    scope_id: Optional[str] = Field(None, max_length=200)
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    consecutive_failures: int = 0
    breaker: BreakerState = "closed"
    breaker_opened_at: Optional[str] = None
    breaker_reset_by: Optional[str] = None

    def public(self) -> dict[str, Any]:
        """The operator wire view (camelCase).

        Credentials are stored as vault references, never values. The ONE
        exception is a webhook's generated bearer token: the operator must be
        able to copy it, and every automation route is operator-gated. The
        public webhook door never reads this view.
        """
        return self.model_dump(by_alias=True)


__all__ = [
    "ACTION_KINDS",
    "Action",
    "ActionKind",
    "AutomationDefinition",
    "BreakerState",
    "ConditionSpec",
    "ConcurrencyConflict",
    "ConcurrencyPolicy",
    "DEFAULT_RETRYABLE_ERROR_CLASSES",
    "DELIVERY_ADAPTERS",
    "DeliveryAdapter",
    "DeliveryPolicy",
    "DeliveryTarget",
    "EXECUTION_STATES",
    "ExecutionAuthority",
    "ExecutionPolicy",
    "ExecutionState",
    "LIFECYCLE_STATES",
    "LifecycleState",
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "MISFIRE_POLICIES",
    "MisfirePolicy",
    "NON_RETRYABLE_ERROR_CLASSES",
    "PermissionPolicy",
    "ResourceLimits",
    "RetryPolicy",
    "RiskAppetite",
    "SizingMode",
    "TERMINAL_EXECUTION_STATES",
    "TRIGGER_KINDS",
    "TradingTier",
    "Trigger",
    "TriggerKind",
]
