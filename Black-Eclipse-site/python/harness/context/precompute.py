"""Background precomputation of the compaction summary (gap #9).

Eclipse used to build a compaction summary only when the window was already
full — on the critical path of the request that needed it, with a 20 s model
timeout. OpenCode writes the summary ~60 k tokens BEFORE the ceiling, inside the
prompt-cache lifetime, and holds one per session.

This module is the Eclipse equivalent, kept deliberately small:

  * one held summary per SESSION, covering a prefix of the append-only history;
  * the prefix is chosen so the tail since it is at most a share of the verbatim
    tail budget (`TAIL_SHARE`), so a later compaction can keep that tail whole;
  * the summary is produced in an `asyncio` task the request does not await —
    off the critical path — and any model failure stores the deterministic
    five-section block instead (fail-closed, never a lossy blank);
  * it is SWAPPED IN only when the dropped prefix is at least what the summary
    covers, the covered turn still matches, and the kept tail fits the token
    budget. Anything else falls back to the normal on-demand compaction.

The held summary is DATA, exactly like the compaction it replaces: it is the
rendered `<compacted-conversation>` block from `compaction.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Optional

#: Fraction of the verbatim tail that may remain AFTER the precomputed prefix.
TAIL_SHARE = 0.4

#: The measured pressure at which precomputation starts. Below this the window
#: has room; above it the current path already compacts.
PRECOMPUTE_LEAD_RATIO = 0.35

#: One summary per session; bound the map so a long-lived process cannot grow.
MAX_HELD = 64


def _estimate_tokens(text: str) -> int:
    from ..eclipse_memory.profile import estimate_tokens  # noqa: PLC0415

    return estimate_tokens(text)


def _turn_hash(turn: Any) -> str:
    role = str(getattr(turn, "role", "") or "")
    content = str(getattr(turn, "content", "") or "")
    return hashlib.sha256(f"{role}\x00{content}".encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True)
class HeldSummary:
    session_id: str
    boundary: int
    covered_hash: str
    block: str
    used_model: bool
    created_at: float

    @property
    def chars(self) -> int:
        return len(self.block)


_HELD: dict[str, HeldSummary] = {}
_TASKS: dict[str, asyncio.Task] = {}


def session_key(request: Any) -> str:
    return str(getattr(request, "sessionId", "") or getattr(request, "runId", "") or "")


def held(session_id: str) -> Optional[HeldSummary]:
    return _HELD.get(str(session_id or ""))


def _remember(summary: HeldSummary) -> None:
    if len(_HELD) >= MAX_HELD and summary.session_id not in _HELD:
        oldest = min(_HELD.values(), key=lambda item: item.created_at)
        _HELD.pop(oldest.session_id, None)
    _HELD[summary.session_id] = summary


def boundary_for(history: list[Any], budget: Any) -> int:
    """The prefix length to summarize, leaving a small tail behind it."""
    if not history:
        return 0
    target = max(
        1, int(int(getattr(budget, "keep_tail_tokens", 0) or 0) * TAIL_SHARE),
    )
    if target <= 1:
        # An inert budget: fall back to a quarter of the conservative ceiling.
        from ..history import MAX_HISTORY_CHARS  # noqa: PLC0415

        target = max(1, (MAX_HISTORY_CHARS // 4) // 4)
    tokens = 0
    boundary = len(history)
    for index in range(len(history) - 1, -1, -1):
        tokens += _estimate_tokens(str(getattr(history[index], "content", "") or ""))
        if tokens > target:
            break
        boundary = index
    # Never summarize a tiny prefix (the skeleton would be all there is).
    return boundary if boundary >= 4 else 0


def should_precompute(request: Any, history: list[Any], budget: Any) -> bool:
    if getattr(budget, "effective", False) and getattr(budget, "pressure", None) is not None:
        if float(getattr(budget, "pressure", 0.0) or 0.0) < PRECOMPUTE_LEAD_RATIO:
            return False
    elif getattr(budget, "gate", "unknown") in ("unknown", "continue"):
        return False
    if not session_key(request):
        return False
    existing = held(session_key(request))
    if existing is not None and existing.boundary <= len(history):
        # One held summary per session: it still covers an append-only prefix.
        if existing.boundary == 0 or _turn_hash(history[existing.boundary - 1]) == existing.covered_hash:
            return False
    return True


def start_precompute(
    request: Any, history: list[Any], *, budget: Any, settings: Optional[dict[str, Any]] = None,
) -> Optional[asyncio.Task]:
    """Start (or return the running) background summary. Never awaits the model."""
    key = session_key(request)
    if not key or not should_precompute(request, history, budget):
        return _TASKS.get(key)
    boundary = boundary_for(history, budget)
    if boundary <= 0:
        return None
    prefix = list(history[:boundary])
    covered_hash = _turn_hash(prefix[-1])
    running = _TASKS.get(key)
    if running is not None and not running.done():
        return running
    settings = dict(settings or {})
    task = asyncio.create_task(
        _run(key, prefix, covered_hash, request, settings),
        name=f"compact-precompute:{key[:12]}",
    )
    _TASKS[key] = task

    def _forget(done: asyncio.Task) -> None:
        if _TASKS.get(key) is done:
            _TASKS.pop(key, None)

    task.add_done_callback(_forget)
    return task


async def _run(
    key: str, prefix: list[Any], covered_hash: str, request: Any, settings: dict[str, Any],
) -> None:
    from ..compaction import MODEL_POLICY_KEY, compact_dropped, render_compaction_block  # noqa: PLC0415

    use_model = bool(settings.get(MODEL_POLICY_KEY, False))
    try:
        result = await compact_dropped(
            prefix, request=request, settings=settings, use_model=use_model,
        )
        block = render_compaction_block(result)
        if not block:
            return
        _remember(HeldSummary(
            session_id=key, boundary=len(prefix), covered_hash=covered_hash,
            block=block, used_model=bool(result.used_model), created_at=time.monotonic(),
        ))
    except Exception:  # noqa: BLE001 - a precomputed summary is never the work
        # Fail closed to the deterministic block. `compact_dropped` already does
        # this internally; this catch is the last line for a broken render.
        try:
            result = await compact_dropped(prefix, use_model=False)
            block = render_compaction_block(result)
            if block:
                _remember(HeldSummary(
                    session_id=key, boundary=len(prefix), covered_hash=covered_hash,
                    block=block, used_model=False, created_at=time.monotonic(),
                ))
        except Exception:  # noqa: BLE001
            return


def applicable(summary: HeldSummary, dropped: list[Any], kept: list[Any], budget: Any) -> bool:
    """May this held summary replace the on-demand one for THIS trim?

    Requires (a) the summary covers no more turns than were dropped, (b) the
    covered turn is still the same, and (c) the kept tail fits the token budget
    the summary was prepared against. Anything else is a fresh compaction.
    """
    if summary.boundary <= 0 or summary.boundary > len(dropped):
        return False
    if _turn_hash(dropped[summary.boundary - 1]) != summary.covered_hash:
        return False
    limit = int(getattr(budget, "keep_tail_tokens", 0) or 0)
    if limit > 0:
        tail = sum(_estimate_tokens(str(getattr(t, "content", "") or "")) for t in kept)
        if tail > limit:
            return False
    elif getattr(budget, "effective", False):
        return False
    return True


def reset_for_tests() -> None:
    for task in list(_TASKS.values()):
        if not task.done():
            task.cancel()
    _TASKS.clear()
    _HELD.clear()


__all__ = [
    "HeldSummary",
    "MAX_HELD",
    "PRECOMPUTE_LEAD_RATIO",
    "TAIL_SHARE",
    "applicable",
    "boundary_for",
    "held",
    "reset_for_tests",
    "session_key",
    "should_precompute",
    "start_precompute",
]
