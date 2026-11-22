"""Observer -> proposer -> candidate -> journal pipeline.

Modes:
* ``observe`` - count signals, never propose.
* ``propose`` - create candidates (default).
* ``shadow``  - create candidates marked shadow; they cannot be applied.
* ``apply``   - same proposal surface; application remains explicit.

The pipeline never admits, evaluates or applies automatically. Those steps stay
operator/evaluator-owned.
"""

from __future__ import annotations

from typing import Any, Optional

from .observer import EvolutionObserver, Suggestion
from .proposer import AssetProposer
from .service import AssetEvolution

MODES = ("observe", "propose", "shadow", "apply")


class EvolutionPipeline:
    def __init__(
        self,
        evolution: AssetEvolution,
        *,
        mode: str = "propose",
        observer: Optional[EvolutionObserver] = None,
        proposer: Optional[AssetProposer] = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown evolution mode {mode!r}")
        self.evolution = evolution
        self.mode = mode
        self.observer = observer or EvolutionObserver()
        self.proposer = proposer or AssetProposer(evolution)

    def observe(
        self,
        *,
        kind: str,
        key: str,
        detail: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        suggestion = self.observer.observe(
            kind=kind, key=key, detail=detail, evidence_refs=evidence_refs,
        )
        if suggestion is None:
            return {"status": "counted"}
        if self.mode == "observe":
            return {"status": "observed", "suggestion": _suggestion(suggestion)}
        candidate = self.proposer.propose(suggestion)
        if candidate is None:
            return {"status": "no-proposal", "suggestion": _suggestion(suggestion)}
        if self.mode == "shadow":
            candidate.decision["shadow"] = True
            self.evolution.persist(candidate)
        return {
            "status": "shadow" if self.mode == "shadow" else "proposed",
            "candidate": candidate.to_dict(),
        }

    def tick(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for suggestion in self.observer.take():
            if self.mode == "observe":
                continue
            candidate = self.proposer.propose(suggestion)
            if candidate is None:
                continue
            if self.mode == "shadow":
                candidate.decision["shadow"] = True
                self.evolution.persist(candidate)
            results.append(candidate.to_dict())
        return results

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "observations": self.observer.observations(),
            "pendingSuggestions": [
                _suggestion(item) for item in self.observer.pending()
            ],
        }


def _suggestion(item: Suggestion) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "key": item.key,
        "count": item.count,
        "evidenceRefs": list(item.evidence_refs),
    }
