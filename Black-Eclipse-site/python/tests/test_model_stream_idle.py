"""A stalled provider stream is bounded by the per-chunk idle timeout."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk

from harness.model_stream import ainvoke_streaming


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.deltas: list[str] = []

    async def emit(self, event_type: str, data=None):  # noqa: ANN001
        self.events.append((event_type, data or {}))
        return {"type": event_type, "data": data or {}}

    def emit_transient(self, event_type: str, data=None) -> None:  # noqa: ANN001
        if event_type == "model.delta":
            self.deltas.append(str((data or {}).get("text") or ""))


class _StreamingModel:
    def __init__(self, hang_after_first: bool) -> None:
        self.hang_after_first = hang_after_first

    async def astream(self, _messages):  # noqa: ANN001
        yield AIMessageChunk(content="partial")
        if self.hang_after_first:
            await asyncio.Event().wait()  # never yields again
        yield AIMessageChunk(content=" done")


class _PlainModel:
    """A model with no astream support: the helper falls back to ainvoke."""

    async def ainvoke(self, _messages):  # noqa: ANN001
        return AIMessageChunk(content="fallback answer")


@pytest.mark.asyncio
async def test_a_stream_that_stops_after_the_first_token_times_out():
    emitter = _Emitter()
    model = _StreamingModel(hang_after_first=True)
    with pytest.raises(asyncio.TimeoutError):
        await ainvoke_streaming(
            model,
            [],
            timeout_s=30.0,
            emitter=emitter,
            finalizing=False,
            first_token_timeout_s=5.0,
            chunk_idle_timeout_s=0.1,
        )
    # The durable event names WHY the stream went quiet, so the UI and the
    # watchdog are not left guessing.
    assert any(name == "model.stalled" for name, _ in emitter.events)


@pytest.mark.asyncio
async def test_a_healthy_stream_still_assembles():
    emitter = _Emitter()
    model = _StreamingModel(hang_after_first=False)
    result = await ainvoke_streaming(
        model,
        [],
        timeout_s=30.0,
        emitter=emitter,
        finalizing=False,
        first_token_timeout_s=5.0,
        chunk_idle_timeout_s=0.1,
    )
    assert result.content == "partial done"
    assert emitter.deltas == ["partial", " done"]


@pytest.mark.asyncio
async def test_a_model_without_astream_falls_back():
    emitter = _Emitter()
    model = _PlainModel()
    result = await ainvoke_streaming(
        model, [], timeout_s=5.0, emitter=emitter, finalizing=True,
    )
    assert result.content == "fallback answer"
