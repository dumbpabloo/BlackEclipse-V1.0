"""Gap #9: compaction is prepared BEFORE the window is full.

The defect this gates: the summary that carries a long conversation forward was
built only when the ceiling had already been hit, on that request's critical
path, with a model timeout. The contract:

1. A background task starts while there is still headroom, one held summary per
   session.
2. Starting it never blocks the run; the model call happens off the critical path.
3. It fails CLOSED to the deterministic five-section block.
4. It is swapped in only when the dropped prefix includes what it covers, the
   covered turn still matches, and the kept tail fits the token budget.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from harness import compaction, history
from harness.context import precompute


class _Turn:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _Request:
    def __init__(self, session_id: str = "sess-pre") -> None:
        self.sessionId = session_id
        self.runId = session_id


def _turn_hash(turn) -> str:
    return hashlib.sha256(
        f"{turn.role}\x00{turn.content}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def _history(n: int = 24, chars: int = 2000) -> list[_Turn]:
    return [
        _Turn("user" if i % 2 == 0 else "assistant", f"{i}:" + "x" * chars)
        for i in range(n)
    ]


def _budget(*, tail: int = 15_000, pressure: float | None = 0.5, effective: bool = True):
    return SimpleNamespace(effective=effective, pressure=pressure, keep_tail_tokens=tail, gate="soft")


@pytest.fixture(autouse=True)
def _clean():
    precompute.reset_for_tests()
    yield
    precompute.reset_for_tests()


def test_the_summary_is_produced_off_the_critical_path(monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_narrative(*_args, **_kwargs):
        started.set()
        await gate.wait()
        return "OBJECTIVE: ship the parser fix", ""

    monkeypatch.setattr(compaction, "_model_narrative", slow_narrative)
    request = _Request()
    turns = _history()

    async def scenario():
        task = precompute.start_precompute(
            request, turns, budget=_budget(), settings={compaction.MODEL_POLICY_KEY: True},
        )
        assert task is not None
        await started.wait()
        # Returned to the caller while the summary is still running.
        assert not task.done()
        assert precompute.held("sess-pre") is None
        gate.set()
        await task

    asyncio.run(scenario())
    summary = precompute.held("sess-pre")
    assert summary is not None
    assert summary.used_model is True
    assert "ship the parser fix" in summary.block
    assert summary.boundary < len(turns)


def test_a_model_failure_falls_closed_to_the_deterministic_block(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(compaction, "_model_narrative", boom)

    async def scenario():
        task = precompute.start_precompute(
            _Request("sess-fail"), _history(), budget=_budget(),
            settings={compaction.MODEL_POLICY_KEY: True},
        )
        assert task is not None
        await task

    asyncio.run(scenario())
    summary = precompute.held("sess-fail")
    assert summary is not None
    assert summary.used_model is False
    assert "WHAT WAS ASKED" in summary.block
    assert "DATA" in summary.block


def test_one_held_summary_per_session():
    async def scenario():
        first = precompute.start_precompute(_Request(), _history(), budget=_budget())
        second = precompute.start_precompute(_Request(), _history(), budget=_budget())
        assert first is second
        await first

    asyncio.run(scenario())
    assert precompute.held("sess-pre") is not None


def test_the_tail_budget_decides_whether_the_held_summary_is_used():
    turns = _history()
    boundary = precompute.boundary_for(turns, _budget())
    assert 0 < boundary < len(turns)
    dropped, kept = turns[: boundary + 2], turns[boundary + 2:]
    summary = precompute.HeldSummary(
        session_id="s", boundary=boundary, covered_hash=_turn_hash(turns[boundary - 1]),
        block="<compacted-conversation>HELD-SUMMARY</compacted-conversation>",
        used_model=True, created_at=0.0,
    )
    assert precompute.applicable(summary, dropped, kept, _budget()) is True
    # A tail budget that cannot hold the kept tail refuses the swap-in.
    assert precompute.applicable(summary, dropped, kept, _budget(tail=1)) is False
    # More prompts than the summary covers, or a changed covered turn, refuses it.
    assert precompute.applicable(summary, turns[: boundary - 1], kept, _budget()) is False
    summary_wrong = precompute.HeldSummary(
        session_id="s", boundary=boundary, covered_hash="deadbeef",
        block="x", used_model=True, created_at=0.0,
    )
    assert precompute.applicable(summary_wrong, dropped, kept, _budget()) is False


def test_the_held_summary_is_swapped_in_with_a_deterministic_remainder(monkeypatch):
    turns = _history()
    boundary = 8
    precompute._HELD["sess-swap"] = precompute.HeldSummary(
        session_id="sess-swap", boundary=boundary, covered_hash=_turn_hash(turns[boundary - 1]),
        block="<compacted-conversation>HELD-SUMMARY</compacted-conversation>",
        used_model=True, created_at=0.0,
    )

    class _Emitter:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def emit(self, name, _data):
            self.events.append(name)

    request = _Request("sess-swap")
    request.input = SimpleNamespace(history=turns)
    emitter = _Emitter()

    async def scenario():
        return await history._compaction_block_with_held(
            request, turns[:10], turns[10:], None, emitter, _budget(),
        )

    block = asyncio.run(scenario())
    assert "HELD-SUMMARY" in block
    assert "WHAT WAS ASKED" in block  # the 2 uncovered turns, deterministically
    assert "memory.history_compacted" in emitter.events


def test_the_held_summary_is_not_used_when_the_tail_does_not_fit():
    turns = _history()
    boundary = 8
    precompute._HELD["sess-tight"] = precompute.HeldSummary(
        session_id="sess-tight", boundary=boundary, covered_hash=_turn_hash(turns[boundary - 1]),
        block="<compacted-conversation>HELD-SUMMARY</compacted-conversation>",
        used_model=True, created_at=0.0,
    )
    request = _Request("sess-tight")
    request.input = SimpleNamespace(history=turns)

    async def scenario():
        return await history._compaction_block_with_held(
            request, turns[:10], turns[10:], None, None, _budget(tail=1),
        )

    block = asyncio.run(scenario())
    assert "HELD-SUMMARY" not in block
