"""W3.1: contiguous read-only batches, ordering, isolation and kill switches."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from harness.retry_policy import RunDeadline
from harness.tool_guardrails import ToolGuardrail
from harness.tool_turn import ToolTurn, ToolTurnDeps, resolve_parallel_safe
from harness.tools import ToolContext, ToolDefinition, ToolRegistry


class _Store:
    def __init__(self):
        self.runs = {}

    def append_event(self, *args, **kwargs):
        return None

    def record_tool_execution(self, *args, **kwargs):
        return None

    def update_run(self, run_id, **kwargs):
        self.runs[run_id] = kwargs


class _Emitter:
    def __init__(self):
        self.events = []

    async def emit(self, name, payload):
        self.events.append((name, payload))


class _Gaps:
    def record_result(self, *args, **kwargs):
        return None


def _request():
    return SimpleNamespace(runId="run-1", taskId=None)


def _ctx(reg):
    async def authorize(_tool, _args, _ctx):
        return True

    return ToolContext(
        run_id="run-1", log=lambda *_: None, tool_registry=reg,
        authorize_effect=authorize,
    )


def _deps(reg, *, parallel="on", concurrency=4, guard=None, max_tool_calls=None, log=None):
    async def hook(*_args, **_kwargs):
        return None

    async def checkpoint(*_args, **_kwargs):
        return None

    async def step_completed(*_args, **_kwargs):
        return None

    return ToolTurnDeps(
        request=_request(), store=_Store(), emitter=_Emitter(), registry=reg,
        ctx=_ctx(reg), effects=None, lineage=None, plan=None, gaps=_Gaps(),
        gap_need="", deadline=RunDeadline(30), run_timeout_s=30,
        max_tool_calls=max_tool_calls, tool_timeout_ms=5_000,
        allowed=None, guard=guard or ToolGuardrail(mode="off"), hook=hook,
        write_checkpoint=checkpoint, emit_step_completed=step_completed,
        parallel_mode=parallel, concurrency=concurrency, batch_timeout_s=10,
    )


def _state(*calls):
    return {
        "messages": [AIMessage(content="", tool_calls=[
            {"name": name, "args": args, "id": call_id}
            for name, args, call_id in calls
        ])],
        "step": 1,
    }


def _add(reg, name, handler, *, permission="read", parallel_safe=True):
    reg.register(ToolDefinition(
        name=name, description=name,
        input_schema={"type": "object", "properties": {}},
        handler=handler, permission=permission, parallel_safe=parallel_safe,
    ))


def _message_texts(result):
    return [message.content for message in result["messages"][1:]]


def test_parallel_safe_requires_an_explicit_opt_in_and_read_permission():
    reg = ToolRegistry()
    _add(reg, "read_fast", lambda *_: None, parallel_safe=True)
    _add(reg, "read_unaudited", lambda *_: None, parallel_safe=None)
    _add(reg, "write_fast", lambda *_: None, permission="write", parallel_safe=True)
    assert resolve_parallel_safe(reg, "read_fast")
    assert not resolve_parallel_safe(reg, "read_unaudited")
    assert not resolve_parallel_safe(reg, "write_fast")


@pytest.mark.asyncio
async def test_six_read_calls_run_concurrently_and_keep_their_order():
    reg = ToolRegistry()

    def make(index):
        async def handler(_args, _ctx):
            await asyncio.sleep(0.15)
            return f"result-{index}"
        return handler

    for index in range(6):
        _add(reg, f"read_{index}", make(index))

    deps = _deps(reg, parallel="on", concurrency=6)
    state = _state(*[(f"read_{i}", {}, f"c{i}") for i in range(6)])
    start = time.monotonic()
    result = await ToolTurn(deps).run(state)
    elapsed = time.monotonic() - start

    assert elapsed < 0.45  # six serial 150 ms calls would be 0.9 s
    assert _message_texts(result) == [f"result-{i}" for i in range(6)]
    assert any(name == "tool.batch_plan" for name, _ in deps.emitter.events)


@pytest.mark.asyncio
async def test_concurrency_one_reproduces_serial_execution():
    reg = ToolRegistry()
    order = []

    def make(index):
        async def handler(_args, _ctx):
            order.append(("start", index))
            await asyncio.sleep(0.05)
            order.append(("end", index))
            return f"r{index}"
        return handler

    for index in range(3):
        _add(reg, f"read_{index}", make(index))

    deps = _deps(reg, parallel="on", concurrency=1)
    state = _state(*[(f"read_{i}", {}, f"c{i}") for i in range(3)])
    result = await ToolTurn(deps).run(state)
    assert order == [
        ("start", 0), ("end", 0), ("start", 1), ("end", 1), ("start", 2), ("end", 2),
    ]
    assert _message_texts(result) == ["r0", "r1", "r2"]


@pytest.mark.asyncio
async def test_an_effectful_call_breaks_the_batch_and_never_overlaps():
    reg = ToolRegistry()
    order = []

    async def read_a(_args, _ctx):
        order.append("start-a")
        await asyncio.sleep(0.05)
        order.append("end-a")
        return "a"

    async def write_b(_args, _ctx):
        assert "end-a" in order, "write started before the preceding read finished"
        order.append("start-b")
        order.append("end-b")
        return "b"

    async def read_c(_args, _ctx):
        assert "end-b" in order, "read after a write started before the write finished"
        order.append("start-c")
        order.append("end-c")
        return "c"

    _add(reg, "read_a", read_a)
    _add(reg, "write_b", write_b, permission="write", parallel_safe=True)
    _add(reg, "read_c", read_c)

    deps = _deps(reg, parallel="on", concurrency=4)
    state = _state(("read_a", {}, "c1"), ("write_b", {}, "c2"), ("read_c", {}, "c3"))
    result = await ToolTurn(deps).run(state)
    assert order == ["start-a", "end-a", "start-b", "end-b", "start-c", "end-c"]
    assert _message_texts(result) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_one_failed_read_is_isolated_and_the_order_is_preserved():
    reg = ToolRegistry()

    async def ok(_args, _ctx):
        await asyncio.sleep(0.02)
        return "ok"

    async def boom(_args, _ctx):
        raise RuntimeError("provider exploded")

    _add(reg, "read_ok_1", ok)
    _add(reg, "read_boom", boom)
    _add(reg, "read_ok_2", ok)

    deps = _deps(reg, parallel="on", concurrency=3)
    state = _state(
        ("read_ok_1", {}, "c1"), ("read_boom", {}, "c2"), ("read_ok_2", {}, "c3"),
    )
    result = await ToolTurn(deps).run(state)
    texts = _message_texts(result)
    assert texts[0] == "ok"
    assert "provider exploded" in texts[1]
    assert texts[2] == "ok"
    assert result["tool_errors"] == 1


@pytest.mark.asyncio
async def test_shadow_mode_plans_but_executes_serially():
    reg = ToolRegistry()
    order = []

    def make(index):
        async def handler(_args, _ctx):
            order.append(index)
            return str(index)
        return handler

    for index in range(3):
        _add(reg, f"read_{index}", make(index))

    deps = _deps(reg, parallel="shadow")
    state = _state(*[(f"read_{i}", {}, f"c{i}") for i in range(3)])
    result = await ToolTurn(deps).run(state)
    assert order == [0, 1, 2]
    plan = next(payload for name, payload in deps.emitter.events if name == "tool.batch_plan")
    assert plan["batches"] == [[0, 1, 2]]


@pytest.mark.asyncio
async def test_budget_refusals_happen_in_order_without_dispatch():
    reg = ToolRegistry()
    calls = []

    async def handler(_args, _ctx):
        calls.append(1)
        return "ok"

    for index in range(3):
        _add(reg, f"read_{index}", handler)

    deps = _deps(reg, parallel="on", max_tool_calls=2)
    state = _state(*[(f"read_{i}", {}, f"c{i}") for i in range(3)])
    result = await ToolTurn(deps).run(state)
    assert len(calls) == 2
    assert "maximum of 2 tool calls" in _message_texts(result)[2]


@pytest.mark.asyncio
async def test_guardrail_halt_stops_the_turn_with_a_controlled_final():
    reg = ToolRegistry()

    async def handler(_args, _ctx):
        return "same result"

    _add(reg, "read_same", handler)
    guard = ToolGuardrail(mode="halt")
    deps = _deps(reg, parallel="off", guard=guard)

    for index in range(2):
        await ToolTurn(deps).run(_state(("read_same", {"x": 1}, f"c{index}")))

    third = await ToolTurn(deps).run(_state(("read_same", {"x": 1}, "c9")))
    assert third["guardrail_halt"]["rule"] == "identical_call"
    assert "Stopped to avoid a tool loop" in third["final"]
    assert not any(
        name == "tool.completed" and payload.get("toolCallId") == "c9"
        for name, payload in deps.emitter.events
    )


@pytest.mark.asyncio
async def test_a_serial_tool_is_bounded_by_the_run_deadline():
    """A serial (non-parallel-safe) call has an outer deadline.

    The batch budget used to apply only to parallel-safe groups, so one hung
    serial tool left the run silent until the stall watchdog (observed
    2026-11-26). The run's remaining time is now the backstop.
    """
    reg = ToolRegistry()

    async def slow(_args, _ctx):
        await asyncio.sleep(5)
        return "late"

    _add(reg, "slow_read", slow, permission="read", parallel_safe=False)
    deps = _deps(reg, parallel="on")
    deps.deadline = RunDeadline(0.15)

    start = time.monotonic()
    result = await ToolTurn(deps).run(_state(("slow_read", {}, "c0")))
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert "timed out" in _message_texts(result)[0].lower()
