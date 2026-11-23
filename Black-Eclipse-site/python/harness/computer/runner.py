"""The best-of-N branch runner: ONE desktop-agent rollout on a bound session.

Best-of-N needs a real agent per branch, not a simulated one. This module wires
the existing run graph to a `ComputerSession`: the branch run carries the
session's run id, the computer tools resolve their desktop through
`ComputerManager.session_for_run`, and the child runs the dedicated
`desktop-agent` preset (without the session-creation/recursion tools).

It is installed on the manager at boot. If the service is not available (a bare
ComputerManager used in tests or a CLI), `branch_runner` stays None and the
`computer_run` tool reports an honest UNAVAILABLE.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("harness.computer.runner")

_BRANCH_PREAMBLE = (
    "\n\n## Best-of-N branch\n"
    "A desktop session is ALREADY OPEN and bound to this run. Do NOT call "
    "`computer_session_start` and do NOT create another session. Capture it, do "
    "the task, keep notes, and finish with `computer_use(action=\"done\")` only "
    "when the task is verified — or `action=\"fail\"` when it is impossible.\n"
)


def install_branch_runner(service: Any) -> None:
    """Attach the desktop-agent branch runner to the process-wide manager."""
    manager = getattr(service, "computer", None)
    if manager is None:
        return

    async def run_branch(session: Any, index: int, task: str,
                         context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return await _run_branch(service, session, index, task, context or {})

    manager.branch_runner = run_branch


async def _run_branch(service: Any, session: Any, index: int, task: str,
                      context: dict[str, Any]) -> dict[str, Any]:
    from ..agents import get_agent
    from ..graph import execute_run
    from ..schemas import AgentConfig, RunInput, RunRequest

    settings = context.get("modelSettings")
    if settings is None:
        return {
            "success": False, "verified": False, "unsafe": False,
            "summary": "no model settings were available for the branch",
            "error": "no model settings",
        }
    base = get_agent("desktop-agent")
    # A branch may not start a new session or recursively start best-of-N.
    tools = [
        name for name in (base.tools or [])
        if name not in ("computer_session_start", "computer_run")
    ]
    agent = AgentConfig(
        id=base.id, name=base.name,
        systemPrompt=(base.systemPrompt + _BRANCH_PREAMBLE),
        tools=tools,
        maxSteps=base.maxSteps,
        maxToolCalls=base.maxToolCalls,
        toolTimeoutMs=base.toolTimeoutMs,
        runTimeoutMs=base.runTimeoutMs,
    )
    request = RunRequest(
        runId=str(session.run_id),
        input=RunInput(query=task),
        agentId=agent.id,
        agent=agent,
        modelSettings=settings,
        parentRunId=context.get("parentRunId"),
        autonomy=context.get("autonomy"),
        callbackBaseUrl=os.environ.get("HARNESS_CALLBACK_URL", "http://127.0.0.1"),
    )
    try:
        result = await execute_run(
            request, service.registry, service.server_bridge(), service.store, service.bus,
            **service._run_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001 - a crashed branch is data
        log.warning("best-of-N branch %s crashed", index, exc_info=True)
        return {
            "success": False, "verified": False, "unsafe": False,
            "summary": f"branch {index + 1} crashed", "error": str(exc)[:300],
        }
    history = list(getattr(session, "action_history", []) or [])
    failed_action = next((a for a in history if str(a.get("action")) == "fail"), None)
    done_action = next((a for a in history if str(a.get("action")) == "done"), None)
    verified = any(str(a.get("verification")) == "verified" for a in history)
    status = str(getattr(result, "status", "failed"))
    success = (
        status == "completed"
        and failed_action is None
        and done_action is not None
        and bool(history)
    )
    summary = str(getattr(result, "output", "") or "")[:400]
    return {
        "success": success,
        "verified": verified,
        "unsafe": False,
        "summary": summary,
        "error": None if status == "completed" else f"run status: {status}",
        "steps": len(history),
        "done": done_action is not None,
    }


__all__ = ["install_branch_runner"]
