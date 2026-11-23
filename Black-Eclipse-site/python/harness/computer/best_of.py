"""Best-of-N for desktop tasks: run k trajectories, judge behavior, keep one.

Single-rollout computer use is brittle: one wrong click compounds. Agent S3's
Behavior Best-of-N (`BJudge`) took OSWorld from 66% to 72.6% by running several
rollouts and comparing them at the level of *what happened*, not raw pixels.

This module is the same idea with Eclipse's constraints:

  * a **branch** is one isolated (or serialized) desktop session driven by a
    caller-supplied runner — the engine never invents an agent;
  * a **behavior caption** is deterministic: step count, failed steps, verified
    steps, the action trail, the working notes and the final frame. No extra
    model call is required to describe a branch;
  * selection is deterministic by default (verified success, then fewest failed
    steps, then fewest steps) and can be overridden by a judge callable — the
    place a model-based comparative judge plugs in;
  * an **unsafe** branch can never win, and finance mutations are not eligible
    for N>1 (the caller refuses that combination before creating branches).

The engine is transport-agnostic and unit-tested with fake sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

MAX_ATTEMPTS = 4


@dataclass
class Branch:
    """One completed rollout, reduced to what a judge needs."""

    index: int
    run_id: str
    session_id: str
    success: bool = False
    verified: bool = False
    unsafe: bool = False
    steps: int = 0
    failed_steps: int = 0
    notes: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None
    final_image: Optional[str] = None
    final_state: dict[str, Any] = field(default_factory=dict)
    caption: str = ""
    workflow: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "runId": self.run_id, "sessionId": self.session_id,
            "success": self.success, "verified": self.verified, "unsafe": self.unsafe,
            "steps": self.steps, "failedSteps": self.failed_steps,
            "summary": self.summary, "error": self.error,
            "finalImage": self.final_image, "caption": self.caption,
            "notes": list(self.notes[-10:]), "workflow": self.workflow,
        }


@dataclass
class BestOfOutcome:
    task: str
    attempts: int
    isolated: bool
    chosen: Optional[int]
    reason: str
    judged_by: str
    branches: list[Branch] = field(default_factory=list)
    workflow: Optional[dict[str, Any]] = None
    workflow_stored: bool = False

    def winner(self) -> Optional[Branch]:
        if self.chosen is None:
            return None
        for branch in self.branches:
            if branch.index == self.chosen:
                return branch
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task, "attempts": self.attempts, "isolated": self.isolated,
            "chosen": self.chosen, "reason": self.reason, "judgedBy": self.judged_by,
            "branches": [b.to_dict() for b in self.branches],
            "workflow": self.workflow, "workflowStored": self.workflow_stored,
        }


def behavior_caption(session: Any, branch: Branch, *, task: str = "") -> str:
    """A deterministic narrative of what this branch did.

    This is the comparable unit: two branches are judged by their captions, not
    by a raw screenshot diff. It is intentionally factual (no speculation), so a
    model judge can rely on it and a deterministic judge can parse it.
    """
    trail = " -> ".join(
        f"{a.get('action')}({a.get('result')}/{a.get('verdict')})"
        for a in (branch.actions or [])[-10:]
    )
    parts = [
        f"Task: {task or '(unspecified)'}",
        f"Outcome: {'success' if branch.success else 'not successful'}"
        f"{' (verified)' if branch.verified else ''}.",
        f"Steps: {branch.steps}; failed: {branch.failed_steps}.",
        f"Action trail: {trail or '(no actions)'}.",
    ]
    if branch.notes:
        parts.append("Notes: " + "; ".join(branch.notes[-8:]) + ".")
    if branch.final_state.get("windowTitle"):
        parts.append(f"Final window: {branch.final_state.get('windowTitle')}.")
    if branch.error:
        parts.append(f"Error: {branch.error}.")
    if branch.unsafe:
        parts.append("UNSAFE: this branch attempted a blocked/destructive action.")
    return " ".join(parts)


def deterministic_choice(branches: list[Branch]) -> tuple[Optional[int], str]:
    """Pick the best branch without a model. Explain the pick."""
    eligible = [b for b in branches if not b.unsafe and b.error is None]
    if not eligible:
        return None, "no branch is eligible (all failed or unsafe)"
    succeeded = [b for b in eligible if b.success]
    if not succeeded:
        # Never present a failed trajectory as the winner; the caller can retry
        # or ask a person.
        return None, "no branch succeeded"
    ranked = sorted(
        succeeded,
        key=lambda b: (
            0 if b.verified else 1,
            b.failed_steps,
            b.steps,
            b.index,
        ),
    )
    best = ranked[0]
    if best.verified:
        reason = "the only verified success" if len(succeeded) == 1 else (
            "verified success with the fewest failed steps"
        )
    else:
        reason = "reported success with the fewest failed steps (unverified)"
    return best.index, reason


def _branch_from_session(session: Any, *, index: int, result: dict[str, Any], task: str) -> Branch:
    history = list(getattr(session, "action_history", []) or [])
    failed = sum(1 for a in history if str(a.get("result") or "") != "ok")
    verified = sum(1 for a in history if str(a.get("verification") or "") == "verified")
    snapshot = getattr(session, "latest_snapshot", None)
    success = bool(result.get("success"))
    branch = Branch(
        index=index, run_id=str(getattr(session, "run_id", "") or ""),
        session_id=str(getattr(session, "id", "")),
        success=success,
        verified=success and (bool(result.get("verified")) or verified > 0),
        unsafe=bool(result.get("unsafe")),
        steps=len(history),
        failed_steps=failed,
        notes=list(getattr(session, "notes", []) or []),
        actions=history[-12:],
        summary=str(result.get("summary") or ""),
        error=result.get("error"),
        final_image=getattr(snapshot, "image_path", None) if snapshot is not None else None,
        final_state=snapshot.summary() if snapshot is not None else {},
    )
    branch.caption = behavior_caption(session, branch, task=task)
    if branch.success:
        try:
            from .procedural import build_workflow_candidate  # noqa: PLC0415

            branch.workflow = build_workflow_candidate(session, task)
        except Exception:  # noqa: BLE001 - memory capture never fails a branch
            branch.workflow = None
    return branch


async def run_best_of(
    *,
    manager: Any,
    task: str,
    run_branch: Callable[[Any, int, str, Optional[dict[str, Any]]], Awaitable[dict[str, Any]]],
    attempts: int = 3,
    judge: Optional[Callable[[str, list[str]], Awaitable[int]]] = None,
    on_branch: Optional[Callable[[Branch], None]] = None,
    context: Optional[dict[str, Any]] = None,
    memory: Any = None,
    auto_capture: bool = False,
) -> BestOfOutcome:
    """Run `attempts` branches and choose one.

    `run_branch(session, index)` drives ONE branch and returns
    `{success, verified?, unsafe?, summary?, error?}`. Isolated desktops run
    concurrently; a shared desktop runs branches serially so two trajectories
    can never interleave input.
    """
    attempts = max(1, min(int(attempts or 1), MAX_ATTEMPTS))
    provisioner = getattr(manager, "provisioner", None)
    isolated = bool(getattr(provisioner, "isolated", False) and getattr(provisioner, "available", False))
    outcome = BestOfOutcome(
        task=task, attempts=attempts, isolated=isolated,
        chosen=None, reason="", judged_by="none",
    )
    if attempts == 1:
        session = await _new_branch_session(manager, 0)
        try:
            result = await run_branch(session, 0, task, context)
            branch = _branch_from_session(session, index=0, result=result, task=task)
        except Exception as exc:  # noqa: BLE001 - a crashed branch is data
            branch = Branch(index=0, run_id=str(getattr(session, "run_id", "") or ""),
                            session_id=str(getattr(session, "id", "")), error=str(exc)[:300])
        finally:
            await manager.destroy_session(session.id)
        outcome.branches.append(branch)
        if on_branch:
            on_branch(branch)
        outcome.chosen = 0 if (branch.success and not branch.unsafe) else None
        outcome.reason = "single attempt" if outcome.chosen == 0 else "the single attempt did not succeed"
        if outcome.chosen == 0 and branch.workflow:
            outcome.workflow = branch.workflow
            if auto_capture:
                try:
                    from .procedural import store_workflow  # noqa: PLC0415

                    stored = store_workflow(memory, branch.workflow)
                    outcome.workflow_stored = bool(stored.get("stored"))
                except Exception:  # noqa: BLE001
                    outcome.workflow_stored = False
        outcome.judged_by = "none"
        return outcome

    if isolated:
        sessions: list[Any] = []
        try:
            for index in range(attempts):
                sessions.append(await _new_branch_session(manager, index))

            async def _one(index: int, session: Any) -> tuple[int, Branch]:
                try:
                    result = await run_branch(session, index, task, context)
                    return index, _branch_from_session(session, index=index, result=result, task=task)
                except Exception as exc:  # noqa: BLE001 - a crashed branch is data
                    blank = Branch(index=index, run_id=str(getattr(session, "run_id", "") or ""),
                                   session_id=str(getattr(session, "id", "")), error=str(exc)[:300])
                    return index, blank

            pairs = await asyncio.gather(*[_one(i, s) for i, s in enumerate(sessions)])
            for _index, branch in sorted(pairs, key=lambda p: p[0]):
                outcome.branches.append(branch)
                if on_branch:
                    try:
                        on_branch(branch)
                    except Exception:  # noqa: BLE001 - an observer never fails a run
                        pass
        finally:
            for session in sessions:
                try:
                    await manager.destroy_session(session.id)
                except Exception:  # noqa: BLE001 - teardown must not mask the result
                    pass
    else:
        # Shared desktop: serialize so input from two branches never interleaves.
        for index in range(attempts):
            session = await _new_branch_session(manager, index)
            try:
                result = await run_branch(session, index, task, context)
                branch = _branch_from_session(session, index=index, result=result, task=task)
            except Exception as exc:  # noqa: BLE001
                branch = Branch(index=index, run_id=str(getattr(session, "run_id", "") or ""),
                                session_id=str(getattr(session, "id", "")), error=str(exc)[:300])
            finally:
                await manager.destroy_session(session.id)
            outcome.branches.append(branch)
            if on_branch:
                try:
                    on_branch(branch)
                except Exception:  # noqa: BLE001
                    pass

    # A judge may override the deterministic pick, but not introduce an
    # unsafe/failed branch that the engine excluded.
    eligible = [b for b in outcome.branches if not b.unsafe and b.error is None and b.success]
    choice, reason = deterministic_choice(outcome.branches)
    outcome.judged_by = "deterministic"
    if judge is not None and eligible:
        try:
            picked = await judge(task, [b.caption for b in outcome.branches])
            candidate = next((b for b in eligible if b.index == int(picked)), None)
            if candidate is not None:
                choice, reason = candidate.index, "chosen by the behavior judge"
                outcome.judged_by = "behavior_judge"
        except Exception:  # noqa: BLE001 - a judge failure is not a run failure
            pass
    outcome.chosen, outcome.reason = choice, reason
    winner = outcome.winner()
    if winner is not None and winner.workflow:
        outcome.workflow = winner.workflow
        if auto_capture:
            try:
                from .procedural import store_workflow  # noqa: PLC0415

                stored = store_workflow(memory, winner.workflow)
                outcome.workflow_stored = bool(stored.get("stored"))
            except Exception:  # noqa: BLE001 - memory never fails a run
                outcome.workflow_stored = False
    return outcome


async def _new_branch_session(manager: Any, index: int) -> Any:
    run_id = f"cuabo_{uuid.uuid4().hex[:12]}_{index}"
    session = await manager.create_session(
        agent_id="desktop-agent", run_id=run_id, task_id=run_id,
    )
    # A branch must not write shared memory/adapters: only the winner is learned.
    session.suppress_auto_memory = True
    return session


__all__ = [
    "MAX_ATTEMPTS", "BestOfOutcome", "Branch", "behavior_caption",
    "deterministic_choice", "run_best_of",
]
