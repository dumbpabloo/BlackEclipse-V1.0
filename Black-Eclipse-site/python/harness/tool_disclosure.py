"""W3.3 — progressive tool disclosure: mode, threshold, tiers, catalogue block.

The bound tool schema is the expensive part of the prompt. When it crosses the
configured share of the model window, Eclipse binds the tools that must stay on
the graph's execution path (effectful, interactive, the discovery core) and
lists the rest cheaply. The hidden tail is still reachable through the EXISTING
`tool_search` / `tool_invoke` pair — this module does not add a second dispatch
path.

Modes (`HARNESS_TOOL_DISCLOSURE`):

  * `off`  — never narrow (byte-identical to the eager surface);
  * `on`   — always narrow (when the discovery pair is bound);
  * `auto` — narrow only when the measured schema cost crosses
             `HARNESS_TOOL_DISCLOSURE_THRESHOLD_PCT` of a KNOWN window.

`HARNESS_LAZY_TOOLS=1` remains a deprecated alias for `on`; the registry flag
wins whenever it is set.

An unknown window no longer forces an eager surface. When the measured schema
alone crosses `ABSOLUTE_SCHEMA_BUDGET_TOKENS`, `auto` narrows anyway: hiding a
READ-ONLY tool behind `tool_search`/`tool_invoke` is reversible, while sending
853 schemas (~100k tokens) to a model is not. The plan still records
`window=None`, so the unknown stays honest and visible in telemetry.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .config import flags
from .context_budget import tool_schema_text
from .eclipse_memory.profile import estimate_tokens
from .tool_bm25 import rank_tools

#: Always bound when disclosure narrows the surface. Kept identical to the
#: Round 5 / C3 lazy core so a run that enabled lazy mode sees no regression.
CORE_TOOLS = frozenset({
    "tool_search",
    "tool_invoke",
    "load_tool_schema",
    "read_tool_output",
    "response_block_schema",
    "response_style_guide",
    "response_validate",
    "clarify",
})

#: Catalogue tier budgets (estimated tokens). Tier 1 is the useful default;
#: tier 2 drops descriptions; tier 3 names only domain counts.
TIER1_TOKENS = 4_000
TIER2_TOKENS = 2_000
TIER3_TOKENS = 500

#: The absolute schema ceiling for `auto` when the context window is UNKNOWN.
#: A window we cannot name is not a reason to ship 100k tokens of tool schemas:
#: above this budget the read-only long tail is hidden (and stays reachable).
ABSOLUTE_SCHEMA_BUDGET_TOKENS = 20_000

#: Read-only tools that stay bound even when the turn's query ranks nothing
#: (greetings, one-word turns): the ordinary data path must never require a
#: discovery round-trip. Only names present in the allowlist are kept.
FOCUS_READ_TOOLS = frozenset({
    "get_quote", "market_get_quote", "get_historical_prices",
    "market_get_ohlcv", "get_market_cap", "get_fx_rate",
    "get_fundamentals", "fundamentals_get_company_profile",
    "fundamentals_get_financials", "financial_resolve_entity",
    "financial_providers", "get_capability_matrix", "get_coverage",
    "get_source_health", "get_research_health",
    "research_search", "research_search_news", "research_retrieve_source",
    "macro_get_series", "web_search", "web_extract", "page_extract",
    "pdf_extract", "structured_extract", "web_scrape", "scraper_run",
    "analysis_run", "finance_calc", "finance_metrics", "finance_growth",
    "finance_series", "finance_valuation", "memory", "session_search",
    "skill_list", "skill_view",
})

#: The coding counterpart of FOCUS_READ_TOOLS. A coding turn must never lose its
#: read/search/inspect half to query ranking: a prompt like "make the failing
#: test pass" ranks finance vocabulary poorly, and a model that must call
#: `tool_search` before it can `read_file` burns a step on every hop. These are
#: the tools the code profiles declare; only names already in the allowlist are
#: kept, so a finance-only run is unaffected.
CODE_FOCUS_READ_TOOLS = frozenset({
    # project + repository
    "workspace_status", "workspace_list", "workspace_read", "workspace_docs",
    "workspace_diff", "project_rules", "git_status",
    # filesystem inspection
    "list_directory", "read_file", "search_text", "search_files",
    "search_symbols", "find_references", "file_metadata", "file_hash",
    "file_permissions", "verify_file", "validate_changes",
    # code intelligence
    "code_get_definition", "code_find_references", "code_find_symbol",
    "code_search_symbols", "code_get_hover", "code_get_diagnostics",
    "code_get_document_symbols", "code_get_imports", "code_get_exports",
    "code_get_dependencies", "code_get_dependents", "code_get_project_structure",
    "code_parse_file", "code_analyze_file", "code_get_relevant_context",
    "code_intel_status", "code_find_related_tests",
    # undo visibility
    "checkpoint_list", "checkpoint_diff",
})

#: How many query-ranked read-only tools stay bound on top of the focus set.
DEFAULT_MAX_READ_TOOLS = 32

_TIER1_DESCRIPTION_CHARS = 120
_TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled"})


@dataclass
class DisclosurePlan:
    """The resolved decision and everything the run needs to explain it."""

    mode: str = "off"
    enabled: bool = False
    reason: str = ""
    tier: Optional[int] = None
    allowed: Optional[set[str]] = None
    hidden: tuple[str, ...] = ()
    advertised: tuple[str, ...] = ()
    schema_tokens: int = 0
    catalogue_tokens: int = 0
    saved_tokens: int = 0
    window: Optional[int] = None
    threshold_pct: int = 8
    catalogue_block: str = ""
    max_read_tools: int = DEFAULT_MAX_READ_TOOLS

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "reason": self.reason,
            "tier": self.tier,
            "hiddenTools": len(self.hidden),
            "advertisedTools": len(self.advertised),
            "toolSchemaTokens": self.schema_tokens,
            "catalogueTokens": self.catalogue_tokens,
            "savedTokens": self.saved_tokens,
            "window": self.window,
            "windowUnknown": self.window is None,
            "thresholdPct": self.threshold_pct,
            "maxReadTools": self.max_read_tools,
        }


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY


def disclosure_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the one disclosure mode, honouring the legacy lazy alias."""
    env = os.environ if environ is None else environ
    if "HARNESS_TOOL_DISCLOSURE" not in env and "HARNESS_LAZY_TOOLS" in env:
        return "on" if _truthy(env.get("HARNESS_LAZY_TOOLS")) else "off"
    return flags.get_flag("HARNESS_TOOL_DISCLOSURE", environ=env)


def disclosure_threshold_pct(environ: Optional[Mapping[str, str]] = None) -> int:
    return flags.get_int("HARNESS_TOOL_DISCLOSURE_THRESHOLD_PCT", environ=environ)


def schema_tokens(names: set[str], registry: Any) -> int:
    """Measured cost of the bound schemas for `names` (the same rendering the
    context budget uses, so the saving is comparable to the real overhead)."""
    try:
        return int(estimate_tokens(tool_schema_text(registry.schemas(sorted(names)))))
    except Exception:  # noqa: BLE001 - a measurement must never break a run
        return 0


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _domain(tool: Any) -> str:
    return str(getattr(tool, "domain", "") or "other").strip() or "other"


def _tier1(tools: list[Any]) -> str:
    return "\n".join(
        f"- {getattr(tool, 'name', '')}: "
        f"{_one_line(getattr(tool, 'description', ''), _TIER1_DESCRIPTION_CHARS)}"
        f" [{_domain(tool)}]"
        for tool in tools
    )


def _tier2(tools: list[Any]) -> str:
    return "\n".join(
        f"- {getattr(tool, 'name', '')} [{_domain(tool)}]" for tool in tools
    )


def _tier3(tools: list[Any]) -> str:
    counts: dict[str, int] = {}
    for tool in tools:
        counts[_domain(tool)] = counts.get(_domain(tool), 0) + 1
    return "\n".join(f"- {domain}: {count} tools" for domain, count in sorted(counts.items()))


def build_catalogue(hidden: list[Any]) -> tuple[int, str]:
    """Choose the cheapest tier that still names what exists.

    Deterministic: sorted by name, no clock, no model. Returns `(tier, block)`.
    The header explains how to reach a hidden tool and states the invariant
    that effectful tools are never hidden.
    """
    ordered = sorted(hidden, key=lambda tool: str(getattr(tool, "name", "")))
    header = (
        "## Tool catalogue (not bound)\n"
        "These tools exist but are not in the bound schema. Find one with "
        "`tool_search`, then run a READ-ONLY tool with `tool_invoke`. "
        "Effectful tools are always bound.\n"
    )
    for tier, render, budget in (
        (1, _tier1, TIER1_TOKENS),
        (2, _tier2, TIER2_TOKENS),
        (3, _tier3, TIER3_TOKENS),
    ):
        body = render(ordered)
        block = f"{header}\n{body}" if body else header
        if estimate_tokens(block) <= budget:
            return tier, block
    return 3, f"{header}\n{_tier3(ordered)}"


def _must_bind(tool: Any) -> bool:
    if getattr(tool, "interactive", False):
        return True
    if getattr(tool, "effect", None):
        return True
    return str(getattr(tool, "permission", "read")) != "read"


def resolve_disclosure(
    allowed: Optional[set[str]],
    registry: Any,
    *,
    window: Optional[int] = None,
    query: str = "",
    max_read_tools: int = DEFAULT_MAX_READ_TOOLS,
    environ: Optional[Mapping[str, str]] = None,
) -> DisclosurePlan:
    """Resolve the disclosure decision for one run's allowlist.

    `query` focuses the read-only tail: the tools most relevant to this turn
    stay bound, the rest are catalogued. It never removes an effectful,
    interactive or core tool, so authority is unchanged.

    When narrowing is active the schema-on-demand tool is installed here, if it
    is not already registered: it is needed exactly when a tool is catalogued but
    its schema is not bound. The install is idempotent and read-only.
    """
    from .tool_schema_load import install_tool_schema_load  # noqa: PLC0415

    install_tool_schema_load(registry)
    mode = disclosure_mode(environ)
    threshold = disclosure_threshold_pct(environ)
    plan = DisclosurePlan(
        mode=mode, allowed=allowed, window=window, threshold_pct=threshold,
        max_read_tools=max(0, int(max_read_tools)),
    )
    if allowed is None or not allowed:
        plan.reason = "unbounded surface"
        return plan
    if mode == "off":
        plan.reason = "mode off"
        return plan
    if not {"tool_search", "tool_invoke"} <= set(allowed):
        plan.reason = "discovery pair not bound"
        return plan

    eager = schema_tokens(allowed, registry)
    plan.schema_tokens = eager
    if mode == "auto":
        if window:
            if eager < int(window * threshold / 100):
                plan.reason = "below threshold"
                return plan
        elif eager < ABSOLUTE_SCHEMA_BUDGET_TOKENS:
            # No declared window and a small surface: the honest conservative
            # choice is to stay eager.
            plan.reason = "window unknown"
            return plan
        # An unknown window with an oversized schema falls through: narrowing a
        # read-only tail is reversible, a 100k-token schema is not.

    kept: set[str] = set()
    readable: list[Any] = []
    for name in sorted(allowed):
        tool = registry.get(name) if hasattr(registry, "get") else None
        if tool is None:
            kept.add(name)  # never silently drop a name the registry cannot explain
            continue
        if _must_bind(tool):
            kept.add(name)
            continue
        readable.append(tool)

    kept |= CORE_TOOLS & set(allowed)
    kept |= FOCUS_READ_TOOLS & set(allowed)
    kept |= CODE_FOCUS_READ_TOOLS & set(allowed)
    # Focus the REST of the read-only tail on this turn. The model keeps the
    # tools a data/research answer needs and reaches the long tail through the
    # existing tool_search/tool_invoke pair.
    remaining = [
        tool for tool in readable
        if str(getattr(tool, "name", "")) not in kept
    ]
    if remaining and plan.max_read_tools:
        if str(query or "").strip():
            ranked = rank_tools(query, remaining, plan.max_read_tools)
        else:
            ranked = sorted(remaining, key=lambda tool: str(getattr(tool, "name", "")))[
                : plan.max_read_tools
            ]
        kept |= {str(getattr(tool, "name", "")) for tool in ranked}

    hidden_tools = [
        tool for tool in readable
        if str(getattr(tool, "name", "")) not in kept
    ]
    if not hidden_tools:
        plan.reason = "nothing to hide"
        return plan

    tier, block = build_catalogue(hidden_tools)
    narrowed = kept & set(allowed)
    narrowed_tokens = schema_tokens(narrowed, registry)
    catalogue_tokens = int(estimate_tokens(block))
    plan.enabled = True
    plan.reason = "active"
    plan.tier = tier
    plan.allowed = narrowed
    plan.hidden = tuple(str(getattr(tool, "name", "")) for tool in hidden_tools)
    plan.advertised = tuple(sorted(narrowed))
    plan.catalogue_tokens = catalogue_tokens
    plan.catalogue_block = block
    plan.saved_tokens = max(0, eager - narrowed_tokens - catalogue_tokens)
    return plan


def apply_disclosure(
    allowed: Optional[set[str]],
    registry: Any,
    *,
    window: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> DisclosurePlan:
    """Convenience wrapper returning only the plan (the caller reads `.allowed`)."""
    return resolve_disclosure(allowed, registry, window=window, environ=environ)


def catalogue_payload(plan: DisclosurePlan) -> str:
    """A JSON-safe catalogue payload for tests/operators (not sent to the model)."""
    return json.dumps(plan.to_dict(), sort_keys=True)


__all__ = [
    "ABSOLUTE_SCHEMA_BUDGET_TOKENS",
    "CORE_TOOLS",
    "CODE_FOCUS_READ_TOOLS",
    "DEFAULT_MAX_READ_TOOLS",
    "DisclosurePlan",
    "FOCUS_READ_TOOLS",
    "TIER1_TOKENS",
    "TIER2_TOKENS",
    "TIER3_TOKENS",
    "apply_disclosure",
    "build_catalogue",
    "catalogue_payload",
    "disclosure_mode",
    "disclosure_threshold_pct",
    "resolve_disclosure",
    "schema_tokens",
]
