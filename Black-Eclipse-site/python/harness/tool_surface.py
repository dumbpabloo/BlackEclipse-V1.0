"""Tool discovery + a safe dispatcher (Round 5 / C3).

Eclipse binds hundreds of tools into every run. The fix is not "give the model
fewer capabilities"; it is "make the capability discoverable, and bind less".

  * `tool_search` returns the matching tools with their schemas. It is read-only.
  * `tool_invoke` runs a discovered READ-ONLY tool by name. Effectful tools are
    refused here on purpose: the graph's tools node owns budget, duplicate
    suppression, idempotency, authorisation and recovery for those, and a second
    execution path around that seam would be unsafe.
  * `lazy_tool_surface` is the opt-in binding policy (`HARNESS_LAZY_TOOLS=1`):
    every effectful tool stays bound; the read-only long tail is reachable via
    search/invoke. Default OFF until eval parity, as the plan requires.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from .tool_bm25 import rank_tools
from .tool_disclosure import CORE_TOOLS, disclosure_mode
from .code.policy import CODING_EXTRA_ENV

# Always bound when disclosure narrows: the discovery pair, the output pager, the
# response-contract read-back and the clarify gate. Re-exported for compatibility
# with the Round 5 / C3 tests; the ONE definition lives in tool_disclosure.py.
LAZY_CORE_TOOLS = CORE_TOOLS

#: A discovery response is bounded so it cannot become the new context flood.
MAX_SEARCH_RESULTS = 12
MAX_SEARCH_SCHEMA_CHARS = 18_000


def lazy_tools_enabled() -> bool:
    """The legacy lazy switch: explicit `on` only.

    `auto` is resolved by `tool_disclosure.resolve_disclosure`, which needs the
    measured schema cost and window. `lazy_tool_surface` keeps the old meaning
    (explicit opt-in) so an existing caller cannot accidentally auto-narrow.
    """
    return disclosure_mode() == "on"


def lazy_tool_surface(
    allowed: Optional[set[str]], registry: Any,
) -> Optional[set[str]]:
    """Narrow a run's surface to effectful tools + the discovery core.

    Effectful tools are NEVER hidden: they must stay on the graph's execution
    path. Read-only tools outside the core become discoverable through
    `tool_search` and callable through `tool_invoke`. Returns `allowed` unchanged
    when the flag is off or the surface is unbounded.
    """
    if not lazy_tools_enabled() or allowed is None:
        return allowed
    kept: set[str] = set()
    for name in allowed:
        tool = registry.get(name) if hasattr(registry, "get") else None
        if tool is None or getattr(tool, "permission", "read") != "read":
            kept.add(name)
    return kept | (LAZY_CORE_TOOLS & set(allowed))


def _tool_row(tool: Any, *, include_schema: bool) -> dict[str, Any]:
    row = {
        "name": getattr(tool, "name", ""),
        "description": str(getattr(tool, "description", ""))[:300],
        "permission": getattr(tool, "permission", "read"),
    }
    if getattr(tool, "domain", ""):
        row["domain"] = getattr(tool, "domain", "")
    if getattr(tool, "tags", ()):
        row["tags"] = list(getattr(tool, "tags", ()))
    if include_schema:
        row["parameters"] = getattr(tool, "input_schema", {})
    return row


def _available_sources(tool_names: list[str], target: Any) -> list[dict[str, Any]]:
    """Domain/count summary for a no-match response. Never invents a tool."""
    counts: dict[str, int] = {}
    for name in sorted(tool_names):
        tool = target.get(name) if hasattr(target, "get") else None
        domain = str(getattr(tool, "domain", "") or "other")
        counts[domain] = counts.get(domain, 0) + 1
    return [
        {"domain": domain, "tools": count}
        for domain, count in sorted(counts.items())
    ]


def register_tool_surface_tools(registry: Any) -> None:
    from .tools import ToolDefinition  # noqa: PLC0415 - avoid a module cycle

    async def _search(args: dict[str, Any], ctx: Any) -> tuple[str, dict[str, Any]]:
        target = getattr(ctx, "tool_registry", None) or registry
        surface = getattr(ctx, "allowed", None)
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 8), MAX_SEARCH_RESULTS))
        if not query:
            return (
                "Give a query: a tool name, a task word (e.g. 'options'), or a domain.",
                {"status": "INVALID_INPUT"},
            )

        names = list(getattr(target, "names", lambda: [])())
        tools = []
        for name in names:
            tool = target.get(name)
            if tool is not None:
                tools.append(tool)
        # Search the run's OWN surface first; then show matching tools outside it
        # as DISCOVERY only. A coding run sees that `container_run` exists and is
        # one operator enable away, without binding its schema or executing it.
        inside = [t for t in tools if surface is None or t.name in surface]
        outside = [t for t in tools if surface is not None and t.name not in surface]
        inside_ranked = rank_tools(query, inside, len(inside))
        outside_ranked = rank_tools(query, outside, len(outside)) if outside else []
        selected = inside_ranked[:limit]
        if len(selected) < limit and outside_ranked:
            selected.extend(outside_ranked[: limit - len(selected)])

        def _row(tool: Any) -> dict[str, Any]:
            row = _tool_row(tool, include_schema=True)
            if surface is not None and tool.name not in surface:
                row["outsideSurface"] = True
                row["requiresEnable"] = True
                row["enableHint"] = (
                    f"{tool.name} is not in this run's coding surface. An operator can "
                    f"enable it with {CODING_EXTRA_ENV} before the run starts; the model "
                    "cannot widen its own surface."
                )
            return row

        include_schema = True
        payload = {"tools": [_row(tool) for tool in selected]}
        if len(json.dumps(payload, default=str)) > MAX_SEARCH_SCHEMA_CHARS:
            include_schema = False
            payload = {
                "tools": [_tool_row(tool, include_schema=False) for tool in selected],
                "schemasOmitted": True,
            }
        payload["matchCount"] = len(inside_ranked) + len(outside_ranked)
        payload["returned"] = len(selected)
        if not selected:
            payload["availableSources"] = _available_sources(names, target)
            payload["hint"] = (
                "No match. Try a broader task word, or use `availableSources` to "
                "pick a domain."
            )
        return json.dumps(payload, default=str), {"status": "OK", **payload}

    async def _invoke(args: dict[str, Any], ctx: Any) -> tuple[str, dict[str, Any]]:
        target = getattr(ctx, "tool_registry", None) or registry
        name = str(args.get("name") or "").strip()
        raw_args = args.get("arguments")
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                return (
                    "INVALID_INPUT: `arguments` must be a JSON object.",
                    {"status": "INVALID_INPUT"},
                )
        call_args = dict(raw_args) if isinstance(raw_args, dict) else {}

        if name in ("tool_invoke",):
            return (
                "REFUSED: tool_invoke cannot call itself.",
                {"status": "REFUSED", "reason": "recursive dispatch"},
            )
        surface = getattr(ctx, "allowed", None)
        if surface is not None and name not in surface:
            return (
                f"REFUSED: {name!r} is outside the surface of this run. "
                "Call tool_search for an allowed tool; an operator can enable a "
                "removed tool before the run starts.",
                {"status": "REFUSED", "name": name, "reason": "outside the surface"},
            )
        tool = target.get(name) if hasattr(target, "get") else None
        if tool is None:
            return (
                f"TOOL_NOT_FOUND: no tool named {name!r}. Call tool_search first.",
                {"status": "NOT_FOUND", "name": name},
            )
        if getattr(tool, "permission", "read") != "read":
            return (
                f"REFUSED: {name!r} is effectful ({getattr(tool, 'permission', '?')}). "
                "Call it directly so the run's budget, approval and idempotency "
                "seam apply.",
                {"status": "REFUSED", "name": name, "reason": "effectful tool"},
            )
        result = await target.execute(
            name, f"invoke_{uuid.uuid4().hex[:10]}", call_args, ctx,
            allowed=surface,
        )
        if not result.ok:
            error = getattr(result, "error", None)
            code = getattr(error, "code", "TOOL_ERROR")
            return f"TOOL_ERROR ({code}): {result.text}", {
                "status": "ERROR", "name": name, "errorClass": str(code),
            }
        return result.text, {
            "status": "OK",
            "name": name,
            "truncated": bool(getattr(result, "truncated", False)),
            "outputId": getattr(result, "output_id", None),
        }

    registry.register(ToolDefinition(
        name="tool_search",
        description=(
            "Find a tool by name, task word or domain and get its exact parameters. "
            "Use this before guessing a tool name. Returns up to 12 matches."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool name, task word or domain."},
                "limit": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
        handler=_search,
        permission="read",
        risk="none",
        timeout_ms=5_000,
        parallel_safe=True,
    ))
    registry.register(ToolDefinition(
        name="tool_invoke",
        description=(
            "Run a READ-ONLY tool discovered with tool_search, by name and exact "
            "arguments. Effectful tools are refused here; call those directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "arguments": {"type": "object", "description": "The tool's exact arguments."},
            },
            "required": ["name"],
        },
        handler=_invoke,
        permission="read",
        risk="low",
        timeout_ms=120_000,
    ))


__all__ = [
    "LAZY_CORE_TOOLS",
    "MAX_SEARCH_RESULTS",
    "lazy_tool_surface",
    "lazy_tools_enabled",
    "register_tool_surface_tools",
]
