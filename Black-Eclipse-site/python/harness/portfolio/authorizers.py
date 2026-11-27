"""The Portfolio Accounting Engine's effect authorizer.

A `portfolio_*` MUTATION (permission `write`: the `portfolio.write` family and the
consequential `portfolio.rebalance_proposal` proposals) is admitted HERE and must
be backed by a SINGLE-USE, digest-bound operator approval of kind
`portfolio_write`. A read tool never reaches an authorizer (the registry skips
reads), so this layer only ever sees a mutation.

Without this branch the family fell through every layer of
`code/authorizers.build_effect_authorizer` to the MCP authorizer, which refuses
any tool without an MCP server id — leaving the tools advertised to `ECLIPSE_AGENT`
(`agents.py`) but dead. Measured 2026-10-14: `portfolio_create` returned
`TOOL_FORBIDDEN` ("has effect 'portfolio.write' and is not approved for this
call"). Identical defect class to the runtime family already fixed by
`_admit_runtime_tools` (`test_authorizer_runtime_admission.py`).

Safety: admission alone is NOT enough. Returning True unconditionally would turn a
dead tool into an ungated one. The layer therefore mirrors the Git authorizer
(`git/approvals.py`): a standing grant, a single-use `consume_kind` claim, or a
request-then-decision pause — and otherwise False, never True by default. The
digest binds the exact tool + arguments (and the portfolio/scope), so a changed
payload needs a NEW approval.
"""

from __future__ import annotations

from typing import Any, Optional

from ..mcp.approvals import _await_then_reclaim, _mark_authorized, _pause_requested, args_digest
from ..sod import effect_amount
from .tools import PORTFOLIO_CONSEQUENTIAL_TOOLS, PORTFOLIO_WRITE_TOOLS

PORTFOLIO_APPROVAL_KIND = "portfolio_write"

# Every tool this layer owns. Reads are never sent here by the registry; naming
# them anyway would be harmless, but the two write families are exact.
_PORTFOLIO_MUTATIONS = frozenset(PORTFOLIO_WRITE_TOOLS) | frozenset(PORTFOLIO_CONSEQUENTIAL_TOOLS)


def owns(tool_name: str) -> bool:
    """Whether this authorizer owns the tool (a portfolio mutation)."""
    return tool_name in _PORTFOLIO_MUTATIONS


def _scope(args: dict[str, Any], workspace_id: str) -> str:
    """The grant/approval scope: the portfolio, else the workspace, else empty."""
    return str(args.get("portfolio_id") or args.get("owner_id") or workspace_id or "")


def _digest(scope: str, tool_name: str, args: dict[str, Any]) -> str:
    """A digest over scope + tool + canonicalized args (a changed payload differs)."""
    return args_digest({"scope": scope, "tool": tool_name, "args": args or {}})


def _summary(tool_name: str, args: dict[str, Any]) -> str:
    label = tool_name.replace("portfolio_", "").replace("_", " ")
    portfolio_id = args.get("portfolio_id")
    if portfolio_id:
        return f"Portfolio change: {label} in portfolio {portfolio_id}."
    name = args.get("name")
    if name:
        return f"Portfolio change: {label} ({name})."
    return f"Portfolio change: {label}."


def make_portfolio_authorizer(
    approvals: Optional[Any], run_id: str, workspace_id: str = "",
    emitter: Any = None, inner: Any = None,
):
    """Compose the Portfolio admission + approval layer over `inner`.

    A portfolio mutation resolves to a single-use approval of kind
    `portfolio_write` bound to the exact scope + tool + arguments. It is admitted
    only when the call-scope digest is already authorized, a standing grant
    matches, or a single-use approval is claimed (consuming, or pausing for the
    operator's decision). Otherwise — including when no approval store is wired —
    it falls through to `inner`, so the chain's refusal is never weakened.
    """

    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if not owns(name):
            if inner is not None:
                return bool(await inner(tool, args, ctx))
            return False

        args = args or {}
        if approvals is None:
            # No store: the request can neither be claimed nor created. Refuse
            # (the caller composes this layer only when a store exists in a real
            # run); never authorize on a missing authority.
            return False

        effect = str(getattr(tool, "effect", "") or "portfolio.write")
        ws_id = str(
            workspace_id or getattr(getattr(ctx, "workspace", None), "id", "") or ""
        )
        scope = _scope(args, ws_id)
        digest = _digest(scope, name, args)
        request_key = f"portfolio:{name}"
        requester = (
            getattr(ctx, "user_id", None) or getattr(ctx, "actor_id", None)
            if ctx is not None else None
        )

        # 0) Optional segregation-of-duties gate (off by default; a configured
        #    policy needs N distinct approvers in addition to the single-use one).
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
                except Exception:  # noqa: BLE001
                    pass

        # 1) Already authorized for THIS call-scope at an earlier seam.
        if not sod_blocked and ctx is not None and getattr(ctx, "effect_authorized_digest", None) == digest:
            return True

        # 2) A standing grant ("always allow this tool for this portfolio").
        grants = getattr(approvals, "grants", None)
        if not sod_blocked and grants is not None and scope:
            try:
                match = grants.matches(PORTFOLIO_APPROVAL_KIND, scope, name)
            except Exception:  # noqa: BLE001 - a broken grant store must not authorize
                match = None
            if match is not None:
                _consume_sod()
                _mark_authorized(ctx, digest)
                if emitter is not None:
                    await emitter.emit("portfolio.action.approved", {
                        "tool": name, "grant": True, "portfolioId": scope,
                    })
                return True

        # 3) A single-use approval already granted for this exact digest.
        if not sod_blocked:
            claimed = approvals.consume_kind(
                kind=PORTFOLIO_APPROVAL_KIND, request_key=request_key, digest=digest,
                workspace_id=ws_id or None,
            )
            if claimed is not None:
                _consume_sod()
                _mark_authorized(ctx, digest)
                if emitter is not None:
                    await emitter.emit("portfolio.action.approved", {
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
                kind=PORTFOLIO_APPROVAL_KIND, request_key=request_key, digest=digest,
                effect="external_action",
                run_id=run_id, workspace_id=ws_id or None,
                action_summary=summary,
                risk="medium",
                grant_kind=PORTFOLIO_APPROVAL_KIND,
                grant_pattern=name,
                grant_scope=scope or None,
            )
            if emitter is not None:
                await emitter.emit("approval.requested", {
                    "approvalId": (pending or {}).get("id"),
                    "kind": PORTFOLIO_APPROVAL_KIND,
                    "title": f"Approve {name.replace('_', ' ')}",
                    "summary": summary,
                    "effect": effect, "risk": "medium", "tool": name,
                    "portfolioId": scope or None,
                    "workspaceId": ws_id,
                    "grantPattern": name,
                    "grantLabel": (
                        f"Always allow `{name}` for this portfolio" if scope else None
                    ),
                })
                if sod_blocked and sod_status:
                    await emitter.emit("sod.pending", {
                        "tool": name, "effect": effect, "digest": digest,
                        "required": sod_status.get("required"),
                        "collected": sod_status.get("collected", []),
                        "remaining": sod_status.get("remaining"),
                        "amount": sod_status.get("amount"),
                        "portfolioId": scope or None,
                    })
        except Exception:  # noqa: BLE001 - a store failure must never authorize
            pending = None

        if sod_blocked:
            return False

        if pending is not None and ctx is not None and _pause_requested(ctx):
            await _await_then_reclaim(
                approvals, pending.get("id"),
                lambda: approvals.consume_kind(
                    kind=PORTFOLIO_APPROVAL_KIND, request_key=request_key, digest=digest,
                    workspace_id=ws_id or None,
                ),
                emitter, ctx, digest, {"tool": name},
                event_type="portfolio.action.approved",
            )
            if ctx is not None and getattr(ctx, "effect_authorized_digest", None) == digest:
                _consume_sod()
                return True

        # 5) Nothing claimed it: refuse. Never True by default.
        return False

    return authorize


__all__ = ["PORTFOLIO_APPROVAL_KIND", "make_portfolio_authorizer", "owns"]
