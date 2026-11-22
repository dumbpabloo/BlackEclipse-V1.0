"""Read-only runtime inspection (`harness_inspect`).

An agent (or a human) can discover the live services, agent catalog, tool
scopes, prompt scopes and plugin-provided services before writing a plugin.
It never invokes a method, reads a secret or returns source code.
"""

from __future__ import annotations

import json
from typing import Any

from ..tools import ToolDefinition, ToolRegistry

TOOL_NAME = "harness_inspect"


async def _handler(args: dict[str, Any], ctx: Any) -> str:
    from .services.runtime import kernel_runtime  # noqa: PLC0415

    payload = kernel_runtime.inspect()
    section = str((args or {}).get("section") or "").strip()
    if section:
        if section not in payload:
            return json.dumps(
                {"error": f"unknown section {section!r}", "sections": sorted(payload)},
                default=str,
            )
        payload = {section: payload[section]}
    return json.dumps(payload, indent=2, default=str, sort_keys=True)


TOOL = ToolDefinition(
    name=TOOL_NAME,
    description=(
        "Read the live harness composition: kernel status, services, agent catalog, "
        "tool/prompt scopes, skills and plugin-provided services. Read-only; never "
        "invokes a method or returns a secret."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "Optional section: enabled, services, agents, tools, skills, provided",
            },
        },
    },
    handler=_handler,
    permission="read",
    risk="low",
    timeout_ms=5_000,
    version="1.0.0",
)


def register_inspect_tool(registry: ToolRegistry) -> None:
    if registry.get(TOOL_NAME) is not None:
        return
    registry.register(TOOL)
