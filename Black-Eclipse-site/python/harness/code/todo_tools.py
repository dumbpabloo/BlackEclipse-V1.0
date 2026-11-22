"""`code_todo` — the coding plan the model maintains (gap #8).

One tool, the whole list: `todos: [{content, status}]`. It is the analogue of
OpenCode's `todowrite`: the model replaces the list as work moves, the list is
persisted per RUN, and the graph injects it at the next step boundary when it
changed. It is read-permission because it changes only the plan, not the world;
`plan_only` keeps it, and it is in the coding surface preset.
"""

from __future__ import annotations

from typing import Any

from ..tools import ToolContext, ToolDefinition, ToolRegistry
from . import todos as todo_mod

TOOL_NAME = "code_todo"


def _definition() -> ToolDefinition:
    async def _handler(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
        try:
            items = todo_mod.normalize(args.get("todos") or [])
        except ValueError as exc:
            return f"REFUSED: {exc}", {"status": "refused", "reason": str(exc)}
        store = getattr(ctx, "memory_store", None)
        run_id = str(getattr(ctx, "run_id", "") or "")
        stored = todo_mod.replace(store, run_id, items)
        summary = todo_mod.summarize(stored)
        from .tools import emit_code_event  # noqa: PLC0415 - avoid a module cycle

        await emit_code_event(ctx, "code.todo.updated", {
            "runId": run_id,
            "todos": [t.to_dict() for t in stored],
            "summary": summary,
            "version": todo_mod.version(stored),
        })
        if not stored:
            return "Plan cleared.", {"status": "ok", "todos": [], "summary": summary}
        counts = summary["counts"]
        head = (
            f"Plan updated: {counts.get('completed', 0)}/{summary['total']} done"
            + (f", {counts.get('in_progress', 0)} in progress" if counts.get("in_progress") else "")
            + (f", {counts.get('pending', 0)} pending" if counts.get("pending") else "")
        )
        return head, {"status": "ok", "todos": [t.to_dict() for t in stored], "summary": summary}

    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Record or update the plan for this coding task: the whole list of steps, "
            "each with a status (pending, in_progress, completed, cancelled). Use it for "
            "multi-step work so the plan survives the whole task and the user can see it. "
            "Send the FULL list each time; it replaces the previous one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "maxLength": todo_mod.MAX_CONTENT_CHARS},
                            "status": {"type": "string", "enum": list(todo_mod.STATUSES)},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
        handler=_handler,
        permission="read",
        risk="none",
        timeout_ms=5_000,
        effect=None,
    )


def register_todo_tools(registry: ToolRegistry) -> list[str]:
    registry.register(_definition())
    return [TOOL_NAME]


__all__ = ["TOOL_NAME", "register_todo_tools"]
