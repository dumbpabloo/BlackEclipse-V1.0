"""Reject a research goal that is really an order.

A research product reasons about whether to buy or sell, so "evaluate whether to buy
NVDA" is a normal objective. What must not pass is a goal that *is* an execution
request. The reference draws the line at three signals, and this is a transcription of
Vibe-Trading's `goal/policy.py` (``_EXECUTION_PATTERNS`` +
``reject_live_execution_objective``, MIT):

* **an execution cue next to a verb** — "Buy NVDA now", "immediately buy TSLA",
  "place a market order for AAPL";
* **the objective OPENS with the order verb** — "Buy NVDA", "Please sell my TSLA",
  which is an order however few words it uses;
* **a quantity right after the verb** — "then buy 100 shares", "short 2 ES";

plus the Chinese equivalents (下单/市价单/马上买/买入100股).

Two deliberate non-matches, both preserved:

* **no bare asset noun** — the *cue* pattern needs "now"/"immediately"/"market order",
  so "Buy NVDA shares" is caught by the opens-with-verb rule alone rather than by
  treating an asset name as evidence of an order;
* **"long-term" / "short-term" are horizons, not the `long`/`short` verbs**, so
  "update the long-term thesis immediately" must pass. A word boundary is not enough
  (`long` is a prefix of `long-term`), so the patterns carry a negative lookahead.

Eclipse had no such guard on goal objectives at all.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["EXECUTION_PATTERNS", "ExecutionObjectiveError", "reject_execution_objective"]

#: Horizon qualifiers that must NOT be read as the `long`/`short` verbs.
_HORIZON = r"(?![\s-]*term\b)"

#: Patterns that make an objective an execution request rather than a research one.
EXECUTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # an execution verb aimed at an order/trade
    re.compile(r"\b(place|submit|execute|send)\b.{0,40}\b(order|trade)\b", re.I),
    # a buy/sell verb with an execution-imminent cue after it
    re.compile(
        rf"\b(?:buy|sell|short{_HORIZON}|long{_HORIZON})\b"
        r".{0,40}\b(?:now|immediately|right away|market order|limit order)\b",
        re.I,
    ),
    # the same cue BEFORE the verb ("immediately buy TSLA")
    re.compile(
        rf"\b(?:now|immediately|right away)\b[\s,:-]+(?:please\s+)?"
        rf"(?:buy|sell|short{_HORIZON}|long{_HORIZON})\b",
        re.I,
    ),
    # Chinese execution cues
    re.compile(r"(下单|市价单|限价单|马上买|立即买|现在买|马上卖|立即卖|现在卖)"),
    # the objective OPENS with the order verb ("Buy NVDA", "Please sell my TSLA").
    # A leading "short" is excluded on purpose: "Short interest ..." and
    # "Short-term ..." open research goals.
    re.compile(r"^\s*(?:please\s+)?(?:buy|sell)\b", re.I),
    # a quantity right after the verb ("then buy 100 shares", "short 2 ES")
    re.compile(r"\b(?:buy|sell|short)\s+\d", re.I),
    # Chinese verb + quantity + unit
    re.compile(r"(?:买入|卖出|做空|买|卖)\s*\d+(?:\.\d+)?\s*(?:股|手|张|份|个)"),
)


class ExecutionObjectiveError(ValueError):
    """The objective asks Eclipse to place a trade, which it never does."""

    def __init__(self, message: str, pattern: Optional[str] = None) -> None:
        super().__init__(message)
        self.pattern = pattern


def reject_execution_objective(objective: str) -> None:
    """Raise when a goal objective looks like an execution request.

    Called at goal creation, so the refusal happens before a task is dispatched —
    the cheap place, and the one where the message can still name the fix.
    """
    text = str(objective or "").strip()
    for pattern in EXECUTION_PATTERNS:
        if pattern.search(text):
            raise ExecutionObjectiveError(
                "live trading or execution goals are not supported; Eclipse is a "
                "research harness and never places orders. Rephrase the goal as a "
                "research question (e.g. 'evaluate whether to buy NVDA').",
                pattern.pattern,
            )
