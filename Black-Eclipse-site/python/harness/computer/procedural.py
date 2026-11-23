"""Automatic procedural memory: turn a VERIFIED desktop run into a workflow.

The old rule was "memory writes are explicit only". That is safe but forgetful:
every successful desktop task is re-derived from scratch. This module captures a
**candidate** after a verified success, with two guarantees that keep the rule's
spirit:

  * it only fires for a run that ENDED in `done` (or a verified branch) and never
    for one that ended in `fail`;
  * it stores NO screenshots, NO typed text and NO field values — only the action
    sequence, the target labels, the notes and the task, with secret-shaped text
    scrubbed.

By default the candidate is returned to the caller for an explicit
`computer_remember`. `memory.auto_capture=true` additionally records it (tagged
`auto`), so recall can surface it next time; the record is inspectable either way.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Actions that describe a reusable workflow. Observation/noise is excluded.
_STEP_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click", "invoke",
    "type", "key", "hotkey", "set_value", "drag", "press_and_hold",
    "scroll_into_view", "open", "close", "wait_for",
})

_SECRET_TEXT = re.compile(
    r"\b(password|passwd|pwd|token|api[ _-]?key|secret|otp|2fa|pin)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    cleaned = _SECRET_TEXT.sub(r"\1=«redacted»", str(text or ""))
    return " ".join(cleaned.split())[:500]


def store_workflow(memory: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Write a prepared candidate to Eclipse-Memory (the only write path)."""
    if memory is None or not getattr(memory, "available", False):
        return {"stored": False, "reason": "Eclipse-Memory is unavailable"}
    try:
        return memory.remember(
            kind="workflow",
            content=str(candidate.get("content") or ""),
            application=str(candidate.get("application") or ""),
            subject=str(candidate.get("task") or "desktop workflow")[:120],
            structured={
                "steps": candidate.get("steps") or [], "task": candidate.get("task"),
                "verified": True, "source": "auto_capture",
            },
            confidence=float(candidate.get("confidence") or 0.7),
        )
    except Exception as exc:  # noqa: BLE001 - a memory write must never lose a candidate
        return {"stored": False, "reason": str(exc)[:200]}


def build_workflow_candidate(
    session: Any,
    task: str,
    *,
    min_actions: int = 2,
    application: str = "",
) -> Optional[dict[str, Any]]:
    """A workflow candidate for a verified success, or None.

    A run qualifies when its last terminal action is `done` (not `fail`), at
    least `min_actions` real steps ran, and at least one step was verified.
    """
    history = list(getattr(session, "action_history", []) or [])
    if not history:
        return None
    if any(str(a.get("action")) == "fail" for a in history):
        return None
    done = next((a for a in history if str(a.get("action")) == "done"), None)
    if done is None:
        return None
    steps = [
        {
            "action": str(a.get("action")),
            "target": _scrub(str(a.get("target") or "")),
            "verdict": str(a.get("verdict") or ""),
        }
        for a in history
        if str(a.get("action")) in _STEP_ACTIONS and (a.get("target") or a.get("action"))
    ]
    if len(steps) < max(1, int(min_actions)):
        return None
    verified = any(str(a.get("verification")) == "verified" for a in history)
    if not verified:
        return None
    notes = [_scrub(n) for n in (getattr(session, "notes", []) or [])][-8:]
    app = application or _app_of(session)
    trail = " -> ".join(
        f"{step['action']}{(' ' + repr(step['target'])) if step['target'] else ''}"
        for step in steps[:12]
    )
    content = _scrub(
        f"Verified desktop workflow for {app or 'this application'}: {task}. "
        f"Steps: {trail}."
        + (f" Notes: {'; '.join(notes)}." if notes else "")
    )
    return {
        "kind": "workflow",
        "application": app,
        "task": _scrub(task),
        "steps": steps[:20],
        "notes": notes,
        "verified": True,
        "content": content,
        "confidence": 0.7,
        "source": "auto_capture",
    }


def _app_of(session: Any) -> str:
    snapshot = getattr(session, "latest_snapshot", None)
    if snapshot is None:
        return ""
    # Never let an adapter's DISPLAY suffix become the application id.
    return str(getattr(snapshot, "application", "") or "").split(" (learned v")[0].strip()


def capture_after_success(
    session: Any,
    task: str,
    *,
    memory: Any = None,
    auto: bool = False,
    min_actions: int = 2,
    application: str = "",
) -> dict[str, Any]:
    """Build the candidate, and store it only when `auto` is on."""
    candidate = build_workflow_candidate(
        session, task, min_actions=min_actions, application=application,
    )
    if candidate is None:
        return {
            "candidate": None, "stored": False,
            "reason": "the run is not a verified success with enough steps",
        }
    if not auto:
        return {
            "candidate": candidate, "stored": False,
            "reason": "auto-capture is off; the candidate is ready for computer_remember",
        }
    stored = store_workflow(memory, candidate)
    return {"candidate": candidate, "stored": bool(stored.get("stored")), "memory": stored}


__all__ = ["build_workflow_candidate", "capture_after_success", "store_workflow"]
