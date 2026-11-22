"""Self-improvement (evolution) route group (Phase 10).

Registered by `main.py` via `app.include_router`. Shared state comes from
`deps.py` — never by importing `main`.

EVERY route carries `require_operator` — reads AND mutations — exactly as the MCP
route group was hardened to do (ADR-H48). A candidate proposes a change to the
app's own code, so none of it is reachable without operator proof, and a body flag
never authorizes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..code import evolution as evo
from ..code.evolution_service import activate, evaluate, rollback
from ..code.evolution_store import EvolutionStore
from ..deps import require_operator, service

router = APIRouter(tags=["evolution"])
from .evolution_assets import router as assets_router  # noqa: E402
router.include_router(assets_router)
from .evolution_autonomy import router as autonomy_router  # noqa: E402
from .evolution_drift import router as drift_router  # noqa: E402
from .evolution_models import router as model_router  # noqa: E402
from .evolution_report import router as report_router  # noqa: E402

router.include_router(drift_router)
router.include_router(model_router)
router.include_router(report_router)
router.include_router(autonomy_router)


def _store() -> EvolutionStore:
    return service.evolution_store()


def _eval_store() -> Any:
    from ..evaluation.store import EvaluationStore  # noqa: PLC0415

    return EvaluationStore(service.store)


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "suite": run.get("suite"),
        "datasetName": run.get("datasetName"),
        "datasetVersion": run.get("datasetVersion"),
        "status": run.get("status"),
        "trialCount": run.get("trialCount"),
        "agentVersion": run.get("agentVersion"),
        "gitCommit": run.get("gitCommit"),
        "model": run.get("model"),
        "provider": run.get("provider"),
        "startedAt": run.get("startedAt"),
        "finishedAt": run.get("finishedAt"),
        "config": _loads(run.get("config")),
        "metadata": _loads(run.get("metadata")),
    }


def _experiments(limit: int = 50, *, proposal_id: Optional[str] = None) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 200))
    out: list[dict[str, Any]] = []
    for run in _eval_store().list_runs(limit=max(cap * 4, 200)):
        metadata = _loads(run.get("metadata"))
        if metadata.get("kind") not in {"rsl", "rsl-recursion"}:
            continue
        if proposal_id and metadata.get("proposalId") != proposal_id:
            continue
        out.append(_run_summary(run))
        if len(out) >= cap:
            break
    return out


class EvaluateRequest(BaseModel):
    runId: Optional[str] = None  # noqa: N815


class ProposeRequest(BaseModel):
    targetPath: Optional[str] = None  # noqa: N815
    ops: list[dict[str, Any]] = []


class DecisionRequest(BaseModel):
    reason: str = ""
    canary: bool = False


class LessonAdoptionRequest(BaseModel):
    cue: str = ""
    lesson: str = ""
    evidence: Any = None
    scope: Optional[dict[str, Any]] = None
    validationRunId: Optional[str] = None  # noqa: N815
    adoptedArtifactType: Optional[str] = None  # noqa: N815
    adoptedArtifactId: Optional[str] = None  # noqa: N815


class RecursiveVariant(BaseModel):
    kind: str
    targetPath: Optional[str] = None  # noqa: N815
    ops: list[dict[str, Any]]
    description: str = ""


class RecursiveExperimentRequest(BaseModel):
    variants: list[RecursiveVariant]
    seed: int = 0


class SurfaceSnapshotInput(BaseModel):
    systemPrompt: str  # noqa: N815
    toolNames: Optional[list[str]] = None  # noqa: N815
    maxSteps: Optional[int] = None  # noqa: N815
    maxToolCalls: Optional[int] = None  # noqa: N815
    modelProvider: Optional[str] = None  # noqa: N815
    modelName: Optional[str] = None  # noqa: N815
    temperature: Optional[float] = None
    skillDraftIds: Optional[list[str]] = None  # noqa: N815


class SurfaceActivationRequest(BaseModel):
    scope: Optional[str] = None


class SurfaceExperimentRequest(BaseModel):
    candidate: SurfaceSnapshotInput
    base: Optional[SurfaceSnapshotInput] = None
    dataset: str = "benchmark_v1"
    taskIds: Optional[list[str]] = None  # noqa: N815
    split: str = "validation"
    targetDimension: str = "task_success"  # noqa: N815
    trials: Optional[int] = None
    seed: Optional[int] = None
    baselineRunId: Optional[str] = None  # noqa: N815
    problemStatement: str = ""  # noqa: N815
    sourceCategory: str = "operator"  # noqa: N815
    scope: str = "global"


def _lesson_store() -> Any:
    store = getattr(service, "skill_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="learning store unavailable")
    return store


def _variant_signature(parent_signature: str, proposal: Any) -> str:
    """Content-addressed child identity: changed ops are a new candidate."""
    raw = json.dumps(
        {
            "id": proposal.id,
            "kind": proposal.kind,
            "targetPath": proposal.targetPath,
            "ops": list(proposal.ops),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{parent_signature}:rsl-{proposal.id}-{proposal.kind}-{digest}"


def _surface_snapshot(value: SurfaceSnapshotInput, base: Any = None) -> Any:
    """Build one validated snapshot, inheriting unspecified fields from `base`."""
    from ..self_improvement.models import HarnessSnapshot  # noqa: PLC0415

    def pick(name: str, default: Any) -> Any:
        current = getattr(value, name, None)
        if current is not None:
            return current
        if base is not None:
            return getattr(base, name, default)
        return default

    return HarnessSnapshot(
        systemPrompt=value.systemPrompt,
        toolNames=tuple(pick("toolNames", ()) or ()),
        toolGuidance=str(pick("toolGuidance", "") or ""),
        skillManifest=pick("skillManifest", None),
        memoryPolicy=pick("memoryPolicy", None),
        guardrails=tuple(pick("guardrails", ()) or ()),
        maxAnswerChars=pick("maxAnswerChars", None),
        subagentPrompt=pick("subagentPrompt", None),
        maxSteps=int(pick("maxSteps", 8)),
        maxToolCalls=int(pick("maxToolCalls", 8)),
        modelProvider=pick("modelProvider", None),
        modelName=pick("modelName", None),
        temperature=pick("temperature", None),
        skillDraftIds=tuple(pick("skillDraftIds", ()) or ()),
    )


def _accepted_decision(run: dict[str, Any]) -> Optional[str]:
    """The verified decision an experiment actually recorded, or None.

    A lesson may never be adopted from an unverified experiment: the route
    derives the decision from the persisted `EvaluationRun`, not from the body.
    """
    metadata = _loads(run.get("metadata"))
    if metadata.get("accepted") is True:
        return "accepted"
    composition = metadata.get("composition") or {}
    if isinstance(composition, dict) and composition.get("decision") == "composed":
        return "composed"
    decision = str(metadata.get("decision") or "").strip().lower()
    if decision in {"accepted", "tests_passed", "composed"}:
        return decision
    return None


@router.get("/evolution/candidates")
def list_candidates(status: Optional[str] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "candidates": _store().list_candidates(status=status, limit=100)}


@router.get("/evolution/candidates/{candidate_id}")
def get_candidate(candidate_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"success": True, "candidate": candidate}


@router.get("/evolution/candidates/{candidate_id}/attempts")
def list_attempts(candidate_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    if _store().get_candidate(candidate_id) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"success": True, "attempts": _store().list_attempts(candidate_id, limit=100)}


@router.post("/evolution/candidates/{candidate_id}/evaluate")
def evaluate_candidate_route(
    candidate_id: str, body: EvaluateRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    ctx = service.evolution_context(run_id=body.runId or f"evolution_{candidate_id}")
    result = evaluate(service.store, candidate_id, ctx=ctx, actor=service.operator_actor())
    if not result.get("ok") and not result.get("refused"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "evaluation failed"))
    return {"success": bool(result.get("ok")), **result}


@router.post("/evolution/candidates/{candidate_id}/activate")
def activate_candidate_route(
    candidate_id: str, body: DecisionRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    result = activate(
        service.store, candidate_id, registry=service.skill_registry,
        actor=service.operator_actor(), canary=body.canary,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "activation failed"))
    return {"success": True, **result}


@router.post("/evolution/candidates/{candidate_id}/rollback")
def rollback_candidate_route(
    candidate_id: str, body: DecisionRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    result = rollback(
        service.store, candidate_id, registry=service.skill_registry,
        actor=service.operator_actor(), reason=body.reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "rollback failed"))
    return {"success": True, **result}


@router.post("/evolution/candidates/{candidate_id}/dismiss")
def dismiss_candidate_route(candidate_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    if _store().get_candidate(candidate_id) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    candidate = _store().update_candidate(candidate_id, status="dismissed")
    return {"success": True, "candidate": candidate}


@router.post("/evolution/candidates/{candidate_id}/propose")
def propose_candidate_route(
    candidate_id: str, body: ProposeRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Stage operator-supplied ops on a code candidate for the conductor.

    The frozen target/op-path guard and `propose_ops` run exactly as in the
    recursive and landing paths. This route only marks the candidate `proposed`;
    a `verify` loop or an explicit tick runs it.
    """
    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.get("subjectType") == "surface":
        raise HTTPException(
            status_code=400,
            detail="surface candidates use POST /evolution/surface-experiments",
        )
    if candidate.get("status") in {
        "evaluating", "evaluated_ok", "canaried", "activated",
        "rolled_back", "dismissed",
    }:
        raise HTTPException(
            status_code=409,
            detail=f"a candidate in status {candidate.get('status')!r} cannot be re-proposed",
        )
    if body.targetPath:
        _store().update_candidate(candidate_id, targetPath=body.targetPath)

    from ..self_improvement.proposer import ProposalRefused, propose_from_ops  # noqa: PLC0415

    try:
        updated = propose_from_ops(
            service.store, candidate_id,
            actor=service.operator_actor(), ops=body.ops,
        )
    except ProposalRefused as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    return {"success": True, "candidate": updated}


@router.post("/evolution/candidates/{candidate_id}/model-propose")
async def model_propose_candidate(
    candidate_id: str,
    k: Optional[int] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Run the model proposer for ONE candidate and persist its validated children.

    The model proposer must be enabled; the route never verifies or lands. A
    drift/observation candidate with no target can become a real child candidate
    here without inventing a patch: every op still passes `propose_ops`, the
    frozen target guard and workspace confinement.
    """
    from ..self_improvement.proposer import ProposalRefused  # noqa: PLC0415

    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.get("subjectType") == "surface":
        raise HTTPException(
            status_code=400,
            detail="surface candidates use POST /evolution/surface-experiments",
        )
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    try:
        child_ids = await loop.propose_with_model(candidate, k=k)
    except ProposalRefused as exc:
        status = 409 if exc.code == "MODEL_PROPOSER_DISABLED" else 400
        raise HTTPException(status_code=status, detail=exc.code) from exc
    evolution = _store()
    return {
        "success": True,
        "candidateId": candidate_id,
        "children": [evolution.get_candidate(child_id) for child_id in child_ids],
    }


@router.get("/evolution/experiments")
def list_experiments(
    limit: int = 50, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """List RSL experiments (code tests-only and surface benchmark runs)."""
    return {"success": True, "experiments": _experiments(limit=limit)}


@router.get("/evolution/experiments/{run_id}")
def get_experiment(run_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    """One experiment with its metrics and bounded trial scores."""
    store = _eval_store()
    run = store.get_run(run_id)
    if run is None or _loads(run.get("metadata")).get("kind") not in {"rsl", "rsl-recursion"}:
        raise HTTPException(status_code=404, detail="Experiment not found")
    trials = [
        {key: value for key, value in trial.items() if key != "trajectory"}
        for trial in store.list_trials(run_id)[:200]
    ]
    return {
        "success": True,
        "experiment": _run_summary(run),
        "metrics": store.list_metrics(run_id),
        "trials": trials,
    }


@router.get("/evolution/candidates/{candidate_id}/evidence")
def candidate_evidence(
    candidate_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """The candidate, its attempts, and every RSL experiment that names it."""
    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {
        "success": True,
        "candidate": candidate,
        "attempts": _store().list_attempts(candidate_id, limit=100),
        "experiments": _experiments(limit=100, proposal_id=candidate_id),
    }


@router.get("/evolution/lessons")
def list_lessons(
    limit: int = 50, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Adopted lessons (the R6/R7 retrieval source), newest first."""
    cap = max(1, min(int(limit), 200))
    return {"success": True, "lessons": _lesson_store().list_candidates(status="adopted", limit=cap)}


@router.get("/evolution/lessons/{lesson_id}/outcomes")
def lesson_outcomes(
    lesson_id: str, limit: int = 50, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Measured lift and recent cohort rows for one adopted lesson."""
    from ..self_improvement.lesson_feedback import LessonOutcomeStore  # noqa: PLC0415

    cap = max(1, min(int(limit), 200))
    store = LessonOutcomeStore(service.store)
    return {
        "success": True,
        "lessonId": lesson_id,
        "stats": store.stats(lesson_id).to_dict(),
        "outcomes": store.recent(lesson_id, limit=cap),
    }


@router.post("/evolution/lessons/{lesson_id}/retire")
def retire_lesson(
    lesson_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Retire a measured non-positive lesson. Inert lifecycle action; no delete."""
    from ..self_improvement.lesson_feedback import (  # noqa: PLC0415
        LessonOutcomeStore,
        retire_unhelpful,
    )

    outcomes = LessonOutcomeStore(service.store)
    stats = outcomes.stats(lesson_id)
    retired = retire_unhelpful(_lesson_store(), outcomes, [lesson_id])
    return {
        "success": True,
        "lessonId": lesson_id,
        "retired": bool(retired),
        "stats": stats.to_dict(),
        "reason": (
            "measured non-positive lift"
            if retired else
            "not retired: the lesson needs measured non-positive lift"
        ),
    }


@router.get("/evolution/candidates/{candidate_id}/state")
def candidate_harness_state(
    candidate_id: str,
    scope: Optional[str] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """The derived harness state(s) for one measured candidate, newest first."""
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    store = HarnessStateStore(service.store)
    rows = [
        row for row in store.list_states(scope=scope, limit=200)
        if str(row.get("candidateId") or "") == candidate_id
    ]
    return {
        "success": True,
        "candidateId": candidate_id,
        "state": rows[0] if rows else None,
        "states": rows,
    }


@router.get("/evolution/states")
def list_harness_states(
    scope: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """The immutable hill-climb states: current pointers plus recent rows."""
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    cap = max(1, min(int(limit), 200))
    store = HarnessStateStore(service.store)
    return {
        "success": True,
        "scope": scope or "global",
        "current": store.current(scope or "global"),
        "currents": store.current_states(limit=cap) if scope is None else [],
        "states": store.list_states(scope=scope, status=status, limit=cap),
    }


@router.post("/evolution/states/{state_id}/promote")
def promote_harness_state(
    state_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Make one candidate harness state current. This is a state pointer, not a code write."""
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    store = HarnessStateStore(service.store)
    try:
        state = store.promote(state_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "state": state}


def _surface_scopes() -> list[str]:
    from ..agents import AGENT_PRESETS  # noqa: PLC0415

    return ["global", *sorted(AGENT_PRESETS.keys())]


def _surface_diff(base: Any, snapshot: Any) -> dict[str, Any]:
    before_tools = set(base.toolNames)
    after_tools = set(snapshot.toolNames)
    model_changed = (
        base.modelProvider != snapshot.modelProvider
        or base.modelName != snapshot.modelName
        or base.temperature != snapshot.temperature
    )
    return {
        "systemPromptChanged": base.systemPrompt != snapshot.systemPrompt,
        "systemPromptBefore": base.systemPrompt[:4000],
        "systemPromptAfter": snapshot.systemPrompt[:4000],
        "systemPromptBeforeChars": len(base.systemPrompt),
        "systemPromptAfterChars": len(snapshot.systemPrompt),
        "toolsAdded": sorted(after_tools - before_tools),
        "toolsRemoved": sorted(before_tools - after_tools),
        "maxStepsBefore": base.maxSteps,
        "maxStepsAfter": snapshot.maxSteps,
        "maxToolCallsBefore": base.maxToolCalls,
        "maxToolCallsAfter": snapshot.maxToolCalls,
        "modelIdentityChanged": model_changed,
        "skillDraftIds": list(snapshot.skillDraftIds),
    }


def _task_rates(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Measured per-task pass rates; an all-unmeasured task is omitted."""
    buckets: dict[str, list[bool]] = {}
    for trial in trials:
        if bool(trial.get("unmeasured")):
            continue
        task_id = str(trial.get("taskId") or "")
        if not task_id:
            continue
        buckets.setdefault(task_id, []).append(bool(trial.get("passed")))
    return {
        task_id: {
            "n": len(values),
            "successes": sum(1 for value in values if value),
            "rate": sum(1 for value in values if value) / len(values),
        }
        for task_id, values in buckets.items()
    }


def _task_deltas(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task_id in sorted(set(baseline) | set(candidate)):
        before = baseline.get(task_id)
        after = candidate.get(task_id)
        before_rate = before["rate"] if before else None
        after_rate = after["rate"] if after else None
        rows.append({
            "taskId": task_id,
            "before": before_rate,
            "after": after_rate,
            "beforeN": before["n"] if before else 0,
            "afterN": after["n"] if after else 0,
            "delta": (
                round(float(after_rate) - float(before_rate), 6)
                if before_rate is not None and after_rate is not None else None
            ),
        })
    return rows


def _critical_task_ids_for_payload(payload: dict[str, Any]) -> set[str]:
    """The frozen critical/safety task ids named by a surface experiment."""
    try:
        from ..evaluation.tasks import load_builtin_dataset, select_tasks  # noqa: PLC0415
        from ..self_improvement.replay import critical_task_ids  # noqa: PLC0415

        dataset = load_builtin_dataset(str(payload.get("dataset") or "benchmark_v1"))
        tasks = select_tasks(dataset, split=str(payload.get("split") or "validation"))
        wanted = {str(task_id) for task_id in (payload.get("taskIds") or []) if str(task_id)}
        if wanted:
            tasks = [task for task in tasks if task.id in wanted]
        return set(critical_task_ids(tasks))
    except Exception:  # noqa: BLE001 - an unavailable dataset is not a veto
        return set()


def _surface_context(state_id: str, scope: Optional[str] = None) -> dict[str, Any]:
    """Build the shared preview/activation decision for one harness state."""
    from ..agents import DEFAULT_AGENT_ID, get_agent  # noqa: PLC0415
    from ..evaluation.candidate_invoker import (  # noqa: PLC0415
        SnapshotError,
        base_snapshot_from_agent,
        validate_snapshot,
    )
    from ..self_improvement.models import HarnessSnapshot  # noqa: PLC0415
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    store = HarnessStateStore(service.store)
    state = store.get_state(state_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Harness state not found")
    available = _surface_scopes()
    chosen = str(scope or state.get("scope") or "global").strip()
    if chosen not in available:
        raise HTTPException(
            status_code=400,
            detail=f"unknown surface scope {chosen!r} (expected {', '.join(available)})",
        )

    reasons: list[str] = []
    if state["status"] not in ("candidate", "current"):
        reasons.append("state is not activatable")
    if state.get("utility") is None:
        reasons.append("state is UNMEASURED")

    candidate = _store().get_candidate(str(state.get("candidateId") or ""))
    if candidate is None or candidate.get("subjectType") != "surface":
        reasons.append("state is not a surface candidate")

    decision = None
    accepted_run = _eval_store().get_run(str(state.get("acceptedRunId") or ""))
    if accepted_run is not None:
        decision = _accepted_decision(accepted_run)
    if decision is None:
        reasons.append("surface experiment was not accepted")

    base = snapshot = None
    payload = (candidate or {}).get("surfaceConfig") or {}
    try:
        base = HarnessSnapshot(**dict(payload.get("base") or {}))
        snapshot = HarnessSnapshot(**dict(payload.get("snapshot") or {}))
        validate_snapshot(base, snapshot)
    except (TypeError, ValueError, SnapshotError) as exc:
        reasons.append(f"SURFACE_INVALID: {exc}")

    if snapshot is not None:
        if snapshot.skillDraftIds:
            reasons.append("SURFACE_LIVE_UNSUPPORTED: skill drafts are not applied live")
        if (
            snapshot.modelProvider is not None
            or snapshot.modelName is not None
            or snapshot.temperature is not None
        ):
            reasons.append(
                "SURFACE_LIVE_UNSUPPORTED: model identity is resolved by the front door"
            )

    evidence = None
    if accepted_run is not None:
        eval_store = _eval_store()
        run_meta = accepted_run.get("metadata") or {}
        baseline_run_id = str(
            payload.get("baselineRunId")
            or run_meta.get("baselineRunId")
            or ""
        ).strip()
        baseline_run = eval_store.get_run(baseline_run_id) if baseline_run_id else None
        metrics = eval_store.list_metrics(str(accepted_run["id"]), scope="overall")
        baseline_metrics = (
            eval_store.list_metrics(baseline_run_id, scope="overall")
            if baseline_run is not None else []
        )
        candidate_trials = [
            {key: value for key, value in trial.items() if key != "trajectory"}
            for trial in eval_store.list_trials(str(accepted_run["id"]))[:50]
        ]
        baseline_trials = (
            [
                {key: value for key, value in trial.items() if key != "trajectory"}
                for trial in eval_store.list_trials(baseline_run_id)[:50]
            ]
            if baseline_run is not None else []
        )
        target_dimension = str(payload.get("targetDimension") or "task_success")
        before = next(
            (row.get("value") for row in baseline_metrics
             if row.get("dimension") == target_dimension),
            None,
        )
        after = next(
            (row.get("value") for row in metrics
             if row.get("dimension") == target_dimension),
            None,
        )
        evidence = {
            "run": _run_summary(accepted_run),
            "metrics": metrics,
            "trials": candidate_trials,
            "baseline": {
                "run": _run_summary(baseline_run) if baseline_run is not None else None,
                "metrics": baseline_metrics,
                "trials": baseline_trials,
            } if baseline_run_id else None,
            "taskDeltas": _task_deltas(
                _task_rates(baseline_trials),
                _task_rates(candidate_trials),
            ),
            "target": {
                "dimension": target_dimension,
                "before": before,
                "after": after,
                "delta": (
                    float(after) - float(before)
                    if before is not None and after is not None else None
                ),
            },
        }
        critical_ids = _critical_task_ids_for_payload(payload)
        critical_deltas = [
            row for row in evidence["taskDeltas"]
            if row["taskId"] in critical_ids
            and row["delta"] is not None
            and row["delta"] < 0
        ]
        evidence["criticalDeltas"] = critical_deltas
        for row in critical_deltas:
            reasons.append(
                f"critical task regressed: {row['taskId']} "
                f"({row['before']:.3f} → {row['after']:.3f})"
            )

    reference_agent = chosen if chosen != "global" else DEFAULT_AGENT_ID
    base_digest_matches = None
    if base is not None:
        try:
            current_base = base_snapshot_from_agent(get_agent(reference_agent))
            base_digest_matches = current_base.sourceDigest == base.sourceDigest
            if not base_digest_matches:
                reasons.append("base snapshot changed; the activation would be skipped")
        except Exception as exc:  # noqa: BLE001 - unavailable base is a refusal
            base_digest_matches = False
            reasons.append(f"base snapshot unavailable: {exc}")

    previous = store.current(chosen)
    return {
        "store": store,
        "state": state,
        "candidate": candidate,
        "scope": chosen,
        "availableScopes": available,
        "referenceAgent": reference_agent,
        "base": base,
        "snapshot": snapshot,
        "baseDigestMatches": base_digest_matches,
        "accepted": decision is not None,
        "acceptedDecision": decision,
        "evidence": evidence,
        "previousState": previous,
        "canActivate": not reasons,
        "reasons": reasons,
        "diff": _surface_diff(base, snapshot) if base is not None and snapshot is not None else None,
    }


@router.get("/evolution/states/{state_id}/surface-preview")
def preview_surface_state(
    state_id: str,
    scope: Optional[str] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Read-only preview of the exact live surface change and any refusal reason."""
    context = _surface_context(state_id, scope)
    return {
        "success": True,
        "stateId": state_id,
        "candidateId": (context.get("state") or {}).get("candidateId"),
        "scope": context["scope"],
        "availableScopes": context["availableScopes"],
        "referenceAgent": context["referenceAgent"],
        "baseDigestMatches": context["baseDigestMatches"],
        "accepted": context["accepted"],
        "acceptedDecision": context["acceptedDecision"],
        "canActivate": context["canActivate"],
        "reasons": context["reasons"],
        "diff": context["diff"],
        "evidence": context["evidence"],
        "previousState": context["previousState"],
    }


@router.post("/evolution/states/{state_id}/activate-surface")
def activate_surface_state(
    state_id: str,
    body: Optional[SurfaceActivationRequest] = None,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Activate a measured surface snapshot, optionally for a chosen agent scope.

    A scope different from the state's own scope creates a new immutable state
    row for that scope and activates it; the original state is untouched. Model
    identity and skill drafts are refused. Rollback restores the previous
    current state for the chosen scope.
    """
    context = _surface_context(state_id, body.scope if body else None)
    if not context["canActivate"]:
        hard = any(
            "SURFACE_LIVE_UNSUPPORTED" in reason
            or "SURFACE_INVALID" in reason
            or "not a surface candidate" in reason
            or "not activatable" in reason
            for reason in context["reasons"]
        )
        status = 400 if hard else 409
        raise HTTPException(status_code=status, detail={"reasons": context["reasons"]})

    store = context["store"]
    state = context["state"]
    target_id = state["id"]
    if context["scope"] != state["scope"]:
        target = store.save_state({
            "scope": context["scope"],
            "parentStateId": state.get("parentStateId"),
            "snapshotDigest": state["snapshotDigest"],
            "candidateId": state.get("candidateId"),
            "acceptedRunId": state.get("acceptedRunId"),
            "utility": state.get("utility"),
            "metrics": state.get("metrics") or {},
            "status": "candidate",
        })
        target_id = target["id"]
    activation = store.activate_surface(
        target_id,
        snapshot=context["snapshot"].to_dict(),
        base=context["base"].to_dict(),
    )
    return {
        "success": True,
        "activation": activation,
        "state": store.get_state(target_id),
        "preview": {
            "scope": context["scope"],
            "referenceAgent": context["referenceAgent"],
            "baseDigestMatches": context["baseDigestMatches"],
            "diff": context["diff"],
            "evidence": context["evidence"],
        },
    }


@router.get("/evolution/surface-activations")
def list_surface_activations(
    scope: Optional[str] = None,
    limit: int = 50,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    store = HarnessStateStore(service.store)
    return {
        "success": True,
        "activations": store.list_activations(
            scope=scope, limit=max(1, min(int(limit), 200)),
        ),
    }


@router.post("/evolution/surface-activations/{activation_id}/rollback")
def rollback_surface_activation(
    activation_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    store = HarnessStateStore(service.store)
    try:
        activation = store.rollback_activation(activation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "activation": activation}


@router.post("/evolution/candidates/{candidate_id}/lessons")
def adopt_lesson(
    candidate_id: str, body: LessonAdoptionRequest,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Adopt a verified experiment's lesson through the R6 gate.

    The decision is read from the persisted experiment, never from the request:
    an unaccepted/unmeasured experiment cannot become a lesson. The lesson text
    still passes the verdict tripwire inside `distill_lesson`.
    """
    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    run_id = str(body.validationRunId or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="validationRunId is required")
    run = _eval_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Validation run not found")
    metadata = _loads(run.get("metadata"))
    if metadata.get("kind") not in {"rsl", "rsl-code", "rsl-recursion"}:
        raise HTTPException(status_code=400, detail="Validation run is not an RSL experiment")
    decision = _accepted_decision(run)
    if decision is None:
        raise HTTPException(
            status_code=409,
            detail="the experiment is not accepted; a lesson requires a verified decision",
        )

    from ..evaluation.verdict_guard import VerdictLeakError  # noqa: PLC0415
    from ..self_improvement.lessons import distill_lesson  # noqa: PLC0415

    evidence = body.evidence
    if not evidence:
        evidence = [{
            "kind": "experiment",
            "runId": run_id,
            "benchmark": metadata.get("benchmark") or "UNMEASURED",
        }]
    try:
        result = distill_lesson(
            _lesson_store(),
            candidate,
            decision,
            evidence,
            cue=body.cue,
            lesson=body.lesson,
            scope=body.scope,
            validation_run_id=run_id,
            adopted_artifact_type=body.adoptedArtifactType or "evolution_candidate",
            adopted_artifact_id=body.adoptedArtifactId or candidate_id,
        )
    except VerdictLeakError as exc:
        # A leak is a hard error: refuse the lesson, never store the answer key.
        raise HTTPException(status_code=400, detail="VERDICT_LEAK") from exc
    if result.get("discarded"):
        raise HTTPException(
            status_code=400, detail=str(result.get("reason") or "lesson discarded"),
        )
    lesson = _lesson_store().get_candidate(str(result.get("lessonId")))
    return {"success": True, **result, "lesson": lesson}


@router.post("/evolution/candidates/{candidate_id}/recursive")
async def run_recursive_experiment(
    candidate_id: str, body: RecursiveExperimentRequest,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Run ONE operator-started k-candidate code experiment.

    The variants are typed operator/agent ops. Each becomes a real child
    `EvolutionCandidate` verified by the existing worktree/tests verifier; the
    R7 conductor records the anonymous selection/composition. The live checkout
    is never written and no change activates.
    """
    parent = _store().get_candidate(candidate_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    if getattr(getattr(loop, "config", None), "mode", "off") not in ("verify", "auto"):
        raise HTTPException(
            status_code=409,
            detail="HARNESS_RSL_MODE must be verify or auto for a recursive experiment",
        )
    variants = [variant.model_dump() for variant in body.variants]
    if not variants:
        raise HTTPException(status_code=400, detail="at least one variant is required")

    from ..self_improvement import verifier  # noqa: PLC0415
    from ..self_improvement.proposer import ProposalRefused, propose_many  # noqa: PLC0415

    try:
        proposals = propose_many(
            parent, min(len(variants), int(loop.config.k)),
            variants=variants, actor=service.operator_actor(),
        )
    except ProposalRefused as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc

    evolution = _store()
    children: list[dict[str, Any]] = []
    for proposal in proposals:
        signature = _variant_signature(parent["signature"], proposal)
        child = evolution.get_by_signature(signature)
        if child is None:
            child = evolution.save_candidate({
                "signature": signature,
                "sourceCategory": parent.get("sourceCategory") or "rsl",
                "observedCount": parent.get("observedCount") or 1,
                "problemStatement": parent.get("problemStatement") or "",
                "targetPath": proposal.targetPath,
                "proposedOps": list(proposal.ops),
                "status": "inert",
                "createdByRunId": parent.get("createdByRunId"),
            })
        children.append(child)
    child_ids = {proposal.id: child["id"] for proposal, child in zip(proposals, children)}

    async def verify_fn(proposal: Any) -> dict[str, Any]:
        child_id = child_ids[proposal.id]
        ctx = service.evolution_context(
            run_id=f"rsl-recursive-{candidate_id}-{proposal.id}",
        )
        return await asyncio.to_thread(
            verifier.verify_code, service.store, child_id,
            ctx=ctx, actor=service.operator_actor(),
        )

    result = await loop.run_recursive(
        parent, verify_fn=verify_fn, variants=variants, seed=body.seed,
        # Each child verifier already records its own attempt; do not double-count
        # k against the daily budget.
        record_attempts=False,
    )
    if result.get("refused"):
        reason = str(result.get("reason") or "recursive experiment refused")
        status = 429 if reason == "daily_budget" else 409 if reason in {"mode", "busy"} else 400
        raise HTTPException(status_code=status, detail=reason)
    return {"success": True, **result, "children": children}


@router.post("/evolution/surface-experiments")
def create_surface_experiment(
    body: SurfaceExperimentRequest, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Queue ONE measured surface experiment as a typed candidate.

    The snapshot must be a bounded variation of the base. The route creates the
    candidate as `proposed` and wakes the existing conductor; the loop owns the
    claim/lease, runs the same base snapshot as a recorded baseline, benchmarks
    the candidate on the selected split, and records the evaluator's decision.
    Nothing is applied and a surface snapshot is never landable in v1.
    """
    from ..evaluation.candidate_invoker import SnapshotError, validate_snapshot  # noqa: PLC0415
    from ..evaluation.tasks import (  # noqa: PLC0415
        SPLITS,
        TaskValidationError,
        load_builtin_dataset,
        select_tasks,
    )
    from ..self_improvement.models import HarnessSnapshot  # noqa: PLC0415

    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    config = getattr(loop, "config", None)
    if getattr(config, "mode", "off") not in ("verify", "auto"):
        raise HTTPException(
            status_code=409,
            detail="HARNESS_RSL_MODE must be verify or auto for a surface experiment",
        )
    budget_ok = getattr(loop, "budget_ok", None)
    if callable(budget_ok) and not budget_ok():
        raise HTTPException(status_code=429, detail="daily experiment budget exhausted")
    scope = str(body.scope or "global").strip()
    available_scopes = _surface_scopes()
    if scope not in available_scopes:
        raise HTTPException(
            status_code=400,
            detail=f"unknown surface scope {scope!r} (expected {', '.join(available_scopes)})",
        )

    try:
        base = (
            _surface_snapshot(body.base) if body.base is not None
            else HarnessSnapshot(
                systemPrompt="You are Eclipse.", toolNames=(),
                maxSteps=8, maxToolCalls=8,
            )
        )
        candidate = _surface_snapshot(body.candidate, base)
        validate_snapshot(base, candidate)
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    split = str(body.split or "validation").strip().lower()
    if split not in SPLITS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown split (expected one of {', '.join(SPLITS)})",
        )
    try:
        dataset = load_builtin_dataset(body.dataset or "benchmark_v1")
    except TaskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tasks = select_tasks(dataset, split=split)
    wanted = [str(task_id) for task_id in (body.taskIds or []) if str(task_id)]
    if wanted:
        wanted_set = set(wanted)
        tasks = [task for task in tasks if task.id in wanted_set]
        missing = sorted(wanted_set - {task.id for task in tasks})
        if missing:
            raise HTTPException(
                status_code=400,
                detail="tasks are not in the selected split: " + ", ".join(missing),
            )
    if not tasks:
        raise HTTPException(status_code=400, detail="the experiment selected no tasks")

    try:
        trials = max(1, min(int(body.trials or getattr(config, "trials", 3)), 10))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="trials must be an integer") from exc
    target_dimension = str(body.targetDimension or "task_success").strip() or "task_success"
    baseline_run_id = str(body.baselineRunId or "").strip() or None
    from ..self_improvement.surface_candidate import (  # noqa: PLC0415
        save_surface_candidate,
    )

    created = save_surface_candidate(
        service.store,
        base=base,
        snapshot=candidate,
        scope=scope,
        dataset=body.dataset or "benchmark_v1",
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        task_ids=[task.id for task in tasks],
        split=split,
        target_dimension=target_dimension,
        trials=trials,
        seed=body.seed,
        baseline_run_id=baseline_run_id,
        status="proposed",
        source_category=body.sourceCategory or "operator",
        problem_statement=body.problemStatement or (
            f"Surface harness experiment on {dataset.name} ({split})"
        ),
    )
    candidate_row = created["candidate"]
    if not created["created"]:
        existing = candidate_row
        if existing.get("status") == "inert":
            existing = _store().update_candidate(existing["id"], status="proposed") or existing
        queued = existing.get("status") in {"inert", "proposed"}
        if queued:
            loop.wake()
        return {
            "success": True, "candidate": existing,
            "created": False, "queued": queued,
        }
    loop.wake()
    return {
        "success": True, "candidate": candidate_row,
        "created": True, "queued": True,
    }


@router.post("/evolution/surface-lab/{candidate_id}/queue")
def queue_surface_lab_candidate(
    candidate_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Queue a STAGED surface-lab candidate for the loop's verification.

    The lab's measurement is evidence, not a decision: it stages the winner as
    an INERT candidate. This route is the operator's one click to queue it, and
    the loop then verifies it through the SAME measured path every other
    surface experiment uses. Nothing is applied and nothing is activated here.
    """
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    config = getattr(loop, "config", None)
    if getattr(config, "mode", "off") not in ("verify", "auto"):
        raise HTTPException(
            status_code=409,
            detail="HARNESS_RSL_MODE must be verify or auto to queue a surface experiment",
        )
    candidate = _store().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.get("subjectType") != "surface":
        raise HTTPException(
            status_code=400, detail="only a surface candidate can be queued here",
        )
    if candidate.get("status") not in {"inert", "proposed"}:
        raise HTTPException(
            status_code=409,
            detail=f"a candidate in status {candidate.get('status')!r} cannot be queued",
        )
    budget_ok = getattr(loop, "budget_ok", None)
    if callable(budget_ok) and not budget_ok():
        raise HTTPException(status_code=429, detail="daily experiment budget exhausted")
    updated = _store().update_candidate(candidate_id, status="proposed") or candidate
    loop.wake()
    return {"success": True, "candidate": updated, "queued": True}


@router.post("/evolution/candidates/{candidate_id}/prepare-landing")
def prepare_candidate_landing(
    candidate_id: str, _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Prepare a worktree + the existing promotion preview. NEVER applies."""
    from ..self_improvement.landing import RSLLandingError, prepare  # noqa: PLC0415

    try:
        result = prepare(service, candidate_id)
    except RSLLandingError as exc:
        status = 403 if exc.code in (
            "RSL_FROZEN_TARGET", "SELFHOST_AUTHORITY_REFUSED",
        ) else 400
        raise HTTPException(status_code=status, detail=exc.code) from exc
    except Exception as exc:  # noqa: BLE001 - promotion refusal is a 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        **result,
        "approvalRequired": True,
        # The existing single-use promotion approval is the only write path.
        "promotionRoute": f"/api/code/tasks/{result['runId']}/promote/preview",
    }


@router.get("/evolution/rsl")
def rsl_status(_: bool = Depends(require_operator)) -> dict[str, Any]:
    """The loop's mode, lifecycle and remaining daily budget."""
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    status = getattr(loop, "status", None)
    if callable(status):
        return {"success": True, **status()}
    return {"success": True, **loop.health()}


@router.post("/evolution/rsl/config")
async def rsl_config(
    payload: dict[str, Any], _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Apply bounded RSL overrides without a restart (persisted)."""
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    try:
        status = await loop.set_config(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **status}


@router.post("/evolution/rsl/tick")
async def rsl_tick(_: bool = Depends(require_operator)) -> dict[str, Any]:
    """Run one bounded conductor tick now (operator-triggered)."""
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    return {"success": True, **await loop.tick()}


@router.post("/evolution/rsl/climb/pause")
def pause_rsl_climb(_: bool = Depends(require_operator)) -> dict[str, Any]:
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    loop.pause_climb()
    return {"success": True, "climbPaused": True}


@router.post("/evolution/rsl/climb/resume")
def resume_rsl_climb(_: bool = Depends(require_operator)) -> dict[str, Any]:
    loop = getattr(service, "_rsl_loop", None)
    if loop is None:
        raise HTTPException(status_code=503, detail="RSL loop unavailable")
    loop.resume_climb()
    return {"success": True, "climbPaused": False}





@router.get("/evolution/states/pareto")
def states_pareto(
    scope: Optional[str] = None,
    limit: int = 100,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    """Read-only multi-objective frontier over recorded harness states (v2)."""
    from ..self_improvement.pareto import frontier  # noqa: PLC0415
    from ..self_improvement.state import HarnessStateStore  # noqa: PLC0415

    states = HarnessStateStore(service.store).list_states(
        scope=scope, limit=max(1, min(int(limit), 500)),
    )
    return {"success": True, **frontier(states)}


@router.get("/evolution/metrics")
def evolution_metrics(windowDays: int = 90, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, **evo.archive_metrics(service.store, window_days=windowDays)}


__all__ = ["router"]
