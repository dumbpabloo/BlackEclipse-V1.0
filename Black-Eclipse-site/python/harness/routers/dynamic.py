"""Operator API for session-scoped dynamic packages.

Every route is operator-gated. `run`/`update` require `approved: true`; the
service never starts model-generated code without an explicit approval.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import require_operator
from ..dynamic.runtime import dynamic_packages
from ..dynamic.service import DynamicError

router = APIRouter(prefix="/dynamic", tags=["dynamic"], dependencies=[Depends(require_operator)])


class DefineBody(BaseModel):
    sessionId: str
    source: str
    label: str = ""


class RunBody(BaseModel):
    sessionId: str
    packageId: str
    approved: bool = False
    ttlSeconds: Optional[int] = None
    maxCalls: int = 100
    maxRuntimeS: int = 60


class InvokeBody(BaseModel):
    sessionId: str
    payload: dict[str, Any] = Field(default_factory=dict)


class UpdateBody(BaseModel):
    sessionId: str
    source: str
    label: str = ""
    approved: bool = False


class SessionBody(BaseModel):
    sessionId: str


def _error(exc: DynamicError) -> HTTPException:
    status = 409 if exc.code in ("DYNAMIC_APPROVAL_REQUIRED", "DYNAMIC_NOT_RUNNING") else 400
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


@router.get("")
def status(sessionId: str, pluginId: Optional[str] = None) -> dict[str, Any]:
    return dynamic_packages().status(session_id=sessionId, plugin_id=pluginId)


@router.post("/{plugin_id}/define")
def define(plugin_id: str, body: DefineBody) -> dict[str, Any]:
    try:
        return dynamic_packages().define(
            session_id=body.sessionId, plugin_id=plugin_id, source=body.source, label=body.label,
        )
    except DynamicError as exc:
        raise _error(exc)


@router.post("/{plugin_id}/run")
async def run(plugin_id: str, body: RunBody) -> dict[str, Any]:
    try:
        return await dynamic_packages().run(
            session_id=body.sessionId, plugin_id=plugin_id, package_id=body.packageId,
            approved=body.approved, ttl_seconds=body.ttlSeconds,
            max_calls=body.maxCalls, max_runtime_s=body.maxRuntimeS,
        )
    except DynamicError as exc:
        raise _error(exc)


@router.post("/{plugin_id}/invoke")
async def invoke(plugin_id: str, body: InvokeBody) -> dict[str, Any]:
    try:
        return await dynamic_packages().invoke(
            session_id=body.sessionId, plugin_id=plugin_id, payload=body.payload,
        )
    except DynamicError as exc:
        raise _error(exc)


@router.post("/{plugin_id}/stop")
async def stop(plugin_id: str, body: SessionBody) -> dict[str, Any]:
    return await dynamic_packages().stop(session_id=body.sessionId, plugin_id=plugin_id)


@router.post("/{plugin_id}/update")
async def update(plugin_id: str, body: UpdateBody) -> dict[str, Any]:
    try:
        return await dynamic_packages().update(
            session_id=body.sessionId, plugin_id=plugin_id, source=body.source,
            label=body.label, approved=body.approved,
        )
    except DynamicError as exc:
        raise _error(exc)


@router.post("/{plugin_id}/undefine")
async def undefine(plugin_id: str, body: SessionBody) -> dict[str, Any]:
    return await dynamic_packages().undefine(session_id=body.sessionId, plugin_id=plugin_id)
