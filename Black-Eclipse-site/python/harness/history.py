"""History bounding + compaction + the inert dropped-context block.

`graph.py` is at its file-size ratchet, so the pure history-bounding helpers and
the dropped-context extraction live here. `graph.py` re-exports the names its
callers and tests use, so nothing else changes. This module owns no state.

Round 3 / W6 (ADR-H286): the dropped turns are now COMPACTED, not just
mined. Before this, `_bounded_history` kept the most recent turns and DROPPED
the oldest, and `eclipse_memory/dropped_context.py` mined the dropped turns
for inert candidates - so the decisions, the open questions and the state of
the work were simply lost. The compaction (`harness/compaction.py`) carries
them forward: a deterministic skeleton of what was asked and what was
answered, plus a bounded model NARRATIVE on top of it. The skeleton always
runs; the narrative fails CLOSED to the skeleton alone.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .events import RunEmitter
from .model_stream import message_text as _text
from .models import build_chat_model
from .model_runtime import get_runtime
from .schemas import RunRequest

MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 40_000


def _bounded_history(
    request: RunRequest, budget: Optional[Any] = None,
) -> tuple[list[Any], list[Any]]:
    """Return `(kept, dropped)` turns under the history bound.

    `kept` is the oldest-first list actually prompted; `dropped` is the prefix
    the bound removed. A history under the bound drops nothing — `dropped` is
    empty and the caller must behave exactly as before.

    On a KNOWN window the bound is a **token-budgeted verbatim tail**: walk the
    history backwards and keep turns until the tail reaches
    `budget.keep_tail_tokens` (default 15 000). The newest turn is always kept
    when there is any allowance, because the next answer depends on it; older
    turns become the compaction input. On an UNKNOWN window the conservative
    fallback (20 turns / 40 000 chars) is byte-for-byte the previous behaviour.
    """
    history = request.input.history or []
    if not history:
        return [], []
    if budget is not None and getattr(budget, "effective", False):
        max_turns = int(getattr(budget, "max_history_turns", MAX_HISTORY_TURNS))
        max_chars = int(getattr(budget, "max_history_chars", MAX_HISTORY_CHARS))
        max_tokens = int(getattr(budget, "keep_tail_tokens", 0))
        if max_tokens <= 0:
            # A budget object from before the token-tail change (or a caller
            # that only knows turns/chars): keep the previous turn/char bound.
            trimmed = list(history[-max_turns:]) if max_turns > 0 else []
            from .compaction import collapse_enabled, collapse_turns  # noqa: PLC0415

            if collapse_enabled():
                trimmed = collapse_turns(trimmed)[0]
            dropped = list(history[: max(0, len(history) - max_turns)]) if max_turns > 0 else list(history)
            total = sum(len(t.content) for t in trimmed)
            while trimmed and total > max_chars:
                total -= len(trimmed[0].content)
                dropped.append(trimmed[0])
                trimmed = trimmed[1:]
            return trimmed, dropped
        if max_turns <= 0 or max_chars <= 0:
            return [], list(history)
        from .eclipse_memory.profile import estimate_tokens  # noqa: PLC0415

        kept_reversed: list[Any] = []
        tokens = 0
        chars = 0
        for turn in reversed(history):
            if len(kept_reversed) >= max_turns:
                break
            content = str(getattr(turn, "content", "") or "")
            turn_tokens = estimate_tokens(content)
            exceeds = (
                chars + len(content) > max_chars
                or (max_tokens > 0 and tokens + turn_tokens > max_tokens)
            )
            # The newest turn survives even a single over-budget turn: the
            # answer in progress is anchored on it. Older turns stop the tail.
            if exceeds and kept_reversed:
                break
            kept_reversed.append(turn)
            tokens += turn_tokens
            chars += len(content)
            if exceeds:
                break
        kept = list(reversed(kept_reversed))
        dropped = list(history[: max(0, len(history) - len(kept))])
        from .compaction import collapse_enabled, collapse_turns  # noqa: PLC0415

        if collapse_enabled():
            kept = collapse_turns(kept)[0]
        return kept, dropped
    # Unknown window: the conservative fallback is unchanged.
    max_turns = MAX_HISTORY_TURNS
    max_chars = MAX_HISTORY_CHARS
    trimmed = list(history[-max_turns:]) if max_turns > 0 else []
    # Zero-cost collapse BEFORE the character cap, when enabled: a single
    # over-long turn is folded to head+tail (marked, digest-bearing, no model
    # call) instead of being dropped whole for its size. Nothing is summarized
    # here; the marker says exactly how much was omitted. OFF by default — see
    # `compaction.collapse_enabled`.
    from .compaction import collapse_enabled, collapse_turns  # noqa: PLC0415

    if collapse_enabled():
        trimmed = collapse_turns(trimmed)[0]
    dropped = list(history[: max(0, len(history) - max_turns)]) if max_turns > 0 else list(history)
    # Also cap total characters, dropping oldest turns first.
    total = sum(len(t.content) for t in trimmed)
    while trimmed and total > max_chars:
        total -= len(trimmed[0].content)
        dropped.append(trimmed[0])
        trimmed = trimmed[1:]
    return trimmed, dropped


def _history_messages(request: RunRequest, budget: Optional[Any] = None) -> list[BaseMessage]:
    """Prior conversation turns, oldest first, bounded so a long chat cannot
    blow the context window. Keeps the MOST RECENT turns when trimming."""
    trimmed, _dropped = _bounded_history(request, budget)
    return [
        HumanMessage(content=t.content) if t.role == "user" else AIMessage(content=t.content)
        for t in trimmed
    ]


async def _dropped_context_block(
    request: RunRequest,
    memory_service: Optional[Any],
    emitter: RunEmitter,
    budget: Optional[Any] = None,
    *,
    emit: bool = True,
) -> tuple[list[BaseMessage], str, list[Any]]:
    """The bounded history messages plus an inert dropped-context block ("").

    Returns `(messages, block, dropped_turns)`. When the history is under the
    bound nothing is dropped, so no block is produced and the message list is
    byte-identical to the pre-change behaviour. Extraction is deterministic-first
    and produces INERT candidates only. It is fail-closed: if extraction cannot
    complete, NO block is emitted (never a lossy summary) and an honest event is
    recorded. Extraction never blocks or fails the run.

    `emit=False` suppresses the extraction event, used when `execute_run`
    re-derives the history after measuring the dropped block's own cost (so the
    event is emitted exactly once).
    """
    kept, dropped = _bounded_history(request, budget)
    messages = [
        HumanMessage(content=t.content) if t.role == "user" else AIMessage(content=t.content)
        for t in kept
    ]
    if not dropped:
        _trigger_precompute(request, budget, memory_service)
        return messages, "", dropped
    # Round 3 / W6 (ADR-H286): the COMPACTION comes first, and the inert
    # dropped-context block follows it. The compaction is never a replacement
    # for the block - it is what carries the conversation forward, and the
    # block stays the deterministic record of what was dropped.
    compaction_block = await _compaction_block_with_held(
        request, dropped, kept, memory_service, emitter, budget,
    )
    try:
        from .eclipse_memory.dropped_context import (  # noqa: PLC0415
            MAX_ITEMS,
            DroppedTurn,
            extract_dropped_context,
            render_dropped_context_block,
        )

        settings: dict[str, Any] = {}
        if memory_service is not None:
            try:
                settings = memory_service.policy.get_all()
            except Exception:  # noqa: BLE001 - policy read failure is not fatal
                settings = {}
        result = extract_dropped_context(
            [DroppedTurn(role=t.role, content=t.content) for t in dropped],
            settings=settings, user_id=request.userId,
        )
        model_used = False
        # Optional, policy-gated model-assisted extraction. It is additive to the
        # deterministic pass, inert, bounded, and fail-closed: any failure leaves
        # the deterministic result untouched and emits no lossy summary.
        if settings.get("dropped_context_model_extraction") and memory_service is not None:
            try:
                model_items = await _model_extract_dropped(
                    request, [DroppedTurn(role=t.role, content=t.content) for t in dropped],
                )
                if model_items:
                    merged = list(result.items)
                    seen = {(i.action, i.content) for i in merged}
                    for item in model_items:
                        key = (item.action, item.content)
                        if key not in seen:
                            merged.append(item)
                            seen.add(key)
                    result.items = merged[:MAX_ITEMS]
                    model_used = True
            except Exception:  # noqa: BLE001 - model extraction must never break the run
                model_used = False
        block = render_dropped_context_block(result)
        try:
            if emit:
                await emitter.emit(
                    "memory.dropped_context_extracted",
                    {
                        "droppedTurns": len(dropped),
                        "scanned": result.scanned,
                        "candidates": len(result.items),
                        "emitted": bool(block),
                        "ok": result.ok,
                        "modelAssisted": model_used,
                        "reason": result.reason or None,
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        return messages, _combine(compaction_block, block), dropped
    except Exception as exc:  # noqa: BLE001 - extraction must never break a run
        try:
            if emit:
                await emitter.emit("memory.dropped_context_failed", {"error": str(exc), "droppedTurns": len(dropped)})
        except Exception:  # noqa: BLE001
            pass
        # Fail closed on the EXTRACTION - but the compaction is independent and
        # never fails, so the conversation is still carried forward.
        return messages, _combine(compaction_block, ""), dropped


def _combine(compaction_block: str, dropped_block: str) -> str:
    """The compaction first, then the inert block. Both, never one."""
    parts = [p for p in (compaction_block, dropped_block) if p]
    return "\n\n".join(parts)


def _trigger_precompute(
    request: RunRequest, budget: Optional[Any], memory_service: Optional[Any],
) -> None:
    """Start a background summary when the window is filling, never await it."""
    try:
        from .context import precompute  # noqa: PLC0415
        from .context.compression_ladder import _settings_for  # noqa: PLC0415

        precompute.start_precompute(
            request, list(request.input.history or []),
            budget=budget, settings=_settings_for(memory_service),
        )
    except Exception:  # noqa: BLE001 - precomputation never breaks a run
        pass


async def _compaction_block_with_held(
    request: RunRequest,
    dropped: list[Any],
    kept: list[Any],
    memory_service: Optional[Any],
    emitter: RunEmitter,
    budget: Optional[Any],
) -> str:
    """Use a held background summary when the tail budget allows, else compact."""
    try:
        from .context import precompute  # noqa: PLC0415

        summary = precompute.held(precompute.session_key(request))
        if summary is not None and precompute.applicable(summary, dropped, kept, budget):
            covered = list(dropped[: summary.boundary])
            remainder = list(dropped[summary.boundary:])
            block = summary.block
            if remainder:
                extra = await _compaction_block(
                    request, remainder, memory_service, emitter, emit=False,
                )
                block = _combine(block, extra)
            try:
                if emitter is not None:
                    await emitter.emit("memory.history_compacted", {
                        "precomputed": True,
                        "coveredTurns": len(covered),
                        "remainderTurns": len(remainder),
                        "usedModel": summary.used_model,
                        "chars": len(block),
                    })
            except Exception:  # noqa: BLE001
                pass
            return block
    except Exception:  # noqa: BLE001 - fall back to the on-demand path
        pass
    return await _compaction_block(request, dropped, memory_service, emitter)


async def _compaction_block(
    request: RunRequest,
    dropped: list[Any],
    memory_service: Optional[Any],
    emitter: RunEmitter,
    *, emit: bool = True,
) -> str:
    """The compaction of the dropped turns, or "" when it cannot be built.

    The decision (skeleton vs model narrative, and the persisted cooldown) lives
    in `context/compression_ladder.py`; this function only supplies the run's
    policy and store. Never raises: a compaction is a convenience, and the run
    continues without one.
    """
    from .context.compression_ladder import (  # noqa: PLC0415
        _settings_for,
        _state_store_for,
        compact_turns,
    )

    settings = _settings_for(memory_service)
    state_store = _state_store_for(memory_service)
    session_id = str(getattr(request, "sessionId", "") or getattr(request, "runId", "") or "")
    try:
        block, _result = await compact_turns(
            dropped, request=request, settings=settings, emitter=emitter if emit else None,
            state_store=state_store, session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - a compaction must never break a run
        try:
            if emit and emitter is not None:
                await emitter.emit("memory.history_compaction_failed", {"error": str(exc)})
        except Exception:  # noqa: BLE001
            pass
        return ""
    return block


async def _model_extract_dropped(
    request: RunRequest, turns: list[Any],
) -> list[Any]:
    """Optionally ask the run's own model for INERT candidates from dropped turns.

    Uses the SAME model/provider the run already resolved (no new dependency, no
    extra client). Bounded by a short timeout and the extractor's own item cap.
    Any failure returns [] — the deterministic extraction is unaffected and the
    caller emits no lossy summary. Never raises to the caller.
    """
    from .eclipse_memory.dropped_context import (  # noqa: PLC0415
        build_extraction_prompt,
        parse_extraction_response,
    )

    prompt = build_extraction_prompt(turns)
    if not prompt:
        return []
    try:
        # Build through the ModelRuntime (task=background_task: a cheap model is
        # appropriate for extraction) while preserving this module's test seam.
        _runtime = get_runtime()
        _resolution = _runtime.resolve_for_run(request.modelSettings, task="background_task")
        model = _runtime.build(
            _resolution.settings, builder=build_chat_model, adapter=_resolution.adapter
        )
        response = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=prompt)]), timeout=20.0,
        )
    except Exception:  # noqa: BLE001 - absence/failure is not an error here
        return []
    text = _text(response)
    parsed = parse_extraction_response(text)
    return parsed.items if parsed.ok else []


def _history_bounded_data(
    budget: Any,
    *,
    kept_turns: int,
    dropped_turns: list[Any],
    retried: bool = False,
) -> dict[str, Any]:
    """The bounded, content-free payload for `memory.history_bounded`.

    Counts and estimated tokens only - never memory content or the dropped text.
    """
    from .eclipse_memory.profile import estimate_tokens  # noqa: PLC0415

    dropped_tokens = sum(estimate_tokens(t.content) for t in dropped_turns)
    return {
        "keptTurns": kept_turns,
        "droppedTurns": len(dropped_turns),
        "droppedTokensEstimate": dropped_tokens,
        "window": budget.window,
        "windowKnown": bool(budget.window_known),
        "keepTailTokens": int(getattr(budget, "keep_tail_tokens", 0) or 0),
        "policy": getattr(budget, "policy", ""),
        "tighterRetry": bool(retried),
        # W8: the measured pressure and gate are what the context SLOs consume.
        # Both are counts/ratios, never content.
        "usageRatio": None if getattr(budget, "pressure", None) is None else round(float(budget.pressure), 4),
        "gate": getattr(budget, "gate", "unknown"),
    }


def _context_budget_blob(
    budget: Any, request: RunRequest, *, kind: str, retried: bool = False,
) -> Optional[dict[str, Any]]:
    """The additive `RunResult.context` blob for a completed/overflowed run.

    Present only when history was actually trimmed or the run overflowed, so an
    ordinary short run leaves `context` absent (old clients unaffected).
    """
    kept, dropped = _bounded_history(request, budget)
    if not dropped and kind != "overflow":
        return None
    data = _history_bounded_data(
        budget, kept_turns=len(kept), dropped_turns=dropped, retried=retried,
    )
    data["status"] = kind
    return data


__all__ = [
    "MAX_HISTORY_CHARS",
    "MAX_HISTORY_TURNS",
    "_bounded_history",
    "_context_budget_blob",
    "_compaction_block",
    "_dropped_context_block",
    "_history_bounded_data",
    "_history_messages",
    "_model_extract_dropped",
]
