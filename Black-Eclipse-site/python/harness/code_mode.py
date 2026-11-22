"""Code Mode: confined code over the READ-ONLY tool catalog (Round 5 / C3).

The model writes a short Python program; a process-isolated child runs it and
calls tools through a JSON-lines bridge. The parent executes each call with the
run's own registry/context, so validation and permission still apply.

Failure of the security gates is always a refusal, never a fallback:

  * `HARNESS_CODE_MODE=1` is required (operator opt-in).
  * the child runs through `code/sandbox.py` (process-tree kill, scrubbed env,
    no network, CPU/memory caps); if no isolation primitive exists, it refuses.
  * a READ-ONLY tool crosses the bridge freely; an EFFECTFUL one crosses only
    when the run has an effect authorizer, and `registry.execute` then applies
    that same approval chain, the run's surface, the effect token and the
    widening cap. A run without an authorizer refuses, and `tool_code` cannot
    recurse.
  * the child starts in a fresh temp directory, so a relative `.env` read finds
    nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .code import sandbox

MAX_CODE_CHARS = 20_000
MAX_RESULT_CHARS = 12_000
MAX_TOOL_CALLS = 20
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 120

_RUNNER = textwrap.dedent(
    r"""
    import contextlib, io, json, sys, traceback

    PROTO_OUT = sys.stdout
    PROTO_IN = sys.stdin

    def send(obj):
        PROTO_OUT.write(json.dumps(obj, default=str) + "\n")
        PROTO_OUT.flush()

    class Tools:
        def __init__(self):
            self.calls = 0
        def call(self, name, **args):
            self.calls += 1
            send({"type": "tool_call", "id": self.calls, "name": str(name), "args": args})
            line = PROTO_IN.readline()
            if not line:
                raise RuntimeError("the harness closed the tool bridge")
            msg = json.loads(line)
            if not msg.get("ok"):
                raise RuntimeError(str(msg.get("error") or "tool failed"))
            return msg.get("data") if msg.get("data") is not None else msg.get("text", "")
        def text(self, name, **args):
            value = self.call(name, **args)
            return value if isinstance(value, str) else json.dumps(value, default=str)

    request = json.loads(PROTO_IN.readline() or "{}")
    code = request.get("code") or ""
    namespace = {"__name__": "code_mode", "tools": Tools(), "json": json}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<code_mode>", "exec"), namespace)
        send({"type": "result", "ok": True, "stdout": out.getvalue(),
              "result": namespace.get("result")})
    except Exception:
        send({"type": "result", "ok": False, "stdout": out.getvalue(),
              "error": traceback.format_exc(limit=4)})
    """
)


@dataclass
class CodeModeResult:
    ok: bool
    output: str = ""
    error: str = ""
    tool_calls: int = 0
    duration_ms: int = 0
    exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "toolCalls": self.tool_calls,
            "durationMs": self.duration_ms,
            "exhausted": self.exhausted,
        }


def enabled() -> bool:
    return (os.environ.get("HARNESS_CODE_MODE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _bounded_json(value: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - a foreign object is stringified
        text = str(value)
    if len(text) <= limit:
        return value
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}


def _drive(
    code: str,
    *,
    ctx: Any,
    loop: asyncio.AbstractEventLoop,
    timeout_s: float,
    holder: dict[str, Any],
) -> CodeModeResult:
    """Run the child and answer its tool calls. Executed in a worker thread."""
    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="be-code-mode-")
    spec = sandbox.RunSpec(
        argv=[os.sys.executable, "-I", "-c", _RUNNER],
        cwd=workdir,
        env=sandbox.build_env(),
        timeout_s=int(timeout_s),
        output_cap_bytes=200_000,
        network=False,
        cpu_seconds=min(10, int(timeout_s)),
        memory_mb=512,
        process_limit=8,
    )
    proc = None
    calls = 0
    try:
        proc = sandbox.spawn(spec)
        holder["proc"] = proc
        proc.write_line(json.dumps({"code": code}))
        deadline = started + timeout_s
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                line = proc.read_line(min(remaining, 10.0))
            except TimeoutError:
                if time.monotonic() >= deadline:
                    return CodeModeResult(
                        ok=False, error="code mode timed out", tool_calls=calls,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        exhausted=True,
                    )
                continue
            try:
                message = json.loads(line)
            except ValueError:
                return CodeModeResult(
                    ok=False, error="the child broke the protocol",
                    tool_calls=calls,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            kind = str(message.get("type") or "")
            if kind == "result":
                output = str(message.get("stdout") or "")
                result = message.get("result")
                if result is not None:
                    output = (
                        f"{output}\nresult: "
                        f"{json.dumps(result, default=str, ensure_ascii=False)}"
                    )
                return CodeModeResult(
                    ok=bool(message.get("ok")),
                    output=output[:MAX_RESULT_CHARS],
                    error=str(message.get("error") or "")[:4_000],
                    tool_calls=calls,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            if kind != "tool_call":
                continue
            calls += 1
            name = str(message.get("name") or "")
            args = message.get("args") if isinstance(message.get("args"), dict) else {}
            reply = _answer_tool_call(ctx, loop, name, args, remaining, calls)
            proc.write_line(json.dumps(reply, default=str))
    except sandbox.SandboxUnavailable as exc:
        return CodeModeResult(
            ok=False, error=f"SANDBOX_UNAVAILABLE: {exc}", tool_calls=calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (EOFError, sandbox.SandboxStreamError) as exc:
        return CodeModeResult(
            ok=False, error=f"the child exited: {exc}", tool_calls=calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def _answer_tool_call(
    ctx: Any,
    loop: asyncio.AbstractEventLoop,
    name: str,
    args: dict[str, Any],
    remaining: float,
    call_number: int,
) -> dict[str, Any]:
    registry = getattr(ctx, "tool_registry", None)
    if registry is None:
        return {"ok": False, "error": "no tool registry is bound"}
    if call_number > MAX_TOOL_CALLS:
        return {"ok": False, "error": f"the {MAX_TOOL_CALLS}-call budget is spent"}
    tool = registry.get(name) if hasattr(registry, "get") else None
    if tool is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    if name in ("tool_code",):
        return {"ok": False, "error": "tool_code cannot call itself"}
    surface = getattr(ctx, "allowed", None)
    if surface is not None and name not in surface:
        return {"ok": False, "error": f"{name!r} is outside this run's surface"}
    effectful = getattr(tool, "permission", "read") != "read"
    if effectful and getattr(ctx, "authorize_effect", None) is None:
        return {
            "ok": False,
            "error": (
                f"{name!r} is effectful and this run has no approval chain; call it "
                "directly so the graph's authorizer applies"
            ),
        }
    try:
        future = asyncio.run_coroutine_threadsafe(
            registry.execute(
                name, f"cm_{call_number}", args, ctx, allowed=surface,
            ),
            loop,
        )
        result = future.result(timeout=max(1.0, remaining))
    except Exception as exc:  # noqa: BLE001 - a tool failure is data
        return {"ok": False, "error": str(exc)[:2_000]}
    return {
        "ok": bool(getattr(result, "ok", False)),
        "error": getattr(getattr(result, "error", None), "message", "") or "",
        "text": str(getattr(result, "text", ""))[:MAX_RESULT_CHARS],
        "data": _bounded_json(getattr(result, "data", None)),
    }


async def run_code_mode(
    code: str, *, ctx: Any, timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CodeModeResult:
    """Execute `code` in the sandbox; returns the bounded result."""
    if not enabled():
        return CodeModeResult(
            ok=False,
            error="Code Mode is disabled. Set HARNESS_CODE_MODE=1 to enable it.",
        )
    if not code.strip():
        return CodeModeResult(ok=False, error="empty code")
    if len(code) > MAX_CODE_CHARS:
        return CodeModeResult(ok=False, error=f"code exceeds {MAX_CODE_CHARS} chars")
    if not sandbox.isolation_satisfied():
        return CodeModeResult(
            ok=False,
            error="SANDBOX_UNAVAILABLE: no process-isolation primitive is available",
        )
    loop = asyncio.get_running_loop()
    holder: dict[str, Any] = {}
    budget = max(5.0, min(float(timeout_s), MAX_TIMEOUT_S))
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_drive, code, ctx=ctx, loop=loop, timeout_s=budget, holder=holder),
            timeout=budget + 5.0,
        )
    except asyncio.TimeoutError:
        proc = holder.get("proc")
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
        return CodeModeResult(ok=False, error="code mode timed out", exhausted=True)
    except asyncio.CancelledError:
        proc = holder.get("proc")
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
        raise


def register_code_mode_tool(registry: Any) -> None:
    from .tools import ToolDefinition  # noqa: PLC0415 - avoid a module cycle

    async def _handler(args: dict[str, Any], ctx: Any) -> tuple[str, dict[str, Any]]:
        code = str(args.get("code") or "")
        timeout_s = int(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
        result = await run_code_mode(code, ctx=ctx, timeout_s=timeout_s)
        text = (
            f"CODE MODE {'OK' if result.ok else 'FAILED'} "
            f"(calls={result.tool_calls}, {result.duration_ms}ms)\n"
        )
        if result.output:
            text += result.output
        if result.error:
            text += f"\n{result.error}"
        return text, result.to_dict()

    registry.register(ToolDefinition(
        name="tool_code",
        description=(
            "Run a short Python program that calls tools through "
            "`tools.call(name, **args)` / `tools.text(name, **args)` and sets "
            "`result`. Process-isolated, no network, 20 tool calls max. Use it "
            "to loop, filter or join work across several tool results. Read-only "
            "tools run directly; an EFFECTFUL tool runs only through this run's "
            "approval chain (and is refused without one)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "timeout_s": {"type": "integer", "description": "Seconds (default 60)."},
            },
            "required": ["code"],
        },
        handler=_handler,
        permission="read",
        risk="low",
        timeout_ms=MAX_TIMEOUT_S * 1000 + 10_000,
    ))


__all__ = [
    "CodeModeResult",
    "DEFAULT_TIMEOUT_S",
    "MAX_CODE_CHARS",
    "MAX_TOOL_CALLS",
    "enabled",
    "register_code_mode_tool",
    "run_code_mode",
]
