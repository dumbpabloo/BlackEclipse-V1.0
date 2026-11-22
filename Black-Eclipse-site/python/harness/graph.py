"""LangGraph execution graph — the agent runtime.

Flow (a StateGraph over a message list):

    START → agent
    agent  — the model is called with the tool schemas bound
             • no tool calls  → END (final answer)
             • tool calls     → tools
    tools  — each call is validated, permission-checked, timed out and executed
             by the Python ToolRegistry (JDdS data via the bridge)
             → back to agent
    agent → … until max_steps, then a MAX_STEPS_EXCEEDED failure

Safety mirrors the Phase 1 design: max steps, per-tool timeout, duplicate-call
suppression, run timeout, cooperative cancellation, and exactly one terminal
status. Events are emitted by the Python runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Annotated, Any, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .agents import DEFAULT_AGENT_ID, get_agent
from .checkpoint_io import (
    checkpoint_payload_of as _checkpoint_payload_of,
    restore_messages as _restore_messages,
    serialize_messages as _serialize_messages,
)
from .code.workspace import resolve_workspace
from .events import EventBus, RunEmitter
from .model_stream import (
    FINALIZE_INSTRUCTION as _FINALIZE_INSTRUCTION,
    message_text as _text,
)
from .goals.runtime import PlanRuntime
from .goals.verifiers import checks_from_ledger
from .retry_policy import RunDeadline
from .run_scope import RunScope, RunSpec
from .step import effective_call_timeout, invoke_model_step
from .models import build_chat_model
from .model_runtime import (
    ErrorClass,
    ModelError,
    classify_exception,
    get_runtime,
    provider_failure_message,
)
from .persistence import AgentMemoryStore, now_iso
from .schemas import AgentConfig, HarnessError, RunRequest, RunResult
from .surface import (
    chat_code_allowlist,
    effective_allowlist,
    resolve_surface,
    resolve_surface_run,
    surface_disabled_tools,
)
from .tools import DataBridge, ToolContext, ToolRegistry
from .kernel.services.runtime import kernel_runtime

log = logging.getLogger("harness.graph")

# The tool surface resolution lives in `surface.py` (Round 3 / W2, ADR-H280) so
# this execution graph stays under its size ratchet. The names are re-exported
# here because they are the public surface of this module: `effective_allowlist`
# and `chat_code_allowlist` are the two halves of a resolution, and
# `resolve_surface` is its narrow view. A surface EXTENDS or NARROWS the
# agent's declared tools; it never silently REPLACES them.

# Checkpoint (de)serialization lives in `checkpoint_io.py` so this execution
# graph stays under its size ratchet; imported above under the old local names.


def _resume_error(
    run_id: str, code: str, message: str, store: AgentMemoryStore,
) -> RunResult:
    """A structured, non-fresh failure for a bad resume request (Phase 1).

    Never persists a terminal status onto the existing run (a failed resume must
    not overwrite the run's real state) and never starts a fresh run in its place.
    """
    error = HarnessError(code=code, message=message)  # type: ignore[arg-type]
    return RunResult(runId=run_id, status="failed", error=error)


class GraphState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    step: int
    tool_calls: int
    tool_errors: int
    prompt_tokens: int
    completion_tokens: int
    final: Optional[str]
    error: Optional[dict[str, Any]]
    # Phase 11 (bounded replanning): the replan count, the failures in the most
    # recent tool step, whether that step repeated the prior one, and the details
    # the replan directive names. Absent/zero for an ordinary run.
    replans: int
    step_failures: int
    stalled: bool
    last_turn_keys: list[str]
    step_failure_details: list[dict[str, Any]]
    # C4: a typed evidence failure that should divert through `replan` before
    # the answer is accepted. Cleared by the replan node.
    replan_needed: Optional[dict[str, Any]]
    # The plan step this turn is executing (C4 checkpoint cursor).
    plan_step_id: Optional[str]
    # Round 3 / W1 (ADR-H281): the run's verify-and-repair state - the budget
    # counters, the failure the model was shown, and the answer seam's word for
    # how the repair ended. Plain data, so it survives a checkpoint.
    repair: dict[str, Any]
    # W3: a deterministic guardrail halt (rule/count/message). The tools node
    # sets `final` too, so the graph ends with a controlled notice, not a loop.
    guardrail_halt: dict[str, Any]
    # W7: steering messages and delegated completions already injected into
    # this run. Consumed at the next durable checkpoint, so a crash re-delivers
    # at most once and a normal step never duplicates them.
    w7_pending: list[dict[str, Any]]
    # Bounded answer recovery: a bare refusal is not accepted while steps and an
    # untried permitted route remain. `recovery_pending` routes the next edge
    # back to the agent; `tools_used`/`unavailable_tools` keep the directive
    # from naming a route the run already exhausted.
    answer_recoveries: int
    recovery_pending: bool
    tools_used: list[str]
    unavailable_tools: list[str]


async def _run_plugin_hook(name: str, payload: dict[str, Any]) -> Optional[str]:
    """Run a plugin hook if the platform has any for `name`.

    Returns a veto reason when the hook phase did not proceed (an `abort` hook
    failed), else None. Never raises and never blocks: a missing platform, a
    missing hook, or a hook timeout is a no-op for the core operation.
    """
    try:
        from .plugins.bootstrap import get_plugin_manager  # noqa: PLC0415

        manager = get_plugin_manager()
        if manager is None or not manager.hooks.has(name):
            return None
        outcome = await manager.run_hook(name, payload)
        if not outcome.proceed:
            errors = outcome.errors or []
            return errors[0].get("error") if errors else "hook vetoed the call"
    except Exception:  # noqa: BLE001 - a hook must never break a core operation
        return None
    return None


def _extract_usage(message: Any) -> tuple[int, int]:
    """Best-effort (prompt, completion) token counts from a LangChain message.

    Providers expose usage differently: `usage_metadata` is the LangChain
    standard; OpenAI-compatible endpoints (incl. Ollama) put it in
    `response_metadata["token_usage"]`; Anthropic uses `response_metadata["usage"]`.
    A missing block yields (0, 0) — the run honestly reports "usage not
    reported" rather than inventing a count.
    """
    prompt = completion = 0
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
    meta = getattr(message, "response_metadata", None) or {}
    if isinstance(meta, dict):
        raw = meta.get("token_usage") or meta.get("usage") or {}
        if isinstance(raw, dict):
            prompt = prompt or int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
            completion = completion or int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    return prompt, completion


def resolve_agent(request: RunRequest) -> AgentConfig:
    """Resolve the agent Python-side: explicit override, else id, else default.

    Python owns the agent definitions, so the front door sends only an id and
    the runtime supplies the system prompt + tool allowlist from its presets.

    The built-in ids come from `agents.py`; anything else is a user-authored
    `AgentPreset` (versioned on `agent-memory.db`). Both surfaces must resolve
    here, because a bot's scheduled `run_agent` enters through this function and
    not through the front door.
    """
    if request.agent is not None:
        return request.agent
    agent_id = request.agentId or DEFAULT_AGENT_ID
    if kernel_runtime.active:
        return kernel_runtime.resolve_agent(agent_id)
    try:
        return get_agent(agent_id)
    except ValueError:
        from .agent_presets import agent_config_from_preset, get_preset_store  # noqa: PLC0415

        store = get_preset_store()
        preset = store.get(agent_id) if store is not None else None
        if preset is None:
            raise
        return agent_config_from_preset(preset)


DEFAULT_CHAT_MAX_STEPS = 24
CHAT_MAX_STEPS_CEILING = 32
#: The ordinary chat turn's WALL CLOCK. The preset's `runTimeoutMs` is the
#: per-MODEL-CALL budget; using it as the whole-run deadline made every real
#: research turn (8-20 steps, multi-second tools) exceed 120s and fail with
#: RUN_TIMEOUT / RUN_STALLED before it could answer. 24 steps need more than one
#: call's worth of time, so the default chat agent gets a floor of 20 minutes:
#: a 20-step research turn (data -> compute -> chart -> report) routinely needs
#: 10-15 minutes, and a 10-minute floor killed real turns right before the
#: answer. The pruning ladder (now ON by default) keeps the prompts small, so
#: the extra wall clock is not extra cost. An operator can still lower it with
#: `ECLIPSE_CHAT_RUN_TIMEOUT_S`.
DEFAULT_CHAT_RUN_TIMEOUT_S = 1200.0


def chat_step_budget(agent: AgentConfig, request: Any) -> int:
    """The model-step budget for one run (QA Round 1, Q5).

    The ordinary chat agent defaults to 24 steps: 8 was too tight for a
    multi-tool navigation turn, and 12 was too tight for a request that carries
    several deliverables (fetch → compute → chart → report → widget): the run
    spent its budget on the data work and never produced the file or the
    widget. An operator can override with `ECLIPSE_CHAT_MAX_STEPS` (clamped
    1..32). An
    EXPLICIT per-run `agent` config wins over both — the caller asked for it.
    """
    base = int(agent.maxSteps or 8)
    explicit = getattr(request, "agent", None) is not None
    if agent.id == "eclipse-agent" and not explicit:
        raw = (os.environ.get("ECLIPSE_CHAT_MAX_STEPS") or "").strip()
        parsed: Optional[int] = None
        if raw:
            try:
                parsed = int(raw)
            except ValueError:
                parsed = None
        base = parsed if parsed is not None else max(base, DEFAULT_CHAT_MAX_STEPS)
    return max(1, min(CHAT_MAX_STEPS_CEILING, base))


def chat_run_budget(agent: AgentConfig, request: Any) -> float:
    """The wall clock for one run, in seconds.

    `runTimeoutMs` is declared per MODEL CALL (see the preset comment and
    `run_watchdog.stall_threshold_s`). A multi-step chat turn needs more than one
    call, so the default chat agent gets `DEFAULT_CHAT_RUN_TIMEOUT_S` as a floor.
    An operator can tune it with `ECLIPSE_CHAT_RUN_TIMEOUT_S` (60..HARD cap); an
    explicit per-run agent config still wins.
    """
    seconds = float((agent.runTimeoutMs or 120_000) / 1000.0)
    explicit = getattr(request, "agent", None) is not None
    if agent.id == "eclipse-agent" and not explicit:
        floor = DEFAULT_CHAT_RUN_TIMEOUT_S
        raw = (os.environ.get("ECLIPSE_CHAT_RUN_TIMEOUT_S") or "").strip()
        if raw:
            try:
                floor = max(60.0, min(float(raw), HARD_RUN_TIMEOUT_S))
            except ValueError:
                pass
        seconds = max(seconds, floor)
    return seconds


# Phase 22 / S2: a build session may buy more than the chat ceiling, but only up
# to these hard caps. The caps belong to the runtime; a caller cannot raise them.
HARD_MAX_STEPS = 240
HARD_RUN_TIMEOUT_S = 10_800.0


def requested_budget(
    max_steps: int, run_timeout_s: float, request: Any,
) -> tuple[int, float]:
    """Apply a session's additive budget, clamped to the hard caps.

    Absent fields leave the run's existing budget untouched. A value can only
    RAISE the budget: a session is a floor on the work, never a way to shrink a
    run that already has more. Pure, so the policy is testable without a graph.
    """
    steps = getattr(request, "maxSteps", None)
    if steps:
        max_steps = max(max_steps, min(int(steps), HARD_MAX_STEPS))
    timeout_ms = getattr(request, "runTimeoutMs", None)
    if timeout_ms:
        run_timeout_s = max(
            run_timeout_s, min(float(timeout_ms) / 1000.0, HARD_RUN_TIMEOUT_S),
        )
    return max_steps, run_timeout_s


def _task_profile_of(request: RunRequest, agent: AgentConfig) -> str:
    """The task profile the ModelRouter scores against for this run (spec §14)."""
    from .model_runtime.integration import task_profile_of

    return task_profile_of(request, agent)


def _runtime_requirements(request: RunRequest) -> dict[str, Any]:
    """Routing requirements a run may carry via its input context (additive)."""
    from .model_runtime.integration import runtime_requirements

    return runtime_requirements(request)


def _runtime_policy_overrides(request: RunRequest) -> dict[str, Any]:
    """User/workflow routing overrides read from `input.context.modelPolicy`."""
    from .model_runtime.integration import runtime_policy_overrides

    return runtime_policy_overrides(request)


def _ui_focus_block(context: Any) -> str:
    """A per-run note naming the UI page the user is working on (additive).

    Presentation context only: it tells the agent which blueprint the `ui_*`
    tools should target by default and grants nothing. Returns "" when absent.
    Rendered into the per-run `runtime_context` (excluded from the cache key), so
    it never invalidates the reusable prompt prefix.
    """
    if not isinstance(context, dict):
        return ""
    ui = context.get("ui")
    if not isinstance(ui, dict):
        return ""
    page = ui.get("page")
    if not isinstance(page, dict):
        return ""
    blueprint_id = str(page.get("blueprintId") or "").strip()
    title = str(page.get("title") or "").strip()
    path = str(page.get("path") or "").strip()
    if not (blueprint_id or title or path):
        return ""
    label = title or path or blueprint_id
    lines = ["# Current UI page", f'The user is currently working on the page "{label}".']
    if blueprint_id:
        lines.append(
            f'Use blueprintId "{blueprint_id}" with the ui_* tools unless the user '
            "names another page; call ui_get_current_page to read it first."
        )
    return "\n".join(lines)


def _app_control_block(context: Any) -> str:
    """A per-run note for an explicit app-control turn (grants nothing)."""
    if not isinstance(context, dict) or context.get("appControl") is not True:
        return ""
    return (
        "# App control\n"
        "The user is controlling their Eclipse app surface. Prefer the sdk_* tools: "
        "sdk_dashboard_* for the Command Center, sdk_widget_* for declarative widgets, sdk_app_* for navigation, "
        "sdk_page_* for saved pages, sdk_theme_* for themes/presets, and sdk_lock_* for locks. "
        "Use ui_* only when the SDK has no verb for the operation. Do not edit source code "
        "or add a new renderer unless the user asks for a new visual kind; that is the "
        "/eclipsesdk code path."
    )


def _project_brief_block(project_id: Optional[str], project_store: Optional[Any]) -> str:
    """The project this run belongs to, as a per-run context block.

    The brief is the **curated** view when the curator is available: what the
    project is about, which members matter most and what it still lacks. It
    never says what the run should conclude: a project is a scope, not an
    opinion. An unknown project id is reported, never silently ignored, so a
    typo cannot masquerade as "no project".
    """
    if not project_id or project_store is None:
        return ""
    try:
        from .projects.curate import curate_project  # noqa: PLC0415

        digest = curate_project(project_store, str(project_id))
        if digest is not None:
            return digest.to_markdown()
    except Exception:  # noqa: BLE001 - a project read must never break the run
        pass
    try:
        brief = project_store.brief(str(project_id))
    except Exception:  # noqa: BLE001 - a project read must never break the run
        return ""
    if not brief:
        return (f"## Project\nThis run names project `{project_id}`, but no such "
                f"project exists.")
    return brief


def _run_workspace(request: Any) -> Any:
    """Resolve this run's workspace, honouring a per-chat approved root id.

    Called without the second argument when the run has no per-chat binding, so
    the historical one-argument seam (and tests that patch it) stays intact.
    """
    token = getattr(request, "workspaceScratchToken", None)
    if token:
        # W7: an isolated delegated child. The token was minted by
        # `worktree.allocate`; it is consumed once and must resolve under the
        # server's own scratch root.
        from .code.worktree import claim_scratch  # noqa: PLC0415
        from .code.workspace import resolve_scratch_workspace  # noqa: PLC0415

        return resolve_scratch_workspace(claim_scratch(token))
    root_id = getattr(request, "workspaceRootId", None)
    if root_id:
        return resolve_workspace(request.workspace, root_id)
    return resolve_workspace(request.workspace)


def _resolve_direct(request: Any) -> bool:
    """Whether a run edits the real folder or a disposable copy.

    An explicit `request.direct` always wins. When absent (None), a run edits
    DIRECTLY only if the active root is one the user chose and approved; the
    built-in repo root stays isolated.
    """
    explicit = getattr(request, "direct", None)
    if explicit is not None:
        return bool(explicit)
    from .code.workspace import trusted_root_path, trusted_root_selected  # noqa: PLC0415

    # A per-chat approved folder is a user act, exactly like the global
    # selection: edit it directly. Without a per-run id, fall back to the
    # globally selected trusted root (the built-in repo root stays isolated).
    root_id = getattr(request, "workspaceRootId", None)
    if root_id:
        return trusted_root_path(root_id) is not None
    return trusted_root_selected()


def _effect_authorizer(
    approvals: Optional[Any],
    run_id: str,
    emitter: RunEmitter,
    workspace_id: Optional[str] = None,
) -> Optional[Any]:
    """The run's effect authorizer (composition lives in `code/authorizers.py`)."""
    from .code.authorizers import build_effect_authorizer  # noqa: PLC0415

    return build_effect_authorizer(approvals, run_id, emitter, workspace_id)


# The tool-surface resolution moved to `surface.py` (Round 3 / W2, ADR-H280) so this
# execution graph stays under its size ratchet. `effective_allowlist`,
# `chat_code_allowlist`, `surface_disabled_tools` and `resolve_surface` are
# imported above and stay importable from this module.
#
# A surface EXTENDS or NARROWS the declared tools; it never silently REPLACES
# them. Both halves go through the ONE policy authority
# (`code.policy.allowed_tools`), so an autonomy profile still removes every
# write/execute/external tool it bans - from the base half AND the surface half.

def build_graph(spec: RunSpec, scope: RunScope) -> Any:
    """Compile a LangGraph StateGraph for one run. Returns an awaitable app.

    `spec` carries the facts of this invocation (request, emitter, resume point,
    allowlist, authorizer, result sinks); `scope` carries the process-wide
    services the run reuses. Both are unpacked here, so the node bodies see the
    same locals as before the split.
    """
    request = spec.request
    emitter = spec.emitter
    plan = spec.plan
    agent = spec.agent or resolve_agent(request)
    if kernel_runtime.active:
        request.modelSettings = kernel_runtime.apply_model_route(agent.id, request.modelSettings)
    effect_authorizer = spec.effect_authorizer
    allowed = spec.allowed
    # Disclosure hides an ADVERTISEMENT, not a tool: dispatch keeps the full
    # resolved surface (a policy removal is absent from both).
    dispatch_allowed = spec.dispatch_allowed or allowed
    source_sink = spec.source_sink
    lineage = spec.lineage
    resume_from = spec.resume_from
    code_ledger = spec.code_ledger
    vision_sink = spec.vision_sink
    document_sink = spec.document_sink
    image_sink = spec.image_sink
    video_sink = spec.video_sink
    advisory_sink = spec.advisory_sink

    registry = kernel_runtime.view_for(
        request.runId, scope.registry, agent_id=agent.id,
    )
    data_bridge = scope.data_bridge
    store = scope.store
    # W6: the identity that owns this run's lease. `None` means the embedded
    # path; the fenced write falls back to the process default, which is the
    # same identity `RunLeaseGuard` uses.
    runtime_id = scope.runtime_id
    from .run_lease import LeaseLost, fenced_progress  # noqa: PLC0415

    def _write_progress(**fields: Any) -> None:
        """Write run progress only while this runtime owns the lease.

        `LeaseLost` is deliberately NOT swallowed: a stale worker that keeps
        writing can clobber the new owner's counters and terminal status.
        """
        fenced_progress(store, request.runId, runtime_id=runtime_id, **fields)
    from .context.usage_anchor import prepare_anchor  # noqa: PLC0415

    anchor_store, anchor_id, _anchor, history_chars = prepare_anchor(store, request)
    from .run_effects import RunEffects  # noqa: PLC0415

    effects = RunEffects(store)
    bus = scope.bus
    cancel_event = scope.cancel_event
    retrieval_cache = scope.retrieval_cache
    memory_service = scope.memory_service
    skill_registry = scope.skill_registry
    memory_router = scope.memory_router
    mcp_manager = scope.mcp_manager
    approvals = scope.approvals
    code_store = scope.code_store
    code_intel_runtime = scope.code_intel_runtime
    evidence_store = scope.evidence_store
    learning_store = scope.learning_store
    market = scope.market
    portfolio = scope.portfolio
    ledger = scope.ledger
    advisory = scope.advisory
    advisory_service = scope.advisory
    decision_service = scope.decision
    quant = scope.quant
    project_store = scope.project_store
    search = scope.search
    artifact_service = scope.artifact_service
    ui_service = scope.ui_service
    task_manager = scope.task_manager

    # Capability gaps from this run's tool results (fail-open; never changes a
    # tool decision). The answer seam uses its own instance below.
    from .gaps.runtime import GapRuntime  # noqa: PLC0415

    gaps = GapRuntime(store)
    gap_need = str(getattr(getattr(request, "input", None), "query", "") or "")
    # On a resume, reuse the allowlist + accumulated counters recorded at the last
    # durable boundary so the run continues exactly where it stopped.
    _resume_payload = (resume_from or {}).get("payload") or {}
    if resume_from and isinstance(_resume_payload.get("allowlist"), list):
        allowed = set(_resume_payload["allowlist"])
    max_steps: int = chat_step_budget(agent, request)
    tool_timeout_ms = agent.toolTimeoutMs or 20_000
    run_timeout_s = chat_run_budget(agent, request)
    if request.autonomy in ("edit_verify", "approve_external"):
        # A coding turn needs room to explore, edit, run checks and repair. 24
        # steps routinely ended a real task mid-repair; 48 is still bounded by
        # the hard cap and walls, and the duplicate/loop guards stay active.
        max_steps = max(max_steps, 48); run_timeout_s = max(run_timeout_s, 1200.0)
    # Phase 22 / S2: a build session's budget, clamped to the hard caps.
    max_steps, run_timeout_s = requested_budget(max_steps, run_timeout_s, request)
    # A deep quant cycle (catalog -> data -> build -> validate -> robustness ->
    # artifact) needs more than the ordinary chat navigation budget.
    from .quant_intent import is_deep_quant_turn  # noqa: PLC0415
    if is_deep_quant_turn(request.input.query, getattr(request.input, "history", None)):
        max_steps = max(max_steps, 32); run_timeout_s = max(run_timeout_s, 600.0)
    # Round 5 / C1: `run_timeout_s` is the RUN's wall clock. A single model call
    # gets its own, smaller ceiling, so one dead provider cannot consume the
    # whole budget and the retry policy has room to act.
    model_call_timeout_s = effective_call_timeout(run_timeout_s)
    deadline = RunDeadline(run_timeout_s)
    # A hard total tool-call budget. A skill manifest declares
    # `limits.maxToolCalls`; this ENFORCES it in the loop (a skill cannot exceed
    # its declared budget by prompting or by looping). None = unbounded (chat).
    max_tool_calls: Optional[int] = agent.maxToolCalls
    # W3: tool execution flags resolved ONCE per run/attempt. A guardrail ledger
    # is per attempt (a resume starts clean); parallel planning is read-only and
    # can never widen the surface.
    from .config import flags as _tool_flags  # noqa: PLC0415
    from .tool_guardrails import ToolGuardrail  # noqa: PLC0415

    tool_guard = ToolGuardrail(
        mode=_tool_flags.get_flag("HARNESS_TOOL_GUARDRAILS"),
    )
    parallel_mode = _tool_flags.get_flag("HARNESS_PARALLEL_TOOLS")
    tool_concurrency = _tool_flags.get_int("HARNESS_TOOL_CONCURRENCY")
    tool_batch_timeout_s = _tool_flags.get_int("HARNESS_TOOL_BATCH_TIMEOUT_S")
    # Phase 11: bounded replanning is FLAG-GATED and DEFAULT OFF. When enabled, a
    # failed or stalled tool step routes through ONE `replan` node (a revised
    # approach) before the agent continues — never a second loop or registry.
    from .replan import (  # noqa: PLC0415
        ReplanBudget,
        build_replan_instruction,
        replan_enabled,
        should_replan,
    )

    # C4: an explicit goal plan always gets the bounded replan path, even when
    # the generic tool/stall replan flag is off. The cap is unchanged.
    # Ordinary chat also gets it by default: a failed or stalled tool step is
    # exactly the moment a second look pays for itself. `HARNESS_REPLAN_ENABLED`
    # remains the explicit switch for custom agents and for ops to force it.
    replan_budget: Optional[ReplanBudget] = (
        ReplanBudget()
        if (replan_enabled() or plan is not None or agent.id == "eclipse-agent")
        else None
    )
    # `allowed` is the policy-resolved surface from `execute_run` (a disabled
    # store's tool already removed). Fall back to the raw allowlist for direct
    # callers/tests that build a graph without resolving the surface.
    if allowed is None:
        allowed = effective_allowlist(agent, registry)

    # ── Model runtime (provider-agnostic) ──────────────────────────────────
    # The agent no longer talks to a provider. It asks the ModelRuntime to
    # resolve the run's settings through the ModelRouter, then builds the model
    # THROUGH the runtime. The module-level `build_chat_model` is passed as the
    # builder so the existing test seam (and the single construction path) is
    # preserved; the runtime still owns routing, health and usage accounting.
    runtime = get_runtime()
    resolution = runtime.resolve_for_run(
        request.modelSettings,
        task=_task_profile_of(request, agent),
        requirements=_runtime_requirements(request),
        policy_overrides=_runtime_policy_overrides(request),
    )
    from .model_runtime.credential_pool import get_credential_pool  # noqa: PLC0415

    credential_pool = get_credential_pool(request.modelSettings)
    model = runtime.build(
        resolution.settings, builder=build_chat_model, adapter=resolution.adapter
    )
    model = runtime.prepare_model(model, resolution)
    # The allowlist was resolved (policy, intent surface, progressive disclosure)
    # by `execute_run` BEFORE this spec was built, so this binding is already the
    # FINAL surface: the model never sees a tool the tool turn would refuse.
    lc_tools = registry.langchain_tools(allowed)
    model_with_tools = model.bind_tools(lc_tools) if lc_tools else model

    # The retrieval layer is built lazily by the retrieval tools from `ctx`;
    # the shared process-wide cache is attached here so concurrent runs reuse
    # each other's in-flight/cached retrievals (Milestone 2H). Eclipse-Memory is
    # attached so the memory/skill tools work during a normal run.
    ctx = ToolContext(
        run_id=request.runId,
        log=lambda m, d=None: None,
        user_id=request.userId,
        tenant_id=request.tenantId,
        # The durable task this run executes, when the scheduler supplied it.
        task_id=str((request.input.context or {}).get("taskId") or "") or None,
        # Private per-conversation memory (bot chats / group members). Absent for
        # ordinary chat, where memory scoping is unchanged.
        conversation_id=getattr(request, "conversationId", None),
        # The chat session id (the front door's `chatId`). For an ordinary chat
        # turn this IS the user-visible conversation, so produced artifacts can
        # be associated with it (conversation_id stays None there, by design).
        session_id=request.sessionId,
        user_message=request.input.query,
        # A skill run is composed with an agent id of the form `skill:<id>`; its
        # memory saves carry execution-lesson provenance, not explicit-user.
        is_skill_run=agent.id.startswith("skill:"),
        # The resolved agent (prompt, allowlist, surface policy) so a delegated
        # child inherits the same context policy as its parent.
        agent=agent,
        data_bridge=data_bridge,
        retrieval_cache=retrieval_cache,
        emitter=emitter,
        memory_store=store,
        memory_service=memory_service,
        memory_router=memory_router,
        source_sink=source_sink,
        skill_registry=skill_registry,
        model_settings=request.modelSettings,
        bus=bus,
        tool_registry=registry,
        task_manager=task_manager,
        authorize_effect=effect_authorizer,
        mcp_manager=mcp_manager,
        # The read-only workspace binding for this run (Phase 1). Absent ⇒ no
        # code capability (the default-actor rule).
        workspace=_run_workspace(request),
        # Phase 2: the run's autonomy profile and the code-workspace store.
        autonomy=request.autonomy,
        code_store=code_store,
        # The process-wide CIUE runtime (LSP manager + code index/watcher), so the
        # `code_*` tools resolve one shared runtime instead of a global.
        code_intel_runtime=code_intel_runtime,
        # Phase 4: the run's verification ledger — every command execution is
        # recorded here so the final answer can be checked against proven fact.
        code_ledger=code_ledger,
        # Phase 7: the resolved allowlist (a delegate narrows children against it)
        # and how deep this run sits in a delegation chain.
        allowed=allowed,
        delegation_depth=int((request.input.context or {}).get("delegationDepth") or 0),
        # Phase 8: the analysis sandbox is OFF per run unless the caller enables it.
        analysis_enabled=bool(request.analysisEnabled), cancel_event=cancel_event,
        # The approval store, so the terminal authorizer can claim a single-use
        # approval for an `ask`-classified command.
        approvals=approvals,
        # Approval cards: when the request asked to pause, a non-read tool with no
        # matching approval blocks for the operator's decision instead of being
        # refused. The wait is bounded by the agent's run deadline.
        pause_for_approval=str(getattr(request, "approvalMode", "") or "") == "ask",
        approval_timeout_s=max(30.0, run_timeout_s - 15.0),
        # Folder-selection: a run bound to a user-chosen trusted folder edits it
        # directly (that is the point of choosing it); the built-in repo root
        # stays isolated. An explicit `request.direct` always wins.
        direct=_resolve_direct(request),
        # The executing agent, so the terminal subsystem can attribute a session
        # or process to it (multi-agent isolation + observability).
        agent_id=agent.id,
        # The shared EvidenceStore for the web research engine (bound to the ONE
        # agent-memory connection). None = the research tools build their own from
        # the run's memory store (still persisted, just not the shared instance).
        evidence_store=evidence_store,
        # The shared LearningStore for the learning-research engine (bound to the
        # ONE eclipse-memory connection). None = the learning tools build their own
        # from `memory_service` (still persisted, just not the shared instance).
        learning_store=learning_store,
        # The run's vision-result collector, so a `vision_*` tool's structured
        # result reaches the run blob the right sidebar renders.
        vision_sink=vision_sink,
        # The run's document-intelligence collector (same idiom).
        document_sink=document_sink,
        # The run's internet-image collector: `image_search` hands validated
        # results here so they render inline in the chat (never fabricated).
        image_sink=image_sink,
        # The run's native-video job collector: `video_*` hands queued job
        # handles here so the chat renders live job cards (progress → player).
        video_sink=video_sink,
        # The run's advisory collector: every `portfolio_*` tool call is recorded
        # here so the answer seam can verify actionable advice (the Fiduciary Gate).
        advisory_sink=advisory_sink,
        # The Advisory Ledger service, so `advisory_*` tools reach the ONE ledger.
        advisory_service=advisory_service,
        # The typed-decision service, so `decision_*` tools reach the ONE
        # provider registry + audit store.
        decision_service=decision_service,
        # The Market Data Layer: gateway (routing + quality), durable snapshot/
        # instrument store and stream manager, as one bundle. Shared process-wide.
        market_gateway=getattr(market, "gateway", None),
        market_store=getattr(market, "store", None),
        market_stream=getattr(market, "stream", None),
        # Quant Engine store (strategies + experiments) on the ONE agent-memory
        # connection, so `quant_*` tools persist and replay research.
        quant_store=getattr(quant, "store", None),
    project_store=project_store,
        # Immutable versioned research datasets (manifests + gzipped artifacts),
        # so `build_research_dataset`/`query_dataset` persist reproducible data.
        dataset_store=getattr(quant, "dataset_store", None),
        # The Artifact Center service, so `artifact_*` tools register and discover
        # produced files through the ONE catalog (shared with producers).
        artifact_service=artifact_service,
        # The UI Engineering service, so `ui_*` tools reach the ONE catalog +
        # blueprint store (shared with the routes and producers).
        ui_service=ui_service,
        # The Portfolio Accounting Engine service, so `portfolio_*` tools reach
        # the ONE ledger/valuation engine and the per-run market gateway.
        portfolio_service=getattr(portfolio, "service", None),
        # The General Ledger service, so `ledger_*` tools reach the ONE
        # double-entry book. Accepts either the service or a bundle exposing
        # `.service`.
        ledger_service=getattr(ledger, "service", None) or ledger,
        # The unified search service, so the retrieval/search tools reach the ONE
        # index. The run's workspace/owner scope the agent's authorized results.
        search=search,
        # Phase 4: the kernel's provider catalog, when enabled, so the retrieval
        # tools describe plugin-contributed data providers too.
        provider_registry=getattr(kernel_runtime, "provider_registry", None),
        workspace_id=request.workspaceId,
    )
    # Discovery/invoke sees the PRE-disclosure surface; policy removals do not.
    try:
        ctx.allowed = dispatch_allowed
    except Exception:  # noqa: BLE001 - a frozen context must not break a run
        pass
    registry.set_active_context(ctx)

    def _checkpoint_payload(state: GraphState) -> dict[str, Any]:
        """A bounded checkpoint reference — enough to resume, never a trace."""
        last = state["messages"][-1] if state.get("messages") else None
        pending = [
            {"id": c.get("id"), "name": c.get("name"), "args": c.get("args")}
            for c in (getattr(last, "tool_calls", None) or [])
        ]
        payload = {
            "step": state.get("step", 0),
            "toolCalls": state.get("tool_calls", 0),
            "toolErrors": state.get("tool_errors", 0),
            "replans": state.get("replans", 0),
            "promptTokens": state.get("prompt_tokens", 0),
            "completionTokens": state.get("completion_tokens", 0),
            "pendingToolCalls": pending,
            "allowlist": sorted(allowed) if allowed else None,
            "messages": _serialize_messages(state.get("messages", [])),
        }
        if plan is not None:
            # C4: the checkpoint names the durable plan/step cursor. The plan
            # itself is durable; resume rehydrates the same step, not a new one.
            payload.update(plan.checkpoint_fields(state.get("plan_step_id")))
        return payload

    def _consume_w7(state: GraphState) -> None:
        """Mark injected steers/completions consumed at the durable boundary.

        Called only after the checkpoint is written. A crash before this point
        leaves them pending, so a resume re-injects them; a normal step marks
        them exactly once.
        """
        pending = [item for item in (state.get("w7_pending") or []) if isinstance(item, dict)]
        if not pending:
            return
        try:
            from .delegation_lifecycle import mark_inbox_consumed  # noqa: PLC0415
            from .delegation_store import DelegationStore  # noqa: PLC0415

            store_view = DelegationStore(store)
            steer_seqs = [
                int(item.get("seq") or 0) for item in pending
                if item.get("kind") == "steer" and _w7_delegation_id
            ]
            if steer_seqs and _w7_delegation_id:
                store_view.consume_steers(_w7_delegation_id, [s for s in steer_seqs if s])
            inbox_keys = [
                (str(item.get("delegationId")), int(item.get("childIndex") or 0))
                for item in pending if item.get("kind") == "inbox"
            ]
            if inbox_keys:
                mark_inbox_consumed(store, inbox_keys)
        except Exception:  # noqa: BLE001 - consumption is best-effort, never fatal
            pass

    async def _write_checkpoint(kind: str, state: GraphState) -> None:
        if lineage is None:
            return
        payload = _checkpoint_payload(state)
        from .config.flags import get_bool  # noqa: PLC0415

        if get_bool("HARNESS_CHECKPOINT_ATOMIC"):
            # W6: one transaction for the message cursor and the checkpoint.
            # `save_boundary` serializes before the lock and retries once; a
            # persistent failure must stop the run, never continue without a
            # durable boundary.
            from .run_lineage import CheckpointWriteFailed  # noqa: PLC0415

            try:
                lineage.save_boundary(
                    request.runId, kind, payload, state.get("messages", []), retries=1,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a run failure
                raise CheckpointWriteFailed(
                    f"could not write the {kind!r} boundary for {request.runId!r}: {exc}"
                ) from exc
            _consume_w7(state)
            return
        # Rollback path: the legacy two-commit write. A checkpoint remains
        # best-effort only when the operator explicitly turns atomicity off.
        try:
            cursor = lineage.save_messages(request.runId, state.get("messages", []))
            if cursor is not None:
                payload["messageSeq"] = cursor
            lineage.save_checkpoint(request.runId, kind, payload)
        except Exception:  # noqa: BLE001 - a checkpoint must never break a run
            pass
        _consume_w7(state)

    # Round 5 / C7: one step span per loop iteration. The step id is stable and
    # carries the run id so a trace tree is reconstructable from the bus alone.
    async def _emit_step_started(step: int) -> None:
        try:
            await emitter.emit("step.started", {
                "step": step,
                "stepId": f"{request.runId}:step:{step}",
                "agentId": getattr(agent, "id", None),
            })
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass

    async def _emit_step_completed(
        step: int, *, status: str, tool_calls: int = 0, tool_errors: int = 0,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        data: dict[str, Any] = {
            "step": step,
            "stepId": f"{request.runId}:step:{step}",
            "status": status,
            "toolCalls": int(tool_calls or 0),
            "toolErrors": int(tool_errors or 0),
        }
        if error:
            data["error"] = error
        try:
            await emitter.emit("step.completed", data)
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            pass

    # Round 3 / W1 (ADR-H281): the repair budget's wall clock starts when the
    # run starts, not when the first failure happens - a run that spent an hour
    # before its first check has not earned an hour of repair.
    _repair_started = time.monotonic()

    # W7: a delegated child (or an operator steering one) gets new context only
    # at a STEP BOUNDARY. This is the one drain point; tools never see a steer
    # mid-batch. `_write_checkpoint` consumes what was injected.
    _w7_delegation_id = str(
        (getattr(request.input, "context", None) or {}).get("delegationId") or ""
    )

    def _apply_w7_context(state: GraphState) -> GraphState:
        # Gap #4: a background command that finished now becomes a message the
        # model sees at THIS step boundary, once. The notice is data and says
        # not to poll. Failure to inject must never break a run.
        try:
            from .background_jobs import inject_into_state  # noqa: PLC0415

            state = inject_into_state(state, request.runId)
        except Exception:  # noqa: BLE001 - a notice is a convenience, not the work
            pass
        # Gap #8: the coding plan is injected only when it CHANGED, so the model
        # sees its own list across steps without paying for it every turn.
        try:
            from .code.todos import plan_message  # noqa: PLC0415

            state = plan_message(state, store, request.runId)
        except Exception:  # noqa: BLE001 - a plan is never the work
            pass
        applied = {
            str(item.get("key"))
            for item in (state.get("w7_pending") or [])
            if isinstance(item, dict)
        }
        new_items: list[dict[str, Any]] = []
        extra: list[BaseMessage] = []
        if _w7_delegation_id:
            try:
                from .delegation_store import DelegationStore  # noqa: PLC0415

                steers = DelegationStore(store).peek_steers(_w7_delegation_id)
            except Exception:  # noqa: BLE001 - steering must never break a run
                steers = []
            fresh = [row for row in steers if f"steer:{row.get('seq')}" not in applied]
            if fresh:
                text = "\n".join(str(row.get("message") or "") for row in fresh).strip()
                if text:
                    extra.append(HumanMessage(content=(
                        "<operator_steer>\n"
                        "The operator added this guidance for the current work. Apply it "
                        "at this step; it is not a new task.\n\n"
                        f"{text[:4_000]}\n</operator_steer>"
                    )))
                for row in fresh:
                    new_items.append({"key": f"steer:{row.get('seq')}", "kind": "steer",
                                      "seq": int(row.get("seq") or 0)})
        try:
            from .delegation_lifecycle import peek_inbox  # noqa: PLC0415

            inbox_text, inbox_keys = peek_inbox(store, request.runId)
        except Exception:  # noqa: BLE001 - a completion display must not break a run
            inbox_text, inbox_keys = "", []
        fresh_keys = [
            (delegation_id, index) for delegation_id, index in inbox_keys
            if f"inbox:{delegation_id}:{index}" not in applied
        ]
        if fresh_keys and inbox_text:
            extra.append(HumanMessage(content=inbox_text))
            for delegation_id, index in fresh_keys:
                new_items.append({
                    "key": f"inbox:{delegation_id}:{index}", "kind": "inbox",
                    "delegationId": delegation_id, "childIndex": index,
                })
        if not extra:
            return state
        return {
            **state,
            "messages": [*(state.get("messages") or []), *extra],
            "w7_pending": [*(state.get("w7_pending") or []), *new_items],
        }

    async def agent_node(state: GraphState) -> GraphState:
        state = _apply_w7_context(state)
        # The recovery flag is a one-shot edge signal, never a loop condition.
        if state.get("recovery_pending"):
            state = {**state, "recovery_pending": False}
        step = state.get("step", 0) + 1
        await _emit_step_started(step)
        step_now = plan.current_step if plan is not None else None
        plan_step_id = step_now.id if step_now is not None else state.get("plan_step_id")
        # Round 5 / C1: the RUN deadline is checked before every model call. A
        # run that spent its budget in earlier steps fails honestly here instead
        # of starting one more call that can only be abandoned.
        if deadline.expired():
            message = f"Run exceeded its {int(run_timeout_s)}s deadline."
            await _emit_step_completed(step, status="failed", error={"code": "RUN_TIMEOUT", "message": message})
            await emitter.emit("run.failed", {"error": {"code": "RUN_TIMEOUT", "message": message}})
            return {**state, "step": step, "error": {"code": "RUN_TIMEOUT", "message": message}}
        await emitter.emit("model.requested", {"step": step, "messages": len(state["messages"])})
        # On the last allowed step, forbid tools so the model MUST answer from the
        # evidence it already gathered. Without this a tool-calling model can loop
        # until max steps and produce MAX_STEPS_EXCEEDED instead of an answer —
        # the exact failure that made the agent "unable to answer". The same
        # applies once a hard tool-call budget is spent.
        budget_spent = max_tool_calls is not None and state.get("tool_calls", 0) >= max_tool_calls
        token_budget = getattr(agent, "maxTokens", None)
        tokens_spent = int(state.get("prompt_tokens", 0)) + int(state.get("completion_tokens", 0))
        token_budget_spent = token_budget is not None and tokens_spent >= int(token_budget)
        finalizing = step >= max_steps or budget_spent or token_budget_spent
        active_model = model if finalizing else model_with_tools
        # A local model often ignores an empty tool list and keeps emitting tool
        # calls from the conversation's momentum (observed hallucinating a
        # `terminal` tool that does not exist). Unbinding tools is necessary but
        # not sufficient: also tell it, in-band, that this is the answer turn.
        model_messages = (
            [*state["messages"], HumanMessage(content=_FINALIZE_INSTRUCTION)]
            if finalizing
            else state["messages"]
        )

        def _fallback_build(settings: Any) -> Any:
            """Build a fallback model through the runtime, preserving tools."""
            built = runtime.build(
                settings, builder=build_chat_model, adapter=resolution.adapter,
            )
            built = runtime.prepare_model(built, resolution)
            return built.bind_tools(lc_tools) if (not finalizing and lc_tools) else built

        from .config import flags as _flags  # noqa: PLC0415

        _step_started = time.monotonic()
        try:
            response = await invoke_model_step(
                active_model,
                model_messages,
                timeout_s=model_call_timeout_s,
                emitter=emitter,
                finalizing=finalizing,
                deadline=deadline,
                step=step,
                provider_id=request.modelSettings.provider,
                model_id=request.modelSettings.model,
                resolution=resolution,
                fallback_build=_fallback_build,
                run_id=request.runId,
                first_token_timeout_s=float(_flags.get_int("HARNESS_MODEL_FIRST_TOKEN_S")),
                max_run_tokens=(
                    int(token_budget) - tokens_spent
                    if token_budget is not None and not finalizing and tokens_spent < token_budget
                    else None
                ),
                credential_pool=credential_pool,
                usage_anchor_store=anchor_store,
                anchor_id=anchor_id,
                history_chars=history_chars,
            )
        except asyncio.TimeoutError:
            timed_out_message = (
                f"Run exceeded its {int(run_timeout_s)}s deadline."
                if deadline.expired()
                else "model call timed out"
            )
            code = "RUN_TIMEOUT" if deadline.expired() else "PROVIDER_TIMEOUT"
            await _emit_step_completed(step, status="failed", error={"code": code, "message": timed_out_message})
            await emitter.emit("run.failed", {"error": {"code": code, "message": timed_out_message}})
            return {**state, "step": step, "error": {"code": code, "message": timed_out_message}}
        except Exception as exc:  # noqa: BLE001
            # Classify a provider context-length rejection SPECIFICALLY, before the
            # generic provider error. The terminal run.failed for overflow is
            # emitted once by `execute_run` after the bounded recovery attempt, so
            # a successful retry does not leave a spurious terminal failure.
            from .models import (  # noqa: PLC0415
                classify_context_overflow,
                context_overflow_error,
                resolve_context_window,
            )

            if classify_context_overflow(exc):
                provider = request.modelSettings.provider
                model_name = request.modelSettings.model
                # Prefer the operator-declared window when the catalog cannot know
                # it, so the error message states the real window.
                window = getattr(request.modelSettings, "context_window", None)
                if window is None:
                    window = resolve_context_window(provider, model_name)
                code, message = context_overflow_error(provider, model_name, window)
                await emitter.emit(
                    "context.overflow",
                    {"step": step, "provider": provider, "windowKnown": window is not None},
                )
                await _emit_step_completed(step, status="failed", error={"code": code, "message": message})
                return {**state, "step": step, "error": {"code": code, "message": message}}
            # Name the local server and the remedy, not a bare "Connection error.".
            failure_message = provider_failure_message(
                exc, provider_id=request.modelSettings.provider, model_id=request.modelSettings.model,
                base_url=getattr(request.modelSettings, "base_url", None))
            await emitter.emit("run.failed", {"error": {"code": "PROVIDER_ERROR", "message": failure_message}})
            await _emit_step_completed(step, status="failed", error={"code": "PROVIDER_ERROR", "message": failure_message})
            return {**state, "step": step, "error": {"code": "PROVIDER_ERROR", "message": failure_message}}

        tool_calls = getattr(response, "tool_calls", None) or []
        # On the final step we do NOT execute tools — the model must answer. If a
        # provider still returned tool calls, drop them so routing falls through
        # to the final answer instead of looping into MAX_STEPS_EXCEEDED.
        if finalizing and tool_calls:
            try:
                response.tool_calls = []
            except Exception:  # noqa: BLE001
                pass
            tool_calls = []
        step_prompt, step_completion = _extract_usage(response)
        total_prompt = state.get("prompt_tokens", 0) + step_prompt
        total_completion = state.get("completion_tokens", 0) + step_completion
        await emitter.emit(
            "model.completed",
            {
                "step": step,
                "hasToolCalls": bool(tool_calls),
                "promptTokens": step_prompt,
                "completionTokens": step_completion,
                "totalTokens": step_prompt + step_completion,
            },
        )
        _write_progress(
            modelSteps=step,
            promptTokens=total_prompt,
            completionTokens=total_completion,
            totalTokens=total_prompt + total_completion,
        )

        usage_state = {"prompt_tokens": total_prompt, "completion_tokens": total_completion}

        if not tool_calls:
            from .quant_intent import finalize_quant_text  # noqa: PLC0415
            final_text = finalize_quant_text(
                request.input.query, getattr(request.input, "history", None),
                state["messages"], _text(response),
            )
            # The final-answer branch (empty-final failure, bounded answer
            # recovery, C4 settlement, accepted state) lives in its own module
            # to keep this loop under its size ratchet.
            from .final_answer import finish_turn  # noqa: PLC0415

            return await finish_turn(
                request=request, emitter=emitter, state=state, response=response,
                step=step, final_text=final_text, usage_state=usage_state,
                plan=plan, checks=checks_from_ledger(code_ledger),
                max_steps=max_steps, max_tool_calls=max_tool_calls,
                token_budget=token_budget, budget_spent=budget_spent,
                token_budget_spent=token_budget_spent, finalizing=finalizing,
                allowed=allowed, plan_step_id=plan_step_id,
                emit_step_completed=_emit_step_completed,
                write_checkpoint=_write_checkpoint,
            )

        tool_state: GraphState = {
            **state, **usage_state, "step": step, "messages": [*state["messages"], response],
            "plan_step_id": plan_step_id,
        }
        # Durable boundary: one checkpoint per completed agent step, BEFORE the
        # tools node runs, so a crash during tool execution resumes here.
        await _write_checkpoint("agent_step", tool_state)
        return tool_state

    async def tools_node(state: GraphState) -> GraphState:
        # W3: the turn body lives in `tool_turn.py` so this file keeps wiring
        # only. It is the SAME preflight/dispatch/postflight contract as before;
        # parallel mode batches only CONTIGUOUS parallel-safe calls.
        from .tool_turn import ToolTurn, ToolTurnDeps  # noqa: PLC0415

        deps = ToolTurnDeps(
            request=request, store=store, emitter=emitter, registry=registry,
            ctx=ctx, effects=effects, lineage=lineage, plan=plan, gaps=gaps,
            gap_need=gap_need, deadline=deadline, run_timeout_s=run_timeout_s,
            max_tool_calls=max_tool_calls, tool_timeout_ms=tool_timeout_ms,
            # Dispatch sees the pre-disclosure surface; the schema is narrowed.
            allowed=dispatch_allowed, guard=tool_guard, hook=_run_plugin_hook,
            write_checkpoint=_write_checkpoint,
            write_progress=_write_progress,
            emit_step_completed=_emit_step_completed,
            parallel_mode=parallel_mode, concurrency=tool_concurrency,
            batch_timeout_s=tool_batch_timeout_s,
        )
        return await ToolTurn(deps).run(state)

    def route_after_agent(state: GraphState) -> str:
        if state.get("error"):
            return END
        # Answer recovery: the node appended a directive and asked for one more
        # agent turn; there is no `final` yet and no tool call to dispatch.
        if state.get("recovery_pending"):
            return "agent"
        # C4: an evidence failure outranks the final answer; the replan node
        # clears the draft and sends the model back with the missing evidence.
        if state.get("replan_needed"):
            return "replan"
        if state.get("final") is not None:
            return END
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    def route_after_tools(state: GraphState) -> str:
        if state.get("error"):
            return END
        # W3.2: a guardrail halt is a controlled stop; the tools node already
        # wrote the deterministic `final`, so do not run verify/repair or replan.
        if state.get("guardrail_halt"):
            return END
        if state.get("step", 0) >= max_steps:
            return "exhausted"
        # Phase 11: only when the flag wired a replan node AND a bounded replan
        # remains do we divert through one revision step.
        if replan_budget is not None and should_replan(
            replans=int(state.get("replans", 0) or 0),
            budget=replan_budget,
            step_failures=int(state.get("step_failures", 0) or 0),
            stalled=bool(state.get("stalled")),
        ):
            return "replan"
        # Round 3 / W1 (ADR-H281): every tool turn passes the verify-and-repair
        # node, so a check that the project pins runs after a change and a
        # failure is handed back to the model. The node is a no-op when the
        # turn changed nothing, when no check is pinned, or when the profile
        # does not permit a repair.
        return "verify_repair"

    def route_after_repair(state: GraphState) -> str:
        if state.get("error"):
            return END
        if state.get("step", 0) >= max_steps:
            return "exhausted"
        return "agent"

    async def verify_repair_node(state: GraphState) -> GraphState:
        """Run the project's pinned checks after a change, and drive a repair.

        The logic lives in `code/repair_graph.py` (the file-size ratchet: this
        graph grows by import, not by body). The node is a no-op when the turn
        changed nothing, when no check is pinned, or when the profile does not
        permit a repair - and it never widens authority: the pinned class
        still goes through `code.policy.decide_command` and the profile's
        grants, exactly as it would if the model had asked for it.
        """
        from .code.repair_graph import repair_turn  # noqa: PLC0415

        repair_state, message = await repair_turn(
            state=state, ctx=ctx, registry=registry, autonomy=request.autonomy,
            emitter=emitter, ledger=code_ledger, started_at=_repair_started,
        )
        if repair_state is None:
            return state
        updated: GraphState = {**state, "repair": repair_state}
        if message is not None:
            updated["messages"] = [*state["messages"], HumanMessage(content=message)]
        return updated

    async def replan_node(state: GraphState) -> GraphState:
        """One bounded revision step on the SAME loop (Phase 11).

        It appends INERT prose naming what failed or stalled; it authorizes
        nothing and cannot widen the allowlist. The model may act on it or
        ignore it. `run.replanned` makes the diversion honest and observable.
        """
        evidence_failure = state.get("replan_needed") if isinstance(
            state.get("replan_needed"), dict,
        ) else None
        if evidence_failure is not None:
            reason = "evidence"
            replans = int(state.get("replans", 0) or 0) + 1
            instruction = (
                plan.evidence_replan_instruction() if plan is not None
                else "Your answer failed the evidence check. Gather concrete "
                     "evidence or label the claim unverified."
            )
            if plan is not None:
                plan.replan(run_id=request.runId, reason="evidence")
        else:
            reason = should_replan(
                replans=int(state.get("replans", 0) or 0),
                budget=replan_budget,
                step_failures=int(state.get("step_failures", 0) or 0),
                stalled=bool(state.get("stalled")),
            ) or "tool_failures"
            replans = int(state.get("replans", 0) or 0) + 1
            instruction = build_replan_instruction(
                reason,
                failures=state.get("step_failure_details") or [],
                stalled_calls=state.get("last_turn_keys") or [],
            )
        await emitter.emit("run.replanned", {
            "replan": replans,
            "reason": reason,
            "maxReplans": replan_budget.max_replans if replan_budget else 0,
        })
        revised: GraphState = {
            **state,
            "replans": replans,
            "messages": [*state["messages"], HumanMessage(content=instruction)],
            # Clear the trigger so only a new failure/stall causes another replan.
            "step_failures": 0,
            "stalled": False,
            "replan_needed": None,
            # The rejected draft answer is not the run's answer.
            "final": None,
        }
        await _write_checkpoint("replan", revised)
        return revised

    async def exhausted_node(state: GraphState) -> GraphState:
        message = f"Reached the maximum of {max_steps} steps without a final answer."
        await emitter.emit("run.failed", {"error": {"code": "MAX_STEPS_EXCEEDED", "message": message}})
        exhausted: GraphState = {"error": {"code": "MAX_STEPS_EXCEEDED", "message": message}}
        await _write_checkpoint("terminal", {**state, **exhausted})
        return {**state, **exhausted}

    graph = StateGraph(GraphState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("verify_repair", verify_repair_node)
    graph.add_node("exhausted", exhausted_node)
    graph.add_edge(START, "agent")
    # Round 3 / W1 (ADR-H281): the verify-and-repair node sits between the tool
    # turn and the next agent turn, so a pinned check runs after a change and a
    # failure is handed back to the model.
    graph.add_conditional_edges(
        "verify_repair", route_after_repair,
        {"agent": "agent", "exhausted": "exhausted", END: END},
    )
    if replan_budget is not None:
        graph.add_node("replan", replan_node)
        graph.add_conditional_edges(
            "agent", route_after_agent,
            {"tools": "tools", "replan": "replan", "agent": "agent", END: END},
        )
        graph.add_conditional_edges(
            "tools", route_after_tools,
            {"replan": "replan", "verify_repair": "verify_repair",
             "agent": "agent", "exhausted": "exhausted", END: END},
        )
        graph.add_edge("replan", "agent")
    else:
        graph.add_conditional_edges(
            "agent", route_after_agent,
            {"tools": "tools", "agent": "agent", END: END},
        )
        graph.add_conditional_edges(
            "tools", route_after_tools,
            {"verify_repair": "verify_repair", "agent": "agent", "exhausted": "exhausted",
             END: END},
        )
    graph.add_edge("exhausted", END)
    return graph.compile()


async def execute_run(
    request: RunRequest,
    registry: ToolRegistry,
    data_bridge: DataBridge,
    store: AgentMemoryStore,
    bus: Optional[EventBus] = None,
    cancel_event: Optional[asyncio.Event] = None,
    retrieval_cache: Optional[Any] = None,
    memory_service: Optional[Any] = None,
    skill_registry: Optional[Any] = None,
    approvals: Optional[Any] = None,
    memory_router: Optional[Any] = None,
    mcp_manager: Optional[Any] = None,
    code_store: Optional[Any] = None,
    code_intel_runtime: Optional[Any] = None,
    evidence_store: Optional[Any] = None,
    learning_store: Optional[Any] = None,
    market: Optional[Any] = None,
    portfolio: Optional[Any] = None,
    ledger: Optional[Any] = None,
    advisory: Optional[Any] = None,
    decision: Optional[Any] = None,
    quant: Optional[Any] = None,
    project_store: Optional[Any] = None,
    search: Optional[Any] = None,
    artifact_service: Optional[Any] = None,
    ui_service: Optional[Any] = None,
    task_manager: Optional[Any] = None,
    runtime_id: Optional[str] = None,
) -> RunResult:
    """Run the graph and write the terminal status exactly once."""
    from .run_lineage import CheckpointWriteFailed, RunLineage  # noqa: PLC0415

    agent = resolve_agent(request)
    if getattr(request, "liveSurface", True):
        try:
            from .self_improvement.live_surface import apply_active_surface  # noqa: PLC0415

            agent, _surface_activation = apply_active_surface(store, agent)
        except Exception:  # noqa: BLE001 - a bad activation must never break a run
            pass
    emitter = RunEmitter(request.runId, store, bus)
    # C4: an explicit `input.context.c4Plan` binds this run to a durable goal
    # step. No context => None and the run is unchanged.
    try:
        plan_runtime = PlanRuntime.from_request(store, request)
    except Exception:  # noqa: BLE001 - plan wiring must never break a run
        plan_runtime = None
    lineage = RunLineage(store)
    from .run_lease import LeaseLost, fenced_progress, fenced_update_run  # noqa: PLC0415

    def _owner_write(**fields: Any) -> bool:
        """Fence a run mutation to the live lease; False means we lost the run."""
        return fenced_update_run(store, request.runId, runtime_id=runtime_id, **fields)

    def _lease_lost_result() -> RunResult:
        # Never write a terminal row here: the new owner owns it. The bus event
        # is live-only so a stale worker cannot pollute the durable log.
        try:
            if bus is not None:
                bus.publish({
                    "id": f"evt_{request.runId}_lease_lost",
                    "runId": request.runId, "type": "run.lease_lost",
                    "seq": -1, "at": now_iso(), "data": {"runtimeId": runtime_id},
                    "transient": True,
                })
        except Exception:  # noqa: BLE001 - telemetry only
            pass
        log.warning("run %s lease was lost; stopping without a terminal write", request.runId)
        return RunResult(
            runId=request.runId, status="failed",
            error=HarnessError(
                code="RUN_LEASE_LOST",
                message="another live runtime owns this run; this worker stopped",
            ),
            toolCalls=0, toolErrors=0, modelSteps=0,
        )
    # Capability gaps: one durable record per offerable dead end. Recording is
    # fail-open; this never changes a tool decision.
    from .gaps.runtime import GapRuntime  # noqa: PLC0415

    gaps = GapRuntime(store)
    gap_need = str(getattr(getattr(request, "input", None), "query", "") or "")
    if code_store is None:
        from .code.store import CodeWorkspaceStore  # noqa: PLC0415

        code_store = CodeWorkspaceStore(store)
    # Phase 4: the per-run verification ledger. Rehydrated from the persisted blob
    # when resuming (EMPTY when absent, so a resumed run starts unverified); a
    # fresh run starts empty.
    from .code.verify import VerificationLedger  # noqa: PLC0415

    _prior_run = store.get_run(request.runId)
    code_ledger = VerificationLedger.from_blob(
        request.runId, (_prior_run or {}).get("codeVerification"),
    )
    started = now_iso()
    # Resume, when requested, is validated BEFORE a run row is created so a
    # missing checkpoint / a terminal run never silently starts a fresh run.
    resume_from: Optional[dict[str, Any]] = None
    if request.resumeFrom:
        existing = store.get_run(request.runId)
        if existing is None:
            return _resume_error(request.runId, "RESUME_CONFLICT", "no run exists to resume", store)
        checkpoint = lineage.latest_checkpoint(request.runId)
        if checkpoint is None:
            return _resume_error(
                request.runId, "CHECKPOINT_NOT_FOUND",
                "no checkpoint exists for this run — it cannot be resumed", store,
            )
        # Only a run that already reached a NORMAL end is terminal. A `failed`
        # run (timeout, provider outage, process death) still has unfinished
        # work, so it is resumable from its last non-terminal checkpoint — this
        # is what makes Retry continue instead of starting over.
        if existing.get("status") in ("completed", "cancelled"):
            return _resume_error(
                request.runId, "RESUME_CONFLICT",
                f"run is already in terminal status '{existing.get('status')}'", store,
            )
        if checkpoint.get("kind") == "terminal":
            # The graph already wrote its exhausted/terminal boundary; there is
            # no earlier boundary to continue from.
            return _resume_error(
                request.runId, "CHECKPOINT_NOT_FOUND",
                "the latest checkpoint is terminal - nothing left to resume", store,
            )
        resume_from = checkpoint
        # Re-enter the SAME run: keep its identity, re-mark it running, and clear
        # the previous attempt's terminal error so the row reflects live work.
        lineage.set_run_parent(request.runId, request.parentRunId)
        if not _owner_write(
            status="running", startedAt=started, error=None, finishedAt=None,
        ):
            return _lease_lost_result()
        await emitter.emit("run.resumed", {"checkpointId": checkpoint["id"], "step": (checkpoint.get("payload") or {}).get("step")})
    else:
        # A caller (the code-task route) may have pre-created the run row so it
        # could write a FK-bound `task.created` checkpoint first. Reuse that row
        # instead of re-inserting it; any other pre-existing row is a genuine
        # duplicate and still fails loudly.
        prior_status = (_prior_run or {}).get("status")
        if _prior_run is None:
            store.create_run(
                request.runId,
                agent.id,
                request.input.model_dump(),
                request.userId,
                request.modelSettings.provider,
                request.modelSettings.model,
                session_id=request.sessionId,
                profile_id=request.profileId,
                project_id=request.projectId,
            )
        elif prior_status != "created":
            raise RuntimeError(
                f"run {request.runId!r} already exists in status {prior_status!r}"
            )
        # Record lineage AFTER the row exists (columns owned by the lineage module).
        lineage.set_run_parent(request.runId, request.parentRunId, request.workflowId)
        await emitter.emit("run.created", {"agentId": agent.id})
        if not _owner_write(status="running", startedAt=started):
            return _lease_lost_result()
        # The effective budget: a session profile may raise `request.maxSteps`.
        await emitter.emit("run.started", {"maxSteps": int(getattr(request, "maxSteps", 0) or 0)
                                           or chat_step_budget(agent, request),
                                           "provider": request.modelSettings.provider})

    # W0: record the effective feature-flag snapshot once per run/resume. This is
    # additive and fail-open; it never changes the prompt, tools or model.
    from .run_flags import record_run_flags, with_flags  # noqa: PLC0415

    flags_snapshot = await record_run_flags(
        emitter, store, request.runId,
        existing_context=(_prior_run or {}).get("context"),
        runtime_id=runtime_id,
    )

    # Phase 4: an unambiguous supported financial comparison runs the evidence gate
    # BEFORE the agent graph (no duplicate tool loop); uncertain inputs fall through.
    research = None
    if resume_from is None:
        # A resumed run was already past classification (it has checkpoints), so
        # it does not re-enter the research gate.
        try:
            research = await _maybe_research(request, data_bridge, store, emitter, retrieval_cache, cancel_event)
        except asyncio.CancelledError:
            fenced_update_run(store, request.runId, runtime_id=runtime_id, status="cancelled", finishedAt=now_iso())
            await emitter.emit("run.cancelled", {"reason": "cancelled"})
            return _result(request.runId, "cancelled", store, None)
    if research is not None:
        return await _finish_research(
            request, store, emitter, research, runtime_id=runtime_id,
        )

    # Retrieve relevant memory BEFORE the model sees the turn, and inject it as
    # clearly-labelled non-authoritative context. Memory is scoped to the user
    # and bounded by a character budget; a retrieval failure must never block the
    # run (it is recorded in the retrieval audit instead).
    memory_block = await _memory_context(request, memory_service, emitter, agent=agent)

    # The always-available compact profile is materialized ONCE here, at run
    # start: a memory saved during this run is visible to LATER runs, never to
    # this one (the snapshot stays stable for the whole run). It is a separate,
    # clearly-labelled, non-authoritative block; its text is data, never
    # instructions. A profile failure must never block the run.
    profile_block, profile_snapshot_id = await _profile_context(request, agent, memory_service, emitter)

    # A bounded, relevant-only skill manifest (never all skills); a hint only.
    skill_manifest, skill_manifest_version = await _skill_manifest_context(
        request, skill_registry, memory_service, emitter, agent=agent,
    )
    lesson_manifest, _lesson_ids = await _lesson_manifest_context(request, memory_service, store, emitter)

    # Deterministic explicit-intent handling. A clear "remember this" / "forget
    # this" / "correct this" must not depend on the model voluntarily calling the
    # `memory` tool. Detection runs before the model; a refused or
    # confirmation-pending intent is recorded but NEVER written. Detection is
    # purely a function of the user's query, so one call suffices.
    #
    # A skill child-run's "query" is the rendered skill input, not the user's
    # words — it never carries chat intent, so skip detection there.
    memory_outcome = (
        None if agent.id.startswith("skill:")
        else await _apply_memory_intent(request, memory_service, emitter)
    )

    # Prompt-cache plan: stable segments + an identity-derived cache key.
    from .prompt_segments import build_prompt_plan, runtime_context_block  # noqa: PLC0415

    # Surface honesty (Milestone 4): resolve the tool surface AND the system
    # prompt FROM POLICY per run — never at import or __init__ time. A disabled
    # store drops its tool from the bound schema and its guidance from the
    # prompt. The resolved prompt is byte-stable for the whole run; a policy
    # change takes effect on the NEXT run.
    try:
        surface_settings = memory_service.policy.get_all() if memory_service is not None else {}
    except Exception:  # noqa: BLE001 - a policy read failure falls back to the full surface
        surface_settings = {}
    # Round 3 / W2 (ADR-H280): the resolution is taken WITH its reasoning, so the
    # run can publish what the surface added, what the autonomy profile removed
    # and what a disabled store removed. A surface EXTENDS the agent's declared
    # tools; it never silently replaces them.
    _context = getattr(request.input, "context", None) or {}
    _task_run = bool(_context.get("taskRun")) if isinstance(_context, dict) else False
    surface_resolution, system_prompt = kernel_runtime.resolve_surface(
        agent, registry, surface_settings, request.autonomy,
        apply_code_surface=not _task_run,
    )
    allowed = (
        set(surface_resolution.allowed)
        if surface_resolution.allowed is not None
        else None
    )
    # Trade surface first; a quantitative turn then gets the data/compute surface.
    disclosure_plan = None
    # The bridge surface is the FINAL resolved surface BEFORE disclosure: a tool
    # hidden by disclosure stays reachable, a tool removed by the surface does not.
    bridge_surface = allowed
    if allowed:
        from .quant_intent import apply_intent_surfaces  # noqa: PLC0415
        allowed, surface_resolution = apply_intent_surfaces(
            request, allowed, surface_resolution
        )
        # The tool bridge (`tool_search`/`tool_invoke`) must use the FINAL
        # resolved surface, not the pre-intent agent allowlist. Hidden-by-
        # disclosure tools stay in this set (that is what makes them reachable);
        # tools removed by the surface are never discoverable or invokable.
        bridge_surface = allowed
        # W3.3: progressive disclosure. Effectful/interactive tools stay bound;
        # the read-only long tail is listed in a bounded catalogue and reachable
        # through the existing tool_search/tool_invoke pair. `auto` uses the
        # declared window, or an absolute schema ceiling when the window is
        # unknown, so an oversized surface can never be bound by accident.
        from .models import resolve_context_window  # noqa: PLC0415
        from .tool_disclosure import resolve_disclosure  # noqa: PLC0415

        _declared_window = getattr(request.modelSettings, "context_window", None)
        _window = int(_declared_window) if _declared_window else resolve_context_window(
            request.modelSettings.provider, request.modelSettings.model,
        )
        disclosure_plan = resolve_disclosure(
            allowed, registry, window=_window, query=request.input.query,
        )
        if disclosure_plan.enabled and disclosure_plan.allowed is not None:
            allowed = disclosure_plan.allowed
    if disclosure_plan is not None and disclosure_plan.enabled:
        system_prompt = f"{system_prompt}\n\n{disclosure_plan.catalogue_block}"
    # Settings ? Persona: a bounded user overlay appended to the authoritative
    # prompt for the ordinary chat agent. It is style/emphasis only — the tool
    # allowlist above was already resolved, so the block cannot widen it. Read
    # ONCE at run start and recorded on the run (`surface.persona`), so a later
    # edit never rewrites what an earlier run saw.
    persona_meta: Optional[dict[str, Any]] = None
    if agent.id == DEFAULT_AGENT_ID:
        from .personas import active_persona_prompt  # noqa: PLC0415

        persona_block, persona_meta = active_persona_prompt()
        if persona_block:
            system_prompt = f"{system_prompt}\n\n{persona_block}"

    # Round 3 / W2 (ADR-H280): publish the surface resolution on the run, so a
    # missing tool is always explainable - the run names what the surface added,
    # what the autonomy profile removed and what a disabled store removed. The
    # write is additive and cannot widen anything. The active persona rides
    # alongside it as provenance (id@version), never as a permission.
    surface_payload = surface_resolution.to_dict()
    if persona_meta is not None:
        surface_payload["persona"] = persona_meta
    if disclosure_plan is not None:
        surface_payload["disclosure"] = disclosure_plan.to_dict()
        if disclosure_plan.enabled:
            await emitter.emit("tool.disclosure", {
                **disclosure_plan.to_dict(),
                "runId": request.runId,
            })
    _owner_write(
        surface=surface_payload,
        autonomy=surface_resolution.autonomy,
        workspaceId=request.workspaceId,
    )

    # Response-contract schema digest: generated LIVE from the TS contract over
    # the read-only bridge, so a surface that may emit structured blocks is given
    # the EXACT field names and enum values up front — the model no longer has to
    # remember to call `response_block_schema` and cannot guess a drifted field
    # (e.g. `datasets` for `series`, or a semantic `tone`). It rides in the
    # byte-stable system head for the whole run. Best-effort: a bridge miss or a
    # surface without the response tools degrades silently to the on-demand tool
    # and never blocks the run.
    if allowed and "response_block_schema" in allowed:
        try:
            from .response_tools import response_schema_prompt_block  # noqa: PLC0415

            _schema_digest = await response_schema_prompt_block(data_bridge)
            if _schema_digest:
                system_prompt = f"{system_prompt}\n\n{_schema_digest}"
        except Exception:  # noqa: BLE001 - a digest must never break the run
            pass

    # JIT design modules: the schema above gives exact FIELDS, not taste. A compact
    # index tells the model to load ONE family's guide + worked examples on demand
    # through `response_style_guide`, keeping the byte-stable head small while the
    # design rules stay versionable in `response_style.py`.
    if allowed and "response_style_guide" in allowed:
        try:
            from .response_style import response_style_index_prompt_block  # noqa: PLC0415

            _style_index = response_style_index_prompt_block()
            if _style_index:
                system_prompt = f"{system_prompt}\n\n{_style_index}"
        except Exception:  # noqa: BLE001 - an index must never break the run
            pass

    # Intelligent Response Orchestrator: a deterministic, model-free plan of HOW
    # to present this turn's answer (text | explanation | visualization |
    # simulation | workflow | composite). It appends presentation guidance to the
    # byte-stable head and records the chosen mode via `response.plan` for
    # evaluation. It never mandates structure and never fabricates data; a
    # classification failure degrades to text and must never block the run.
    if allowed and "response_block_schema" in allowed:
        try:
            from .response_mode_llm import orchestrate_response_mode, response_mode_prompt_block  # noqa: PLC0415

            _response_plan = await orchestrate_response_mode(
                request.input.query, request.input.history, request.modelSettings
            )
            # Quant mode is figure-first: a standard/deep quantitative turn that
            # produced a series must not be presented as plain text just because
            # the generic classifier saw a question it did not recognise. This
            # only upgrades the presentation mode; it never fabricates data, and
            # the quant protocol still says when a figure is unavailable.
            try:
                from .quant_intent import classify_quant_intent  # noqa: PLC0415
                from .response_mode import plan_for_mode  # noqa: PLC0415

                _quant_context = getattr(request.input, "context", None)
                _quant_mode = (
                    _quant_context.get("quantMode")
                    if isinstance(_quant_context, dict) else None
                )
                _quant_intent = classify_quant_intent(
                    request.input.query, getattr(request.input, "history", None),
                    mode=_quant_mode,
                )
                if (
                    _quant_intent.tier in ("standard", "deep")
                    and _response_plan.mode not in ("visualization", "composite")
                ):
                    _response_plan = plan_for_mode(
                        "visualization", "likely",
                        "quant protocol: a computed result is presented figure-first",
                        ("quant:figure-first",),
                    )
            except Exception:  # noqa: BLE001 - an upgrade must never break a run
                pass
            _mode_block = response_mode_prompt_block(_response_plan)
            if _mode_block:
                system_prompt = f"{system_prompt}\n\n{_mode_block}"
            await emitter.emit("response.plan", _response_plan.to_dict())
        except Exception:  # noqa: BLE001 - presentation guidance must never break the run
            pass

    # A test/operator seam: allows a deterministic tighter budget to be injected
    # (used to exercise the overflow-recovery path without a live provider). It
    # only narrows the derived allowance — it can never widen it.
    budget_overrides: dict[str, Any] = {}
    try:
        raw_override = (request.input.context or {}).get("_context_budget_override")
        if isinstance(raw_override, dict):
            budget_overrides = dict(raw_override)
    except Exception:  # noqa: BLE001 - an override is never required
        budget_overrides = {}

    # The runtime date/time block: the run's real clock, rendered as its own
    # per-run block (never in the byte-stable system-prompt head). It gives the
    # model the as-of period it needs without a shell call and is excluded from
    # the cache key so a new run date never invalidates the reusable prefix.
    runtime_context = runtime_context_block(request.runId)
    # The page the user is working on, per-run and cache-key-excluded.
    ui_focus = _ui_focus_block(request.input.context)
    if ui_focus:
        runtime_context = f"{runtime_context}\n\n{ui_focus}"
    app_control = _app_control_block(request.input.context)
    if app_control:
        runtime_context = f"{runtime_context}\n\n{app_control}"
    # The concrete fallback ladder for THIS surface, next to the user turn.
    from .final_answer import with_fallback_context  # noqa: PLC0415

    runtime_context = with_fallback_context(
        runtime_context, allowed, request.input.query,
    )
    # A "place a trade" turn is an execution request, not a research project.
    # Only added when the broker surface (and therefore `order_submit`) is bound.
    if allowed and ("order_submit" in allowed or "position_close" in allowed):
        trading_intent = trading_intent_block(request.input.query)
        if trading_intent:
            runtime_context = f"{runtime_context}\n\n{trading_intent}"
    from .quant_intent import apply_quant_context  # noqa: PLC0415 - quant protocol
    runtime_context = await apply_quant_context(request, allowed, runtime_context, emitter)
    # The composer's "Choices" preference: `on` tells the model to ask one
    # question with selectable options before it starts an ambiguous turn. It only
    # adds guidance (the `clarify` tool stays read-only, and this grants nothing).
    from .choices_mode import apply_choices_context  # noqa: PLC0415 - choices mode
    runtime_context = apply_choices_context(request, allowed, runtime_context)
    # The composer's "Plan" preference: `on` tells the model to lay out a plan and
    # wait for approval before starting the work. Guidance only; it grants nothing.
    from .plan_mode import apply_plan_context  # noqa: PLC0415 - plan mode
    runtime_context = apply_plan_context(request, allowed, runtime_context)
    if plan_runtime is not None:
        plan_block = plan_runtime.prompt_block()
        if plan_block:
            runtime_context = f"{runtime_context}\n\n{plan_block}"
    # The project this run is scoped to (its members and instruments). Per-run
    # and cache-key-excluded, exactly like the date and the UI focus.
    project_brief = _project_brief_block(getattr(request, "projectId", None),
                                          project_store)
    if project_brief:
        runtime_context = f"{runtime_context}\n\n{project_brief}"
    # Round 3 / W4 (ADR-H287): the REPO MAP and the SEEDED relevant slice. The
    # map is built eagerly and deterministically, so the model no longer has to
    # guess a path to find out what exists. Its digest is the cache key's
    # witness that the project shape has not changed.
    repo_map_block, repo_map_digest = await _repo_map_context(
        request, workspace=_run_workspace(request),
        code_intel_runtime=code_intel_runtime, emitter=emitter,
    )
    if repo_map_block:
        runtime_context = f"{runtime_context}\n\n{repo_map_block}"

    prompt_plan = build_prompt_plan(
        system_prompt=system_prompt,
        agent_id=agent.id,
        tool_names=list(allowed) if allowed else list(registry.names()),
        profile_block=profile_block,
        profile_snapshot_id=profile_snapshot_id,
        skill_manifest=skill_manifest,
        skill_manifest_version=skill_manifest_version, lesson_manifest=lesson_manifest,
        runtime_context=runtime_context,
        user_message=request.input.query,
        repo_map=repo_map_block, repo_map_version=repo_map_digest,
        provider=request.modelSettings.provider,
    )
    await emitter.emit("prompt.plan", {**prompt_plan.to_dict(), "surface": surface_summary(agent.id, allowed)})
    if prompt_plan.cache_plan is not None:
        # W2.6: the provider's cache decision is observable even when it is
        # "none" — the operator can see that no marker was available rather
        # than inferring it from a flat cache-hit rate.
        _cache = prompt_plan.cache_plan
        _stable_chars = sum(
            len(s.content) for s in prompt_plan.segments
            if s.id in _cache.stable_segment_ids
        )
        await emitter.emit("prompt.cache", {
            "cacheKey": prompt_plan.cache_key,
            "provider": _cache.provider,
            "stableChars": _stable_chars,
            "markerCount": _cache.marker_count,
            "markerKind": _cache.marker_kind,
            "stableSegmentIds": list(_cache.stable_segment_ids),
        })

    # Measure the request and derive the history budget from the RESOLVED model's
    # declared context window. When the window is unknown the budget is inert and
    # the bound falls back to the module constants (byte-identical to before).
    #
    # The dropped-context block itself costs tokens, so the budget must be
    # measured with it present. We derive it once with the conservative bound to
    # extract the dropped context, then re-derive so the final bound accounts for
    # the block's own size. Two passes, no loop.
    from .context.usage_anchor import prepare_anchor  # noqa: PLC0415

    anchor_store, anchor_id, usage_anchor, history_chars = prepare_anchor(store, request)
    budget = _history_budget(
        request, allowed, registry, system_prompt, profile_block, skill_manifest,
        memory_block, "", runtime_context=runtime_context, overrides=budget_overrides,
        anchor=usage_anchor, history_chars=history_chars,
    )
    history_messages, dropped_block, dropped_turns = await _dropped_context_block(
        request, memory_service, emitter, budget,
    )
    budget = _history_budget(
        request, allowed, registry, system_prompt, profile_block, skill_manifest,
        memory_block, dropped_block, runtime_context=runtime_context, overrides=budget_overrides,
        anchor=usage_anchor, history_chars=history_chars,
    )
    if budget.effective:
        # Re-derive the trimmed set under the measured bound (re-extraction is
        # cheap and deterministic; the extraction event is emitted once).
        history_messages, dropped_block, dropped_turns = await _dropped_context_block(
            request, memory_service, emitter, budget, emit=False,
        )

    # W2 ladder: at a hard/emergency gate the assembled prompt is pruned and,
    # if still under pressure, summarised before the first call. The decision and
    # cooldown live in `context/compression_ladder.py`; a short run is untouched.
    from .context.compression_ladder import compress as _compress_run  # noqa: PLC0415

    async def _compress_if_needed(run_messages: list[BaseMessage]) -> list[BaseMessage]:
        gate = str(getattr(budget, "gate", "unknown") or "unknown")
        if not getattr(budget, "effective", False) or gate not in ("hard", "emergency"):
            return run_messages
        ladder = await _compress_run(
            run_messages, budget=budget, request=request, emitter=emitter,
            memory_service=memory_service,
            session_id=str(getattr(request, "sessionId", "") or request.runId),
        )
        return ladder.messages if ladder.ok else run_messages

    # Ordinary-run source collection: retrieval tools hand their results to this
    # collector so the answer can show a Sources panel built ONLY from what was
    # actually retrieved (the gated research path has its own citations). It is
    # additive, best-effort provenance; it never affects the tool result or the
    # run's authorization.
    from .source_collector import SourceCollector  # noqa: PLC0415

    source_collector = SourceCollector()

    # Ordinary-run advisory collection (the Fiduciary Gate): every `portfolio_*`
    # tool call is recorded so an actionable recommendation in the final answer
    # can be checked against a computation the run ACTUALLY performed.
    from .advisory import integration as advisory_seam  # noqa: PLC0415

    advisory_collector = advisory_seam.new_collector()

    # Ordinary-run vision collection: a `vision_*` tool hands its structured
    # result here so the run carries a bounded vision blob for the right sidebar
    # (asset ids, regions, OCR, confidence, provenance). Additive and best-effort.
    from .vision.collector import VisionCollector  # noqa: PLC0415

    vision_collector = VisionCollector()

    # Ordinary-run document collection: a `document_*` tool hands its structured
    # result here so the run carries a bounded document blob for the right sidebar
    # (type, pages, status, capabilities, entities, evidence counts). Best-effort.
    from .document_intelligence.collector import DocumentCollector  # noqa: PLC0415

    document_collector = DocumentCollector()

    # Ordinary-run internet-image collection: an `image_search` /
    # `render_image_gallery` tool hands its validated results here so the run
    # carries a bounded image blob the chat renders INLINE. Deterministic, and it
    # never fabricates: only results with a safe direct image URL are collected.
    from .research.image_contracts import ImageCollector  # noqa: PLC0415

    image_collector = ImageCollector()

    # Ordinary-run native-video collection: a `video_*` tool hands the job it
    # just QUEUED here so the run carries a bounded video blob the chat renders
    # as a live job card (progress while running; player + download when a final
    # succeeds). Deterministic: only real submitted job handles are collected.
    from .video.collector import VideoJobCollector  # noqa: PLC0415

    video_collector = VideoJobCollector()

    # Bounded recovery (Milestone 4): if the run failed with a classified
    # CONTEXT_OVERFLOW, retry ONCE with a materially tighter history budget. The
    # terminal status is persisted once, below, after recovery resolves.
    # Resume (Phase 1): rebuild the run state from the recorded checkpoint so the
    # graph continues at the boundary it stopped at instead of starting fresh.
    # Already-executed tool calls are reused (not re-run) via the idempotency
    # ledger wired into `build_graph`.
    resume_state: Optional[dict[str, Any]] = None
    if resume_from is not None:
        payload = resume_from.get("payload") or {}
        # Prefer the durable cursor (full conversation) and fall back to the
        # bounded legacy tail for checkpoints written before this change.
        restored = None
        if payload.get("messageSeq") is not None:
            try:
                restored = lineage.load_messages(request.runId) or None
            except Exception:  # noqa: BLE001 - a bad cursor falls back to the tail
                restored = None
        resume_state = {
            "messages": restored if restored is not None else _restore_messages(payload.get("messages") or []),
            "step": int(payload.get("step", 0)),
            "tool_calls": int(payload.get("toolCalls", 0)),
            "tool_errors": int(payload.get("toolErrors", 0)),
            "prompt_tokens": int(payload.get("promptTokens", 0)),
            "completion_tokens": int(payload.get("completionTokens", 0)),
            "final": None,
        }

    async def _invoke_with_history(
        history_msgs: list[BaseMessage], dropped: Optional[str] = None,
        resume_override: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if resume_override is not None and resume_override.get("messages"):
            initial_state: dict[str, Any] = dict(resume_override)
        elif resume_state is not None and resume_state.get("messages"):
            initial_state: dict[str, Any] = dict(resume_state)
        else:
            run_messages = _assemble_run_messages(
                request, system_prompt=system_prompt, history_messages=history_msgs,
                profile_block=profile_block, skill_manifest=skill_manifest,
                lesson_manifest=lesson_manifest, memory_block=memory_block,
                dropped_block=dropped if dropped is not None else dropped_block,
                runtime_context=runtime_context,
            )
            run_messages = await _compress_if_needed(run_messages)
            initial_state = {
                "messages": run_messages,
                "step": 0,
                "tool_calls": 0,
                "tool_errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "final": None,
            }
        scope = RunScope(
            registry=registry, data_bridge=data_bridge, store=store,
            runtime_id=runtime_id,
            bus=bus, cancel_event=cancel_event,
            retrieval_cache=retrieval_cache, memory_service=memory_service,
            skill_registry=skill_registry, memory_router=memory_router,
            mcp_manager=mcp_manager, approvals=approvals,
            code_store=code_store, code_intel_runtime=code_intel_runtime,
            evidence_store=evidence_store, learning_store=learning_store,
            market=market, portfolio=portfolio, ledger=ledger,
            advisory=advisory, decision=decision, quant=quant,
            project_store=project_store, search=search,
            artifact_service=artifact_service, ui_service=ui_service,
            task_manager=task_manager,
        )
        spec = RunSpec(
            request=request, emitter=emitter, agent=agent,
            effect_authorizer=_effect_authorizer(
                approvals, request.runId, emitter, request.workspaceId,
            ),
            allowed=allowed, dispatch_allowed=bridge_surface,
            source_sink=source_collector,
            lineage=lineage, resume_from=resume_from, code_ledger=code_ledger,
            vision_sink=vision_collector.add,
            document_sink=document_collector.add,
            image_sink=image_collector.add,
            video_sink=video_collector.add,
            advisory_sink=advisory_collector.add,
            plan=plan_runtime,
        )
        app = build_graph(spec, scope)
        return await app.ainvoke(initial_state)

    from .context.retry import retry_with_tighter_budget as _retry_with_tighter_budget  # noqa: PLC0415
    from .run_recovery import is_context_overflow  # noqa: PLC0415

    retry_budget: Optional[Any] = None
    try:
        final_state = await _invoke_with_history(history_messages)
        if is_context_overflow(final_state.get("error")):
            final_state, retry_budget = await _retry_with_tighter_budget(
                request, emitter, final_state, base_budget=budget,
                overrides=budget_overrides,
                system_prompt=system_prompt, profile_block=profile_block,
                skill_manifest=skill_manifest, memory_block=memory_block,
                runtime_context=runtime_context,
                memory_service=memory_service, allowed=allowed,
                registry=registry, invoke=_invoke_with_history,
                anchor=usage_anchor, history_chars=history_chars,
                recover_context=(store, request.runId, emitter, _invoke_with_history),
                budget_fn=_history_budget,
            )
    except asyncio.CancelledError:
        fenced_update_run(store, request.runId, runtime_id=runtime_id, status="cancelled", finishedAt=now_iso())
        await emitter.emit("run.cancelled", {"reason": "cancelled"})
        return _result(request.runId, "cancelled", store, None)
    except LeaseLost:
        return _lease_lost_result()
    except CheckpointWriteFailed as exc:
        error = HarnessError(code="CHECKPOINT_WRITE_FAILED", message=str(exc))
        _owner_write(status="failed", error=error.model_dump(), finishedAt=now_iso())
        await emitter.emit("run.failed", {"error": error.model_dump()})
        return RunResult(
            runId=request.runId, status="failed", error=error,
            toolCalls=0, toolErrors=0, modelSteps=0,
        )
    except Exception as exc:  # noqa: BLE001
        # A node may have already recorded a precise terminal outcome (for
        # example RUN_TIMEOUT) and then a late routing/bookkeeping error escaped
        # the graph. Never overwrite that honest status with INTERNAL_ERROR.
        prior = store.get_run(request.runId) or {}
        prior_status = prior.get("status")
        prior_error = prior.get("error")
        if (
            prior_status in ("failed", "completed", "cancelled")
            and isinstance(prior_error, dict)
            and prior_error.get("code")
        ):
            log.warning(
                "run %s ended as %s but a late error escaped: %s",
                request.runId, prior_status, exc,
            )
            return _result(request.runId, prior_status, store, prior_error)
        error = HarnessError(code="INTERNAL_ERROR", message=str(exc))
        fenced_update_run(store, request.runId, runtime_id=runtime_id, status="failed", error=error.model_dump(), finishedAt=now_iso())
        await emitter.emit("run.failed", {"error": error.model_dump()})
        return RunResult(runId=request.runId, status="failed", error=error, toolCalls=0, toolErrors=0, modelSteps=0)

    if final_state.get("error"):
        if plan_runtime is not None:
            try:
                plan_runtime.settle(request.runId, ok=False)
            except Exception:  # noqa: BLE001
                pass
        err = dict(final_state["error"])
        # An unrecovered CONTEXT_OVERFLOW is a specific, user-facing failure.
        # Rewrite the message to state whether the tighter retry was actually
        # attempted (honest — no claim of a retry that did not happen), and emit
        # the terminal event here (the classifier deliberately does not) so the
        # stream and the persisted run agree.
        context_blob = _context_budget_blob(
            retry_budget or budget, request, kind="overflow",
            retried=retry_budget is not None,
        )
        context_blob = with_flags(context_blob, flags_snapshot)
        if is_context_overflow(err):
            from .models import context_overflow_error  # noqa: PLC0415

            window = getattr(retry_budget or budget, "window", None)
            _code, message = context_overflow_error(
                request.modelSettings.provider, request.modelSettings.model,
                window, retried=retry_budget is not None,
            )
            err["message"] = message
            fenced_update_run(
                store, request.runId, runtime_id=runtime_id, status="failed",
                error=err, finishedAt=now_iso(), context=context_blob,
            )
            await emitter.emit("run.failed", {"error": err})
        else:
            fenced_update_run(store, request.runId, runtime_id=runtime_id, status="failed", error=err, finishedAt=now_iso())
        await _post_run_analysis(request.runId, store, memory_service, skill_registry)
        result = _result(request.runId, "failed", store, err)
        result.context = context_blob
        return result

    # The user-visible trimming signal (Defect C): emitted on the SUCCESS path
    # whenever history was actually trimmed, with counts and estimates only —
    # never memory/dropped content. Nothing is emitted when nothing was dropped.
    # Re-derived with the FINAL budget so a tighter retry is reflected.
    final_budget = retry_budget or budget
    kept_final, dropped_final = _bounded_history(request, final_budget)
    if dropped_final:
        await emitter.emit("memory.history_bounded", _history_bounded_data(
            final_budget, kept_turns=len(kept_final), dropped_turns=dropped_final,
            retried=retry_budget is not None,
        ))

    output = final_state.get("final") or ""
    # Phase 4: make the answer consistent with what the executor actually
    # recorded. A pass-claim with no zero-exit execution is marked, never
    # asserted — the same discipline the finance renderer applies to claims.
    from .code.verify import enforce_run_answer  # noqa: PLC0415

    # Round 3 / W1 (ADR-H281): the REAL repair outcome, not a hard-coded None.
    # `REPAIR_NOT_RUN` and `REPAIR_PASSED` are different facts, and the answer
    # seam must be able to tell them apart: a run whose repair loop exhausted
    # its budget must not read as a run whose checks passed.
    from .code.repair_graph import RepairLoop as _RepairLoop  # noqa: PLC0415

    _repair_outcome = _RepairLoop.from_dict(final_state.get("repair")).to_dict()
    output, code_verification_blob = await enforce_run_answer(
        output, code_ledger, repair_outcome=_repair_outcome, emitter=emitter,
    )
    # Phase 14: record the run's code profile ALONGSIDE the verification ledger, so
    # a reader (the chat capability panel) can name what Eclipse set out to do. The
    # profile rides in the existing additive blob — no new persisted column, no
    # migration. Only a coding turn carries one; an ordinary run's blob is
    # unchanged.
    if request.autonomy or request.workspaceId:
        code_verification_blob = dict(code_verification_blob or {})
        if request.autonomy:
            code_verification_blob["autonomy"] = request.autonomy
        if request.workspaceId:
            code_verification_blob["workspaceId"] = request.workspaceId
    _owner_write(codeVerification=code_verification_blob)
    # Universal financial-fact enforcement (finance_facts.enforce): if the answer
    # asserts a metric the run's own analysis EXCLUDED (ambiguous definition, no
    # period, superseded), or states one side of a cross-source conflict, append the
    # disclosure rather than silently presenting an unverified figure as clean.
    # Deterministic, no model call; a no-op when the run retrieved no finance facts.
    finance_disclosures: list[str] = []
    finance_presentable = True
    finance_excluded: list[str] = []
    try:
        from .finance_facts.enforce import disclosure_note, enforce_finance_answer  # noqa: PLC0415

        finance_analysis = source_collector.finance_analysis()
        if finance_analysis:
            verdict = enforce_finance_answer(output, finance_analysis)
            finance_disclosures = verdict.required_disclosures
            finance_presentable = bool(finance_analysis.get("presentable", True))
            finance_excluded = list(finance_analysis.get("excluded") or [])
            if finance_disclosures:
                note = disclosure_note(finance_disclosures)
                output = f"{output}\n\n{note}" if output.strip() else note
            await emitter.emit("finance.analysis", {
                "presentable": finance_presentable,
                "excluded": finance_excluded,
                "disclosures": finance_disclosures,
            })
    except Exception:  # noqa: BLE001 - enforcement is additive, never fatal
        pass
    # Evidence release gate (ordinary runs): a cited link with no runtime
    # provenance is disclosed as unverified (`research/release_gate.py`).
    from .research import release_gate  # noqa: PLC0415

    output, evidence_release = await release_gate.apply(
        output, source_collector, emitter=emitter, user_text=request.input.query,
    )
    # Fiduciary Gate (advisory): an actionable recommendation must be backed by a
    # portfolio computation the run performed, or grounded in a source/symbol it
    # consulted; otherwise it is disclosed as unverified advice. Never places a trade.
    output, advisory_verdict = await advisory_seam.verify(output, advisory_collector, emitter)
    # C4: one cross-domain evidence ledger. Checkable sentences inherit the
    # run's handles, the verifier registry refines them, and a specifically
    # rejected claim is disclosed (never silently shipped as fact).
    from .goals.answer import enforce_evidence_answer  # noqa: PLC0415

    output, evidence_blob = await enforce_evidence_answer(
        output, store, request.runId, emitter=emitter,
        checks=checks_from_ledger(code_ledger),
    )
    # Persist each verdict as a durable decision in the Advisory Ledger (the
    # "what did I recommend, on what basis" record). Best-effort and idempotent.
    if advisory is not None and advisory_verdict and advisory_verdict.get("claims"):
        try:
            advisory.record_verdict(
                run_id=request.runId, verdict=advisory_verdict,
                portfolio_id=advisory_collector.portfolio_id(),
                conversation_id=getattr(request, "conversationId", None),
                owner_id=request.userId,
                sources=sorted(source_collector.known_urls())[:20],
                entry_prices=advisory_collector.prices(),
            )
        except Exception:  # noqa: BLE001 - recording must never fail a run
            pass
    context_blob = _context_budget_blob(
        final_budget, request, kind="completed", retried=retry_budget is not None,
    )
    context_blob = with_flags(context_blob, flags_snapshot)
    # Provenance collected from this run's retrieval tool calls (None when the
    # run consulted no sources, so an empty panel is never shown). An ordinary
    # run's collected blob carries status `collected`, distinct from a gated
    # research run's `accepted`/`partial`/`rejected`.
    research_blob = release_gate.attach(source_collector.to_research_blob(), evidence_release)
    # Attach the advisory verdict to the run blob so the panel can show which
    # recommendations were computed/sourced vs. disclosed as unverified.
    research_blob = advisory_seam.attach(research_blob, advisory_verdict)
    # Vision results collected from this run's `vision_*` tool calls (None when no
    # vision tool ran), shaped for the right sidebar.
    vision_blob = vision_collector.to_blob()
    # Document results collected from this run's `document_*` tool calls (None when
    # no document tool ran), shaped for the right sidebar.
    documents_blob = document_collector.to_blob()
    # Internet-image results collected from this run's `image_search` /
    # `render_image_gallery` tool calls (None when no image tool ran), shaped for
    # the chat's inline gallery. Deterministic from validated tool results only.
    images_blob = image_collector.to_blob()
    # Native-video jobs queued by this run's `video_*` tool calls (None when no
    # video tool ran), shaped for the chat's live job card. Deterministic from
    # real submitted job handles only — a failed submission is never collected.
    video_blob = video_collector.to_blob()
    # Surface guardrails (v3 P3): a measured, activated surface may require the
    # finished answer to satisfy deterministic rules. A violation FAILS the run
    # with a named code instead of shipping the answer. A guardrail can only
    # reject an answer; it never grants a tool, a permission or an effect.
    guardrail_violation = _guardrail_failure(agent, output, store, request.runId)
    if guardrail_violation is not None:
        fenced_update_run(
            store, request.runId, runtime_id=runtime_id, status="failed",
            error=guardrail_violation, finishedAt=now_iso(),
        )
        try:
            await emitter.emit("run.failed", guardrail_violation)
        except Exception:  # noqa: BLE001 - the event must never mask the failure
            pass
        return _result(request.runId, "failed", store, guardrail_violation, output)
    # Capability offers: a run that hit an offerable dead end asks the user,
    # once, whether Eclipse should add the missing piece. The event drives the
    # chat card; the sentence keeps the ask visible to every client. Neither
    # authorizes anything: the named surface still asks for its own approval.
    output = await gaps.attach_offers(
        request.runId, output, emitter=emitter,
        tool_calls=int(final_state.get("tool_calls") or 0),
        need=gap_need, task_id=str(getattr(request, "taskId", "") or ""),
    )
    fenced_update_run(
        store, request.runId, runtime_id=runtime_id, status="completed",
        output=output, finishedAt=now_iso(), context=context_blob,
        **( {"research": research_blob} if research_blob else {} ),
        **( {"vision": vision_blob} if vision_blob else {} ),
        **( {"documents": documents_blob} if documents_blob else {} ),
        **( {"images": images_blob} if images_blob else {} ),
        **( {"video": video_blob} if video_blob else {} ),
    )
    # Durable terminal boundary: a resume of a completed run is a RESUME_CONFLICT.
    if lineage is not None:
        try:
            terminal_payload = _checkpoint_payload_of(final_state)
            if plan_runtime is not None:
                terminal_payload.update(
                    plan_runtime.checkpoint_fields(final_state.get("plan_step_id"))
                )
            lineage.save_checkpoint(request.runId, "terminal_completed", terminal_payload)
        except Exception:  # noqa: BLE001 - a checkpoint must never break a run
            pass
    # Phase 4: response-contract telemetry. Measure the FINAL structured answer
    # through the same render chokepoint the client uses (bridge op "finalize"),
    # so a repaired/degraded block becomes a visible metric and the recurring
    # block types can be fed back into repair/normalization rules. Best-effort:
    # a bridge miss or a prose answer is a no-op and never breaks the run.
    try:
        from .response_tools import emit_response_telemetry  # noqa: PLC0415

        await emit_response_telemetry(data_bridge, output)
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        pass
    await emitter.emit("run.completed", {"steps": final_state.get("step", 0)})
    eligibility = _learning_eligibility(run_id=request.runId, store=store, memory_outcome=memory_outcome)
    # An explicit "turn this into a skill" request is now turned into a real
    # inert skill draft from the completed run's trace — through the same
    # candidate + draft persistence path, without a second click. It is never
    # enabled: evaluation + operator approval remain the only promotion gates.
    if memory_outcome and memory_outcome.get("action") == "skill_requested":
        skill_draft = _create_explicit_skill_draft(
            request, store=store, skill_registry=skill_registry, registry=registry,
        )
        if skill_draft:
            memory_outcome["skillDraft"] = skill_draft
            eligibility["skillDraftReady"] = bool(skill_draft.get("captured"))
    # Automatic learning analysis runs for eligible completed runs — the user
    # should not have to ask. It creates ONLY inert candidates (never enables a
    # skill) and, for implicit procedures, only when recurrence evidence exists.
    analysis = await _post_run_analysis(request.runId, store, memory_service, skill_registry)
    memory_blob = _memory_blob(memory_outcome, eligibility, analysis)
    if memory_blob:
        _owner_write(memory=memory_blob)
    # Index the run's meaningful messages into the bounded session-history search
    # index so later sessions can ask "what did we decide?". Best-effort; never
    # affects this result.
    await _index_session(request, store, memory_service, emitter)
    # A run that names a project records itself and its artifacts in that
    # project, so the project fills itself up with what was actually made.
    from .projects.attach import attach_run_to_project_for_run  # noqa: PLC0415

    await attach_run_to_project_for_run(request, project_store, artifact_service,
                                        output, final_state)
    result = _result(request.runId, "completed", store, None, output)
    result.memory = memory_blob or None
    result.context = context_blob
    result.research = research_blob
    result.vision = vision_blob
    result.documents = documents_blob
    result.images = images_blob
    result.video = video_blob
    return result


async def _maybe_research(
    request: RunRequest,
    data_bridge: DataBridge,
    store: AgentMemoryStore,
    emitter: RunEmitter,
    retrieval_cache: Optional[Any],
    cancel_event: Optional[asyncio.Event],
) -> Optional[dict[str, Any]]:
    """Attempt the gated research flow; None when the request is not supported.

    Builds the same model the run uses (no separate adapter) and reuses the
    shared retrieval layer/cache. Cancellation is honored by the caller.
    """
    from .research.mandate import parse_mandate
    from .research.run import maybe_run_research
    from .retrieval import FinancialRetrievalLayer

    # Classify BEFORE building anything heavy: an unsupported/uncertain request
    # must fall through untouched and must not pay a model-construction cost
    # (which can raise for an unconfigured provider — that must not break chat).
    if not parse_mandate(
        request.input.query, ticker=request.input.ticker, market=request.input.market
    ).supported:
        return None

    layer = FinancialRetrievalLayer(data_bridge, cache=retrieval_cache, store=store)

    async def _retrieve(req: Any) -> Any:
        return await layer.retrieve(req)

    try:
        # Route research through the ModelRuntime for the financial_research task
        # profile (reasoning + tools + context), still building via the one
        # factory so the graph's test seam is preserved.
        _runtime = get_runtime()
        _resolution = _runtime.resolve_for_run(
            request.modelSettings, task="financial_research"
        )
        model = _runtime.build(
            _resolution.settings, builder=build_chat_model, adapter=_resolution.adapter
        )
        model = _runtime.prepare_model(model, _resolution)
    except Exception as exc:  # noqa: BLE001 - a supported request must not fall through ungated
        await emitter.emit("research.failed", {"error": str(exc)})
        return {
            "output": (
                "This is an evidence-grounded research request, but the configured model "
                f"could not be initialised, so no answer was produced. ({exc})"
            ),
            "status": "rejected",
            "metadata": None,
            "receipt": None,
            "persistenceError": None,
            "usage": {"promptTokens": 0, "completionTokens": 0},
            "coverage": None,
            "validation": None,
        }

    return await maybe_run_research(
        run_id=request.runId,
        query=request.input.query,
        ticker=request.input.ticker,
        market=request.input.market,
        model=model,
        retrieve=_retrieve,
        store=store,
        emitter=emitter,
        model_settings=request.modelSettings,
        provider=request.modelSettings.provider,
        bridge=data_bridge,
    )


async def _finish_research(
    request: RunRequest,
    store: AgentMemoryStore,
    emitter: RunEmitter,
    research: dict[str, Any],
    *,
    runtime_id: Optional[str] = None,
) -> RunResult:
    """Persist and return a gated research run's terminal result exactly once.

    The run status is `completed` when the gate produced an answer (even a
    partial one — partial is honest, not a failure); `failed` only when the flow
    itself could not run. Token usage reported by the synthesizer is added to the
    run (never estimated).
    """
    output = str(research.get("output") or "")
    usage = research.get("usage") or {}
    # `fenced_update_run` is imported inside `execute_run` for the main path;
    # the research finisher is a separate function, so it needs its own import.
    from .run_lease import fenced_update_run  # noqa: PLC0415

    prompt_tokens = int(usage.get("promptTokens") or 0)
    completion_tokens = int(usage.get("completionTokens") or 0)
    research_blob = {
        "status": research.get("status"),
        "metadata": research.get("metadata"),
        "receiptAvailable": research.get("receipt") is not None,
        "persistenceError": research.get("persistenceError"),
        "coverage": research.get("coverage"),
        "validation": research.get("validation"),
        "escalation": research.get("escalation"),
        "budget": research.get("budget"),
        # Structured provenance for the chat UI's separate Sources section (the
        # answer prose carries none of it).
        "failedRoutes": research.get("failedRoutes"),
        "stopReasonText": research.get("stopReasonText"),
        "blockedComparisons": research.get("blockedComparisons"),
    }
    fenced_update_run(
        store,
        request.runId,
        runtime_id=runtime_id,
        status="completed",
        output=output,
        promptTokens=prompt_tokens,
        completionTokens=completion_tokens,
        totalTokens=prompt_tokens + completion_tokens,
        research=research_blob,
        finishedAt=now_iso(),
    )
    await emitter.emit("run.completed", {"steps": 0, "research": True, "status": research.get("status")})

    result = _result(request.runId, "completed", store, None, output)
    # Expose the full receipt on the run result (not persisted inline — the
    # durable copy lives in ResearchReceipt and is read via the receipt route).
    result.research = {**research_blob, "receipt": research.get("receipt")}
    return result


async def _index_session(
    request: RunRequest,
    store: AgentMemoryStore,
    memory_service: Optional[Any],
    emitter: RunEmitter,
) -> None:
    """Index the completed run's messages for historical search (best-effort).

    Policy-gated; redacted + bounded; never affects the run result. A skill
    child-run is indexed too (its transcript is useful history).
    """
    try:
        settings = memory_service.policy.get_all() if memory_service is not None else {}
    except Exception:  # noqa: BLE001
        settings = {}
    if not bool(settings.get("session_indexing_enabled", True)):
        return
    if not bool(settings.get("session_history_enabled", True)):
        return
    try:
        from .session_index import index_run  # noqa: PLC0415

        result = index_run(
            store, request.runId, user_id=request.userId,
            session_id=request.sessionId, profile_id=request.profileId,
            project_id=request.projectId,
        )
        await emitter.emit("session.indexed", {"runId": request.runId, "indexed": result.get("indexed", 0)})
        # Keep the session's bounded summary fresh at natural boundaries. It is
        # searchable/opt-in and never injected into every prompt automatically.
        if request.sessionId and memory_service is not None:
            try:
                from .session_index import build_summary  # noqa: PLC0415

                build_summary(store, request.sessionId)
            except Exception:  # noqa: BLE001 - a summary is a projection, not truth
                pass
    except Exception as exc:  # noqa: BLE001 - indexing must never break a run
        try:
            await emitter.emit("session.index_failed", {"error": str(exc)})
        except Exception:  # noqa: BLE001
            pass


async def _post_run_analysis(
    run_id: str,
    store: AgentMemoryStore,
    memory_service: Optional[Any],
    skill_registry: Optional[Any],
) -> Optional[dict[str, Any]]:
    """Deterministic, policy-gated post-run analysis (mistakes + candidates).
    Never raises and never blocks the terminal result: analysis is diagnostics,
    not control flow. It can only create inert records — it cannot enable a
    skill or claim improvement. Returns a bounded summary for the run's memory
    blob (so the UI can show that automatic learning ran and what it produced).
    """
    if memory_service is None:
        return None
    try:
        skill_store = getattr(skill_registry, "store", None)
        analysis = memory_service.analyze_run(run_id, store, skill_store=skill_store)
        # Learned preferences are a separate, deterministic pass over the same
        # run. It can only stage inert suggestions; a failure is a diagnostic.
        try:
            if isinstance(analysis, dict):
                analysis["preferences"] = memory_service.observe_run_preferences(run_id, store)
        except Exception:  # noqa: BLE001
            pass
        return analysis
    except Exception:  # noqa: BLE001
        try:
            from .event_sequence import append_durable  # noqa: PLC0415

            append_durable(run_id, "memory.analysis_failed", {})
        except Exception:  # noqa: BLE001
            pass
        return None


# `MAX_HISTORY_TURNS` / `MAX_HISTORY_CHARS` and the history-bounding helpers live
# in `history.py` (re-exported below) so this graph stays under its size ratchet.


def _history_budget(
    request: RunRequest,
    allowed: Optional[set[str]],
    registry: ToolRegistry,
    system_prompt: str,
    profile_block: str,
    skill_manifest: str,
    memory_block: str,
    dropped_block: str,
    runtime_context: str = "",
    *,
    overrides: Optional[dict[str, Any]] = None,
    tighter: bool = False,
    anchor: Optional[Any] = None,
    history_chars: int = 0,
) -> Any:
    """Compute the per-run history budget from the resolved model + real request.

    Pure and local (no network). The window is the resolved model's declared
    context size; an unknown window yields an inert budget and the bound falls
    back to the module constants. `overrides` is the test/operator seam — it may
    supply a known window or a reserved-output value, never fabricate one.
    """
    from .config import flags  # noqa: PLC0415
    from .context_budget import DEFAULT_RESERVED_OUTPUT_TOKENS, compute_history_budget  # noqa: PLC0415

    overrides = overrides or {}
    soft_gate = flags.get_int("HARNESS_CONTEXT_GATE_PCT") / 100.0
    reserved = int(overrides.get("reserved_output_tokens", DEFAULT_RESERVED_OUTPUT_TOKENS))
    # Window precedence: an explicit override (test seam) wins, else the
    # operator-declared `ModelSettings.contextWindow` (used when the catalog
    # cannot know the window), else compute_history_budget falls back to the
    # catalog resolver. A declared value is a real declaration, not a guess.
    window_override = overrides.get("window")
    if window_override is None:
        window_override = getattr(request.modelSettings, "context_window", None)
    if overrides.get("tighter") and not tighter:
        tighter = True
    tool_schemas = ""
    try:
        from .context_budget import tool_schema_text  # noqa: PLC0415

        tool_schemas = tool_schema_text(registry.schemas(allowed))
    except Exception:  # noqa: BLE001 - a schema read failure must not break the run
        tool_schemas = ""
    return compute_history_budget(
        provider=request.modelSettings.provider,
        model=request.modelSettings.model,
        system_prompt=system_prompt,
        tool_schemas=tool_schemas,
        profile_block=profile_block,
        skill_manifest=skill_manifest,
        memory_block=memory_block,
        runtime_context=runtime_context,
        dropped_block=dropped_block,
        user_turn=_user_message(request),
        absolute_max_turns=MAX_HISTORY_TURNS,
        absolute_max_chars=MAX_HISTORY_CHARS,
        reserved_output_tokens=reserved,
        tighter=tighter,
        window_override=window_override,
        anchor=anchor,
        history_chars=history_chars,
        gate_soft=soft_gate,
        gate_hard=min(0.95, soft_gate + 0.20),
        gate_emergency=min(0.98, soft_gate + 0.35),
    )


def _assemble_run_messages(
    request: RunRequest,
    *,
    system_prompt: str,
    history_messages: list[BaseMessage],
    profile_block: str,
    skill_manifest: str,
    lesson_manifest: str,
    memory_block: str,
    dropped_block: str,
    runtime_context: str = "",
) -> list[BaseMessage]:
    """The full message list for one run, with the SAME slot order as before.

    Only the number of history messages varies per budget; the system-prompt
    head and the per-run block order are byte-stable within a run. The
    run-scoped `runtime_context` block sits immediately after the system head so
    the stable head prefix is unaffected.
    """
    return [
        SystemMessage(content=system_prompt),
        *history_messages,
        *([SystemMessage(content=profile_block)] if profile_block else []),
        *([SystemMessage(content=skill_manifest)] if skill_manifest else []),
        *([SystemMessage(content=lesson_manifest)] if lesson_manifest else []),
        *([SystemMessage(content=memory_block)] if memory_block else []),
        *([SystemMessage(content=dropped_block)] if dropped_block else []),
        # W2: run date/time is volatile. It must sit AFTER history so the
        # reusable prefix (system + tools + early history) stays cacheable; it
        # still precedes the user turn so the model reads it as context.
        *([SystemMessage(content=runtime_context)] if runtime_context else []),
        HumanMessage(content=_user_content(request)),
    ]


# Context retry lives in `context/retry.py` (size ratchet); re-exported for
# existing callers and tests that import it from this module.
from .context.retry import retry_with_tighter_budget as _retry_with_tighter_budget  # noqa: E402,F401

# History bounding + dropped-context extraction live in `history.py` (size
# ratchet) and are re-exported here so existing callers/tests are unchanged.
from .history import (  # noqa: E402,F401
    MAX_HISTORY_CHARS,
    MAX_HISTORY_TURNS,
    _bounded_history,
    _context_budget_blob,
    _dropped_context_block,
    _history_bounded_data,
    _history_messages,
    _model_extract_dropped,
)

# Deterministic per-run trade/close directive (kept out of the execution graph
# and out of the byte-stable prompt head).
from .trading_intent import (  # noqa: E402,F401
    apply_trade_surface,
    is_trade_turn,
    narrow_trade_surface,
    trading_intent_block,
)




# Per-run context blocks + memory/learning outcome helpers were extracted to
# graph_context.py to keep this execution graph under its size ratchet. They
# are re-exported here so existing callers/tests (rom harness.graph import
# _memory_context) are unchanged.
from .graph_context import (  # noqa: E402,F401
    _agent_tool_names,
    _apply_memory_intent,
    _create_explicit_skill_draft,
    _is_recurring_statement,
    _learning_eligibility,
    _lesson_manifest_context,
    _memory_blob,
    _memory_context,
    _outcome_blob,
    _profile_context,
    _repo_map_context,
    _skill_manifest_context,
    learning_eligibility_for_run, surface_summary,
)


def _user_message(request: RunRequest) -> str:
    from .turn_content import user_message  # noqa: PLC0415

    return user_message(request)


def _user_content(request: RunRequest) -> Any:
    from .turn_content import user_content  # noqa: PLC0415

    return user_content(request)


def _guardrail_failure(
    agent: AgentConfig,
    output: str,
    store: AgentMemoryStore,
    run_id: str,
) -> Optional[dict[str, Any]]:
    """The run error when the finished answer breaks a surface guardrail.

    A guardrail is a runtime rule, not a prompt hint: it is checked once, at the
    end of the run, against what the run really did (the persisted tool
    executions) and what it really answered. A broken guardrail implementation
    must never break the run, so any failure here degrades to "no violation".
    """
    guardrails = tuple(getattr(agent, "guardrails", ()) or ())
    max_chars = getattr(agent, "maxAnswerChars", None)
    if not guardrails and max_chars is None:
        return None
    try:
        from .self_improvement.guardrails import evaluate_guardrails, guardrail_error  # noqa: PLC0415

        try:
            executions = store.list_tool_executions(run_id)
        except Exception:  # noqa: BLE001 - an unreadable ledger is "unknown"
            executions = []
        tool_calls = len(executions)
        tool_texts = [str(row.get("text") or "") for row in executions[:200]]
        violations = evaluate_guardrails(
            guardrails,
            output=output or "",
            tool_calls=tool_calls,
            max_answer_chars=max_chars,
            tool_texts=tool_texts,
        )
        return guardrail_error(violations)
    except Exception:  # noqa: BLE001 - a broken guardrail never breaks the run
        return None


def _result(run_id: str, status: str, store: AgentMemoryStore, error: Optional[dict[str, Any]], output: Optional[str] = None) -> RunResult:
    run = store.get_run(run_id) or {}
    try:
        from .run_lineage import RunLineage  # noqa: PLC0415

        children = RunLineage(store).list_child_runs(run_id)
    except Exception:  # noqa: BLE001 - lineage is additive
        children = []
    return RunResult(
        runId=run_id,
        status=status,  # type: ignore[arg-type]
        output=output if output is not None else run.get("output"),
        error=HarnessError(**error) if error else None,
        parentRunId=run.get("parentRunId"),
        childRunIds=children,
        modelSteps=run.get("modelSteps", 0),
        toolCalls=run.get("toolCalls", 0),
        toolErrors=run.get("toolErrors", 0),
        promptTokens=run.get("promptTokens", 0),
        completionTokens=run.get("completionTokens", 0),
        totalTokens=run.get("totalTokens", 0),
        codeVerification=run.get("codeVerification"),
        autonomy=run.get("autonomy"),
        workspaceId=run.get("workspaceId"),
        capability=_capability_of(run),
        surface=run.get("surface"),
    )


def _capability_of(run: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The chat capability view for a run, derived from its verification blob."""
    from .code.capability import capability_from_verification  # noqa: PLC0415

    return capability_from_verification(run.get("codeVerification"))

__all__ = ["build_graph", "execute_run"]
