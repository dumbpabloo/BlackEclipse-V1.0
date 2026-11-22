"""Wire the existing evaluation gates to asset candidates.

The evaluator does not invent a score. It applies the candidate through the
asset pipeline, runs the existing ``evaluate_candidate`` (the same
measured-vs-UNMEASURED, critical-regression and target-dimension logic used by
the self-improvement loop), then records the outcome:

* accepted                 -> ``measured_ok`` (the asset remains applied)
* measured but not better  -> ``measured_bad`` (the proposal's revert ran)
* no measured trial        -> ``unmeasured`` (nothing is applied)

A missing baseline dimension is ``unmeasured`` before any apply, so an
unmeasured asset is never mounted.
"""

from __future__ import annotations

from typing import Any, Optional

from ..evaluation.improvement import ImprovementProposal, evaluate_candidate
from .assets import AssetError, CandidateStatus


class AssetEvaluator:
    def __init__(self, evolution: Any) -> None:
        self.evolution = evolution

    async def evaluate(
        self,
        candidate_id: str,
        *,
        runner: Any,
        tasks: list[Any],
        baseline_metrics: dict[str, Any],
        target_dimension: str = "task_success",
        min_improvement: float = 0.0,
        trials: Optional[int] = None,
        split: str = "validation",
        run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        watch_keys: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        candidate = self.evolution.get(candidate_id)
        if candidate.status is not CandidateStatus.ADMITTED:
            raise AssetError(
                "EVOLUTION_NOT_ADMITTED",
                f"only an admitted candidate can be evaluated (got {candidate.status.value})",
            )
        baseline_agg = baseline_metrics.get("aggregates", baseline_metrics)
        if baseline_agg.get(target_dimension) is None:
            decision = {
                "accepted": False,
                "reason": f"baseline {target_dimension} is UNMEASURED",
                "measured": 0,
                "runId": run_id,
            }
            self.evolution.mark_measured(candidate_id, CandidateStatus.UNMEASURED, decision)
            return {
                "candidateId": candidate_id,
                "status": CandidateStatus.UNMEASURED.value,
                "decision": decision,
            }

        proposal = ImprovementProposal(
            id=candidate.id,
            description=f"{candidate.kind.value}:{candidate.target}",
            target_dimension=target_dimension,
            apply=lambda: self.evolution.apply(candidate_id),
            revert=lambda: self.evolution.rollback(candidate_id),
        )
        try:
            result = await evaluate_candidate(
                runner,
                tasks,
                proposal,
                baseline_metrics,
                min_improvement=min_improvement,
                trials=trials,
                split=split,
                run_id=run_id,
                metadata=metadata,
            )
        except BaseException:
            # The suite failed after the asset was applied; never leave an
            # unmeasured asset live.
            current = self.evolution.get(candidate_id)
            if current.status in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
                try:
                    self.evolution.rollback(candidate_id)
                except AssetError:
                    pass
            raise

        decision = result.to_dict()
        if result.accepted:
            status = CandidateStatus.MEASURED_OK
        elif result.measured == 0 or "UNMEASURED" in str(result.reason):
            status = CandidateStatus.UNMEASURED
        else:
            status = CandidateStatus.MEASURED_BAD
        self.evolution.mark_measured(candidate_id, status, decision)
        if status is CandidateStatus.MEASURED_OK and watch_keys:
            self.evolution.watch(candidate_id, watch_keys)
        return {
            "candidateId": candidate_id,
            "status": status.value,
            "decision": decision,
        }
