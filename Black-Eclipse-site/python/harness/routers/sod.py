"""Operator-only segregation-of-duties policy + sign-off routes.

These routes are deliberately NOT agent tools: an agent must never be able to
satisfy a policy that exists to constrain it. The finance authorizers read the
collected sign-offs; this surface only configures the policy and records a human
decision.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..deps import require_operator, service
from ..sod import SoDError

router = APIRouter(tags=["sod"])

_STATUS = {
    "SOD_EFFECT_REQUIRED": 422,
    "SOD_INVALID": 422,
    "SOD_APPROVER_REQUIRED": 422,
    "SOD_SELF_APPROVAL": 409,
}


def _sod() -> Any:
    s = getattr(service, "sod", None)
    if s is None:
        raise HTTPException(status_code=503, detail="segregation-of-duties is unavailable")
    return s


def _actor(request: Request, body: Optional[dict[str, Any]] = None,
           query_actor: Optional[str] = None) -> Optional[str]:
    return (
        request.headers.get("x-harness-user")
        or (body or {}).get("actorId")
        or query_actor
    )


def _raise(exc: SoDError) -> None:
    raise HTTPException(
        status_code=_STATUS.get(exc.code, 400),
        detail=exc.to_dict().get("message") or exc.code,
    ) from exc


@router.get("/sod/policies")
async def sod_policies(request: Request, effect: Optional[str] = None,
                       active_only: bool = True,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"policies": _sod().list_policies(effect, active_only=active_only)}


@router.post("/sod/policies")
async def sod_set_policy(request: Request, body: dict[str, Any] = Body(...),
                         _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _sod().set_policy(
            str(body.get("effect") or ""),
            threshold_amount=body.get("threshold_amount", body.get("thresholdAmount", 0)),
            required_approvals=int(body.get("required_approvals", body.get("requiredApprovals", 2))),
            separate_requester=bool(body.get("separate_requester", body.get("separateRequester", True))),
            actor=_actor(request, body),
        )
    except SoDError as exc:
        _raise(exc)


@router.delete("/sod/policies/{policy_id}")
async def sod_deactivate_policy(policy_id: str, request: Request,
                                _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"deactivated": _sod().deactivate_policy(policy_id)}


@router.get("/sod/status")
async def sod_status(request: Request, effect: str, digest: str,
                     amount: Optional[str] = None, requester: Optional[str] = None,
                     _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _sod().check(effect, amount or 0, digest,
                        requester=requester or _actor(request))


@router.post("/sod/signoffs")
async def sod_signoff(request: Request, body: dict[str, Any] = Body(...),
                      _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _sod().sign_off(
            str(body.get("effect") or ""),
            str(body.get("digest") or ""),
            str(body.get("approver") or _actor(request, body) or ""),
            amount=body.get("amount", 0),
            requester=body.get("requester"),
        )
    except SoDError as exc:
        _raise(exc)


@router.get("/sod/signoffs")
async def sod_signoffs(request: Request, effect: str, digest: str,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"signoffs": _sod().list_signoffs(effect, digest)}
