"""Code-task lifecycle + external-action approvals routes (Phase 5).

A code task IS a run whose agent id is `code-agent`; the lifecycle is the ONE
run lineage, never a second table. Every MUTATING route (and any read that
touches a workspace) carries `require_operator` — transport/token proof only,
never a request-body flag — mirroring the MCP router. Shared state comes from
`harness.deps`; this module never imports `main`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..code.request_intent import classify_code_request
from ..code.land import LandPlan, plan_land_checks, run_land_plan
from ..code.session import SessionError, SessionStore, apply_budget, resolve_budget
from ..code.tasks import DEFAULT_AUTONOMY, TaskStore
from ..deps import require_operator, service
from ..schemas import RunImage, RunInput, RunRequest
from ..schemas import ModelSettings as _ModelSettings

router = APIRouter(tags=["code"])

_AUTONOMY_PROFILES = ("plan_only", "edit_verify", "approve_external")


class CreateTaskRequest(BaseModel):
    """Create a code task. `autonomy` defaults to edit_verify."""

    query: str = Field(min_length=1, max_length=4000)
    autonomy: Optional[str] = None
    workspaceId: Optional[str] = None  # noqa: N815
    # Phase 23 (additive): the APPROVED project root this task builds in. Absent =
    # the globally selected root; the runtime resolves the id through TrustedRoot.
    workspaceRootId: Optional[str] = Field(None, max_length=200)  # noqa: N815
    runId: Optional[str] = None  # noqa: N815
    callbackBaseUrl: str  # noqa: N815
    callbackToken: Optional[str] = None  # noqa: N815
    modelSettings: Optional[dict[str, Any]] = None  # noqa: N815
    # Folder-selection (additive): edit the selected real folder directly
    # instead of a disposable copy. None = resolve from the selected root
    # (a chosen folder edits directly; the built-in repo stays isolated).
    direct: Optional[bool] = None
    # Phase 11 (additive): a caller-supplied scope FILTER label. It narrows what is
    # listed; it NEVER authorizes anything — `require_operator` does.
    scopeId: Optional[str] = Field(None, max_length=200)  # noqa: N815
    # Phase 22 (additive): the build session's budget profile (small / standard /
    # deep). Absent means `standard`. It is a profile NAME, never a raw number.
    budgetProfile: Optional[str] = Field(None, max_length=20)  # noqa: N815
    # What "done" means, in the caller's words. Recorded with the budget so a
    # resumed session can restate its contract.
    acceptance: list[str] = Field(default_factory=list, max_length=20)
    # Gap #7b: reference images attached to a coding task (screenshots, designs,
    # error captures). The SAME bounded RunImage the chat path uses.
    images: list[RunImage] = Field(default_factory=list, max_length=4)


class CodeIntentRequest(BaseModel):
    """One chat turn to classify. Pure, read-only — no state is created."""

    query: str = Field(min_length=1, max_length=4000)
    history: Optional[list[dict[str, Any]]] = None


class DecideApprovalRequest(BaseModel):
    approve: bool
    reason: str = Field("", max_length=400)
    # "Always allow": create a standing grant for the exact program/tool the
    # pending approval describes (only when the row carries a grantable pattern).
    remember: bool = False


class RequestActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    message: str = Field("", max_length=500)
    baseRef: str = Field("", max_length=200)  # noqa: N815
    headRef: str = Field("", max_length=200)  # noqa: N815


class PromoteRequest(BaseModel):
    """Confirm a previewed change: the single-use approval + the preview digest."""

    approvalId: str = Field(min_length=1, max_length=64)  # noqa: N815
    previewDigest: str = Field(min_length=8, max_length=128)  # noqa: N815
    # Phase 22 / S5 slice 2: the files the reviewer kept. Empty = all of them.
    include: list[str] = Field(default_factory=list)
    # Gap #7: the hunks the reviewer kept, as "path#hN". Empty = every hunk.
    hunks: list[str] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    """Narrow the preview to the files (and hunks) the reviewer kept."""

    include: list[str] = Field(default_factory=list)
    hunks: list[str] = Field(default_factory=list)


class PostLandingRequest(BaseModel):
    """Identify the promoted RSL run whose restart/health probe to run."""

    runId: str = Field(min_length=1, max_length=200)  # noqa: N815
    activationId: Optional[str] = Field(None, max_length=200)  # noqa: N815
    # The front door/supervisor has already restarted the runtime; probe only.
    alreadyRestarted: bool = False  # noqa: N815


def _resolve_autonomy(raw: Optional[str]) -> str:
    value = (raw or DEFAULT_AUTONOMY).strip()
    if value not in _AUTONOMY_PROFILES:
        raise HTTPException(status_code=400, detail=f"unknown autonomy profile {raw!r}")
    return value


def _tasks() -> TaskStore:
    return service.code_tasks()


def _sessions() -> SessionStore:
    return SessionStore(service.store)


def _task_checkout_root(task_id: str) -> Optional[Path]:
    """The project a task was built in, from its workspace row (or None)."""
    try:
        for row in service.code_store.list_workspaces(run_id=task_id, limit=20):
            recorded = str(row.get("checkoutRoot") or "").strip()
            if recorded:
                return Path(recorded)
    except Exception:  # noqa: BLE001 - a read failure keeps the caller's fallback
        return None
    return None# ── Chat request classification (Phase 14) ──────────────────────────────────


@router.post("/code/intent")
def classify_intent(body: CodeIntentRequest, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Classify one chat turn. A read-only, stateless deterministic verdict.

    Operator-gated (transport/token only) like every other code route, so the
    front-door proxy is the only caller. No model call, no state.
    """
    intent = classify_code_request(body.query, body.history)
    return {"success": True, "intent": {
        "verdict": intent.verdict,
        "confidence": intent.confidence,
        "reason": intent.reason,
        "signals": list(intent.signals),
    }}


class ShellRunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    cwd: Optional[str] = None  # relative to the resolved root; must stay inside it
    timeoutS: Optional[int] = Field(default=None, ge=1, le=900)  # noqa: N815


@router.post("/code/shell")
def code_shell(body: ShellRunRequest, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Run one command through the host shell for the in-app terminal panel.

    Operator-gated like every other code route; cwd is confined to the
    server-resolved workspace root, so the panel cannot escape it.
    """
    from ..code import shell as shell_mod  # noqa: PLC0415
    from ..code.workspace import default_root  # noqa: PLC0415
    from ..security.hardline import evaluate as hardline  # noqa: PLC0415

    fatal = hardline(body.command)
    if fatal is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "COMMAND_BLOCKED", "message": f"blocked ({fatal.rule}): {fatal.reason}"},
        )

    root = default_root()
    if root is None:
        raise HTTPException(status_code=503, detail="no workspace root")
    cwd = root
    if body.cwd:
        candidate = (root / body.cwd).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="cwd must stay inside the workspace")
        if candidate.is_dir():
            cwd = candidate
    result = shell_mod.run(body.command, cwd=cwd, timeout_s=body.timeoutS)
    return {"success": True, "result": {
        "command": result.command, "cwd": result.cwd, "exitCode": result.exit_code,
        "stdout": result.stdout, "stderr": result.stderr,
        "durationMs": result.duration_ms, "timedOut": result.timed_out,
        "truncated": result.truncated, "status": result.status,
    }}


# ── Task lifecycle ──────────────────────────────────────────────────────────


@router.get("/code/root")
def code_root(_: bool = Depends(require_operator)) -> dict[str, Any]:
    """The runtime's SERVER-RESOLVED code root. Read-only and operator-gated.

    Used by the chat front door (a confirmation ref) and the task list's
    schedule action (a code job needs a folder to work in). The runtime binds
    only its own server-resolved root (`workspace.resolve_workspace` refuses a
    mismatch), so the value can never point a run at another directory.
    """
    from ..code.workspace import default_root  # noqa: PLC0415

    root = default_root()
    if root is None:
        return {"success": True, "root": None, "available": False}
    return {"success": True, "root": str(root), "available": True}


@router.post("/code/tasks")
async def create_task(body: CreateTaskRequest, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Create and run a code task. The autonomy defaults to edit_verify."""
    autonomy = _resolve_autonomy(body.autonomy)
    try:
        budget = resolve_budget(body.budgetProfile)
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    run_id = body.runId or f"code_{abs(hash(body.query)) & 0xFFFFFFFF:08x}_{int(__import__('time').time())}"
    if body.modelSettings is None:
        raise HTTPException(status_code=400, detail="modelSettings is required")
    request = RunRequest(
        runId=run_id,
        input=RunInput(query=body.query, images=body.images or None),
        agentId="code-agent",
        modelSettings=_ModelSettings(**body.modelSettings),
        autonomy=autonomy,
        workspaceId=body.workspaceId,
        workspaceRootId=body.workspaceRootId,
        callbackBaseUrl=body.callbackBaseUrl,
        callbackToken=body.callbackToken,
        userId=body.scopeId,
        direct=body.direct,
    )
    # Phase 22 / S2: the session's budget floor (clamped again in the graph).
    apply_budget(request, budget)
    # Create the run ROW first: `RunCheckpoint` has a FOREIGN KEY on
    # `HarnessRun(id)`, so `record_created` before the row exists fails with a
    # FOREIGN KEY constraint error. `execute_run` re-uses this row (it does not
    # re-create an existing run), so the `task.created` boundary and the run row
    # are guaranteed to name the SAME run.
    try:
        service.store.create_run(
            run_id, "code-agent", request.input.model_dump(), body.scopeId,
            body.modelSettings.get("provider"), body.modelSettings.get("model"),
        )
    except Exception:  # noqa: BLE001 - a duplicate run id is not fatal here
        pass
    # Record the durable `task.created` boundary BEFORE the run so a resume can
    # prove it re-enters the SAME workspace + profile.
    _tasks().record_created(
        run_id, workspace_id=body.workspaceId, autonomy=autonomy, scope_id=body.scopeId,
    )
    # Phase 22 / S2: the goal + acceptance + budget, on the SAME lineage.
    _sessions().record(
        run_id, goal=body.query, budget=budget,
        acceptance=body.acceptance, workspace_id=body.workspaceId,
    )
    result = await service.run(request)
    return {
        "success": True,
        "task": _tasks().view(run_id),
        "session": _sessions().view(run_id),
        "run": result.model_dump(),
    }


@router.get("/code/tasks")
def list_tasks(
    status: Optional[str] = None, limit: int = 50,
    scopeId: Optional[str] = None,  # noqa: N803 - the wire name
) -> dict[str, Any]:
    """List code tasks. `scopeId` is a caller-supplied FILTER, never authorization."""
    return {"success": True, "tasks": _tasks().list(limit=limit, status=status, scope_id=scopeId)}


@router.get("/code/scorecard")
def code_scorecard(
    days: int = 30, taskId: Optional[str] = None, rootId: Optional[str] = None,  # noqa: N803
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """The honest self-build scorecard. Read-only; `UNKNOWN` where unmeasured.

    `taskId` / `rootId` narrow it to ONE project (resolved server-side); absent
    means every build. A filter is never authorization.
    """
    from ..code.scorecard import build_scorecard  # noqa: PLC0415
    from ..code.workspace import trusted_root_path  # noqa: PLC0415

    project_root: Optional[Path] = None
    if taskId:
        project_root = _task_checkout_root(taskId)
    elif rootId:
        project_root = trusted_root_path(rootId)
    return {
        "success": True,
        **build_scorecard(
            service.store, window_days=days,
            project_root=str(project_root) if project_root else None,
        ),
    }


@router.get("/code/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    view = _tasks().view(task_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "task": view, "session": _sessions().view(task_id)}


@router.get("/code/tasks/{task_id}/todos")
def get_task_todos(task_id: str) -> dict[str, Any]:
    """The coding plan the model recorded for this run (gap #8)."""
    from ..code import todos as todo_mod  # noqa: PLC0415

    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    items = todo_mod.load(service.store, task_id)
    return {
        "success": True,
        "todos": [t.to_dict() for t in items],
        "summary": todo_mod.summarize(items),
        "version": todo_mod.version(items),
    }


def _land_preview(task_id: str) -> tuple[dict[str, Any], Path]:
    """The task's current preview and the folder the checks must run in.

    Checks run against the project ON DISK (the checkout the change lands in),
    not the disposable copy: the copy has no installed dependencies, so npm and
    the vendored Python would fail there for the wrong reason. The Code screen
    places the checks after the change is applied.
    """
    from ..code import promotion  # noqa: PLC0415
    from ..code.workspace import default_root  # noqa: PLC0415

    try:
        preview = promotion.build_preview(task_id, code_store=service.code_store)
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    root = default_root() or Path(".")
    return preview, Path(root).resolve()


@router.get("/code/tasks/{task_id}/land")
def get_land_plan(task_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """The ordered checks this change must pass. Read-only, runs nothing."""
    preview, root = _land_preview(task_id)
    files = [str(item.get("path") or "") for item in (preview.get("files") or [])]
    plan = plan_land_checks(files, root=root)
    return {
        "success": True,
        "previewDigest": preview.get("previewDigest"),
        "plan": plan.to_dict(),
    }


@router.post("/code/tasks/{task_id}/land/verify")
def verify_landing(
    task_id: str, full: bool = False, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Run the landing checks. `full=false` skips the long suite for a quick pass.

    The exit code is the judge; the result is recorded as a `land.verified`
    checkpoint so the landing is auditable. A check that cannot run is an error,
    never a pass.
    """
    preview, root = _land_preview(task_id)
    files = [str(item.get("path") or "") for item in (preview.get("files") or [])]
    plan = plan_land_checks(files, root=root)
    if not full:
        plan = LandPlan(
            changed=plan.changed,
            checks=tuple(check for check in plan.checks if check.id != "tests"),
            source=plan.source, warnings=plan.warnings,
        )
    result = run_land_plan(plan, root=root)
    checkpoint = service.run_lineage().save_checkpoint(task_id, "land.verified", {
        "previewDigest": preview.get("previewDigest"),
        "ok": result.ok,
        "failed": result.failed,
        "full": bool(full),
        "checks": [item.to_dict() for item in result.results],
    })
    return {
        "success": True,
        "previewDigest": preview.get("previewDigest"),
        "full": bool(full),
        "checkpointId": checkpoint["id"],
        "result": result.to_dict(),
    }


@router.post("/code/tasks/{task_id}/resume")
def resume_task(task_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Whether a task can resume from its latest checkpoint (dry, read-only).

    The actual resume re-enters through `POST /runs/{id}/resume`, which owns the
    CHECKPOINT_NOT_FOUND / RESUME_CONFLICT semantics.
    """
    run = service.get_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Task not found")
    checkpoint = service.run_lineage().latest_checkpoint(task_id)
    if checkpoint is None:
        return {"success": False, "error": "CHECKPOINT_NOT_FOUND"}
    if run.get("status") in ("completed", "failed", "cancelled"):
        return {"success": False, "error": "RESUME_CONFLICT", "status": run.get("status")}
    return {"success": True, "checkpointId": checkpoint["id"]}


@router.post("/code/tasks/{task_id}/cancel")
def cancel_task(task_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    cancelled = service.cancel(task_id)
    # Release the disposable workspace so a cancel never leaves an orphan.
    released = False
    created = _tasks().latest_created(task_id)
    workspace_id = ((created or {}).get("payload") or {}).get("workspaceId")
    if workspace_id:
        try:
            from ..code import worktree  # noqa: PLC0415

            worktree.release(workspace_id, root=_task_checkout_root(task_id))
            released = True
        except Exception:  # noqa: BLE001 - release is best-effort on cancel
            released = False
    _tasks().record_cancelled(task_id, reason="operator cancelled", worktree_released=released)
    return {"success": True, "taskId": task_id, "cancelled": cancelled, "worktreeReleased": released}


@router.post("/code/tasks/{task_id}/discard")
def discard_task(task_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """Remove the task's disposable workspace; keep the run record as audit."""
    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    released = False
    created = _tasks().latest_created(task_id)
    workspace_id = ((created or {}).get("payload") or {}).get("workspaceId")
    if workspace_id:
        try:
            from ..code import worktree  # noqa: PLC0415

            worktree.release(workspace_id, root=_task_checkout_root(task_id))
            released = True
        except Exception:  # noqa: BLE001
            released = False
    _tasks().record_discarded(task_id, reason="operator discarded", worktree_released=released)
    return {"success": True, "taskId": task_id, "discarded": True, "worktreeReleased": released}


class AcceptPlanRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)
    checkClasses: list[str] = Field(default_factory=list)  # noqa: N815


@router.post("/code/tasks/{task_id}/plan/accept")
def accept_plan(
    task_id: str, body: AcceptPlanRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Operator accepts a plan_only plan, permitting a later edit_verify re-run."""
    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    checkpoint = _tasks().record_plan_accepted(
        task_id, steps=list(body.steps), check_classes=list(body.checkClasses),
    )
    return {"success": True, "planDigest": checkpoint["payload"]["planDigest"]}


@router.get("/code/tasks/{task_id}/checks")
def task_checks(task_id: str) -> dict[str, Any]:
    run = service.get_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "verification": run.get("codeVerification")}


@router.get("/code/tasks/{task_id}/diff")
def task_diff(task_id: str, mode: str = "working", base: Optional[str] = None) -> dict[str, Any]:
    """The change for a task's disposable copy, from a chosen source.

    `working` = the workspace vs `base` (default HEAD); `committed` = the stored
    base revision vs HEAD (what was committed inside the workspace); `revision`
    = the workspace vs a caller-supplied `base` (a branch or revision); `turn` =
    the latest turn snapshot vs the workspace, so a reviewer can see exactly what
    the last turn changed.
    """
    created = _tasks().latest_created(task_id)
    workspace_id = ((created or {}).get("payload") or {}).get("workspaceId")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace bound to this task")
    if mode not in ("working", "committed", "revision", "turn"):
        raise HTTPException(status_code=400, detail="mode must be working, committed, revision or turn")
    try:
        from ..code import worktree  # noqa: PLC0415

        if mode == "turn":
            return {"success": True, "diff": _turn_diff(task_id, workspace_id)}
        if mode == "revision" and not base:
            raise ValueError("a revision comparison needs a base revision")
        base_rev = base
        if mode == "committed" and not base_rev:
            record = service.code_store.get_workspace(workspace_id) or {}
            base_rev = str(record.get("baseRevision") or "HEAD")
        return {
            "success": True,
            "diff": worktree.diff(
                workspace_id, base_rev,
                mode="committed" if mode == "committed" else "working",
            ),
        }
    except Exception as exc:  # noqa: BLE001 - a diff failure is an honest 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _turn_diff(task_id: str, workspace_id: str) -> dict[str, Any]:
    """The change since the newest snapshot recorded for this task's run.

    A snapshot is taken before a turn writes, so diffing the newest one against
    the workspace is "what the last turn changed". No snapshot means an honest
    unavailable result, never an empty diff that implies nothing changed.
    """
    import hashlib  # noqa: PLC0415

    from ..code import checkpoints, worktree  # noqa: PLC0415

    target = worktree.workspace_path(workspace_id)
    latest = None
    for item in checkpoints.list_checkpoints(target, limit=30):
        if str(item.get("runId") or "") == task_id:
            latest = item
            break
    empty = {
        "available": False,
        "strategy": "turn",
        "mode": "turn",
        "workspaceRoot": str(target),
        "reason": "NO_TURN_SNAPSHOT",
        "unified": "",
        "truncated": False,
        "added": [],
        "modified": [],
        "deleted": [],
        "filesChanged": 0,
        "additions": 0,
        "deletions": 0,
        "digest": "",
    }
    if latest is None:
        return empty
    checkpoint_id = str(latest.get("id") or "")
    result = checkpoints.diff(target, checkpoint_id)
    changes = checkpoints.change_paths(target, checkpoint_id)
    added = [c["path"] for c in changes if c.get("status") == "A"]
    deleted = [c["path"] for c in changes if c.get("status") == "D"]
    modified = [c["path"] for c in changes if c.get("status") not in ("A", "D")]
    unified = str(result.get("unified") or "")
    additions = sum(1 for line in unified.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in unified.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {
        "available": True,
        "strategy": "turn",
        "mode": "turn",
        "workspaceRoot": str(target),
        "base": str(latest.get("turnKey") or checkpoint_id),
        "checkpointId": checkpoint_id,
        "unified": unified,
        "truncated": bool(result.get("truncated")),
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "filesChanged": len(added) + len(modified) + len(deleted),
        "additions": additions,
        "deletions": deletions,
        "digest": hashlib.sha256(unified.encode("utf-8")).hexdigest(),
    }


@router.get("/code/tasks/{task_id}/image")
def task_image(task_id: str, path: str, side: str = "after") -> dict[str, Any]:
    """One bounded picture from the task's disposable copy, for the review diff.

    `side=after` reads the workspace file; `side=before` reads the base revision.
    The path is confined by `worktree.read_image` (no absolute/`..`, resolved
    inside the workspace) and the bytes are checked by magic number, so this is
    not a general file-read door.
    """
    created = _tasks().latest_created(task_id)
    workspace_id = ((created or {}).get("payload") or {}).get("workspaceId")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace bound to this task")
    try:
        from ..code import worktree  # noqa: PLC0415

        return {"success": True, "image": worktree.read_image(workspace_id, path, side=side)}
    except Exception as exc:  # noqa: BLE001 - a missing/invalid picture is an honest 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Workspaces ──────────────────────────────────────────────────────────────


@router.get("/code/workspaces")
def list_workspaces(
    scopeId: Optional[str] = None,  # noqa: N803 - the wire name
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """List disposable workspaces. `scopeId` is a FILTER; it never widens access."""
    rows = service.code_store.list_workspaces(limit=100, scope_id=scopeId)
    return {
        "success": True,
        "workspaces": [
            {"id": r["id"], "baseRevision": r["baseRevision"], "strategy": r["strategy"],
             "releasedAt": r["releasedAt"], "fileCount": r["fileCount"]}
            for r in rows
        ],
    }


# ── External-action approvals ───────────────────────────────────────────────


# Kinds a person can review on this surface. `code_action` is a commit/push/PR;
# `terminal_command`/`shell_command` are an `ask`-classified command the terminal
# authorizers created (e.g. `New-Item`). They MUST be listed here, or a pending
# command approval is created but never shown — the agent reports "blocked" and
class PreviewBody(BaseModel):
    """Which project to run. A task id wins; an approved root id is the fallback."""

    taskId: Optional[str] = Field(default=None, max_length=200)  # noqa: N815
    rootId: Optional[str] = Field(default=None, max_length=200)  # noqa: N815


def _preview_manager() -> Any:
    layer = getattr(service, "execution", None)
    if layer is None:
        raise HTTPException(status_code=503, detail="the execution layer is unavailable")
    from ..code.preview import get_preview_manager  # noqa: PLC0415

    return get_preview_manager(layer)


def _preview_key(task_id: Optional[str], root_id: Optional[str]) -> str:
    if task_id:
        return task_id
    if root_id:
        return f"root:{root_id}"
    return "default"


def _preview_root(task_id: Optional[str], root_id: Optional[str]) -> Path:
    """Resolve the project server-side. A body never carries a folder path."""
    from ..code.workspace import default_root, trusted_root_path  # noqa: PLC0415

    if task_id:
        root = _task_checkout_root(task_id)
        if root is None:
            raise HTTPException(status_code=404, detail="TASK_HAS_NO_PROJECT")
        return root
    if root_id:
        root = trusted_root_path(root_id)
        if root is None:
            raise HTTPException(status_code=400, detail="UNKNOWN_PROJECT")
        return root
    root = default_root()
    if root is None:
        raise HTTPException(status_code=503, detail="WORKSPACE_UNAVAILABLE")
    return root


@router.post("/code/preview/start")
def start_preview(body: PreviewBody, _: bool = Depends(require_operator)) -> dict[str, Any]:
    key = _preview_key(body.taskId, body.rootId)
    root = _preview_root(body.taskId, body.rootId)
    # A refusal ("no command for this project") is a product state, not an HTTP
    # error: return it with the plan so the surface can say what is missing.
    data = _preview_manager().start(key, root)
    if data.get("status") == "unavailable":
        raise HTTPException(status_code=503, detail=data)
    return {"success": data.get("status") == "ok", "preview": data}


@router.get("/code/preview")
def get_preview(
    taskId: Optional[str] = None, rootId: Optional[str] = None,  # noqa: N803
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    key = _preview_key(taskId, rootId)
    root: Optional[Path] = None
    try:
        root = _preview_root(taskId, rootId)
    except HTTPException:
        # A status read is still useful when the project cannot be resolved: it
        # reports idle without inventing a plan.
        root = None
    return {"success": True, "preview": _preview_manager().status(key, root=root)}


@router.post("/code/preview/stop")
def stop_preview(body: PreviewBody, _: bool = Depends(require_operator)) -> dict[str, Any]:
    key = _preview_key(body.taskId, body.rootId)
    return {"success": True, "preview": _preview_manager().stop(key)}


@router.get("/code/preview/logs")
def preview_logs(
    taskId: Optional[str] = None, rootId: Optional[str] = None, tail: int = 200,  # noqa: N803
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    key = _preview_key(taskId, rootId)
    return {"success": True, "preview": _preview_manager().logs(key, tail=tail)}


@router.get("/code/project")
def get_project(
    taskId: Optional[str] = None, rootId: Optional[str] = None,  # noqa: N803
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """The project brief the onboarding surface shows BEFORE the first build.

    Stack, run command, discovered checks, git state and whether the project
    ships its own rules. Read-only and resolved server-side, like the preview.
    """
    from ..code.onboarding import project_brief  # noqa: PLC0415

    return {"success": True, "project": project_brief(_preview_root(taskId, rootId))}


# ── The approval queue ──────────────────────────────────────────────────────
# the user has no way to approve it.
_REVIEWABLE_APPROVAL_KINDS = (
    "code_action", "terminal_command", "shell_command", "code_promote", "scraper_domain",
    "trade_order",
)


@router.get("/code/approvals")
def list_code_approvals(status: Optional[str] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    if service.approvals is None:
        return {"success": True, "approvals": [], "unavailable": True}
    approvals: list[dict[str, Any]] = []
    for kind in _REVIEWABLE_APPROVAL_KINDS:
        approvals.extend(service.approvals.list_kind(kind, status, limit=100))
    return {"success": True, "approvals": approvals}


@router.post("/code/approvals/{approval_id}/decide")
def decide_code_approval(
    approval_id: str, body: DecideApprovalRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    if service.approvals is None:
        raise HTTPException(status_code=503, detail="approvals unavailable")
    current = service.approvals.get(approval_id)
    if current is None or current.get("kind") not in _REVIEWABLE_APPROVAL_KINDS:
        raise HTTPException(status_code=404, detail="Approval not found")
    decided = service.approvals.decide(
        approval_id, approve=body.approve, decided_by="operator", reason=body.reason,
    )
    grant = None
    if body.approve and body.remember:
        grant = service.approvals.remember_grant(approval_id)
    return {"success": True, "approval": decided, "grant": grant}


@router.post("/code/tasks/{task_id}/actions")
def request_task_action(
    task_id: str, body: RequestActionRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Request an external action (commit/push/open_pr/deploy) for operator review."""
    from ..code.actions import CODE_ACTION_KINDS, CodeActionRequest, request_code_action  # noqa: PLC0415

    if body.action not in CODE_ACTION_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown action {body.action!r}")
    if service.approvals is None:
        raise HTTPException(status_code=503, detail="approvals unavailable")
    created = _tasks().latest_created(task_id)
    workspace_id = ((created or {}).get("payload") or {}).get("workspaceId")
    if not workspace_id:
        raise HTTPException(status_code=400, detail="no workspace bound to this task")
    request = CodeActionRequest(
        kind=body.action,  # type: ignore[arg-type]
        workspaceId=workspace_id, message=body.message or None,
        baseRef=body.baseRef or None, headRef=body.headRef or None, runId=task_id,
    )
    approval = request_code_action(service.approvals, request)
    return {"success": True, "approval": approval}


# ── Promotion: put a reviewed change into the user's real project (Phase 15) ──
# A chat/code run edits a DISPOSABLE copy. Nothing reaches the user's real folder
# until they preview the exact file set and confirm it through a single-use,
# digest-bound approval (ADR-H119). The write itself reuses `code/patch.py`'s
# atomic, base-hash-checked, protected-path-aware engine.


@router.post("/code/tasks/{task_id}/promote/preview")
def promote_preview(
    task_id: str, body: Optional[PreviewRequest] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Preview exactly what a task's change would put into the user's project."""
    from ..code import promotion, selfhost  # noqa: PLC0415

    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        preview = promotion.build_preview(task_id, code_store=service.code_store)
        include = (body.include if body else []) or []
        hunks = (body.hunks if body else []) or []
        if include or hunks:
            preview = promotion.filter_preview(preview, include, hunks=hunks)
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    verdict = selfhost.classify_landing([str(f.get("path") or "") for f in preview.get("files", [])])
    if verdict.refused:
        # Never request an approval for a change the self-host floor refuses.
        raise HTTPException(status_code=403, detail="SELFHOST_AUTHORITY_REFUSED")
    approval = None
    if service.approvals is not None:
        approval = service.approvals.request_kind(
            kind=promotion.PROMOTION_KIND,
            request_key=preview["workspaceId"],
            digest=preview["previewDigest"],
            effect="external_action",
            run_id=task_id,
            workspace_id=preview["workspaceId"],
            action_summary=(
                f"Put {preview['filesChanged']} change(s) into your project "
                f"(+{preview['added']} new, {preview['modified']} edited, {preview['deleted']} removed)"
            ),
            risk="high",
        )
    view = promotion.public_preview(preview)
    return {"success": True, "preview": view, "approval": approval, "landing": verdict.to_dict()}


@router.post("/code/tasks/{task_id}/promote")
def promote_apply(
    task_id: str, body: PromoteRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Confirm a previewed change and put it into the user's real project."""
    from ..code import promotion, selfhost  # noqa: PLC0415

    if service.approvals is None:
        raise HTTPException(status_code=503, detail="approvals unavailable")
    current = service.approvals.get(body.approvalId)
    if current is None or current.get("kind") != promotion.PROMOTION_KIND:
        raise HTTPException(status_code=404, detail="Approval not found")
    # Re-derive the live preview: a change to either copy since the preview makes
    # the confirmed set stale and the promotion is refused (nothing is written).
    try:
        preview = promotion.build_preview(task_id, code_store=service.code_store)
        if body.include or body.hunks:
            preview = promotion.filter_preview(preview, body.include, hunks=body.hunks)
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    if preview["previewDigest"] != body.previewDigest:
        raise HTTPException(status_code=409, detail="PROMOTION_PREVIEW_STALE")
    verdict = selfhost.classify_landing([str(f.get("path") or "") for f in preview.get("files", [])])
    if verdict.refused:
        # Refuse BEFORE consuming the approval: the change can never be landed
        # through this path, so the operator must not spend the decision.
        raise HTTPException(status_code=403, detail="SELFHOST_AUTHORITY_REFUSED")
    claimed = service.approvals.consume_kind(
        kind=promotion.PROMOTION_KIND,
        request_key=preview["workspaceId"],
        digest=body.previewDigest,
        workspace_id=preview["workspaceId"],
    )
    if claimed is None:
        raise HTTPException(status_code=403, detail="PROMOTION_NOT_APPROVED")
    try:
        result = promotion.apply_promotion(preview)
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    # Phase 22 / S6: the durable landing marker the self-build scorecard reads.
    # A best-effort checkpoint: a landing that succeeded must not be reported as
    # failed because its metric row could not be written.
    try:
        service.run_lineage().save_checkpoint(task_id, "land.promoted", {
            "previewDigest": preview.get("previewDigest"),
            "rollbackId": result.get("rollbackId"),
            "filesChanged": result.get("filesChanged"),
            "landing": verdict.to_dict(),
        })
    except Exception:  # noqa: BLE001 - metrics never block a landing
        pass
    # Tell the front door whether this change needs a runtime restart / build gate
    # (self-hosting). The route cannot restart the process, so it reports the
    # requirement; `code/selfhost.py` owns the health-gated apply when a restart
    # seam is available. An RSL landing also carries the post-landing probe URL,
    # so the front door/supervisor can prove the restarted runtime is healthy and
    # roll the change back when it is not.
    post_landing = None
    if task_id.startswith("rsl-") and verdict.restartRequired:
        rollback_id = str(result.get("rollbackId") or "")
        post_landing = {
            "required": True,
            "runId": task_id,
            "rollbackId": rollback_id or None,
            "endpoint": (
                f"/api/code/promotions/{rollback_id}/post-landing"
                if rollback_id else None
            ),
            "status": "pending" if rollback_id else "unavailable",
        }
    return {
        "success": True,
        "promotion": result,
        "landing": verdict.to_dict(),
        "postLanding": post_landing,
    }


@router.post("/code/tasks/{task_id}/autoland")
def autoland_task(
    task_id: str, full: bool = False, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Trust Mode: land in one call, verify, and undo on a failed check.

    The operator has already granted trust for this action (the front door's
    single "Let Eclipse change this project" switch). This route still applies
    the same path safety, digest, backup and rollback rules as the reviewed
    path; the only difference is that no second confirmation is required.
    """
    from ..code import autoland, promotion  # noqa: PLC0415

    if service.get_run(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        result = autoland.autoland(
            task_id, code_store=service.code_store, full=full,
        )
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    try:
        service.run_lineage().save_checkpoint(
            task_id,
            "land.promoted" if result.get("ok") else "land.rolled_back",
            {
                "auto": True,
                "previewDigest": (result.get("preview") or {}).get("previewDigest"),
                "rollbackId": result.get("rollbackId"),
                "checks": result.get("checks"),
                "rolledBack": result.get("rolledBack"),
            },
        )
    except Exception:  # noqa: BLE001 - metrics never block the outcome
        pass
    return {"success": True, **result}


@router.post("/code/promotions/{rollback_id}/rollback")
def rollback_promotion_route(
    rollback_id: str, runId: Optional[str] = None,  # noqa: N803 - the wire name
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Put a previously-promoted change back - the exact original contents.

    The inverse of promotion: a created file is removed, an edited file is
    restored, a deleted file is recreated. Both versions are kept.
    """
    from ..code import promotion  # noqa: PLC0415

    # Restore into the PROJECT that produced this change, not whatever root is
    # selected now. The run's workspace row records it; a legacy row without one
    # keeps the old default-root behaviour.
    checkout_root = _task_checkout_root(runId) if runId else None
    try:
        result = promotion.rollback_promotion(rollback_id, checkout_root=checkout_root)
    except promotion.PromotionError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    # The scorecard's rollback marker, only when the caller named the run.
    if runId:
        try:
            service.run_lineage().save_checkpoint(runId, "land.rolled_back", {
                "rollbackId": rollback_id,
            })
        except Exception:  # noqa: BLE001 - metrics never block a rollback
            pass
    return {"success": True, "rollback": result}


@router.post("/code/promotions/{rollback_id}/post-landing")
async def post_landing_probe(
    rollback_id: str,
    body: PostLandingRequest,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Restart, probe, and roll back an unhealthy RSL landing.

    The restart itself is a front-door/supervisor seam (`service._restart_runtime`).
    Without it the probe is recorded as `unverified`, never as a success.
    """
    from ..self_improvement.post_landing import (  # noqa: PLC0415
        PostLandingPolicy,
        run_post_landing,
    )

    restart = getattr(service, "_restart_runtime", None)
    record = await run_post_landing(
        store=service.store,
        run_id=body.runId,
        rollback_id=rollback_id,
        activation_id=body.activationId or "",
        restart=restart,
        policy=PostLandingPolicy(),
        restart_already_done=body.alreadyRestarted,
    )
    return {"success": True, "postLanding": record}


@router.get("/code/promotions/post-landing")
def list_post_landing(
    runId: Optional[str] = None,  # noqa: N803
    limit: int = 50,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Read the bounded post-landing records (newest first)."""
    from ..self_improvement.post_landing import PostLandingStore  # noqa: PLC0415

    return {
        "success": True,
        "records": PostLandingStore(service.store).list(
            run_id=runId, limit=limit,
        ),
    }


# ── W7: file checkpoints + /rollback ─────────────────────────────────────────


class CheckpointRestoreRequest(BaseModel):
    paths: Optional[list[str]] = Field(default=None, max_length=500)
    all: bool = False


def _checkpoint_root() -> Path:
    from ..code.workspace import default_root  # noqa: PLC0415

    root = default_root()
    if root is None:
        raise HTTPException(status_code=409, detail={
            "code": "WORKSPACE_ROOT_UNAVAILABLE",
            "message": "There is no project folder to restore into.",
        })
    return root


@router.get("/code/checkpoints")
def list_checkpoints(
    limit: int = 50, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..code import checkpoints  # noqa: PLC0415

    return {"success": True, "checkpoints": checkpoints.list_checkpoints(_checkpoint_root(), limit=limit)}


@router.get("/code/checkpoints/{checkpoint_id}/diff")
def diff_checkpoint(
    checkpoint_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..code import checkpoints  # noqa: PLC0415

    try:
        return {"success": True, **checkpoints.diff(_checkpoint_root(), checkpoint_id)}
    except checkpoints.CheckpointError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": exc.reason}) from exc


@router.post("/code/checkpoints/{checkpoint_id}/restore/preview")
def preview_checkpoint_restore(
    checkpoint_id: str, body: CheckpointRestoreRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..code import checkpoints  # noqa: PLC0415

    try:
        return {
            "success": True,
            **checkpoints.preview_restore(
                _checkpoint_root(), checkpoint_id, paths=body.paths, all=body.all,
            ),
        }
    except checkpoints.CheckpointError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": exc.reason}) from exc


@router.post("/code/checkpoints/{checkpoint_id}/restore")
def restore_checkpoint(
    checkpoint_id: str, body: CheckpointRestoreRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..code import checkpoints  # noqa: PLC0415

    try:
        return {
            "success": True,
            **checkpoints.restore(
                _checkpoint_root(), checkpoint_id, paths=body.paths, all=body.all,
                turn_key=f"manual-{checkpoint_id}",
            ),
        }
    except checkpoints.CheckpointError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.reason}) from exc


__all__ = ["router"]
