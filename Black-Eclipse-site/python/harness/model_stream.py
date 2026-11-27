"""Model invocation + streaming helpers for the agent graph.

Extracted from `graph.py` (pure move) so the graph stays under its size ratchet.
These are the token-streaming best-effort path and the small message helpers it
uses. Behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from langchain_core.messages import BaseMessage

from .async_timeout import await_bounded

#: Longest gap allowed BETWEEN streamed chunks after the first token. A provider
#: that stops mid-answer is indistinguishable from a hang, and the outer call
#: deadline is minutes away (observed 2026-11-26: a stream went quiet after 196
#: chars and the run was only settled by the stall watchdog at 6.5 minutes).
DEFAULT_CHUNK_IDLE_TIMEOUT_S = 90.0


def _chunk_idle_timeout_s(override: float | None = None) -> float:
    if override is not None and override > 0:
        return float(override)
    try:
        value = float((os.environ.get("HARNESS_MODEL_IDLE_S") or "").strip())
        return value if value > 0 else DEFAULT_CHUNK_IDLE_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_IDLE_TIMEOUT_S


async def ainvoke_streaming(
    model: Any,
    messages: list[BaseMessage],
    timeout_s: float,
    emitter: Any,
    finalizing: bool,
    *,
    first_token_timeout_s: float | None = None,
    chunk_idle_timeout_s: float | None = None,
) -> Any:
    """Invoke the model, streaming tokens live when it supports it.

    Streaming is best-effort: a provider/model that cannot stream falls back to
    a single `ainvoke`. Token deltas are emitted as transient `model.delta`
    events so the UI shows the agent working and writing, without persisting
    every fragment. The assembled message is what the graph stores, so tool
    calls and usage metadata still work exactly as before.

    The deadline is HARD (`await_bounded`): a provider that ignores cancellation
    cannot hold the run hostage past `timeout_s` — the run fails with a timeout
    instead of hanging forever, and the abandoned stream is flagged so it stops
    emitting deltas.

    On the final step (`finalizing`) tools are unbound upstream, so this only
    ever yields the closing answer.
    """
    abandoned = False
    idle_timeout_s = _chunk_idle_timeout_s(chunk_idle_timeout_s)

    async def _stream() -> Any:
        nonlocal abandoned
        aggregated: Any = None
        last_with_tools: Any = None
        last_with_usage: Any = None
        first = True
        started = time.monotonic()
        iterator = model.astream(messages).__aiter__()
        while True:
            try:
                if first:
                    # Wait at most the first-token budget for the FIRST chunk; a
                    # provider that never starts is a different failure from one
                    # that stalls mid-answer.
                    chunk = await await_bounded(
                        iterator.__anext__(),
                        min(float(first_token_timeout_s or timeout_s), float(timeout_s)),
                        on_timeout=_abandon,
                    )
                else:
                    # Bound every chunk after the first. Without this, a stalled
                    # provider stream is only noticed by the outer call deadline
                    # (or, worse, by the stall watchdog minutes later).
                    chunk = await await_bounded(
                        iterator.__anext__(),
                        min(idle_timeout_s, float(timeout_s)),
                        on_timeout=_abandon,
                    )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                if first:
                    try:
                        await emitter.emit("model.first_token_timeout", {
                            "timeoutS": float(first_token_timeout_s or timeout_s),
                            "finalizing": finalizing,
                        })
                    except Exception:  # noqa: BLE001 - telemetry must not mask the timeout
                        pass
                else:
                    # Persist this as a durable event: the watchdog and the UI
                    # both need to see why a stream went quiet.
                    try:
                        await emitter.emit("model.stalled", {
                            "idleTimeoutS": idle_timeout_s,
                            "finalizing": finalizing,
                            "partialChars": len(message_text(aggregated)) if aggregated is not None else 0,
                        })
                    except Exception:  # noqa: BLE001 - telemetry must not mask the timeout
                        pass
                raise
            if abandoned:
                break
            if first:
                first = False
                try:
                    await emitter.emit("model.first_token", {
                        "ttftMs": int((time.monotonic() - started) * 1000.0),
                        "finalizing": finalizing,
                    })
                except Exception:  # noqa: BLE001 - telemetry must not break the stream
                    pass
            aggregated = chunk if aggregated is None else merge_chunks(aggregated, chunk)
            if getattr(chunk, "tool_calls", None):
                last_with_tools = chunk
            if getattr(chunk, "usage_metadata", None):
                last_with_usage = chunk
            text = message_text(chunk) if getattr(chunk, "content", "") else ""
            if text:
                emitter.emit_transient(
                    "model.delta", {"text": text, "finalizing": finalizing}
                )
        # Some providers/tests stream full AIMessage objects, whose `+` drops
        # tool_calls/usage. Carry those forward so the graph still sees them.
        if aggregated is not None:
            if not getattr(aggregated, "tool_calls", None) and last_with_tools is not None:
                try:
                    aggregated.tool_calls = last_with_tools.tool_calls
                except Exception:  # noqa: BLE001
                    pass
            if not getattr(aggregated, "usage_metadata", None) and last_with_usage is not None:
                try:
                    aggregated.usage_metadata = last_with_usage.usage_metadata
                except Exception:  # noqa: BLE001
                    pass
        return aggregated

    def _abandon() -> None:
        nonlocal abandoned
        abandoned = True

    try:
        aggregated = await await_bounded(_stream(), timeout_s, on_timeout=_abandon)
        if aggregated is None:
            # An empty stream is not an answer; fall back to a plain call.
            return await await_bounded(model.ainvoke(messages), timeout_s)
        return aggregated
    except (asyncio.TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - not all models implement astream
        # A real provider rejection must NOT be retried as "streaming is
        # unsupported": that hides a context overflow and makes a second call.
        try:
            from .model_runtime.errors import ErrorClass, classify_exception  # noqa: PLC0415

            if classify_exception(exc).error_class is ErrorClass.CONTEXT_OVERFLOW:
                raise
        except ImportError:  # pragma: no cover - defensive only
            pass
        return await await_bounded(model.ainvoke(messages), timeout_s)


FINALIZE_INSTRUCTION = (
    "You have reached the last allowed step. Tools are no longer available. "
    "Answer the user's request NOW using only the information already gathered "
    "above. Do not call tools, do not describe what you would do, and do not ask "
    "for permission. If some data was unavailable, say so plainly in the answer."
)


def merge_chunks(a: Any, b: Any) -> Any:
    """Merge two stream chunks, tolerating providers that yield full messages.

    LangChain's `AIMessageChunk + AIMessageChunk` is correct; a full `AIMessage`
    addition is not (it drops tool_calls and can raise). Fall back to keeping the
    richer object so a non-conforming provider cannot crash the run.
    """
    try:
        return a + b
    except Exception:  # noqa: BLE001
        return b if getattr(b, "content", None) or getattr(b, "tool_calls", None) else a


def message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


__all__ = [
    "DEFAULT_CHUNK_IDLE_TIMEOUT_S",
    "ainvoke_streaming",
    "merge_chunks",
    "message_text",
    "FINALIZE_INSTRUCTION",
]
