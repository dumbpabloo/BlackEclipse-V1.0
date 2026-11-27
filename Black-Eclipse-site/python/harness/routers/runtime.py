"""Runtime lifecycle routes: cooperative drain before a supervised restart.

A supervised replacement may only kill the process after in-flight runs finish.
`POST /runtime/drain` flips the admission flag (new runs get 503) and waits,
bounded, for the active-run registry to empty. The front door calls it before
clearing a genuinely silent port.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import require_operator, service
from ..runtime_state import begin_draining, is_draining, wait_for_idle

router = APIRouter(tags=["runtime"])


def require_operator_accepting_runs(_: bool = Depends(require_operator)) -> bool:
    """Operator gate + admission gate: a draining runtime refuses new work."""
    if is_draining():
        raise HTTPException(
            status_code=503,
            detail="the agent runtime is draining for a supervised restart; retry shortly",
        )
    return True


@router.post("/runtime/drain")
async def drain(
    timeout_s: float = Query(30.0, ge=0.0, le=300.0),
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Refuse new runs, wait for the active ones, report what is left."""
    begin_draining()
    status = await wait_for_idle(service, timeout_s)
    return {"success": True, "draining": True, "timeoutS": timeout_s, **status}
