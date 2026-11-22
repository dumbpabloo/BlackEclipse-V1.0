"""Measured, model-aware context budgeting.

The runtime previously bounded conversation history with two hardcoded constants
(`graph.MAX_HISTORY_TURNS` / `MAX_HISTORY_CHARS`) and never measured what the
assembled request actually cost against the resolved model's context window. A
40 000-char history is ~10–13k tokens on its own, which overflows a small-context
model and wastes most of a large one.

This module computes the history allowance from what actually remains after the
real per-run overhead, using the resolved model's **declared** window (Milestone
1). It is deliberately small and side-effect-free: no tokenizer dependency, no
network call, no summarization.

Rules:

  * **Measure the request, not the history in isolation.** The system prompt
    (after surface filtering), the tool schemas actually advertised, every
    per-run context block, and the user turn are summed.
  * **Unknown stays unknown.** When `resolve_context_window` returns None the
    budget is *inert* (`effective=False`): the caller must fall back to the
    conservative constants, never assume a large window. The conservative
    fallback keeps `absolute_max_chars` (20 turns / 40 000 chars).
  * **The tail is token-budgeted, not turn-counted.** On a known window the
    history allowance is spent on a verbatim tail of up to `KEEP_TAIL_TOKENS`
    (15 000 estimated tokens), minus a reserve for the compaction summary that
    sits beside it. `MAX_HISTORY_CHARS` remains the absolute ceiling for an
    unknown window, so a pathological history cannot flood an unmeasured model.
  * **Approximation margin.** `estimate_tokens` is ~4 chars/token; the budget
    keeps a safety margin because of that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .eclipse_memory.profile import estimate_tokens
from .models import resolve_context_window

# Output tokens reserved out of the window for the model's own answer. A
# conservative default; agents may produce longer answers, but the margin below
# plus the char ceiling absorbs that.
DEFAULT_RESERVED_OUTPUT_TOKENS = 1024

# Fraction of the remaining window kept as margin (the estimator is approximate).
SAFETY_MARGIN_RATIO = 0.10

# A turn budget is derived from the allowance using a generously small per-turn
# floor. The character allowance is the binding constraint in practice; this only
# prevents an absurd number of microscopic turns on a huge window.
MIN_TURN_TOKENS = 4

# The estimator's unit: how many characters one estimated token represents.
CHARS_PER_TOKEN = 4

#: The verbatim tail a KNOWN window may keep, in tokens. This is the default
#: target; the measured allowance and the summary reserve can lower it. An
#: unknown window never uses it — the conservative fallback stays at 20 turns /
#: 40 000 chars.
KEEP_TAIL_TOKENS = 15_000
KEEP_TAIL_CHARS = KEEP_TAIL_TOKENS * CHARS_PER_TOKEN

#: Tokens held back out of the allowance for the compaction summary that sits
#: beside the tail, so a full tail cannot crowd the summary out of the prompt.
SUMMARY_RESERVE_TOKENS = 2_000

UNKNOWN_WINDOW_POLICY = "conservative_ceiling"
KNOWN_WINDOW_POLICY = "measured_model_window"


@dataclass
class ContextBudget:
    """The per-run history budget, derived from the resolved model's window."""

    window: Optional[int]
    window_known: bool
    # False when the window is unknown: the caller must use the conservative
    # constants (this object does not carry a model-derived bound).
    effective: bool
    reserved_output_tokens: int
    measured_overhead_tokens: int
    history_allowance_tokens: int
    history_allowance_chars: int
    max_history_turns: int
    max_history_chars: int
    #: The verbatim tail this budget actually permits, in estimated tokens.
    keep_tail_tokens: int = 0
    policy: str = UNKNOWN_WINDOW_POLICY
    components: dict[str, int] = field(default_factory=dict)
    tighter: bool = False
    # W2: measured pressure from the provider usage anchor, when available.
    anchor_used_tokens: Optional[int] = None
    anchor_anchored: bool = False
    pressure: Optional[float] = None
    gate: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "windowKnown": self.window_known,
            "effective": self.effective,
            "reservedOutputTokens": self.reserved_output_tokens,
            "measuredOverheadTokens": self.measured_overhead_tokens,
            "historyAllowanceTokens": self.history_allowance_tokens,
            "historyAllowanceChars": self.history_allowance_chars,
            "maxHistoryTurns": self.max_history_turns,
            "maxHistoryChars": self.max_history_chars,
            "keepTailTokens": self.keep_tail_tokens,
            "policy": self.policy,
            "tighterRetry": self.tighter,
            "anchorUsedTokens": self.anchor_used_tokens,
            "anchorAnchored": self.anchor_anchored,
            "pressure": self.pressure,
            "gate": self.gate,
            "components": dict(self.components),
        }


def tool_schema_text(schemas: list[dict[str, Any]]) -> str:
    """A deterministic text rendering of the advertised tool schemas.

    The model receives these as bound tools; measuring their JSON is a
    conservative stand-in for their token cost. Sorted by name so the value is
    stable regardless of registry insertion order.
    """
    if not schemas:
        return ""
    try:
        ordered = sorted(schemas, key=lambda s: str(s.get("name") or ""))
        return json.dumps(ordered, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001 - a schema is always JSON-encodable in practice
        return " ".join(str(s) for s in schemas)


def compute_history_budget(
    *,
    provider: str,
    model: str,
    system_prompt: str,
    tool_schemas: str = "",
    profile_block: str = "",
    skill_manifest: str = "",
    memory_block: str = "",
    runtime_context: str = "",
    dropped_block: str = "",
    user_turn: str = "",
    absolute_max_turns: int,
    absolute_max_chars: int,
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    tighter: bool = False,
    window_override: Optional[int] = None,
    anchor: Optional[Any] = None,
    history_chars: int = 0,
    keep_tail_tokens: int = KEEP_TAIL_TOKENS,
    gate_soft: float = 0.50,
    gate_hard: float = 0.70,
    gate_emergency: float = 0.85,
) -> ContextBudget:
    """Derive the history allowance from the measured request and the window.

    Returns an inert (`effective=False`) budget when the window is unknown — the
    caller falls back to `absolute_max_turns` / `absolute_max_chars`. When the
    window is known, `max_history_chars` is the derived allowance capped by the
    absolute char ceiling, and `max_history_turns` is a derived turn budget.

    `tighter=True` halves the derived allowance for the one recovery retry.
    `window_override` is an explicit caller-supplied window (a test/operator
    seam); it never fabricates a value — it only lets a caller who KNOWS the
    window supply it where the catalog does not.
    """
    components = {
        "systemPrompt": estimate_tokens(system_prompt),
        "toolSchemas": estimate_tokens(tool_schemas),
        "profile": estimate_tokens(profile_block),
        "skillManifest": estimate_tokens(skill_manifest),
        "memory": estimate_tokens(memory_block),
        "runtimeContext": estimate_tokens(runtime_context),
        "droppedContext": estimate_tokens(dropped_block),
        "userTurn": estimate_tokens(user_turn),
    }
    overhead = sum(components.values())
    window = window_override if window_override is not None else resolve_context_window(provider, model)

    if window is None or window <= 0:
        return ContextBudget(
            window=None,
            window_known=False,
            effective=False,
            reserved_output_tokens=reserved_output_tokens,
            measured_overhead_tokens=overhead,
            history_allowance_tokens=0,
            history_allowance_chars=0,
            max_history_turns=absolute_max_turns,
            max_history_chars=absolute_max_chars,
            keep_tail_tokens=0,
            policy=UNKNOWN_WINDOW_POLICY,
            components=components,
            tighter=tighter,
            anchor_used_tokens=None,
            anchor_anchored=False,
            pressure=None,
            gate="unknown",
        )

    anchor_used: Optional[int] = None
    anchor_anchored = bool(getattr(anchor, "anchored", False))
    if anchor_anchored and getattr(anchor, "model_key", "") == f"{provider}/{model}":
        try:
            anchor_used = int(anchor.estimated_used(history_chars=int(history_chars)))
        except Exception:  # noqa: BLE001 - a bad anchor falls back to the estimate
            anchor_used = None
    used = overhead if anchor_used is None else max(0, anchor_used)
    pressure = (used / window) if window else None
    if pressure is None:
        gate = "unknown"
    elif pressure >= gate_emergency:
        gate = "emergency"
    elif pressure >= gate_hard:
        gate = "hard"
    elif pressure >= gate_soft:
        gate = "soft"
    else:
        gate = "continue"

    usable = window - used - reserved_output_tokens
    usable = max(0, usable)
    usable = int(usable * (1.0 - SAFETY_MARGIN_RATIO))
    if tighter:
        usable = usable // 2

    # Hold room for the summary that will sit beside the verbatim tail, then
    # let the tail use the rest up to the token target. A known window is
    # measured, so the 40k conservative ceiling does not apply; an unknown
    # window never reaches this branch.
    summary_reserve = min(SUMMARY_RESERVE_TOKENS, usable // 4)
    history_usable = max(0, usable - summary_reserve)
    tail_tokens = max(0, min(int(keep_tail_tokens), history_usable))
    allowance_chars = history_usable * CHARS_PER_TOKEN
    max_history_chars = min(KEEP_TAIL_CHARS, allowance_chars)
    # A derived turn budget from the allowance, never below 1 when there is any
    # allowance. The token/char ceiling is the binding constraint in practice.
    max_history_turns = max(1, tail_tokens // MIN_TURN_TOKENS) if tail_tokens > 0 else 0

    return ContextBudget(
        window=window,
        window_known=True,
        effective=True,
        reserved_output_tokens=reserved_output_tokens,
        measured_overhead_tokens=overhead,
        history_allowance_tokens=history_usable,
        history_allowance_chars=allowance_chars,
        max_history_turns=max_history_turns,
        max_history_chars=max_history_chars,
        keep_tail_tokens=tail_tokens,
        policy=KNOWN_WINDOW_POLICY,
        components=components,
        tighter=tighter,
        anchor_used_tokens=anchor_used,
        anchor_anchored=anchor_anchored,
        pressure=pressure,
        gate=gate,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "ContextBudget",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "KEEP_TAIL_CHARS",
    "KEEP_TAIL_TOKENS",
    "KNOWN_WINDOW_POLICY",
    "MIN_TURN_TOKENS",
    "SAFETY_MARGIN_RATIO",
    "SUMMARY_RESERVE_TOKENS",
    "UNKNOWN_WINDOW_POLICY",
    "compute_history_budget",
    "tool_schema_text",
]
