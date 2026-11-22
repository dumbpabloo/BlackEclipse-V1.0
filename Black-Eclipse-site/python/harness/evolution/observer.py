"""Recurrence observation for evolution signals.

An observation is a stable `(kind, key)` pair plus bounded detail and evidence
refs. The observer counts recurrences in a rolling window and emits one
suggestion when the threshold is first crossed. It never invents a mutation and
never applies anything; the proposer turns a suggestion into a candidate.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Observation:
    kind: str
    key: str
    detail: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    count: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    suggested_at: Optional[float] = None


@dataclass(frozen=True)
class Suggestion:
    kind: str
    key: str
    count: int
    detail: dict[str, Any]
    evidence_refs: tuple[str, ...]


class EvolutionObserver:
    def __init__(
        self,
        *,
        threshold: int = 3,
        window_seconds: int = 3600,
        max_observations: int = 500,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.window_seconds = max(1, int(window_seconds))
        self.max_observations = max(1, int(max_observations))
        self._now = now or time.monotonic
        self._observations: dict[str, Observation] = {}
        self._pending: list[Suggestion] = []

    @staticmethod
    def _id(kind: str, key: str) -> str:
        return hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()[:24]

    def observe(
        self,
        *,
        kind: str,
        key: str,
        detail: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None,
    ) -> Optional[Suggestion]:
        if not kind or not key:
            raise ValueError("an observation needs a kind and a key")
        now = self._now()
        ident = self._id(kind, key)
        observation = self._observations.get(ident)
        if observation is None:
            observation = Observation(kind=kind, key=key, first_at=now)
            self._observations[ident] = observation
        # A quiet window starts a fresh streak; the evidence is kept for lineage.
        if now - observation.last_at > self.window_seconds:
            observation.count = 0
            observation.suggested_at = None
        observation.count += 1
        observation.last_at = now
        observation.detail.update(detail or {})
        for ref in evidence_refs or []:
            if ref and ref not in observation.evidence_refs:
                observation.evidence_refs.append(ref)
        if len(self._observations) > self.max_observations:
            oldest = min(self._observations, key=lambda item: self._observations[item].last_at)
            self._observations.pop(oldest, None)
        if observation.count < self.threshold or observation.suggested_at is not None:
            return None
        observation.suggested_at = now
        suggestion = Suggestion(
            kind=observation.kind,
            key=observation.key,
            count=observation.count,
            detail=dict(observation.detail),
            evidence_refs=tuple(observation.evidence_refs),
        )
        self._pending.append(suggestion)
        return suggestion

    def take(self) -> list[Suggestion]:
        pending, self._pending = self._pending, []
        return pending

    def pending(self) -> list[Suggestion]:
        return list(self._pending)

    def observations(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": item.kind,
                "key": item.key,
                "count": item.count,
                "evidenceRefs": list(item.evidence_refs),
                "suggested": item.suggested_at is not None,
            }
            for item in self._observations.values()
        ]

    def reset(self) -> None:
        self._observations.clear()
        self._pending.clear()
