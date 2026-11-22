"""The ONE surface resolver: what a run's tool surface IS, and what it lost.

Round 3 / W2 (ADR-H280).

**Why this module exists.** A *surface* is the set of tools a run may use. The
finance agent has one (`ECLIPSE_AGENT.tools`); a chat coding turn has another
(`CHAT_CODE_TOOLS`). Until this module, the two were combined by
**replacement**: `graph.resolve_surface` computed

    allowed = chat_profile if chat_profile is not None else effective_allowlist(...)

so a chat coding turn **threw the finance agent's entire allowlist away** and
bound only the code profile. Measured on 2026-10-20 against a real
`AgenticService` (956 registered tools):

  * the finance agent declares **668** tools; the code profile declares **454**;
  * the two are largely disjoint — **441** base tools and **227** surface tools
    are unique to their half;
  * so a chat coding turn silently lost **441** tools, including the whole
    task/plan family (`create_plan`, `create_task`, `read_plan`, `run_plan`,
    `update_task`, ...), the whole web/research family (`web_search`,
    `web_extract`, `research_query`, `claim_verify`, ...), the unified
    retrieval/search family (`semantic_search`, `hybrid_search`,
    `search_evidence`, ...), the truthful-capability diagnostics
    (`get_capability_matrix`, `get_research_health`), and the response-contract
    tools (`response_block_schema`, `response_validate`).

Two of those losses were not merely inconvenient. `response_block_schema` is
what `graph.execute_run` checks to decide whether to inject the live
response-schema digest and the response-mode plan into the system prompt
(`graph.py:1291`, `graph.py:1307`) — so a chat coding turn produced a
*degraded structured response* with no error. And `get_capability_matrix` is
the tool whose absence was already recorded as a real incident in the
`ECLIPSE_AGENT` allowlist comment (`agents.py:299-304`: *"verified in the
SYS/RELIANCE/Tata runs, where `get_capability_matrix` returned
TOOL_FORBIDDEN"*).

The stated intent was always the opposite of the behaviour: `agents.py` said
*"ADDITIVE: `ECLIPSE_AGENT`'s declared tools are NOT touched"*, and ADR-H117
described the profile as applied *"after the autonomy narrowing"*. The code
replaced instead. This module makes the code match the stated intent.

**The rule this module enforces.**

> A surface EXTENDS or NARROWS an agent's declared tools. It never silently
> REPLACES them, and it never removes a base tool without naming it.

Both halves are narrowed through the ONE policy authority
(`code.policy.allowed_tools`), so an autonomy profile still removes every
write/execute/external tool it bans — from the base half *and* from the
surface half. The union of two policy-narrowed sets is still policy-narrowed.

**What is published.** A `SurfaceResolution` names what the surface added,
what the autonomy profile removed, and what a disabled store removed, so the
surface can never lose a tool quietly. It is attached to the run row and
returned on the run result (additive, optional field `surface`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .schemas import AgentConfig
from .tools import ToolRegistry

#: The ceiling on the schema characters a chat code surface may ADD to a run's
#: bound tool set. Measured on 2026-10-20: the union adds 64_494 characters at
#: `edit_verify` and 28_093 at `plan_only` (the code tools the finance base did
#: not already carry). The ceiling leaves ~1.9x headroom and is asserted by
#: `python/tests/test_surface_resolution.py`.
#:
#: This is a RATCHET, not a runtime budget: it makes a silent regression
#: impossible to merge. Round 3 / W6 turns it into a runtime refusal by moving
#: the schema set behind lazy tool loading, so the bound cost stops growing
#: with the declared surface.
MAX_SURFACE_ADDED_SCHEMA_CHARS = 120_000

#: The surface ids this module can resolve. `base` is an ordinary run with no
#: surface applied; `chat_code` is a chat turn the deterministic classifier
#: recognised as a coding request.
SURFACE_BASE = "base"
SURFACE_CHAT_CODE = "chat_code"


@dataclass(frozen=True)
class SurfaceResolution:
    """What a run's tool surface is, and how it got there.

    `allowed` is the set the model may call. The other fields exist so a
    missing tool is always explainable: `added` by the surface, `policyRemoved`
    by the autonomy profile, `disabled` by an operator-disabled store.
    """

    agentId: str  # noqa: N815 - the wire shape uses camelCase
    autonomy: Optional[str]
    surfaceId: str  # noqa: N815
    allowed: Optional[frozenset[str]]
    base: Optional[frozenset[str]]
    surface: Optional[frozenset[str]]
    #: Tools the surface brought that the base did not have.
    added: tuple[str, ...] = ()
    #: Tools the autonomy profile removed, from either half. Named, never silent.
    policyRemoved: tuple[str, ...] = ()  # noqa: N815
    #: Tools the CODING profile preset removed (runtime/container/cloud/browser/
    #: computer/orchestration/package). Reachable only by explicit operator enable.
    presetRemoved: tuple[str, ...] = ()  # noqa: N815
    #: Tools an operator-disabled store removed, from either half.
    disabled: tuple[str, ...] = ()
    #: Base tools this resolution does not carry AND cannot explain. This is
    #: ALWAYS empty: a tool may leave the surface only through the autonomy
    #: profile or a disabled store, and both of those are named above. A
    #: non-empty `removed` is a defect, not a report.
    removed: tuple[str, ...] = ()
    #: How many tools are actually bound (declared AND registered).
    boundTools: int = 0  # noqa: N815
    #: The schema characters the surface ADDED to the bound set. The marginal
    #: cost of the surface, measured rather than assumed.
    addedSchemaChars: int = 0  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        """The published blob. Additive and optional on the run result."""
        return {
            "agentId": self.agentId,
            "autonomy": self.autonomy,
            "surfaceId": self.surfaceId,
            "applied": self.surfaceId != SURFACE_BASE,
            "baseTools": len(self.base) if self.base is not None else None,
            "surfaceTools": len(self.surface) if self.surface is not None else None,
            "allowedTools": len(self.allowed) if self.allowed is not None else None,
            "boundTools": self.boundTools,
            "addedTools": len(self.added),
            "addedSchemaChars": self.addedSchemaChars,
            "added": list(self.added),
            "policyRemoved": list(self.policyRemoved),
            "presetRemoved": list(self.presetRemoved[:50]),
            "presetRemovedCount": len(self.presetRemoved),
            "presetRemovedTruncated": max(0, len(self.presetRemoved) - 50),
            "disabled": list(self.disabled),
            "removed": list(self.removed),
            "maxAddedSchemaChars": MAX_SURFACE_ADDED_SCHEMA_CHARS,
        }

    def summary(self) -> str:
        """One honest line about this run's surface."""
        if self.surfaceId == SURFACE_BASE:
            return f"surface=base tools={self.boundTools}"
        return (
            f"surface={self.surfaceId} added={len(self.added)} "
            f"bound={self.boundTools} policyRemoved={len(self.policyRemoved)} "
            f"presetRemoved={len(self.presetRemoved)} disabled={len(self.disabled)}"
        )


def _declared_allowlist(agent: AgentConfig, registry: ToolRegistry) -> Optional[set[str]]:
    """The agent's declared tools, plus the surfaces it explicitly opts into.

    An MCP or plugin tool is added only when the AGENT opts in
    (`allowMcpTools` / `allowPluginTools`); registration already required an
    enabled server, an explicit per-tool selection and a read-only effect, so
    this cannot surface a merely discovered tool. An agent with no declared
    allowlist (`tools is None`) is unrestricted and stays that way.
    """
    if agent.tools is None:
        return None
    allowed = set(agent.tools)
    if agent.id == "eclipse-agent":
        from .self_improvement.widening_runtime import granted_tools  # noqa: PLC0415

        for name in granted_tools(agent.id):
            tool = registry.get(name)
            if tool is None:
                continue
            if getattr(tool, "permission", "") != "read":
                continue
            if str(getattr(tool, "effect", "") or "") not in ("", "read"):
                continue
            if getattr(tool, "risk", "low") not in ("none", "low"):
                continue
            allowed.add(name)
        from .self_improvement.effectful_widening import granted_tools as effectful_granted  # noqa: PLC0415

        for name in effectful_granted(agent.id):
            tool = registry.get(name)
            if tool is not None and getattr(tool, "permission", "") != "read":
                allowed.add(name)
    if agent.allowMcpTools:
        allowed.update(name for name in registry.names() if name.startswith("mcp_"))
    if getattr(agent, "allowPluginTools", False):
        allowed.update(name for name in registry.names() if name.startswith("plug_"))
        allowed.update(name for name in registry.names() if name.startswith("plugin_"))
    if getattr(agent, "allowModelTools", False):
        allowed.update(name for name in registry.names() if name.startswith("ml_model_"))
    return allowed


def _narrow_to_coding_surface(
    agent: AgentConfig, allowed: Optional[set[str]], autonomy: Optional[str],
) -> tuple[Optional[set[str]], set[str]]:
    """Apply the coding preset for the code-agent profile.

    Returns `(kept, removed)`. Only the code agent is narrowed here: the chat
    half is narrowed in `chat_code_allowlist`, and a finance run with no coding
    autonomy keeps its full surface.
    """
    if allowed is None or agent.id != "code-agent" or not autonomy:
        return allowed, set()
    from .code.policy import coding_surface, coding_surface_enabled  # noqa: PLC0415

    if not coding_surface_enabled():
        return allowed, set()
    preset = set(coding_surface(autonomy))
    return allowed & preset, allowed - preset


def effective_allowlist(
    agent: AgentConfig, registry: ToolRegistry, autonomy: Optional[str] = None,
) -> Optional[set[str]]:
    """The run's tool allowlist, with an explicit, narrow MCP opt-in.

    When the agent sets `allowMcpTools`, the allowlist is expanded to include the
    MCP tools CURRENTLY REGISTERED in the registry (names under the `mcp_`
    namespace). Registration already requires an enabled server, explicit
    per-tool selection and a read-only effect, so this cannot surface a merely
    discovered tool. Every other agent keeps its exact allowlist - the opt-in is
    per-agent and defaults off, so a restricted custom agent or skill can never
    reach an MCP tool it did not name. When the agent has NO allowlist (None =
    all tools), the expansion is a no-op.

    When an `autonomy` profile is given, the declared allowlist is NARROWED
    through the ONE policy authority (`code.policy.allowed_tools`), so a
    write/execute/external tool the profile does not permit is removed BEFORE the
    model sees its schema - the autonomy boundary is structural, not a prompt.
    """
    allowed = _declared_allowlist(agent, registry)
    if allowed is None:
        return None
    if autonomy is not None:
        from .kernel.services.runtime import kernel_runtime  # noqa: PLC0415

        authority = kernel_runtime.policy_authority if kernel_runtime.active else None
        if authority is not None:
            allowed = set(authority.allowed_tools(autonomy, tuple(sorted(allowed))))
        else:
            from .code.policy import allowed_tools  # noqa: PLC0415

            allowed = set(allowed_tools(autonomy, tuple(sorted(allowed))))
    allowed, _removed = _narrow_to_coding_surface(agent, allowed, autonomy)
    return allowed


def _chat_code_declared_tools() -> tuple[str, ...]:
    """The code surface's DECLARED tool set, before any policy narrowing."""
    from .agents import CHAT_CODE_TOOLS  # noqa: PLC0415

    return tuple(CHAT_CODE_TOOLS)


def chat_code_allowlist(
    agent: AgentConfig, registry: ToolRegistry, autonomy: Optional[str],
) -> Optional[set[str]]:
    """The code surface's OWN tool set for a chat turn that carries an autonomy.

    Only the finance agent (`eclipse-agent`) has a chat code profile, and only
    when the front door sent an autonomy (i.e. the deterministic classifier saw a
    coding request). The profile is `agents.CHAT_CODE_TOOLS`, narrowed through the
    ONE policy authority (`code.policy.allowed_tools`) so `plan_only` removes the
    write/execute tools and `edit_verify` keeps them - and the external actions
    are absent from the declared set entirely (they cannot be added back by a
    profile). Returns None when the agent is not the finance agent, or no autonomy
    was sent, so the caller keeps the ordinary surface.

    This is the surface's half of the resolution. It is NOT the run's allowlist:
    `resolve_surface_detail` UNIONS it with the agent's own declared tools.
    """
    from .agents import CHAT_CODE_TOOLS  # noqa: PLC0415

    if agent.id != "eclipse-agent" or not autonomy:
        return None
    from .code.policy import allowed_tools, coding_surface, coding_surface_enabled  # noqa: PLC0415

    narrowed = set(allowed_tools(autonomy, tuple(CHAT_CODE_TOOLS)))
    if coding_surface_enabled():
        narrowed &= set(coding_surface(autonomy))
    return narrowed


def surface_disabled_tools(settings: Optional[dict[str, Any]]) -> set[str]:
    """Tool names whose surface is disabled by the resolved policy."""
    settings = settings or {}
    disabled: set[str] = set()
    memory_on = bool(settings.get("memory_enabled", True))
    if not memory_on:
        # The whole memory/skill surface is off (except skill_list/view, which are
        # read-only discovery the prompt still offers; the store is the memory DB
        # but skills are a separate concern - only the memory WRITE tool and the
        # memory-dependent search tools are dropped).
        disabled.update({"memory", "memory_search_native", "memory_search_external"})
    if not bool(settings.get("session_history_enabled", True)):
        disabled.update({"session_search", "session_open"})
    if not bool(settings.get("external_memory_search_enabled", False)):
        disabled.add("memory_search_external")
    if not bool(settings.get("agent_scheduling_enabled", False)):
        # Phase 17 (repo-mapped): the agent may only put work on a cadence when
        # the operator's master switch is on. Off by default - the whole
        # scheduling surface (reads included) is dropped so the model cannot
        # even attempt it.
        from .automation.scheduling_tools import TOOL_NAMES as _SCHEDULING  # noqa: PLC0415

        disabled.update(_SCHEDULING)
    if not bool(settings.get("acquisition_enabled", False)):
        # Phase 16 slice: bulk collection is off until the operator turns it on.
        from .acquire.tools import TOOL_NAMES as _ACQUIRE  # noqa: PLC0415

        disabled.update(_ACQUIRE)
    return disabled


def _schema_chars(registry: ToolRegistry, names: set[str]) -> int:
    """The schema characters of `names`, measured over the REAL registry.

    A tool's cost to the model is its name, its description and its input
    schema. Counting them is how the surface's marginal cost stays a number
    rather than an assumption.
    """
    total = 0
    for tool in registry.list():
        if tool.name not in names:
            continue
        total += len(tool.name) + len(tool.description or "")
        try:
            total += len(json.dumps(tool.input_schema, sort_keys=True))
        except (TypeError, ValueError):  # pragma: no cover - a schema that is not JSON-able
            total += len(str(tool.input_schema))
    return total


def resolve_surface_detail(
    agent: AgentConfig,
    registry: ToolRegistry,
    settings: Optional[dict[str, Any]],
    autonomy: Optional[str] = None,
    *,
    apply_code_surface: bool = True,
) -> SurfaceResolution:
    """Resolve ONE run's tool surface, and name everything it added and lost.

    The combination is a UNION, not a replacement:

        allowed = policy_narrowed(base) UNION policy_narrowed(surface)

    Both halves go through the ONE policy authority, so the autonomy profile
    still removes every write/execute/external tool it bans - from the base half
    and from the surface half. The surface therefore never removes a base tool:
    what a run does not carry was removed by the autonomy profile or by an
    operator-disabled store, and both are named in the resolution.

    An agent with NO declared allowlist (`tools is None`) is unrestricted; the
    surface is then the only narrowing there is, exactly as before.
    """
    settings = settings or {}
    disabled = surface_disabled_tools(settings)
    # A code-agent run with no explicit autonomy resolves to edit_verify, so
    # write/execute tools are available by default; an ordinary finance run keeps
    # `autonomy=None` and is never narrowed.
    effective_autonomy = autonomy
    if effective_autonomy is None and agent.id == "code-agent":
        effective_autonomy = "edit_verify"

    declared = _declared_allowlist(agent, registry)
    base = effective_allowlist(agent, registry, effective_autonomy)
    # A task-dispatched run (a goal turn, a UI build, an automation) is not a
    # chat coding request: the chat code profile would REPLACE the finance
    # agent's data/compute tools with a code subset, hiding e.g.
    # `get_historical_prices` and `analysis_run` from a backtest goal.
    surface = (
        chat_code_allowlist(agent, registry, autonomy)
        if apply_code_surface else None
    )

    # What the autonomy profile removed, from EITHER half. Both halves go
    # through the ONE policy authority, so a write/execute/external tool the
    # profile bans is gone from the base AND from the surface.
    policy_removed: set[str] = set()
    if effective_autonomy is not None:
        from .code.policy import allowed_tools  # noqa: PLC0415

        if declared is not None:
            policy_removed |= declared - set(
                allowed_tools(effective_autonomy, tuple(sorted(declared)))
            )
        if surface is not None:
            surface_declared = set(_chat_code_declared_tools())
            policy_removed |= surface_declared - set(
                allowed_tools(effective_autonomy, tuple(sorted(surface_declared)))
            )

    preset_removed: set[str] = set()
    if effective_autonomy is not None:
        from .code.policy import (  # noqa: PLC0415
            coding_surface, coding_surface_enabled,
        )

        if coding_surface_enabled():
            preset = set(coding_surface(effective_autonomy))
            if agent.id == "code-agent" and declared is not None:
                preset_removed |= declared - preset
            if surface is not None:
                preset_removed |= set(_chat_code_declared_tools()) - preset

    if surface is None:
        allowed = base
        surface_id = SURFACE_BASE
        added: tuple[str, ...] = ()
        surface_set: Optional[frozenset[str]] = None
    elif base is None:
        # The agent declared no allowlist: the surface is the only narrowing.
        allowed = surface
        surface_id = SURFACE_CHAT_CODE
        added = tuple(sorted(surface))
        surface_set = frozenset(surface)
    else:
        allowed = base | surface
        surface_id = SURFACE_CHAT_CODE
        added = tuple(sorted(surface - base))
        surface_set = frozenset(surface)

    # A disabled store removes its tool from the bound schema AND the prompt
    # guidance, so the model is never told about a tool it cannot use.
    if allowed is None:
        if disabled:
            allowed = {name for name in registry.names() if name not in disabled}
        else:
            allowed = None
    else:
        allowed = {name for name in allowed if name not in disabled}

    # Kernel tool policy: an operator/evolution overlay can deny names or narrow
    # to an allow-list. A denial is explained as policy removal, not unexplained.
    from .kernel.services.runtime import kernel_runtime  # noqa: PLC0415

    overlay = kernel_runtime.tool_policy_for(agent.id) if kernel_runtime.active else None
    if overlay:
        denied = set(overlay.get("deny") or ())
        keep = set(overlay.get("allow") or ())
        if allowed is None:
            allowed = set(registry.names())
        removed_by_overlay = (allowed & denied)
        allowed -= denied
        if keep:
            removed_by_overlay |= (allowed - keep)
            allowed &= keep
        policy_removed = set(policy_removed) | removed_by_overlay
        disabled = set(disabled) | denied | (removed_by_overlay if keep else set())

    registered = set(registry.names())
    bound = registered & allowed if allowed is not None else registered
    added_chars = _schema_chars(registry, set(added) & registered) if added else 0

    # A tool may leave the surface only through the autonomy profile or a
    # disabled store. Anything else is UNEXPLAINED and is reported as such -
    # it must always be empty.
    carried = base if base is not None else set()
    unexplained = (carried - set(allowed or ())) - policy_removed - preset_removed - set(disabled)

    return SurfaceResolution(
        agentId=agent.id,
        autonomy=effective_autonomy,
        surfaceId=surface_id,
        allowed=frozenset(allowed) if allowed is not None else None,
        base=frozenset(base) if base is not None else None,
        surface=surface_set,
        added=added,
        policyRemoved=tuple(sorted(policy_removed)),
        presetRemoved=tuple(sorted(preset_removed)),
        disabled=tuple(sorted(disabled)),
        removed=tuple(sorted(unexplained)),
        boundTools=len(bound),
        addedSchemaChars=added_chars,
    )


def resolve_surface_run(
    agent: AgentConfig,
    registry: ToolRegistry,
    settings: Optional[dict[str, Any]],
    autonomy: Optional[str] = None,
    *,
    apply_code_surface: bool = True,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[SurfaceResolution, str]:
    """Resolve ONE run's surface AND its byte-stable system prompt.

    The prompt is computed here (never at import time), so a policy change
    takes effect on the next run and the system-prompt head stays byte-stable
    within a run. A disabled store's tool is dropped from BOTH the allowlist
    and the prompt guidance.
    """
    from .prompts import filter_system_prompt  # noqa: PLC0415

    resolution = resolve_surface_detail(
        agent, registry, settings, autonomy,
        apply_code_surface=apply_code_surface,
    )
    settings = settings or {}
    prompt = filter_system_prompt(
        agent.systemPrompt,
        memory_enabled=bool(settings.get("memory_enabled", True)),
        session_history_enabled=bool(settings.get("session_history_enabled", True)),
    )
    # Gap #13: one audited base prompt, plus the resolved model family's short
    # tool-use guidance. Finance turns are untouched.
    from .prompt_variants import apply_coding_variant  # noqa: PLC0415

    prompt = apply_coding_variant(
        prompt, provider=provider, model=model,
        agent_id=agent.id, autonomy=autonomy,
    )
    return resolution, prompt


def resolve_surface(
    agent: AgentConfig,
    registry: ToolRegistry,
    settings: Optional[dict[str, Any]],
    autonomy: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[Optional[set[str]], str]:
    """Resolve `(allowlist, system_prompt)` for ONE run from policy.

    `allowlist` is `None` only when the agent has no allowlist AND nothing is
    disabled (the unrestricted case is passed through unchanged).

    `resolve_surface_detail` is the same resolution with the reasoning
    attached; this function is the narrow view of it.
    """
    resolution, prompt = resolve_surface_run(
        agent, registry, settings, autonomy, provider=provider, model=model,
    )
    return (set(resolution.allowed) if resolution.allowed is not None else None), prompt


__all__ = [
    "MAX_SURFACE_ADDED_SCHEMA_CHARS",
    "SURFACE_BASE",
    "SURFACE_CHAT_CODE",
    "SurfaceResolution",
    "chat_code_allowlist",
    "effective_allowlist",
    "resolve_surface",
    "resolve_surface_detail",
    "resolve_surface_run",
    "surface_disabled_tools",
]
