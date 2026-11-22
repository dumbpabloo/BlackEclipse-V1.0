"""Operator API for declarative evolution assets.

Every route is operator-gated. Apply is refused when the kernel is off because
the asset would not reach the live prompt/agent plane.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import require_operator, service
from ..kernel.services.runtime import kernel_runtime
from ..evolution.assets import AssetError, AssetKind, CandidateStatus
from ..evolution.runtime import asset_evolution, asset_pipeline

router = APIRouter(prefix="/evolution/assets", tags=["evolution"], dependencies=[Depends(require_operator)])


class MutationBody(BaseModel):
    op: str
    kind: str
    target: str
    content: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    expectedEffect: str = ""


class ProposeBody(BaseModel):
    kind: str
    target: str
    mutations: list[MutationBody]
    parentId: Optional[str] = None
    evidenceRefs: list[str] = Field(default_factory=list)


class WatchBody(BaseModel):
    keys: list[str] = Field(default_factory=list)


class ObserveBody(BaseModel):
    kind: str
    key: str
    detail: dict[str, Any] = Field(default_factory=dict)
    evidenceRefs: list[str] = Field(default_factory=list)


class EvaluateBody(BaseModel):
    dataset: str = ""
    suite: str = ""
    taskId: str = ""
    trials: Optional[int] = Field(default=None, ge=1, le=50)
    baseline: Optional[dict[str, Any]] = None
    baselineRunId: Optional[str] = None
    targetDimension: str = "task_success"
    minImprovement: float = 0.0
    live: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None
    watchKeys: list[str] = Field(default_factory=list)


def _error(exc: AssetError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message})


def _require_kernel() -> None:
    if not kernel_runtime.enabled or kernel_runtime.kernel is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "ECLIPSE_KERNEL_OFF", "message": "enable ECLIPSE_KERNEL to apply assets"},
        )
    if not kernel_runtime.active:
        raise HTTPException(
            status_code=409,
            detail={"code": "KERNEL_SHADOW_READONLY",
                    "message": "ECLIPSE_KERNEL=shadow validates but never changes live behavior"},
        )


@router.get("")
def list_assets() -> dict[str, Any]:
    evolution = asset_evolution()
    return {
        **evolution.status(),
        "candidates": [candidate.to_dict() for candidate in evolution.list()],
        "journal": {
            "path": str(evolution.journal.path) if evolution.journal is not None else None,
            "verified": evolution.journal.verify()[0] if evolution.journal is not None else None,
        },
        "kernelEnabled": kernel_runtime.enabled,
        "kernelMode": kernel_runtime.mode,
    }


@router.post("/observe")
def observe(body: ObserveBody) -> dict[str, Any]:
    return asset_pipeline().observe(
        kind=body.kind, key=body.key, detail=body.detail, evidence_refs=body.evidenceRefs,
    )


@router.post("/propose")
def propose(body: ProposeBody) -> dict[str, Any]:
    try:
        candidate = asset_evolution().propose(
            kind=AssetKind(body.kind),
            target=body.target,
            mutations=[mutation.model_dump() for mutation in body.mutations],
            parent_id=body.parentId,
            evidence_refs=body.evidenceRefs,
        )
    except (AssetError, ValueError) as exc:
        raise _error(exc if isinstance(exc, AssetError) else AssetError("EVOLUTION_MUTATION_INVALID", str(exc)))
    return candidate.to_dict()


@router.post("/{candidate_id}/admit")
def admit(candidate_id: str) -> dict[str, Any]:
    try:
        return asset_evolution().admit(candidate_id).to_dict()
    except AssetError as exc:
        raise _error(exc)


@router.post("/{candidate_id}/apply")
async def apply(candidate_id: str) -> dict[str, Any]:
    _require_kernel()
    try:
        return asset_evolution().apply(candidate_id).to_dict()
    except AssetError as exc:
        raise _error(exc)


@router.post("/{candidate_id}/rollback")
async def rollback(candidate_id: str) -> dict[str, Any]:
    _require_kernel()
    try:
        return asset_evolution().rollback(candidate_id).to_dict()
    except AssetError as exc:
        raise _error(exc)


@router.post("/{candidate_id}/watch")
def watch(candidate_id: str, body: WatchBody) -> dict[str, Any]:
    try:
        return asset_evolution().watch(candidate_id, body.keys).to_dict()
    except AssetError as exc:
        raise _error(exc)


@router.post("/{candidate_id}/evaluate")
async def evaluate(candidate_id: str, body: EvaluateBody) -> dict[str, Any]:
    """Run a measured evaluation and land the candidate honestly.

    The baseline is never invented. Without explicit `baseline` aggregates (or
    a run whose metadata carries them) the evaluator reports `unmeasured`
    instead of accepting a change on no evidence.
    """
    _require_kernel()
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from ..evolution.evaluator import AssetEvaluator  # noqa: PLC0415
    from ..evaluation.runner import EvaluationRunner, ScriptedAgentInvoker  # noqa: PLC0415
    from ..evaluation.store import EvaluationStore  # noqa: PLC0415
    from ..evaluation.tasks import load_all_builtin, load_builtin_dataset, select_tasks  # noqa: PLC0415
    from ..persistence import AgentMemoryStore  # noqa: PLC0415

    baseline = body.baseline
    if baseline is None and body.baselineRunId:
        store = getattr(service, "store", None)
        if store is None:
            raise HTTPException(status_code=503, detail={"code": "EVALUATION_RUNTIME_UNAVAILABLE"})
        run = EvaluationStore(store).get_run(body.baselineRunId)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "EVALUATION_BASELINE_NOT_FOUND"})
        metadata = run.get("metadata") or {}
        baseline = metadata.get("baseline") or metadata.get("aggregates")
    if not isinstance(baseline, dict) or not baseline:
        raise HTTPException(
            status_code=409,
            detail={"code": "EVALUATION_BASELINE_REQUIRED",
                    "message": "an unmeasured candidate cannot be accepted; supply baseline aggregates"},
        )

    dataset = load_builtin_dataset(body.dataset) if body.dataset else load_all_builtin()
    tasks = select_tasks(dataset, task_id=body.taskId or None, suite=body.suite or None)
    if not tasks:
        raise HTTPException(status_code=400, detail={"code": "EVALUATION_NO_TASKS"})

    if body.live:
        from ..evaluation.runner import ServiceAgentInvoker  # noqa: PLC0415
        from ..schemas import ModelSettings  # noqa: PLC0415

        store = getattr(service, "store", None)
        if store is None:
            raise HTTPException(status_code=503, detail={"code": "EVALUATION_RUNTIME_UNAVAILABLE"})
        settings = None
        if body.provider or body.model:
            settings = ModelSettings(provider=body.provider or "ollama",
                                     model=body.model or "llama3.1")
        runner = EvaluationRunner(store, invoker=ServiceAgentInvoker(service, settings))
        evaluator = AssetEvaluator(asset_evolution())
        try:
            return await evaluator.evaluate(
                candidate_id, runner=runner, tasks=tasks, baseline_metrics=baseline,
                target_dimension=body.targetDimension, min_improvement=body.minImprovement,
                trials=body.trials, watch_keys=body.watchKeys or None,
            )
        except AssetError as exc:
            raise _error(exc)

    directory = tempfile.mkdtemp(prefix="be-evolve-eval-")
    db_path = os.path.join(directory, "agent-memory.db")
    temp_store = AgentMemoryStore(db_path)
    try:
        runner = EvaluationRunner(
            temp_store, invoker=ScriptedAgentInvoker(temp_store),
            eval_store=EvaluationStore(temp_store),
        )
        evaluator = AssetEvaluator(asset_evolution())
        try:
            result = await evaluator.evaluate(
                candidate_id, runner=runner, tasks=tasks, baseline_metrics=baseline,
                target_dimension=body.targetDimension, min_improvement=body.minImprovement,
                trials=body.trials or 1, watch_keys=body.watchKeys or None,
            )
        except AssetError as exc:
            raise _error(exc)
        result["live"] = False
        return result
    finally:
        temp_store.close()


@router.post("/sweep")
def sweep() -> dict[str, Any]:
    _require_kernel()
    return {"expired": asset_evolution().sweep_expired()}


@router.post("/failure/{failure_key}")
def failure(failure_key: str) -> dict[str, Any]:
    _require_kernel()
    return {"rolledBack": asset_evolution().observe_failure(failure_key)}
