"""Asset evolution: propose -> admit -> apply -> rollback for declarative assets.

This is the Phase 6 *contract* slice, not the evaluator. It enforces the state
machine and the reversal property (an undo per applied mutation, run in reverse
on rollback), and it refuses to apply anything that was not admitted. Evaluation,
measured vs UNMEASURED status, TTL and the regression watch are later phases over
the same candidate vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .adapters import AdapterRegistry, Undo
from .assets import (
    AssetError,
    AssetKind,
    Candidate,
    CandidateStatus,
    Mutation,
    MutationOp,
    new_candidate,
)
from .journal import EvolutionJournal


class AssetEvolution:
    def __init__(
        self,
        adapters: AdapterRegistry,
        journal: Optional[EvolutionJournal] = None,
        *,
        max_active: int = 10,
        ttl_seconds: int = 0,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.adapters = adapters
        self.journal = journal
        self.max_active = max(0, int(max_active))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._candidates: dict[str, Candidate] = {}
        self._undos: dict[str, list[Undo]] = {}
        self.recovered_intents: list[str] = []
        if self.journal is not None:
            self.recover()

    # -- recovery -------------------------------------------------------------

    def recover(self) -> list[str]:
        """Restore candidate state from the journal and name unfinished intents."""
        if self.journal is None:
            return []
        for candidate in self.journal.latest_candidates().values():
            candidate.recovered = candidate.status in (
                CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK,
            )
            self._candidates[candidate.id] = candidate
        pending = self.journal.pending()
        self.recovered_intents = sorted(pending)
        for key in pending:
            if not key.startswith("apply:"):
                continue
            candidate_id = key.split(":", 3)[1]
            candidate = self._candidates.get(candidate_id)
            if candidate is not None:
                candidate.status = CandidateStatus.APPLIED
                candidate.recovered = True
                candidate.refusal = "recovered_after_crash: the live effects may already be applied"
        return self.recovered_intents

    def _now_iso(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat()

    def _persist(self, candidate: Candidate) -> None:
        if self.journal is not None:
            self.journal.snapshot(candidate)

    def persist(self, candidate: Candidate) -> None:
        """Public snapshot write for the proposer/pipeline (no state transition)."""
        self._persist(candidate)

    # -- pipeline -------------------------------------------------------------

    def propose(
        self,
        *,
        kind: AssetKind | str,
        target: str,
        mutations: list[Mutation] | list[dict[str, Any]],
        parent_id: Optional[str] = None,
        evidence_refs: Optional[list[str]] = None,
    ) -> Candidate:
        candidate = new_candidate(
            kind=kind,
            target=target,
            mutations=mutations,
            parent_id=parent_id,
            evidence_refs=evidence_refs,
        )
        self._candidates[candidate.id] = candidate
        self._persist(candidate)
        return candidate

    def admit(self, candidate_id: str) -> Candidate:
        candidate = self.get(candidate_id)
        if candidate.status is not CandidateStatus.PROPOSED:
            raise AssetError("EVOLUTION_STATE", f"cannot admit a {candidate.status.value} candidate")
        adapter = self.adapters.require(candidate.kind)
        for mutation in candidate.mutations:
            if mutation.op is not MutationOp.RETIRE:
                adapter.validate(mutation.content)
            existing = adapter.read(mutation.target, mutation.content)
            if mutation.op is MutationOp.ADD and existing is not None:
                candidate.status = CandidateStatus.REFUSED
                candidate.refusal = "EVOLUTION_TARGET_EXISTS"
                self._persist(candidate)
                raise AssetError("EVOLUTION_TARGET_EXISTS", f"{mutation.target!r} already exists")
            if mutation.op in (MutationOp.PATCH, MutationOp.RETIRE) and existing is None:
                candidate.status = CandidateStatus.REFUSED
                candidate.refusal = "EVOLUTION_TARGET_MISSING"
                self._persist(candidate)
                raise AssetError("EVOLUTION_TARGET_MISSING", f"{mutation.target!r} does not exist")
        candidate.status = CandidateStatus.ADMITTED
        self._persist(candidate)
        return candidate

    def apply(self, candidate_id: str) -> Candidate:
        candidate = self.get(candidate_id)
        if candidate.status in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
            return candidate  # idempotent: a recovered/replayed apply never runs twice
        if candidate.decision.get("shadow"):
            raise AssetError(
                "EVOLUTION_SHADOW",
                "a shadow candidate records a proposal only and cannot be applied",
            )
        if candidate.status is not CandidateStatus.ADMITTED:
            raise AssetError(
                "EVOLUTION_NOT_ADMITTED",
                f"only an admitted candidate can be applied (got {candidate.status.value})",
            )
        active = sum(
            1 for item in self._candidates.values()
            if item.status in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK)
        )
        if self.max_active and active >= self.max_active:
            raise AssetError("EVOLUTION_ACTIVE_CAP", f"active asset cap reached ({self.max_active})")
        key = f"apply:{candidate.id}:{candidate.digest}"
        if self.journal is not None and not self.journal.begin(key, {"candidateId": candidate.id}):
            if candidate.status in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
                return candidate
            raise AssetError(
                "EVOLUTION_ACTION_PENDING",
                f"an earlier apply intent for {candidate.id!r} is unresolved; recover first",
            )
        adapter = self.adapters.require(candidate.kind)
        undo_list: list[Undo] = []
        try:
            for mutation in candidate.mutations:
                if mutation.op is MutationOp.RETIRE:
                    retire = getattr(adapter, "retire", None)
                    if not callable(retire):
                        raise AssetError(
                            "EVOLUTION_OP_UNSUPPORTED",
                            f"{candidate.kind.value} adapter does not implement retire",
                        )
                    undo_list.append(retire(mutation.target, mutation.content))
                else:
                    undo_list.append(adapter.apply(mutation.target, mutation.content))
        except BaseException as exc:
            for undo in reversed(undo_list):
                try:
                    undo()
                except BaseException:  # noqa: BLE001 - best-effort rollback
                    pass
            candidate.status = CandidateStatus.REFUSED
            candidate.refusal = "apply_failed"
            self._persist(candidate)
            if self.journal is not None:
                self.journal.finish(key, {"error": f"{type(exc).__name__}: {exc}"})
            raise
        self._undos[candidate.id] = undo_list
        candidate.status = CandidateStatus.APPLIED
        if self.ttl_seconds:
            from datetime import timedelta  # noqa: PLC0415

            candidate.expires_at = (self._now() + timedelta(seconds=self.ttl_seconds)).astimezone(timezone.utc).isoformat()
        self._persist(candidate)
        if self.journal is not None:
            self.journal.finish(key, {"candidateId": candidate.id, "status": candidate.status.value})
        return candidate

    def rollback(self, candidate_id: str) -> Candidate:
        candidate = self.get(candidate_id)
        if candidate.status not in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
            raise AssetError("EVOLUTION_STATE", f"cannot roll back a {candidate.status.value} candidate")
        undos = self._undos.pop(candidate.id, [])
        if candidate.recovered and not undos:
            raise AssetError(
                "EVOLUTION_RECOVERED_NO_UNDO",
                "a recovered candidate has no in-process undo handle; repair it explicitly",
            )
        errors: list[str] = []
        for undo in reversed(undos):
            try:
                undo()
            except BaseException as exc:  # noqa: BLE001 - continue the rest, report
                errors.append(f"{type(exc).__name__}: {exc}")
        candidate.status = CandidateStatus.ROLLED_BACK
        if errors:
            candidate.refusal = "rollback_partial: " + "; ".join(errors[:3])
        self._persist(candidate)
        if self.journal is not None:
            self.journal.append("rollback", candidate.id, {"reason": candidate.refusal or "requested"})
        return candidate

    def refuse(self, candidate_id: str, reason: str) -> Candidate:
        candidate = self.get(candidate_id)
        candidate.status = CandidateStatus.REFUSED
        candidate.refusal = reason
        self._persist(candidate)
        return candidate

    def mark_measured(
        self,
        candidate_id: str,
        status: CandidateStatus,
        decision: dict[str, Any],
    ) -> Candidate:
        if status not in (
            CandidateStatus.MEASURED_OK,
            CandidateStatus.MEASURED_BAD,
            CandidateStatus.UNMEASURED,
        ):
            raise AssetError("EVOLUTION_STATE", f"{status.value} is not a measurement status")
        candidate = self.get(candidate_id)
        candidate.status = status
        candidate.decision = dict(decision)
        run_id = str(decision.get("runId") or "")
        if run_id and run_id not in candidate.evidence_refs:
            candidate.evidence_refs.append(run_id)
        self._persist(candidate)
        return candidate

    def watch(self, candidate_id: str, failure_keys: list[str]) -> Candidate:
        candidate = self.get(candidate_id)
        if candidate.status not in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
            raise AssetError("EVOLUTION_STATE", "only an applied candidate can be watched")
        candidate.watch_keys = sorted({key for key in failure_keys if key})
        self._persist(candidate)
        return candidate

    def observe_failure(self, failure_key: str) -> list[str]:
        """Roll back every applied candidate watching this failure key."""
        rolled_back: list[str] = []
        for candidate in list(self._candidates.values()):
            if candidate.status not in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
                continue
            if failure_key not in candidate.watch_keys:
                continue
            try:
                self.rollback(candidate.id)
            except AssetError:
                continue
            candidate.refusal = f"regression: {failure_key}"
            rolled_back.append(candidate.id)
            if self.journal is not None:
                self.journal.append("regression", candidate.id, {"failureKey": failure_key})
        return rolled_back

    def sweep_expired(self) -> list[str]:
        now = self._now()
        expired: list[str] = []
        for candidate in list(self._candidates.values()):
            if candidate.status not in (CandidateStatus.APPLIED, CandidateStatus.MEASURED_OK):
                continue
            if not candidate.expires_at:
                continue
            try:
                due = datetime.fromisoformat(candidate.expires_at)
            except ValueError:
                continue
            if due > now:
                continue
            try:
                self.rollback(candidate.id)
            except AssetError:
                continue
            candidate.refusal = "ttl_expired"
            expired.append(candidate.id)
            if self.journal is not None:
                self.journal.append("ttl", candidate.id, {"expiresAt": candidate.expires_at})
        return expired

    # -- inspection -----------------------------------------------------------

    def get(self, candidate_id: str) -> Candidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise AssetError("EVOLUTION_CANDIDATE_NOT_FOUND", f"unknown candidate {candidate_id!r}")
        return candidate

    def list(self, *, status: Optional[CandidateStatus] = None) -> list[Candidate]:
        values = list(self._candidates.values())
        if status is not None:
            values = [candidate for candidate in values if candidate.status is status]
        return values

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for candidate in self._candidates.values():
            counts[candidate.status.value] = counts.get(candidate.status.value, 0) + 1
        return {
            "adapters": self.adapters.kinds(),
            "candidates": counts,
            "applied": len(self._undos),
            "journalPending": sorted(self.journal.pending()) if self.journal is not None else [],
            "recoveredIntents": list(self.recovered_intents),
        }
