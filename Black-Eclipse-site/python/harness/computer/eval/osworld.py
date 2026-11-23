"""OSWorld episode bridge: one task, Eclipse's engine, OSWorld's scorer.

OSWorld owns the VM and the evaluation function; Eclipse owns observation,
action, verification and (optionally) the agent. This module joins them without
pretending either side is something it is not:

  * the driver is `OSWorldDriver` (pyautogui actions into `env.step`);
  * the session is a normal `ComputerSession`, so verification/approvals/finance
    guardrails still apply;
  * `run_agent(session, prompt)` is the caller's agent (the desktop agent, a
    scripted policy, or best-of-N); it is NOT reimplemented here;
  * the score comes from `evaluate(env)` (default `env.evaluate()`), never from
    the agent's own claim.
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Awaitable, Callable, Optional

from ..config import computer_config
from ..drivers.osworld import OSWorldDriver
from ..manager import ComputerManager


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def build_osworld_manager(env: Any, *, config: Any = None) -> ComputerManager:
    """A ComputerManager wired to an OSWorld environment (not started)."""
    manager = ComputerManager(config=config or computer_config())
    manager.driver = OSWorldDriver(env)
    manager.ax = None
    return manager


async def run_osworld_episode(
    *,
    env: Any,
    prompt: str,
    run_agent: Callable[[Any, str], Awaitable[Any]],
    evaluate: Optional[Callable[[Any], Any]] = None,
    config: Any = None,
    policy: Any = None,
) -> dict[str, Any]:
    """Run one OSWorld task and return the environment's own score.

    `run_agent` receives the bound `ComputerSession` and the task prompt. Its
    return value is recorded but NEVER used as the score.
    """
    manager = build_osworld_manager(env, config=config)
    run_id = f"osworld_{int(time.time() * 1000)}"
    started = time.monotonic()
    session = None
    agent_result: Any = None
    agent_error = ""
    agent_crashed = False
    try:
        await manager.start()
        session = await manager.create_session(
            agent_id="desktop-agent", run_id=run_id, task_id=run_id, policy=policy,
        )
        try:
            agent_result = await run_agent(session, prompt)
        except Exception as exc:  # noqa: BLE001 - an agent failure is a scored failure
            agent_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            agent_crashed = True
        scorer = evaluate
        if scorer is None:
            bound = getattr(env, "evaluate", None)
            if callable(bound):
                async def scorer(_env=None, _fn=bound):
                    return _fn()
            else:
                scorer = None
        if not callable(scorer):
            score: Any = None
            score_error = "the environment exposes no evaluate()"
        else:
            try:
                score = await _maybe_await(scorer(env))
                score_error = ""
            except Exception as exc:  # noqa: BLE001
                score = None
                score_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if session is not None:
            try:
                await manager.destroy_session(session.id)
            except Exception:  # noqa: BLE001
                pass
        try:
            await manager.stop()
        except Exception:  # noqa: BLE001
            pass
    numeric = None
    if isinstance(score, (int, float)):
        numeric = float(score)
    elif isinstance(score, dict):
        for key in ("score", "success", "passed"):
            if key in score and isinstance(score[key], (int, float, bool)):
                numeric = float(score[key])
                break
    score_success = False
    if numeric is not None:
        score_success = numeric > 0
    elif isinstance(score, bool):
        score_success = score
    # A crashed agent can never be reported as a successful EPISODE, even if the
    # environment's final state happens to score. The score is still returned
    # separately so the failure is visible, not hidden.
    success = score_success and not agent_crashed
    return {
        "runId": run_id,
        "prompt": prompt,
        "success": success,
        "agentCrashed": agent_crashed,
        "score": score,
        "scoreError": score_error,
        "agentError": agent_error,
        "agentResult": str(agent_result)[:1000] if agent_result is not None else None,
        "durationMs": int((time.monotonic() - started) * 1000),
        "sessionId": getattr(session, "id", None),
    }


__all__ = ["build_osworld_manager", "run_osworld_episode"]
