"""The RSL conductor: one bounded asyncio task beside `learning_review`.

It scans inert/proposed `EvolutionCandidate` rows, claims one atomically, runs
the approved verifier, records the experiment, and stops. It adds no worker
kind, queue, scheduler or second runtime. `HARNESS_RSL_MODE=off` (the default)
is a strict no-op.

R4 verification is honest:
* code candidates run their owning tests in a worktree, with the benchmark
  recorded as UNMEASURED;
* surface candidates use `verifier.verify_surface` (the only measured path).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import inspect
import json
import time
from typing import Any, Optional

from ..code.evolution_store import EvolutionStore
from ..evaluation.regression import detect_regressions
from ..evaluation.significance import paired_bootstrap
from ..evaluation.store import EvaluationStore
from ..persistence import now_iso
from ..skills.permissions import Actor
from . import hillclimb, proposer, proposer_model, recorder, verifier
from .archive import ArchiveConfig, StateArchive
from .autonomy import (
    AutonomyKillSwitch,
    autonomy_status,
    classify_ops_risk,
    classify_snapshot_risk,
    decide as autonomy_decide,
)
from .config import RSLConfig
from .improvement_memory import (
    ImprovementMemory,
    MemoryConfig,
    record_hillclimb_attempts,
)
from .models import HarnessSnapshot
from .runtime_settings import RuntimeSettingsStore
from .state import HarnessStateStore


def _utc_day_start() -> str:
    today = _dt.datetime.now(_dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return today.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _variant_signature(candidate: dict[str, Any], proposal: Any) -> str:
    """A stable child identity: parent + typed variant + exact ops."""
    payload = json.dumps(
        {
            "parent": candidate.get("signature"),
            "kind": getattr(proposal, "kind", ""),
            "targetPath": getattr(proposal, "targetPath", ""),
            "ops": list(getattr(proposal, "ops", ()) or ()),
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class RSLLoop:
    """The bounded conductor. Construct with a service, or inject test doubles."""

    def __init__(
        self,
        service: Any = None,
        *,
        config: Optional[RSLConfig] = None,
        run_store: Any = None,
        evolution: Optional[EvolutionStore] = None,
        eval_store: Optional[EvaluationStore] = None,
        verify_fn: Any = None,
        propose_fn: Any = None,
        model_propose_fn: Any = None,
        code_benchmark_fn: Any = None,
        recorder_fn: Any = None,
        state_store: Optional[HarnessStateStore] = None,
        climb_runner: Any = None,
    ) -> None:
        self.service = service
        self.run_store = run_store or getattr(service, "store", None)
        # An explicit config (tests, smoke) is authoritative. Otherwise start
        # from env and overlay the last operator-applied runtime override.
        self.config = config or RSLConfig.from_env()
        self._config_source = "explicit" if config is not None else "env"
        if config is None and self.run_store is not None:
            try:
                self.config = RuntimeSettingsStore(
                    self.run_store
                ).load_rsl_config(self.config)
                self._config_source = "env+persisted"
            except Exception:  # noqa: BLE001 - a settings failure never blocks boot
                pass
        self._config_lock = asyncio.Lock()
        self.evolution = evolution or (
            service.evolution_store() if service is not None
            else EvolutionStore(self.run_store)
        )
        self.eval_store = eval_store or (
            EvaluationStore(self.run_store) if self.run_store is not None else None
        )
        self.state_store = state_store or (
            HarnessStateStore(self.run_store) if self.run_store is not None else None
        )
        self.autonomy_kill = (
            AutonomyKillSwitch(self.run_store)
            if self.run_store is not None else None
        )
        self.verify_fn = verify_fn
        self.propose_fn = propose_fn
        self.model_propose_fn = model_propose_fn
        self.code_benchmark_fn = code_benchmark_fn
        self.recorder_fn = recorder_fn or recorder.record_code_experiment
        self.climb_runner = climb_runner
        self._climb_paused = False
        self._next_climb_at = 0.0
        self._last_climb: Optional[dict[str, Any]] = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._wake = asyncio.Event()
        self._busy = False
        # Per-scope experiment leases. One experiment per scope may run at a
        # time; a tick still owns the global `_busy` flag.
        self._busy_scopes: set[str] = set()
        self._stopping = False
        # Self-Improvement Memory. Loaded lazily, only when the operator opts in;
        # a read or write failure degrades to "no memory" and never blocks a run.
        self._memory: Optional[ImprovementMemory] = None
        self._memory_loaded = False
        self._memory_error = ""

    # -- lifecycle ---------------------------------------------------------
    def _actor(self) -> Actor:
        return Actor(id="rsl", operator=False, autonomy="edit_verify")

    # -- Self-Improvement Memory ------------------------------------------
    def memory_config(self) -> MemoryConfig:
        return MemoryConfig(
            enabled=bool(self.config.memory),
            path=str(self.config.memory_path or ""),
            max_chars=int(self.config.memory_chars or 6000),
        )

    def improvement_memory(self) -> Optional[ImprovementMemory]:
        """The ledger, or `None`. A corrupt file is reported, never repaired."""
        if not self.config.memory:
            return None
        if not self._memory_loaded:
            self._memory_loaded = True
            try:
                self._memory = ImprovementMemory.load(
                    self.memory_config().resolved_path,
                )
                self._memory_error = ""
            except Exception as exc:  # noqa: BLE001 - memory is never a blocker
                self._memory = None
                self._memory_error = f"{type(exc).__name__}: {exc}"[:300]
        return self._memory

    def memory_block(self) -> str:
        """The bounded prompt block handed to the proposer ("" when off)."""
        memory = self.improvement_memory()
        if memory is None:
            return ""
        return memory.render(max_chars=self.memory_config().max_chars)

    def memory_status(self) -> dict[str, Any]:
        if not self.config.memory:
            return {"enabled": False}
        memory = self.improvement_memory()
        return {
            "enabled": True,
            "path": self.memory_config().resolved_path,
            "error": self._memory_error or None,
            "stats": memory.stats() if memory is not None else None,
        }

    def _save_memory(self) -> str:
        memory = self.improvement_memory()
        if memory is None:
            return ""
        try:
            path = memory.save(self.memory_config().resolved_path)
            self._memory_error = ""
            return path
        except Exception as exc:  # noqa: BLE001 - a failed write is reported
            self._memory_error = f"{type(exc).__name__}: {exc}"[:300]
            return ""

    def _record_memory_attempts(
        self, result: dict[str, Any], scope: str,
    ) -> dict[str, Any]:
        """Append one attempt per evaluated neighbor, accepted or not.

        The mapping lives in `improvement_memory` so the conductor stays a
        conductor. A read/write failure is reported, never raised.
        """
        memory = self.improvement_memory()
        if memory is None:
            return {
                "enabled": True, "recorded": 0,
                "error": self._memory_error or None,
            }
        outcome = record_hillclimb_attempts(memory, result, scope=scope)
        path = self._save_memory() if outcome["recorded"] else ""
        return {
            "enabled": True,
            **outcome,
            "path": path or None,
            "error": self._memory_error or None,
        }

    async def start(self) -> None:
        if self.config.is_off:
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="rsl-loop")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _run(self) -> None:
        try:
            await self.resume()
        except Exception:  # noqa: BLE001 - resume is best-effort
            pass
        self._next_climb_at = (
            time.monotonic() + self.config.climb_seconds
            if self._climb_enabled() else 0.0
        )
        try:
            # Process anything already proposed before the first periodic tick.
            await self.tick()
        except Exception:  # noqa: BLE001 - one boot tick never kills the loop
            pass
        while not self._stopping:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.config.tick_seconds)
            except asyncio.TimeoutError:
                pass
            if self._stopping:
                return
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one tick never kills the loop
                pass

    def _climb_enabled(self) -> bool:
        return bool(
            self.config.climb_seconds > 0
            and self.climb_runner is not None
            and self.config.mode in ("verify", "auto")
            and self.config.strategy == "hillclimb"
        )

    async def _maybe_climb(self) -> None:
        if not self._climb_enabled() or self._climb_paused:
            return
        if time.monotonic() < self._next_climb_at:
            return
        if self._busy or not self.budget_ok():
            self._next_climb_at = time.monotonic() + self.config.climb_seconds
            return
        self._next_climb_at = time.monotonic() + self.config.climb_seconds
        try:
            result = self.climb_runner(self, self.config.climb_scope)
            if inspect.isawaitable(result):
                result = await result
            self._last_climb = result if isinstance(result, dict) else {"result": result}
        except Exception as exc:  # noqa: BLE001 - one climb failure is recorded
            self._last_climb = {
                "error": f"{type(exc).__name__}: {exc}",
                "at": now_iso(),
            }

    def pause_climb(self) -> None:
        self._climb_paused = True

    def resume_climb(self) -> None:
        self._climb_paused = False
        if self._climb_enabled():
            self._next_climb_at = time.monotonic() + self.config.climb_seconds

    # -- supervised autonomy ----------------------------------------------
    def autonomy_status(self) -> dict[str, Any]:
        kill = (
            self.autonomy_kill.status() if self.autonomy_kill is not None
            else {"paused": False, "at": None, "reason": ""}
        )
        return autonomy_status(self.config, kill)

    def pause_autonomy(self, reason: str = "") -> dict[str, Any]:
        if self.autonomy_kill is None:
            return {"paused": False, "at": None, "reason": "no_store"}
        return self.autonomy_kill.pause(reason)

    def resume_autonomy(self) -> dict[str, Any]:
        if self.autonomy_kill is None:
            return {"paused": False, "at": None, "reason": "no_store"}
        return self.autonomy_kill.resume()

    def decide_autonomy(self, *, risk: str, **evidence: Any) -> dict[str, Any]:
        """Pure admission decision; it never activates anything."""
        kill = (
            self.autonomy_kill.status()["paused"]
            if self.autonomy_kill is not None else False
        )
        return autonomy_decide(
            level=self.config.autonomy,
            risk=risk,
            kill=kill,
            config=self.config,
            **evidence,
        )

    def classify_surface_risk(self, base: Any, candidate: Any) -> dict[str, Any]:
        return classify_snapshot_risk(base, candidate)

    @staticmethod
    def classify_operator_risk(operator: str) -> str:
        return classify_ops_risk(operator)

    def _run_task_rates(self, run_id: Optional[str]) -> dict[str, float]:
        if not run_id or self.eval_store is None:
            return {}
        try:
            trials = self.eval_store.list_trials(run_id)
        except Exception:  # noqa: BLE001 - a missing run is no evidence
            return {}
        by_task: dict[str, list[float]] = {}
        for trial in trials:
            if trial.get("unmeasured"):
                continue
            task_id = str(trial.get("taskId") or "")
            if not task_id:
                continue
            by_task.setdefault(task_id, []).append(
                1.0 if trial.get("passed") else 0.0
            )
        return {
            task_id: sum(values) / len(values)
            for task_id, values in by_task.items()
        }

    def _run_aggregates(self, run_id: Optional[str]) -> dict[str, Any]:
        rates = self._run_task_rates(run_id)
        if not rates:
            return {}
        return {"task_success": sum(rates.values()) / len(rates)}

    async def auto_apply_surface(self, state_id: str) -> dict[str, Any]:
        """Apply ONE measured surface state when the supervised policy allows.

        This is the only automatic activation path. It refuses anything that is
        not a surface candidate, requires paired task evidence from the recorded
        baseline/candidate runs, classifies the change risk, obeys the kill
        switch and the daily budget, and records the activation counter. The
        existing activation row is the rollback handle.
        """
        if self.state_store is None or self.eval_store is None:
            return {"applied": False, "reason": "store_unavailable"}
        state = self.state_store.get_state(state_id)
        if state is None:
            return {"applied": False, "reason": "unknown_state", "stateId": state_id}
        if state.get("status") != "candidate":
            return {
                "applied": False, "reason": "not_a_candidate",
                "status": state.get("status"), "stateId": state_id,
            }
        candidate = self.evolution.get_candidate(
            str(state.get("candidateId") or "")
        )
        if candidate is None or candidate.get("subjectType") != "surface":
            return {"applied": False, "reason": "not_a_surface_candidate"}
        surface = candidate.get("surfaceConfig") or {}
        try:
            base = HarnessSnapshot(**dict(surface.get("base") or {}))
            snapshot = HarnessSnapshot(**dict(surface.get("snapshot") or {}))
        except Exception as exc:  # noqa: BLE001 - an invalid snapshot is a refusal
            return {
                "applied": False, "reason": "invalid_snapshot",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        risk = classify_snapshot_risk(base, snapshot)
        baseline_run_id = str(surface.get("baselineRunId") or "")
        candidate_run_id = str(state.get("acceptedRunId") or "")
        base_rates = self._run_task_rates(baseline_run_id)
        candidate_rates = self._run_task_rates(candidate_run_id)
        paired = paired_bootstrap(base_rates, candidate_rates)
        base_aggregates = self._run_aggregates(baseline_run_id)
        candidate_aggregates = self._run_aggregates(candidate_run_id)
        regressions = (
            detect_regressions(
                {"aggregates": base_aggregates},
                {"aggregates": candidate_aggregates},
            ).get("regressions") or []
            if base_aggregates and candidate_aggregates else []
        )
        at_today = (
            self.autonomy_kill.activations_today()
            if self.autonomy_kill is not None else 0
        )
        decision = self.decide_autonomy(
            risk=risk["riskClass"],
            delta_ci_low=paired.get("ciLow"),
            critical_regression=bool(regressions),
            canary_ok=True,
            replay_ok=True,
            measured_delta=paired.get("delta"),
            activations_today=at_today,
            changed=risk["changed"],
        )
        result = {
            "stateId": state_id,
            "risk": risk,
            "paired": paired,
            "decision": decision,
        }
        if not decision["allowed"]:
            return {"applied": False, "reason": "policy_refused", **result}

        activation = self.state_store.activate_surface(
            state_id,
            snapshot=snapshot.to_dict(),
            base=base.to_dict(),
        )
        if self.autonomy_kill is not None:
            self.autonomy_kill.record_activation()
        return {
            "applied": True,
            "activation": activation,
            "state": self.state_store.get_state(state_id),
            **result,
        }

    def wake(self) -> None:
        self._wake.set()

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "strategy": self.config.strategy,
            "running": bool(self._task is not None and not self._task.done()),
            "busy": self._busy or bool(self._busy_scopes),
            "busyScopes": sorted(self._busy_scopes),
            "maxCandidates": self.config.max_candidates,
            "maxExperimentsPerDay": self.config.max_experiments_per_day,
            "k": self.config.k,
            "maxParallel": self.config.max_parallel,
            "generations": self.config.generations,
            "objective": self.config.objective,
            "modelProposer": self.config.model_proposer,
            "codeBenchmark": self.config.code_benchmark,
            "autonomy": self.config.autonomy,
            "memory": self.config.memory,
        }

    def status(self) -> dict[str, Any]:
        """The operator's bounded view of mode, lifecycle and daily budget."""
        started = self.evolution.experiments_started_since(_utc_day_start())
        maximum = max(0, int(self.config.max_experiments_per_day))
        watch = getattr(self.service, "_drift_watch", None)
        session = getattr(self.service, "_rsl_session_watch", None)
        return {
            **self.health(),
            "startedToday": started,
            "remainingToday": max(0, maximum - started),
            "budgetExhausted": started >= maximum,
            "tickSeconds": self.config.tick_seconds,
            "climbSeconds": self.config.climb_seconds,
            "climbScope": self.config.climb_scope,
            "climbPaused": self._climb_paused,
            "climbEnabled": self._climb_enabled(),
            "nextClimbInSeconds": (
                max(0, round(self._next_climb_at - time.monotonic()))
                if self._climb_enabled() and not self._climb_paused else None
            ),
            "lastClimb": self._last_climb,
            "leaseSeconds": self.config.lease_seconds,
            "trials": self.config.trials,
            "maxCostUsd": self.config.max_cost_usd,
            "configSource": self._config_source,
            "driftWatch": watch.health() if watch is not None else {
                "enabled": False, "intervalSeconds": 0, "running": False, "last": {},
            },
            "sessionWatch": (
                session.health() if session is not None else {
                    "enabled": False, "intervalSeconds": 0, "running": False, "last": {},
                }
            ),
            "surfaceLab": self.config.surface_lab,
            "maxSurfaceExperimentsPerDay": self.config.max_surface_experiments_per_day,
            "autonomyStatus": self.autonomy_status(),
            "memory": self.memory_status(),
        }

    async def set_config(
        self, payload: dict[str, Any], *, persist: bool = True,
    ) -> dict[str, Any]:
        """Apply bounded operator overrides without a restart.

        Only `RSLConfig` fields are accepted; every value goes through the same
        clamps as `from_env`. Enabling a mode that was off starts the conductor;
        switching to off stops it. A persisted override survives a restart.
        """
        async with self._config_lock:
            previous = self.config
            updated = previous.apply_payload(payload)
            if persist and self.run_store is not None:
                RuntimeSettingsStore(self.run_store).save_rsl_config(updated)
            self.config = updated
            self._config_source = "env+persisted" if persist else "explicit"
            if (
                updated.memory != previous.memory
                or updated.memory_path != previous.memory_path
                or updated.memory_chars != previous.memory_chars
            ):
                # A settings change is a new ledger view, not a merge.
                self._memory = None
                self._memory_loaded = False
                self._memory_error = ""

            if updated.is_off:
                if self._task is not None and not self._task.done():
                    await self.stop()
            elif previous.is_off:
                await self.start()
            else:
                self.wake()
        return self.status()

    async def run_recursive(
        self,
        candidate: dict[str, Any],
        *,
        verify_fn: Any,
        variants: Any = None,
        seed: int = 0,
        measure: Any = None,
        trajectories: Any = None,
        evidence_dir: Any = None,
        record_attempts: bool = True,
    ) -> dict[str, Any]:
        """Run ONE bounded k-candidate experiment through the R7 conductor.

        The existing loop still owns single-candidate ticks. This entry point
        uses the same configured `k`, actor, stores and daily budget, records
        the selection/composition as an `EvaluationRun`, and never activates.
        """
        if self.config.mode not in ("verify", "auto"):
            return {
                "refused": True,
                "reason": "mode",
                "mode": self.config.mode,
                "k": 0,
                "selection": None,
                "composition": None,
            }
        scope = str(candidate.get("scope") or "global")
        if self._busy or scope in self._busy_scopes:
            return {
                "refused": True,
                "reason": "busy",
                "scope": scope,
                "k": 0,
                "selection": None,
                "composition": None,
            }
        from .recursion import run_recursive  # noqa: PLC0415 - one-way dependency

        self._busy_scopes.add(scope)
        try:
            return await run_recursive(
                candidate,
                verify_fn=verify_fn,
                k=self.config.k,
                max_parallel=self.config.max_parallel,
                variants=variants,
                seed=seed,
                actor=self._actor(),
                measure=measure,
                trajectories=trajectories,
                novelty_min=self.config.novelty_min,
                evidence_dir=evidence_dir,
                started_today=self.evolution.experiments_started_since(_utc_day_start()),
                max_per_day=self.config.max_experiments_per_day,
                eval_store=self.eval_store,
                run_store=self.run_store,
                record_attempts=record_attempts,
            )
        finally:
            self._busy_scopes.discard(scope)

    async def run_hillclimb(
        self,
        current: Optional[dict[str, Any]] = None,
        *,
        propose: Any,
        evaluate: Any,
        replay_baseline: Optional[dict[str, float]] = None,
        critical_tasks: tuple[str, ...] = (),
        scope: str = "global",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Run ONE bounded hill-climb experiment through the conductor.

        The caller supplies neighbor generation and evaluation; this method owns
        the shared busy flag, the state store and the persistence hook. Accepted
        states are saved as `candidate` rows and are never promoted here. An
        operator promotion remains the only way to make a state current.
        """
        if self.config.mode not in ("verify", "auto"):
            return {
                "refused": True, "reason": "mode", "mode": self.config.mode,
                "accepted": [], "current": None,
            }
        if self.config.strategy != "hillclimb":
            return {
                "refused": True, "reason": "strategy",
                "strategy": self.config.strategy, "accepted": [], "current": None,
            }
        scope = str(scope or "global")
        if self._busy or scope in self._busy_scopes:
            return {
                "refused": True, "reason": "busy", "scope": scope,
                "accepted": [], "current": None,
            }
        if self.state_store is None:
            return {
                "refused": True, "reason": "state_store_unavailable",
                "accepted": [], "current": None,
            }
        if not self.budget_ok():
            return {
                "refused": True, "reason": "daily_budget",
                "accepted": [], "current": None,
            }
        state = current or self.state_store.current(scope)
        if state is None:
            return {
                "refused": True, "reason": "no_current_state", "scope": scope,
                "accepted": [], "current": None,
            }

        started = self.evolution.experiments_started_since(_utc_day_start())
        remaining = max(
            0, int(self.config.max_experiments_per_day) - int(started),
        )
        settings = hillclimb.HillClimbConfig(
            generations=self.config.generations,
            objective=self.config.objective,
            k=self.config.k,
            max_parallel=self.config.max_parallel,
            require_replay=True,
            max_evaluations=remaining,
            archive_mode="off" if not self.config.archive else self.config.archive_sample,
            archive_max=self.config.archive_max,
            require_significance=self.config.significance,
            significance_min_tasks=self.config.significance_min_tasks,
        )
        archive = (
            StateArchive(
                self.state_store,
                ArchiveConfig(
                    max_states=self.config.archive_max,
                    sample=self.config.archive_sample,
                    cmp=self.config.cmp,
                    cmp_horizon=self.config.cmp_horizon,
                    islands=self.config.islands,
                    island_migration_every=self.config.island_migration_every,
                ),
            )
            if self.config.archive
            else None
        )
        self._busy_scopes.add(scope)
        try:
            result = await hillclimb.run_hillclimb(
                state,
                propose=propose,
                evaluate=evaluate,
                replay_baseline=replay_baseline,
                critical_tasks=critical_tasks,
                config=settings,
                scope=scope,
                on_accept=self.state_store.save_state if persist else None,
                archive=archive,
            )
            if persist:
                self._record_hillclimb_attempts(result, state, scope)
            if persist and self.config.memory:
                result["memory"] = self._record_memory_attempts(result, scope)
            result["refused"] = False
            result["strategy"] = "hillclimb"
            result["persisted"] = bool(persist)
            return result
        finally:
            self._busy_scopes.discard(scope)

    def _record_hillclimb_attempts(
        self,
        result: dict[str, Any],
        parent: dict[str, Any],
        scope: str,
    ) -> None:
        """Count each evaluated neighbor against the daily experiment budget."""
        for generation in result.get("generations") or []:
            for record in generation.get("records") or []:
                validation = record.get("validation")
                try:
                    self.evolution.save_attempt({
                        "candidateId": str(
                            record.get("candidateId") or record.get("id") or ""
                        ),
                        "targetPath": str(
                            record.get("targetPath") or f"state:{scope}"
                        ),
                        "baseDigest": str(parent.get("snapshotDigest") or ""),
                        "diffDigest": str(record.get("digest") or ""),
                        "proposedOps": list(record.get("ops") or ()),
                        "evaluation": (
                            validation if isinstance(validation, dict) else {}
                        ),
                        "outcome": (
                            "success" if record.get("decision") == "accepted"
                            else "failure"
                        ),
                        "outcomeReason": (
                            "; ".join(record.get("reasons") or ())
                            or str(record.get("decision") or "")
                        ),
                        "finishedAt": now_iso(),
                    })
                except Exception:  # noqa: BLE001 - accounting is best-effort
                    continue

    # -- recovery / budget -------------------------------------------------
    def resume(self) -> list[str]:
        reclaimed = self.evolution.reclaim_stale(self.config.lease_seconds)
        for candidate_id in reclaimed:
            candidate = self.evolution.get_candidate(candidate_id) or {}
            self.evolution.save_attempt({
                "candidateId": candidate_id,
                "targetPath": candidate.get("targetPath") or "",
                "baseDigest": "",
                "diffDigest": "lease-expired",
                "proposedOps": candidate.get("proposedOps") or [],
                "outcome": "failure",
                "outcomeReason": "lease expired",
                "finishedAt": now_iso(),
            })
        return reclaimed

    def budget_ok(self) -> bool:
        started = self.evolution.experiments_started_since(_utc_day_start())
        return started < max(0, int(self.config.max_experiments_per_day))

    def scan(self, *, status: str = "inert") -> list[dict[str, Any]]:
        """The bounded candidate window for one tick."""
        return self.evolution.list_candidates(
            status=status, limit=self.config.max_candidates,
        )

    # -- ticks -------------------------------------------------------------
    def surface_budget_ok(self) -> bool:
        """Whether one more measured SURFACE session may run today."""
        if not self.config.surface_lab:
            return False
        maximum = max(0, int(self.config.max_surface_experiments_per_day))
        if maximum <= 0:
            return False
        used = 0
        try:
            for candidate in self.evolution.list_candidates(limit=500):
                if str(candidate.get("sourceCategory") or "") == "surface-lab":
                    created = str(candidate.get("createdAt") or "")
                    if created >= _utc_day_start():
                        used += 1
        except Exception:  # noqa: BLE001 - an unreadable ledger is "no session"
            return False
        return used < maximum

    async def run_surface_session(self) -> dict[str, Any]:
        """Run ONE measured surface session and stage its winner (inert).

        This is the loop improving its OWN harness: the variants come from the
        lessons it already adopted, each is measured on the corpus, and the
        winner is staged as an inert candidate for the operator. The session
        never lands, never activates and never queues.
        """
        from .lab_runner import run_lab_session  # noqa: PLC0415

        memory_store = None
        memory_service = getattr(self.service, "memory_service", None)
        if memory_service is not None:
            memory_store = getattr(memory_service, "store", None)
        return await run_lab_session(
            self.run_store or self.service.store,
            memory_store=memory_store,
            trials=self.config.trials,
        )

    async def _surface_lab_tick(self) -> dict[str, Any]:
        """One bounded surface session, when the operator enabled it."""
        if not self.surface_budget_ok():
            return {"mode": "surface", "skipped": "budget"}
        try:
            lab = await self.run_surface_session()
        except Exception as exc:  # noqa: BLE001 - a failed session never breaks the loop
            return {"mode": "surface", "skipped": f"error:{type(exc).__name__}"}
        return {
            "mode": "surface",
            "winner": lab.get("winner"),
            "staged": bool((lab.get("staged") or {}).get("staged")),
        }

    async def tick(self) -> dict[str, Any]:
        if self.config.is_off:
            return {"mode": "off", "processed": []}
        if self.config.mode == "observe":
            # R1's deterministic observer owns observation; the conductor does
            # not run a candidate in observe mode.
            return {"mode": "observe", "processed": []}
        if self._busy:
            return {"mode": self.config.mode, "skipped": "busy", "processed": []}
        self._busy = True
        try:
            if self.config.mode == "propose":
                return await self._propose_tick()
            if self.config.mode == "auto":
                proposed = await self._propose_tick()
                surface = await self._surface_lab_tick()
                verified = await self._verify_tick()
                return {
                    "mode": "auto",
                    "proposed": proposed.get("proposed", []),
                    "refused": proposed.get("refused", []),
                    "surface": surface,
                    "verified": verified,
                    "processed": (
                        list(proposed.get("processed", []))
                        + list(verified.get("processed", []))
                    ),
                }
            return await self._verify_tick()
        finally:
            self._busy = False

    async def _propose_tick(self) -> dict[str, Any]:
        proposed: list[str] = []
        refused: list[dict[str, Any]] = []
        candidates = self.scan(status="inert")
        for candidate in candidates:
            has_ops = bool(candidate.get("targetPath") and candidate.get("proposedOps"))
            if not has_ops and not self.config.model_proposer:
                continue
            try:
                if has_ops:
                    if self.propose_fn is not None:
                        self.propose_fn(candidate)
                    else:
                        proposer.propose_from_ops(
                            self.run_store, str(candidate["id"]), actor=self._actor(),
                        )
                    proposed.append(str(candidate["id"]))
                else:
                    proposed.extend(
                        await self._propose_with_model(candidate, k=self.config.k)
                    )
            except proposer.ProposalRefused as exc:
                refused.append({"candidateId": candidate["id"], **exc.to_dict()})
        return {"mode": "propose", "proposed": proposed, "refused": refused,
                "processed": proposed}

    async def propose_with_model(
        self, candidate: dict[str, Any], *, k: Optional[int] = None,
    ) -> list[str]:
        """Operator-triggered model proposal for ONE candidate.

        The autonomous propose tick uses the same path. The flag stays the
        opt-in; an explicit call still refuses when the model proposer is off.
        Returns the persisted child candidate ids (never verifies or lands).
        """
        if not self.config.model_proposer:
            raise proposer.ProposalRefused(
                "MODEL_PROPOSER_DISABLED",
                "set HARNESS_RSL_MODEL_PROPOSER=on to use the model proposer",
            )
        return await self._propose_with_model(
            candidate, k=max(1, int(k if k is not None else self.config.k)),
        )

    async def _propose_with_model(
        self, candidate: dict[str, Any], *, k: int,
    ) -> list[str]:
        """Ask the model for variants and persist each validated child candidate."""
        if self.model_propose_fn is not None:
            result = self.model_propose_fn(candidate, k=k, actor=self._actor())
            if inspect.isawaitable(result):
                result = await result
        else:
            result = await proposer_model.propose_variants(
                candidate, k=k, actor=self._actor(), memory=self.memory_block(),
            )
        proposals = list(result or [])
        if not proposals:
            raise proposer.ProposalRefused(
                "MODEL_PROPOSER_EMPTY", "the model returned no valid variants",
            )
        child_ids: list[str] = []
        for proposal in proposals:
            signature = _variant_signature(candidate, proposal)
            child = self.evolution.get_by_signature(signature)
            if child is None:
                child = self.evolution.save_candidate({
                    "signature": signature,
                    "sourceCategory": candidate.get("sourceCategory") or "rsl",
                    "observedCount": int(candidate.get("observedCount") or 1),
                    "problemStatement": candidate.get("problemStatement") or "",
                    "targetPath": proposal.targetPath,
                    "proposedOps": list(proposal.ops),
                    "status": "proposed",
                    "createdByRunId": candidate.get("createdByRunId"),
                })
            child_ids.append(str(child["id"]))
        return child_ids

    async def _verify_tick(self) -> dict[str, Any]:
        if not self.budget_ok():
            return {"mode": "verify", "skipped": "budget", "processed": []}
        candidates = self.scan(status="proposed")
        for candidate in candidates:
            claimed = self.evolution.claim(str(candidate["id"]))
            if claimed is None:
                continue
            return await self._verify_claimed(claimed)
        return {"mode": "verify", "skipped": "none", "processed": []}

    async def _verify_claimed(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(candidate["id"])
        run_id = f"rsl-{candidate_id}-{int(time.time())}"
        ctx = None
        if self.service is not None:
            try:
                ctx = self.service.evolution_context(run_id=run_id)
            except Exception:  # noqa: BLE001 - the verifier will report the reason
                ctx = None
        subject_type = str(candidate.get("subjectType") or "code")
        try:
            if self.verify_fn is not None:
                verification = await asyncio.to_thread(self.verify_fn, candidate)
            elif subject_type == "surface":
                # The ONLY measured path: a validated snapshot runs on the real
                # evaluator against a baseline of the same base snapshot.
                surface_kwargs = {
                    "config": self.config,
                    "eval_store": self.eval_store,
                }
                skill_store = getattr(self.service, "skill_store", None)
                if skill_store is not None:
                    surface_kwargs["skill_store"] = skill_store
                verification = await verifier.verify_surface_candidate(
                    self.run_store, candidate, **surface_kwargs,
                )
            else:
                verification = await asyncio.to_thread(
                    verifier.verify_code, self.run_store, candidate_id,
                    ctx=ctx, actor=self._actor(),
                )
        except Exception as exc:  # noqa: BLE001 - a failed verification is a decision
            verification = {
                "ok": False, "passed": False, "refused": False,
                "benchmark": "UNMEASURED", "decision": "verification_error",
                "detail": str(exc),
            }

        # R9 measured code path: owning tests must pass first, then the patched
        # tree runs the deterministic benchmark. The benchmark can reject a
        # candidate; it never lands one.
        if (
            subject_type != "surface"
            and self.config.code_benchmark
            and verification.get("passed")
            and not verification.get("refused")
        ):
            try:
                if self.code_benchmark_fn is not None:
                    bench = self.code_benchmark_fn(
                        self.run_store, candidate,
                        config=self.config, eval_store=self.eval_store,
                    )
                    if inspect.isawaitable(bench):
                        bench = await bench
                else:
                    bench = await asyncio.to_thread(
                        verifier.verify_code_benchmark,
                        self.run_store, candidate,
                        config=self.config, eval_store=self.eval_store,
                    )
                if bench.get("benchmark") == "MEASURED":
                    verification.update({
                        "benchmark": "MEASURED",
                        "decision": "benchmark",
                        "decisionRecord": bench.get("decisionRecord"),
                        "passed": bool(bench.get("passed")),
                        "testsPassed": True,
                        "benchmarkRunId": bench.get("runId"),
                        "benchmarkMetrics": {
                            "baseline": bench.get("baseline"),
                            "candidate": bench.get("candidate"),
                        },
                    })
                else:
                    verification["benchmarkResult"] = bench
            except Exception as exc:  # noqa: BLE001 - benchmark failure is not a pass
                verification["benchmarkError"] = {
                    "code": "CODE_BENCHMARK_ERROR",
                    "detail": str(exc)[:500],
                }

        if verification.get("refused"):
            self.evolution.update_candidate(
                candidate_id, status="refused",
                refusalCode=str(verification.get("code") or "RSL_REFUSED"),
            )
            decision = "refused"
        else:
            self.evolution.update_candidate(
                candidate_id,
                status="evaluated_ok" if verification.get("passed") else "evaluated_bad",
            )
            if subject_type == "surface" or verification.get("benchmark") == "MEASURED":
                record = verification.get("decisionRecord") or {}
                decision = (
                    "benchmark_accepted" if record.get("accepted")
                    else "benchmark_rejected" if record
                    else str(verification.get("decision") or "verification_error")
                )
            else:
                decision = "tests_passed" if verification.get("passed") else "tests_failed"
        state_id = None
        if self.eval_store is not None and self.run_store is not None:
            if subject_type == "surface":
                try:
                    recorder.record_surface_experiment(
                        self.run_store, self.evolution,
                        candidate=candidate, verification=verification,
                        decision=decision,
                    )
                except Exception:  # noqa: BLE001 - recording is best-effort
                    pass
            else:
                try:
                    self.recorder_fn(
                        self.eval_store, self.run_store,
                        run_id=run_id, candidate=candidate,
                        verification=verification, decision=decision,
                    )
                except Exception:  # noqa: BLE001 - recording is best-effort
                    pass
        if (
            subject_type == "surface"
            and verification.get("benchmark") == "MEASURED"
        ):
            state_id = self._record_surface_state(candidate, verification)
        return {
            "mode": "verify",
            "candidateId": candidate_id,
            "runId": verification.get("runId") or run_id,
            "decision": decision,
            "benchmark": verification.get("benchmark", "UNMEASURED"),
            "stateId": state_id,
            "processed": [candidate_id],
        }

    def _record_surface_state(
        self,
        candidate: dict[str, Any],
        verification: dict[str, Any],
    ) -> Optional[str]:
        """Materialize a measured surface as an inert `candidate` state."""
        if self.state_store is None or self.eval_store is None:
            return None
        surface = dict(candidate.get("surfaceConfig") or {})
        scope = str(surface.get("scope") or "global")
        snapshot_digest = str(
            (surface.get("snapshot") or {}).get("sourceDigest")
            or candidate.get("signature")
            or ""
        )
        run_id = str(verification.get("runId") or "")
        if not snapshot_digest or not run_id:
            return None
        try:
            existing = self.state_store.list_states(scope=scope, limit=200)
            for row in existing:
                if (
                    row.get("candidateId") == candidate.get("id")
                    and row.get("snapshotDigest") == snapshot_digest
                ):
                    return row.get("id")
            aggregates: dict[str, Any] = {}
            for metric in self.eval_store.list_metrics(run_id, scope="overall"):
                if metric.get("dimension") and metric.get("value") is not None:
                    aggregates[str(metric["dimension"])] = metric["value"]
            if not aggregates:
                record = verification.get("decisionRecord") or {}
                if record.get("targetAfter") is not None:
                    aggregates["task_success"] = record["targetAfter"]
            run = self.eval_store.get_run(run_id) or {}
            metadata = run.get("metadata") or {}
            measured = int(
                metadata.get("measuredTrials")
                or metadata.get("trialCount")
                or (verification.get("decisionRecord") or {}).get("measured")
                or 0
            )
            metrics = {"aggregates": aggregates, "measuredTrials": measured}
            from .utility import utility  # noqa: PLC0415 - keep the module light

            scored = utility(metrics)
            state = self.state_store.save_state({
                "scope": scope,
                "snapshotDigest": snapshot_digest,
                "candidateId": candidate.get("id"),
                "acceptedRunId": run_id,
                "utility": None if scored.get("unmeasured") else scored.get("utility"),
                "metrics": metrics,
                "status": "candidate",
            })
            return str(state.get("id") or "")
        except Exception:  # noqa: BLE001 - state materialization is best-effort
            return None


__all__ = ["RSLLoop", "now_iso"]
