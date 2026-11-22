"""The manual research-task board's rules, without a database.

A shared board is only useful if its invariants hold: a finished task cannot be handed
to someone else, recording completion twice cannot rewrite the result that was
recorded first, and an agent's load is derived from the tasks rather than maintained by
hand. Those are pure functions over two lists, so they live here; the store that
persists them is elsewhere.

Ported from Vibe-Trading's `pixel_agents/office.py` (MIT), minus its SQLite layer. The
vocabularies and limits are that module's, and the three invariants are the ones a naive
board gets wrong.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "MAX_ID_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_TITLE_CHARS",
    "PRIORITIES",
    "RANKS",
    "assign",
    "complete",
    "dashboard",
    "validate_priority",
    "validate_rank",
    "validate_text",
]

#: Field limits, matching the reference's `_text` bounds.
MAX_ID_CHARS = 100
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_RESULT_CHARS = 2_000

PRIORITIES = ("low", "normal", "high")
RANKS = ("Operator", "Senior", "Lead")


class BoardError(ValueError):
    """A board rule was violated; the message names which."""


def validate_priority(priority: Any) -> str:
    value = str(priority or "").strip().lower()
    if value not in PRIORITIES:
        raise BoardError("priority must be low, normal or high")
    return value


def validate_rank(rank: Any) -> str:
    value = str(rank or "").strip()
    if value not in RANKS:
        raise BoardError("rank must be Operator, Senior or Lead")
    return value


def validate_text(value: Any, field: str, *, maximum: int, empty: bool = False) -> str:
    """Bound one text field; ``empty=True`` allows a blank (description) field."""
    text = "" if value is None else str(value)
    if len(text) > maximum:
        raise BoardError(f"{field} must be 0 to {maximum} characters")
    stripped = text.strip()
    if not empty and not stripped:
        raise BoardError(f"{field} must be 1 to {maximum} characters")
    return stripped


def assign(
    task: Mapping[str, Any],
    agents: Sequence[Mapping[str, Any]],
    *,
    agent_id: Optional[str] = None,
    now: str = "",
) -> dict[str, Any]:
    """Assign or reassign ONE unfinished task; return the updated copy.

    Invariants, in the reference's order:

    * a **completed** task cannot be assigned — its owner is history, not a slot;
    * with no ``agent_id``, the first agent whose ``role`` matches the task's
      ``workstream`` is chosen (the reference's auto-assign);
    * an unknown agent is refused with the actionable message rather than silently
      leaving the task unassigned.
    """
    record = copy.deepcopy(dict(task))
    if str(record.get("status")) == "completed":
        raise BoardError("completed tasks cannot be assigned")

    by_id = {str(agent.get("id")): agent for agent in agents}
    chosen = agent_id
    if chosen is None:
        workstream = str(record.get("workstream") or "")
        chosen = next(
            (str(agent.get("id")) for agent in agents
             if str(agent.get("role") or "") == workstream),
            None,
        )
    if chosen is None or chosen not in by_id:
        raise BoardError("no matching agent; choose an agent explicitly")

    record["assigned_to"] = chosen
    record["status"] = "assigned"
    if now:
        record["updated_at"] = now
    return record


def complete(
    task: Mapping[str, Any], result: Any, *, now: str = ""
) -> dict[str, Any]:
    """Record completion; a second call **preserves the first result**.

    The reference's rule, and the reason for it: a re-completion is usually a retry or
    a duplicate tool call, and letting it overwrite would silently replace the answer
    that was actually produced.
    """
    record = copy.deepcopy(dict(task))
    text = validate_text(result, "result", maximum=MAX_RESULT_CHARS)
    if str(record.get("status")) == "completed":
        return record
    record["status"] = "completed"
    record["result"] = text
    if now:
        record["updated_at"] = now
    return record


def dashboard(
    agents: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """One coherent snapshot with each agent's load **derived** from the tasks.

    ``load`` counts assigned, not-completed tasks; ``status`` is ``busy`` when that
    count is non-zero. Nothing here is stored, so the numbers cannot drift from the
    tasks they describe.
    """
    rendered = [copy.deepcopy(dict(agent)) for agent in agents]
    task_rows = [dict(task) for task in tasks]
    for agent in rendered:
        agent_id = str(agent.get("id"))
        agent["load"] = sum(
            1 for task in task_rows
            if str(task.get("assigned_to")) == agent_id
            and str(task.get("status")) != "completed"
        )
        agent["status"] = "busy" if agent["load"] else "idle"
    completed = sum(1 for task in task_rows if str(task.get("status")) == "completed")
    return {
        "agents": rendered,
        "tasks": task_rows,
        "agentCount": len(rendered),
        "taskCount": len(task_rows),
        "completed": completed,
        "pending": len(task_rows) - completed,
        "mode": "manual_planning",
    }
