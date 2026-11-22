"""Gap #12: effectful tools through Code Mode, behind the SAME approval chain.

Code Mode used to refuse every effectful tool. A program can now call one when
the run has an effect authorizer; `registry.execute` then applies the run's
surface, the authorizer (and its approval card), the effect token and the
widening cap. No authorizer → refusal, never a bypass.
"""

from __future__ import annotations

import asyncio

import pytest

from harness import code_mode
from harness.tools import ToolContext, ToolDefinition, ToolRegistry

CALLED: list[str] = []


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def write_handler(args, _ctx):
        CALLED.append(str(args.get("value") or ""))
        return "wrote", {"status": "ok", "value": args.get("value")}

    async def read_handler(_args, _ctx):
        return "read-ok", {"status": "ok"}

    reg.register(ToolDefinition(
        name="write_x", description="write", input_schema={
            "type": "object", "properties": {"value": {"type": "string"}},
        },
        handler=write_handler, permission="write", risk="medium", effect="write",
        timeout_ms=5_000,
    ))
    reg.register(ToolDefinition(
        name="read_x", description="read", input_schema={"type": "object", "properties": {}},
        handler=read_handler, permission="read", risk="none",
    ))
    async def code_handler(_args, _ctx):
        return "code", {"status": "ok"}

    reg.register(ToolDefinition(
        name="tool_code", description="code", input_schema={"type": "object", "properties": {}},
        handler=code_handler, permission="read", risk="low",
    ))
    return reg


def _ctx(reg: ToolRegistry, *, authorize=None) -> ToolContext:
    ctx = ToolContext(
        run_id="cm-run", log=lambda *_: None,
        allowed={"write_x", "read_x", "tool_code"},
    )
    ctx.tool_registry = reg
    if authorize is not None:
        ctx.authorize_effect = authorize
    return ctx


def _answer(ctx, name, args=None, call=1):
    async def scenario():
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            code_mode._answer_tool_call, ctx, loop, name, args or {}, 10.0, call,
        )
    return asyncio.run(scenario())


@pytest.fixture(autouse=True)
def _clear():
    CALLED.clear()


def test_an_effectful_call_is_refused_without_an_authorizer():
    reg = _registry()
    reply = _answer(_ctx(reg), "write_x", {"value": "nope"})
    assert reply["ok"] is False
    assert "no approval chain" in reply["error"]
    assert CALLED == []


def test_an_effectful_call_is_refused_when_the_authorizer_declines():
    reg = _registry()

    async def decline(*_args, **_kwargs):
        return False

    reply = _answer(_ctx(reg, authorize=decline), "write_x", {"value": "nope"})
    assert reply["ok"] is False
    assert "not approved" in reply["error"] or "TOOL_FORBIDDEN" in reply["error"]
    assert CALLED == []


def test_an_effectful_call_runs_when_the_authorizer_approves():
    reg = _registry()

    async def approve(*_args, **_kwargs):
        return True

    reply = _answer(_ctx(reg, authorize=approve), "write_x", {"value": "yes"})
    assert reply["ok"] is True
    assert reply["text"] == "wrote"
    assert reply["data"]["value"] == "yes"
    assert CALLED == ["yes"]


def test_a_read_only_call_needs_no_authorizer():
    reg = _registry()
    reply = _answer(_ctx(reg), "read_x")
    assert reply["ok"] is True
    assert reply["text"] == "read-ok"


def test_a_tool_outside_the_run_surface_is_refused():
    reg = _registry()
    ctx = _ctx(reg)
    ctx.allowed = {"read_x"}
    reply = _answer(ctx, "write_x", {"value": "x"})
    assert reply["ok"] is False
    assert "outside this run's surface" in reply["error"]


def test_tool_code_cannot_recurse():
    reg = _registry()
    reply = _answer(_ctx(reg), "tool_code", {"code": "result = 1"})
    assert reply["ok"] is False
    assert "cannot call itself" in reply["error"]
