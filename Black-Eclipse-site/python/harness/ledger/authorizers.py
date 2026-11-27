"""The General Ledger's effect authorizer.

A `ledger_*` MUTATION (permission `write`, effect `ledger.write`) is admitted HERE
and must be backed by a SINGLE-USE, digest-bound operator approval of kind
`ledger_write`. Read tools never reach an authorizer (the registry skips reads),
so this layer only ever sees a mutation.

Why this exists at all: without a branch at `code/authorizers.py`, an effectful
tool the registry does not know falls through to the MCP authorizer, which
refuses anything without an MCP server id — leaving the tool advertised to the
agent but dead. That is the exact defect class documented in
`portfolio/authorizers.py`.

Safety: admission is NOT authorization. Returning True unconditionally would
turn a gated write into a free one. The layer mirrors the portfolio/Git
authorizers — a standing grant, a single-use `consume_kind` claim, or a
request-then-decision pause — and otherwise refuses. The digest binds the exact
tool + arguments + ledger, so a changed payload needs a NEW approval.
"""

from __future__ import annotations

from typing import Any, Optional

from ..mcp.approvals import _await_then_reclaim, _mark_authorized, _pause_requested, args_digest
from ..sod import effect_amount
from .tools import LEDGER_WRITE_TOOLS

LEDGER_APPROVAL_KIND = "ledger_write"

_LEDGER_MUTATIONS = frozenset(LEDGER_WRITE_TOOLS)


def owns(tool_name: str) -> bool:
    """Whether this authorizer owns the tool (a ledger mutation)."""
    return tool_name in _LEDGER_MUTATIONS


def _scope(args: dict[str, Any], workspace_id: str) -> str:
    """The grant/approval scope: the ledger, else the workspace, else empty."""
    return str(args.get("ledger_id") or args.get("ledgerId") or workspace_id or "")


def _digest(scope: str, tool_name: str, args: dict[str, Any]) -> str:
    return args_digest({"scope": scope, "tool": tool_name, "args": args or {}})


def _summary(tool_name: str, args: dict[str, Any]) -> str:
    label = tool_name.replace("ledger_", "").replace("_", " ")
    ledger_id = args.get("ledger_id") or args.get("ledgerId")
    if tool_name == "ledger_post":
        lines = args.get("lines") or []
        detail = f"{len(lines)} line(s)" if isinstance(lines, list) else "lines"
        return f"Post a journal entry ({detail}) to ledger {ledger_id}."
    if ledger_id:
        return f"Ledger change: {label} in ledger {ledger_id}."
    name = args.get("name")
    if name:
        return f"Ledger change: {label} ({name})."
    return f"Ledger change: {label}."


def make_ledger_authorizer(
    approvals: Optional[Any], run_id: str, workspace_id: str = "",
    emitter: Any = None, inner: Any = None,
):
    """Compose the Ledger admission + approval layer over `inner`."""

    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if not owns(name):
            if inner is not None:
                return bool(await inner(tool, args, ctx))
            return False

        args = args or {}
        if approvals is None:
            # No store: the request can neither be claimed nor created. Refuse;
            # never authorize on a missing authority.
            return False

        effect = str(getattr(tool, "effect", "") or "ledger.write")
        ws_id = str(
            workspace_id or getattr(getattr(ctx, "workspace", None), "id", "") or ""
        )
        scope = _scope(args, ws_id)
        digest = _digest(scope, name, args)
        request_key = f"ledger:{name}"
        requester = (
            getattr(ctx, "user_id", None) or getattr(ctx, "actor_id", None)
            if ctx is not None else None
        )

        # 0) Optional segregation-of-duties gate. OFF by default: with no policy
        #    configured `check()` is satisfied and this is a no-op. A configured
        #    policy needs N distinct approvers IN ADDITION to the single-use
        #    approval below; a broken SoD store fails closed.
        sod = getattr(approvals, "sod", None)
        sod_status: Optional[dict[str, Any]] = None
        sod_blocked = False
        if sod is not None:
            try:
                sod_status = sod.check(
                    effect, effect_amount(name, args), digest, requester=requester
                )
                sod_blocked = not bool(sod_status.get("satisfied", True))
            except Exception as exc:  # noqa: BLE001 - configured SoD must fail closed
                sod_status = {"satisfied": False, "error": str(exc), "required": None,
                              "collected": []}
                sod_blocked = True

        def _consume_sod() -> None:
            if sod is not None and sod_status is not None:
                try:
                    sod.consume(effect, digest)
                except Exception:  # noqa: BLE001 - never turn a consume failure into a pass
                    pass

        # 1) Already authorized for THIS call-scope at an earlier seam.
        if not sod_blocked and ctx is not None and getattr(ctx, "effect_authorized_digest", None) == digest:
            return True

        # 2) A standing grant ("always allow this tool for this ledger").
        grants = getattr(approvals, "grants", None)
        if not sod_blocked and grants is not None and scope:
            try:
                match = grants.matches(LEDGER_APPROVAL_KIND, scope, name)
            except Exception:  # noqa: BLE001 - a broken grant store must not authorize
                match = None
            if match is not None:
                _consume_sod()
                _mark_authorized(ctx, digest)
                if emitter is not None:
                    await emitter.emit("ledger.action.approved", {
                        "tool": name, "grant": True, "ledgerId": scope,
                    })
                return True

        # 3) A single-use approval already granted for this exact digest.
        if not sod_blocked:
            claimed = approvals.consume_kind(
                kind=LEDGER_APPROVAL_KIND, request_key=request_key, digest=digest,
                workspace_id=ws_id or None,
            )
            if claimed is not None:
                _consume_sod()
                _mark_authorized(ctx, digest)
                if emitter is not None:
                    await emitter.emit("ledger.action.approved", {
                        "tool": name, "approvalId": claimed.get("id"), "singleUse": True,
                    })
                return True

        # 4) Create/refresh the pending request and (in pause mode) wait.
        summary = _summary(name, args)
        if sod_blocked and sod_status:
            summary += (
                f" Segregation of duties: {sod_status.get('remaining', '?')} more "
                f"distinct approval(s) required."
            )
        pending = None
        try:
            pending = approvals.request_kind(
                kind=LEDGER_APPROVAL_KIND, request_key=request_key, digest=digest,
                effect="external_action",
                run_id=run_id, workspace_id=ws_id or None,
                action_summary=summary,
                risk="medium",
                grant_kind=LEDGER_APPROVAL_KIND,
                grant_pattern=name,
                grant_scope=scope or None,
            )
            if emitter is not None:
                await emitter.emit("approval.requested", {
                    "approvalId": (pending or {}).get("id"),
                    "kind": LEDGER_APPROVAL_KIND,
                    "title": f"Approve {name.replace('_', ' ')}",
                    "summary": summary,
                    "effect": effect, "risk": "medium", "tool": name,
                    "ledgerId": scope or None,
                    "workspaceId": ws_id,
                    "grantPattern": name,
                    "grantLabel": (
                        f"Always allow `{name}` for this ledger" if scope else None
                    ),
                })
                if sod_blocked and sod_status:
                    await emitter.emit("sod.pending", {
                        "tool": name, "effect": effect, "digest": digest,
                        "required": sod_status.get("required"),
                        "collected": sod_status.get("collected", []),
                        "remaining": sod_status.get("remaining"),
                        "amount": sod_status.get("amount"),
                        "ledgerId": scope or None,
                    })
        except Exception:  # noqa: BLE001 - a store failure must never authorize
            pending = None

        if sod_blocked:
            # The operator must record the missing sign-off(s); do not pause on the
            # single-use card, because approving it once would not satisfy SoD.
            return False

        if pending is not None and ctx is not None and _pause_requested(ctx):
            await _await_then_reclaim(
                approvals, pending.get("id"),
                lambda: approvals.consume_kind(
                    kind=LEDGER_APPROVAL_KIND, request_key=request_key, digest=digest,
                    workspace_id=ws_id or None,
                ),
                emitter, ctx, digest, {"tool": name},
                event_type="ledger.action.approved",
            )
            if ctx is not None and getattr(ctx, "effect_authorized_digest", None) == digest:
                _consume_sod()
                return True

        # 5) Nothing claimed it: refuse. Never True by default.
        return False

    return authorize


__all__ = ["LEDGER_APPROVAL_KIND", "make_ledger_authorizer", "owns"]
