"""The recovery ladder.

When an action fails, Eclipse must not blindly repeat it. The ladder is an
ordered, bounded set of strategies:

    1. Re-observe        2. Refresh accessibility tree   3. Re-capture screenshot
    4. Check window/app state   5. Re-ground target      6. Try semantic alternative
    7. Try OCR           8. Try vision                   9. Try keyboard navigation
    10. Escalate foreground   11. Re-plan                12. Ask user

`plan()` returns the ordered strategies appropriate to a failure kind (data, so
it is testable without a desktop); `RecoveryEngine.execute()` attempts them in
order until one succeeds or the budget is exhausted, returning what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# The ladder, in order.
LADDER: tuple[str, ...] = (
    "reobserve", "refresh_ax", "recapture", "check_window_state", "reground",
    "semantic_alternative", "try_ocr", "try_vision", "keyboard_navigation",
    "escalate_foreground", "replan", "ask_user",
)

# Which strategies are worth trying for a given failure verdict/state.
_STRATEGY_FOR_FAILURE: dict[str, tuple[str, ...]] = {
    "STALE_ELEMENT": ("recapture", "reground", "reobserve"),
    "TARGET_NOT_FOUND": ("reground", "refresh_ax", "try_ocr", "try_vision", "semantic_alternative"),
    "ACTION_FAILED": ("reobserve", "check_window_state", "reground", "escalate_foreground"),
    "STATE_CHANGED": ("reobserve", "recapture"),
    "APP_UNRESPONSIVE": ("check_window_state", "replan", "ask_user"),
    "TIMEOUT": ("reobserve", "check_window_state", "replan"),
    "UNEXPECTED_DIALOG": ("reobserve", "check_window_state"),
    "AMBIGUOUS_TARGET": ("semantic_alternative", "ask_user"),
    "PERMISSION_REQUIRED": ("ask_user",),
}


@dataclass
class RecoveryAttempt:
    strategy: str
    success: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy, "success": self.success, "detail": self.detail}


@dataclass
class RecoveryResult:
    recovered: bool
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    strategy: Optional[str] = None
    exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovered": self.recovered, "strategy": self.strategy,
            "exhausted": self.exhausted, "attempts": [a.to_dict() for a in self.attempts],
        }


class RecoveryEngine:
    def __init__(self, *, max_attempts: int = 3, enabled: bool = True) -> None:
        self.max_attempts = max(0, int(max_attempts))
        self.enabled = enabled

    def plan(self, failure: str, *, escalate_allowed: bool = True) -> list[str]:
        """The ordered strategies to try for a failure kind."""
        base = list(_STRATEGY_FOR_FAILURE.get((failure or "").upper(), ("reobserve", "reground", "replan")))
        if not escalate_allowed:
            base = [s for s in base if s != "escalate_foreground"]
        return base

    async def execute(
        self,
        failure: str,
        handlers: dict[str, Callable[[str], Awaitable[tuple[bool, str]]]],
        *,
        escalate_allowed: bool = True,
        strategies: Optional[list[str]] = None,
        max_attempts: Optional[int] = None,
    ) -> RecoveryResult:
        """Attempt the planned strategies in order until one succeeds.

        Each handler is `async (strategy) -> (success, detail)`. Missing handlers
        are skipped. `strategies` overrides the ladder for a caller that knows the
        exact rungs it can execute; `max_attempts` overrides this engine's budget
        for one call. The budget bounds the number of ACTUAL attempts.
        """
        result = RecoveryResult(recovered=False)
        budget = self.max_attempts if max_attempts is None else max(0, int(max_attempts))
        if not self.enabled or budget == 0:
            result.exhausted = True
            return result
        planned = list(strategies) if strategies is not None else self.plan(
            failure, escalate_allowed=escalate_allowed,
        )
        for strategy in planned:
            if len(result.attempts) >= budget:
                result.exhausted = True
                break
            if strategy not in handlers:
                continue
            try:
                success, detail = await handlers[strategy](strategy)
            except Exception as exc:  # noqa: BLE001 - a strategy failure is data
                success, detail = False, str(exc)[:200]
            result.attempts.append(RecoveryAttempt(strategy=strategy, success=success, detail=detail))
            if success:
                result.recovered = True
                result.strategy = strategy
                return result
        result.exhausted = not result.recovered
        return result


__all__ = ["LADDER", "RecoveryAttempt", "RecoveryEngine", "RecoveryResult"]
