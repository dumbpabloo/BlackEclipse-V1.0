"""Agent-facing file checkpoint tools (W7).

`checkpoint_list` and `checkpoint_diff` are read-only. `checkpoint_restore` is
effectful: the graph's authorizer requires a single-use approval for it, and the
handler then takes a pre-restore snapshot before writing anything.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..tools import ToolContext, ToolDefinition
from . import checkpoints


def _root(ctx: ToolContext) -> Any:
    ws = getattr(ctx, "workspace", None)
    root = getattr(ws, "root", None) if ws is not None else None
    if root is None:
        raise checkpoints.CheckpointError(
            "WORKSPACE_UNBOUND", "There is no project folder to restore into.",
        )
    return root


async def _emit(ctx: ToolContext, event_type: str, data: dict[str, Any]) -> None:
    try:
        from .tools import emit_code_event  # noqa: PLC0415

        await emit_code_event(ctx, event_type, data)
    except Exception:  # noqa: BLE001 - observability must not break a restore
        pass


async def checkpoint_list(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    try:
        items = checkpoints.list_checkpoints(_root(ctx), limit=int(args.get("limit") or 20))
    except checkpoints.CheckpointError as exc:
        return f"{exc.code}: {exc.reason}", {"status": "unavailable", "code": exc.code}
    if not items:
        return (
            "No file snapshots yet. A snapshot is taken before the first file change.",
            {"status": "ok", "checkpoints": []},
        )
    lines = ["Recent snapshots:"]
    for item in items:
        lines.append(
            f"- {item.get('id')} — {item.get('reason') or 'before-write'}"
            f" ({item.get('files') or 0} files, {item.get('createdAt') or item.get('createdAt') or ''})"
        )
    return "\n".join(lines), {"status": "ok", "checkpoints": items}


async def checkpoint_diff(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    checkpoint_id = str(args.get("checkpointId") or "").strip()
    if not checkpoint_id:
        return "CHECKPOINT_PATH_REQUIRED: name the snapshot to diff.", {"status": "refused"}
    try:
        result = checkpoints.diff(
            _root(ctx), checkpoint_id,
            paths=args.get("paths") if isinstance(args.get("paths"), list) else None,
        )
    except checkpoints.CheckpointError as exc:
        return f"{exc.code}: {exc.reason}", {"status": "unavailable", "code": exc.code}
    text = result["unified"] or "(no differences)"
    return text[:60_000], {"status": "ok", **result}


async def checkpoint_restore(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    checkpoint_id = str(args.get("checkpointId") or "").strip()
    if not checkpoint_id:
        return "CHECKPOINT_PATH_REQUIRED: name the snapshot to restore.", {"status": "refused"}
    paths = args.get("paths") if isinstance(args.get("paths"), list) else None
    restore_all = bool(args.get("all"))
    try:
        result = checkpoints.restore(
            _root(ctx), checkpoint_id,
            paths=paths, all=restore_all,
            run_id=str(getattr(ctx, "run_id", "") or ""),
            turn_key=str(getattr(ctx, "run_id", "") or ""),
        )
    except checkpoints.CheckpointError as exc:
        return f"{exc.code}: {exc.reason}", {"status": "refused", "code": exc.code}
    await _emit(ctx, "checkpoint.restored", {
        "checkpointId": checkpoint_id, "preRestoreId": result.get("preRestoreId"),
        "restored": len(result.get("restored") or []), "skipped": len(result.get("skipped") or []),
    })
    many = "Everything tracked" if restore_all else f"{len(result.get('restored') or [])} file(s)"
    return (
        f"Restored {many} from snapshot {checkpoint_id}. "
        f"A pre-restore snapshot keeps this undoable ({result.get('preRestoreId')}).",
        {"status": "restored", **result},
    )


async def checkpoint_undo_turn(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    """Undo the most recent turn by restoring its before-write snapshot.

    Gap #10: the agent-facing "undo this turn". The snapshot is the latest one
    recorded for THIS run (taken before the turn's first write); restore is the
    same approval-gated path as `checkpoint_restore`, with `all=true`.
    """
    run_id = str(getattr(ctx, "run_id", "") or "")
    try:
        items = checkpoints.list_checkpoints(_root(ctx), limit=200)
    except checkpoints.CheckpointError as exc:
        return f"{exc.code}: {exc.reason}", {"status": "unavailable", "code": exc.code}
    mine = [cp for cp in items if str(cp.get("runId") or "") == run_id]
    if not mine:
        return (
            "CHECKPOINT_NOT_FOUND: there is no file snapshot for this task yet.",
            {"status": "refused", "code": "CHECKPOINT_NOT_FOUND"},
        )
    latest = mine[0]
    try:
        result = checkpoints.restore(
            _root(ctx), str(latest.get("id") or ""),
            all=True, run_id=run_id,
            turn_key=f"undo:{getattr(ctx, 'checkpoint_turn', '') or run_id}",
        )
    except checkpoints.CheckpointError as exc:
        return f"{exc.code}: {exc.reason}", {"status": "refused", "code": exc.code}
    await _emit(ctx, "checkpoint.restored", {
        "checkpointId": latest.get("id"), "preRestoreId": result.get("preRestoreId"),
        "restored": len(result.get("restored") or []), "skipped": len(result.get("skipped") or []),
        "undoTurn": True,
    })
    restored = result.get("restored") or []
    return (
        f"Undid the last turn: restored {len(restored)} file(s) to the snapshot taken "
        f"before it ({latest.get('id')}). A pre-restore snapshot keeps this undoable "
        f"({result.get('preRestoreId')}).",
        {"status": "restored", "undoTurn": True, **result},
    )


_LIST_SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
    "additionalProperties": False,
}
_DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "checkpointId": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["checkpointId"],
    "additionalProperties": False,
}
_RESTORE_SCHEMA = {
    "type": "object",
    "properties": {
        "checkpointId": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
        "all": {"type": "boolean"},
    },
    "required": ["checkpointId"],
    "additionalProperties": False,
}


APPROVAL_KIND = "checkpoint_restore"


def make_checkpoint_authorizer(inner: Any = None):
    """Gate `checkpoint_restore` on a single-use, digest-bound approval.

    Read-only list/diff and every other tool fall through to `inner`.
    """

    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if name not in ("checkpoint_restore", "checkpoint_undo_turn"):
            return bool(await inner(tool, args, ctx)) if inner is not None else False
        undo = name == "checkpoint_undo_turn"
        run_args = args or {}
        checkpoint_id = "latest" if undo else str(run_args.get("checkpointId") or "").strip()
        if not checkpoint_id:
            return False
        paths = [] if undo else (run_args.get("paths") if isinstance(run_args.get("paths"), list) else [])
        digest_source = (
            f"{name}\x00{checkpoint_id}\x00"
            f"{'|'.join(str(p) for p in paths)}\x00{False if undo else bool(run_args.get('all'))}"
        )
        if undo:
            digest_source += f"\x00{str(getattr(ctx, 'run_id', ''))}"
        import hashlib  # noqa: PLC0415

        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        approvals = getattr(ctx, "approvals", None) if ctx is not None else None
        if approvals is None:
            return False
        workspace = getattr(ctx, "workspace", None) if ctx is not None else None
        ws_id = str(getattr(workspace, "id", "")) if workspace is not None else None
        request_key = "checkpoint-undo" if undo else "checkpoint"
        claimed = approvals.consume_kind(
            kind=APPROVAL_KIND, request_key=request_key, digest=digest,
            workspace_id=ws_id,
        )
        if claimed is not None:
            return True
        pending = None
        try:
            pending = approvals.request_kind(
                kind=APPROVAL_KIND, request_key=request_key, digest=digest,
                effect="write", run_id=str(getattr(ctx, "run_id", "")),
                workspace_id=ws_id,
                action_summary=(
                    "Undo the last turn (restore the snapshot from before it)"
                    if undo else f"Restore files from snapshot {checkpoint_id}"
                ),
                risk="medium",
            )
        except Exception:  # noqa: BLE001 - a store failure must never authorize
            return False
        emitter = getattr(ctx, "emitter", None)
        if emitter is not None:
            try:
                await emitter.emit("approval.requested", {
                    "approvalId": (pending or {}).get("id"),
                    "kind": APPROVAL_KIND,
                    "title": "Approve undoing the last turn" if undo else "Approve restoring a file snapshot",
                    "summary": "Undo the latest file changes" if undo else f"Restore from {checkpoint_id}",
                    "effect": "write",
                    "risk": "medium",
                })
            except Exception:  # noqa: BLE001 - the card is best-effort
                pass
        if pending is not None:
            from ..mcp.approvals import pause_for_decision  # noqa: PLC0415

            if await pause_for_decision(
                approvals, ctx, pending.get("id"),
                lambda: approvals.consume_kind(
                    kind=APPROVAL_KIND, request_key=request_key, digest=digest,
                    workspace_id=ws_id,
                ),
                emitter=emitter, digest=digest,
                event_type="approval.approved",
                approved_payload={"kind": APPROVAL_KIND, "checkpointId": checkpoint_id},
            ):
                return True
        return False

    return authorize


def register_checkpoint_tools(registry: Any) -> list[str]:
    definitions = [
        ToolDefinition(
            name="checkpoint_list",
            description="List the file snapshots Eclipse has taken for this project (newest first). Read-only.",
            input_schema=_LIST_SCHEMA,
            handler=checkpoint_list, permission="read", risk="none", timeout_ms=10_000,
        ),
        ToolDefinition(
            name="checkpoint_diff",
            description="Show what changed since a file snapshot, or between snapshots. Read-only.",
            input_schema=_DIFF_SCHEMA,
            handler=checkpoint_diff, permission="read", risk="none", timeout_ms=15_000,
        ),
        ToolDefinition(
            name="checkpoint_restore",
            description=(
                "Restore files from a snapshot. Effectful: requires an approval. "
                "Restores the named paths by default; `all` restores every tracked change. "
                "A pre-restore snapshot is always taken first."
            ),
            input_schema=_RESTORE_SCHEMA,
            handler=checkpoint_restore, permission="write", risk="medium",
            timeout_ms=60_000, effect="write",
        ),
        ToolDefinition(
            name="checkpoint_undo_turn",
            description=(
                "Undo the most recent turn: restore every tracked file to the snapshot "
                "Eclipse took before that turn's first write. Effectful: requires an "
                "approval. A pre-restore snapshot keeps the undo itself undoable."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=checkpoint_undo_turn, permission="write", risk="medium",
            timeout_ms=60_000, effect="write",
        ),
    ]
    added: list[str] = []
    for tool in definitions:
        try:
            registry.register(tool)
            added.append(tool.name)
        except ValueError:
            continue
    return added


__all__ = ["APPROVAL_KIND", "make_checkpoint_authorizer", "register_checkpoint_tools"]
