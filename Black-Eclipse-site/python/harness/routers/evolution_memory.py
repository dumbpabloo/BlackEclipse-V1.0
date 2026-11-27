"""Self-Improvement Memory route, split out to keep the evolution group small.

The ledger is read-only evidence: this route can show what the optimizer
remembered, but it can never promote, enable or land anything.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..deps import require_operator, service

router = APIRouter(tags=["evolution"])


@router.get("/evolution/rsl/memory")
def rsl_memory(_: bool = Depends(require_operator)) -> dict[str, Any]:
    """The SIM ledger, its stats and its rendered block.

    Read-only evidence: the ledger cannot promote, enable or land anything.
    When SIM is off this is an explicit `enabled: false`, not an error.
    """
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    if not getattr(loop.config, "memory", False):
        return {"success": True, "enabled": False, "memory": None, "stats": None}
    memory = loop.improvement_memory()
    return {
        "success": True,
        "enabled": True,
        "path": loop.memory_config().resolved_path,
        "error": loop.memory_status().get("error"),
        "memory": memory.to_dict() if memory is not None else None,
        "stats": memory.stats() if memory is not None else None,
        "rendered": loop.memory_block(),
    }


__all__ = ["router"]
