"""Gap #8: the coding plan survives steps and is shown to the model.

The defect this gates: a multi-step coding run had no todo list, so the model
re-derived its plan every turn and the user could not see it. The contract:

1. `code_todo` replaces the whole list and persists it per RUN (`CodeTodo`).
2. The list survives a new step: it is loaded from the store, not from the
   conversation.
3. The graph injects the plan at a step boundary ONLY when it changed, as a
   DATA-labelled `<coding-plan>` block - the model sees its own list.
4. A bad status is refused with a reason, not silently stored.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from langchain_core.messages import HumanMessage

from harness.code import todos as todo_mod
from harness.code import todo_tools
from harness.persistence import AgentMemoryStore
from harness.tools import ToolContext, ToolRegistry


@pytest.fixture()
def store():
    directory = tempfile.mkdtemp(prefix="be-todos-")
    s = AgentMemoryStore(os.path.join(directory, "agent-memory.db"))
    yield s
    s.close()


def _ctx(store, run_id: str = "run-todo") -> ToolContext:
    return ToolContext(run_id=run_id, log=lambda *_: None, memory_store=store)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    todo_tools.register_todo_tools(reg)
    return reg


def _todos(*items):
    return [{"content": text, "status": status} for text, status in items]


def test_the_tool_persists_the_whole_list_and_returns_it(store):
    reg = _registry()
    ctx = _ctx(store)

    first = asyncio.run(reg.execute(
        "code_todo", "c1",
        {"todos": _todos(("read the parser", "completed"), ("fix the token", "in_progress"))},
        ctx,
    ))
    assert first.ok
    assert "1/2 done" in first.text

    # A SECOND step loads it from the store, not from the conversation.
    loaded = todo_mod.load(store, "run-todo")
    assert [t.content for t in loaded] == ["read the parser", "fix the token"]
    assert [t.status for t in loaded] == ["completed", "in_progress"]

    # The list is REPLACED, not appended.
    asyncio.run(reg.execute(
        "code_todo", "c2",
        {"todos": _todos(("run the tests", "pending"))},
        ctx,
    ))
    final = todo_mod.load(store, "run-todo")
    assert [t.content for t in final] == ["run the tests"]


def test_the_plan_is_injected_once_and_reinjected_only_when_it_changes(store):
    reg = _registry()
    ctx = _ctx(store)
    asyncio.run(reg.execute(
        "code_todo", "c1",
        {"todos": _todos(("step one", "completed"), ("step two", "pending"))},
        ctx,
    ))

    state = {"messages": [HumanMessage(content="start")], "w7_pending": []}
    state = todo_mod.plan_message(state, store, "run-todo")
    assert len(state["messages"]) == 2
    plan = state["messages"][-1].content
    assert plan.startswith("<coding-plan>")
    assert "DATA" in plan
    assert "[x] step one" in plan
    assert "[ ] step two" in plan

    # Calling again without a change does not duplicate the block.
    again = todo_mod.plan_message(state, store, "run-todo")
    assert len(again["messages"]) == 2

    # A changed list is injected as a new message at the next boundary.
    asyncio.run(reg.execute(
        "code_todo", "c2",
        {"todos": _todos(("step one", "completed"), ("step two", "in_progress"))},
        ctx,
    ))
    changed = todo_mod.plan_message(state, store, "run-todo")
    assert len(changed["messages"]) == 3
    assert "[~] step two" in changed["messages"][-1].content


def test_an_invalid_status_is_refused(store):
    reg = _registry()
    result = asyncio.run(reg.execute(
        "code_todo", "c1",
        {"todos": [{"content": "x", "status": "doing"}]},
        _ctx(store),
    ))
    assert result.ok
    assert "REFUSED" in result.text
    assert todo_mod.load(store, "run-todo") == []


def test_clearing_the_plan_is_explicit(store):
    reg = _registry()
    ctx = _ctx(store)
    asyncio.run(reg.execute("code_todo", "c1", {"todos": _todos(("x", "pending"))}, ctx))
    cleared = asyncio.run(reg.execute("code_todo", "c2", {"todos": []}, ctx))
    assert cleared.text == "Plan cleared."
    assert todo_mod.load(store, "run-todo") == []


def test_the_plan_is_in_the_coding_surface():
    from harness.code import policy

    assert "code_todo" in policy.CODING_SURFACE
    assert "code_todo" in policy.coding_surface("plan_only")
