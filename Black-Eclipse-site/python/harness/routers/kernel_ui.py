"""Kernel UI slot API (operator-gated)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from ..deps import require_operator
from ..kernel.services.runtime import kernel_runtime

router = APIRouter(prefix="/kernel/ui", tags=["kernel"], dependencies=[Depends(require_operator)])


@router.get("/slots")
def slots() -> dict[str, Any]:
    service = kernel_runtime.ui_slots
    return {
        "enabled": kernel_runtime.enabled,
        "active": kernel_runtime.active,
        "mode": kernel_runtime.mode,
        "slots": service.slots() if service is not None else [],
        "entries": service.entries() if service is not None else [],
    }


@router.get("/slots/{slot}")
def slot_entries(slot: str) -> dict[str, Any]:
    service = kernel_runtime.ui_slots
    if service is None:
        return {"slot": slot, "entries": []}
    return {"slot": slot, "entries": service.entries(slot)}


@router.get("/slots/{slot}/{entry_id}")
def render(slot: str, entry_id: str) -> dict[str, Any]:
    service = kernel_runtime.ui_slots
    if service is None:
        raise HTTPException(status_code=404, detail={"code": "KERNEL_UI_OFF"})
    try:
        return service.render(slot, entry_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "KERNEL_UI_SLOT_NOT_FOUND", "message": str(exc)})
