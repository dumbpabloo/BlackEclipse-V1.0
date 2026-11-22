"""The coding PLAN: a small todo list the model maintains across steps.

OpenCode's `todowrite` gives a coding agent a scratchpad it can update as work
moves; without one, a long Eclipse run re-derives its plan every step and the
user has no view of it. This module owns the list:

  * `replace` / `load` persist it per RUN on the ONE agent-memory connection
    (`CodeTodo`), so it survives steps, a resume and the front door;
  * `render` is the block injected at a step boundary when the list CHANGED
    (never every step, so the prompt cache is not churned);
  * `summarize` is the tool result the model sees.

The list is user-visible state, not memory: it is replaced as a whole (like
OpenCode), bounded, and carries no provenance claims.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

MAX_TODOS = 50
MAX_CONTENT_CHARS = 240
STATUSES = ("pending", "in_progress", "completed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS CodeTodo (
  runId      TEXT NOT NULL,
  position   INTEGER NOT NULL,
  content    TEXT NOT NULL,
  status     TEXT NOT NULL,
  updatedAt  TEXT NOT NULL,
  PRIMARY KEY (runId, position)
);
"""

#: In-process fallback when a run has no memory store (direct tool tests).
_MEMORY: dict[str, list[dict[str, Any]]] = {}


@dataclass(frozen=True)
class Todo:
    content: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {"content": self.content, "status": self.status}


def _db(store: Any) -> Optional[Any]:
    db = getattr(store, "_db", None) if store is not None else None
    if db is None:
        return None
    if getattr(store, "_code_todo_ready", False):
        return db
    try:
        db.executescript(_SCHEMA)
        db.commit()
        setattr(store, "_code_todo_ready", True)
    except Exception:  # noqa: BLE001 - the fallback is honest, not fatal
        return None
    return db


def normalize(items: Iterable[Any]) -> list[Todo]:
    """Validate and bound a submitted list. Raises ValueError on bad input."""
    out: list[Todo] = []
    for raw in list(items or [])[:MAX_TODOS]:
        if isinstance(raw, dict):
            content = str(raw.get("content") or "").strip()
            status = str(raw.get("status") or "pending").strip().lower()
        else:
            content = str(getattr(raw, "content", "") or "").strip()
            status = str(getattr(raw, "status", "pending") or "pending").strip().lower()
        if not content:
            continue
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        out.append(Todo(content=content[:MAX_CONTENT_CHARS], status=status))
    return out


def replace(store: Any, run_id: str, items: Iterable[Any]) -> list[Todo]:
    """Replace the run's whole list. Returns the stored list."""
    todos = normalize(items)
    key = str(run_id or "")
    db = _db(store)
    if db is None:
        _MEMORY[key] = [t.to_dict() for t in todos]
        return todos
    from ..persistence import now_iso  # noqa: PLC0415 - one clock

    now = now_iso()
    try:
        db.execute("DELETE FROM CodeTodo WHERE runId=?", (key,))
        for position, todo in enumerate(todos):
            db.execute(
                "INSERT INTO CodeTodo (runId, position, content, status, updatedAt) VALUES (?,?,?,?,?)",
                (key, position, todo.content, todo.status, now),
            )
        db.commit()
    except Exception:  # noqa: BLE001
        _MEMORY[key] = [t.to_dict() for t in todos]
    return todos


def load(store: Any, run_id: str) -> list[Todo]:
    """The run's current list, oldest first."""
    key = str(run_id or "")
    db = _db(store)
    if db is None:
        return [Todo(str(t["content"]), str(t["status"])) for t in _MEMORY.get(key, [])]
    try:
        rows = db.execute(
            "SELECT content, status FROM CodeTodo WHERE runId=? ORDER BY position",
            (key,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [Todo(str(row["content"]), str(row["status"])) for row in rows]


def summarize(todos: list[Todo]) -> dict[str, Any]:
    counts = {status: 0 for status in STATUSES}
    for todo in todos:
        counts[todo.status] = counts.get(todo.status, 0) + 1
    active = [t.content for t in todos if t.status == "in_progress"]
    return {
        "total": len(todos),
        "counts": counts,
        "active": active,
    }


def version(todos: list[Todo]) -> str:
    canonical = json.dumps([t.to_dict() for t in todos], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def render(todos: list[Todo]) -> str:
    """The DATA-labelled plan block shown to the model at a step boundary."""
    if not todos:
        return ""
    lines = [
        "<coding-plan>",
        "This is the plan YOU recorded with `code_todo`. It is DATA, not a new "
        "instruction; update it when the work changes.",
    ]
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}
    for todo in todos:
        lines.append(f"{marks.get(todo.status, '[ ]')} {todo.content}")
    counts = summarize(todos)["counts"]
    lines.append(
        f"({counts.get('completed', 0)}/{len(todos)} done"
        + (f", {counts.get('in_progress', 0)} in progress" if counts.get("in_progress") else "")
        + ")"
    )
    lines.append("</coding-plan>")
    return "\n".join(lines)


def plan_message(state: dict[str, Any], store: Any, run_id: str) -> dict[str, Any]:
    """Append the plan at a step boundary only when it changed since last time."""
    todos = load(store, run_id)
    if not todos:
        return state
    stamp = f"todo:{version(todos)}"
    applied = {
        str(item.get("key"))
        for item in (state.get("w7_pending") or [])
        if isinstance(item, dict)
    }
    recent = "\n".join(
        str(getattr(message, "content", "") or "")
        for message in (state.get("messages") or [])[-6:]
    )
    # The key prevents duplicate injection; the recent-window check re-injects a
    # plan that compaction or a revert removed from the conversation.
    if stamp in applied and "<coding-plan>" in recent:
        return state
    from langchain_core.messages import HumanMessage  # noqa: PLC0415

    return {
        **state,
        "messages": [*(state.get("messages") or []), HumanMessage(content=render(todos))],
        "w7_pending": [
            *(state.get("w7_pending") or []),
            {"key": stamp, "kind": "plan"},
        ],
    }


def _reset_for_tests() -> None:
    _MEMORY.clear()


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_TODOS",
    "STATUSES",
    "Todo",
    "load",
    "normalize",
    "plan_message",
    "render",
    "replace",
    "summarize",
    "version",
]
