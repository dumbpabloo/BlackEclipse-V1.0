"""RSL R5 — operator evidence routes and their authorization boundary."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from harness.code.evolution_store import EvolutionStore
from harness.eclipse_memory import EclipseMemoryStore
from harness.evaluation.store import EvaluationStore
from harness.persistence import AgentMemoryStore
from harness.schemas import AgentConfig
from harness.self_improvement.lesson_feedback import LessonOutcomeStore
from harness.self_improvement.models import HarnessSnapshot
from harness.self_improvement.state import HarnessStateStore
import harness.agents as agents_module
import harness.routers.evolution as evolution_router
from harness.routers.evolution_models import ModelExperimentRequest
import harness.routers.evolution_drift as drift_router
import harness.routers.evolution_models as model_router


class _Service:
    def __init__(self, store: AgentMemoryStore, skill_store=None) -> None:
        self.store = store
        self.skill_store = skill_store

    def evolution_store(self) -> EvolutionStore:
        return EvolutionStore(self.store)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    memory = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    lessons = EclipseMemoryStore(str(tmp_path / "eclipse-memory.db"))
    service = _Service(memory, lessons)
    monkeypatch.setattr(evolution_router, "service", service)
    monkeypatch.setattr(drift_router, "service", service)
    yield memory
    lessons.close()
    memory.close()


def _candidate(store: AgentMemoryStore) -> dict:
    return EvolutionStore(store).save_candidate({
        "signature": f"sig-{uuid.uuid4().hex}",
        "sourceCategory": "timeout",
        "observedCount": 3,
        "problemStatement": "the bound that timed out",
        "targetPath": "python/harness/foo.py",
        "proposedOps": [],
        "status": "evaluated_ok",
    })


def _experiment(store: AgentMemoryStore, proposal_id: str, run_id: str = "rsl-e1") -> str:
    eval_store = EvaluationStore(store)
    eval_store.create_run(
        id=run_id, suite="rsl-code", datasetName="owning-tests",
        datasetVersion="1", datasetChecksum="abc",
        config={
            "kind": "rsl", "proposalId": proposal_id, "split": "validation",
            "trials": 0, "tier": 0, "graderVersion": 1, "benchmark": "UNMEASURED",
        },
        metadata={
            "kind": "rsl", "proposalId": proposal_id,
            "benchmark": "UNMEASURED", "testsPassed": True,
            "taskFingerprintDigest": "digest",
        },
        status="completed",
    )
    eval_store.add_metric(runId=run_id, dimension="task_success", value=1.0, sampleSize=1)
    eval_store.add_trial(
        runId=run_id, taskId="t.1", trialIndex=0, passed=True,
        score={"cost": None, "graderVersion": 1},
        trajectory={"events": [{"type": "secret-raw-trace"}]},
    )
    return run_id


def test_list_and_get_experiments(store):
    candidate = _candidate(store)
    run_id = _experiment(store, candidate["id"])

    listed = evolution_router.list_experiments()
    assert listed["success"] is True
    assert [item["id"] for item in listed["experiments"]] == [run_id]
    assert listed["experiments"][0]["metadata"]["benchmark"] == "UNMEASURED"

    detail = evolution_router.get_experiment(run_id)
    assert detail["experiment"]["id"] == run_id
    assert detail["metrics"][0]["dimension"] == "task_success"
    assert detail["trials"][0]["score"]["cost"] is None
    assert "trajectory" not in detail["trials"][0]


def test_candidate_evidence_links_attempts_and_experiments(store):
    candidate = _candidate(store)
    evolution = EvolutionStore(store)
    evolution.save_attempt({
        "candidateId": candidate["id"], "targetPath": "python/harness/foo.py",
        "baseDigest": "", "diffDigest": "d", "proposedOps": [],
        "outcome": "success", "outcomeReason": "owning checks passed",
    })
    _experiment(store, candidate["id"])

    evidence = evolution_router.candidate_evidence(candidate["id"])
    assert evidence["candidate"]["id"] == candidate["id"]
    assert evidence["attempts"][0]["outcome"] == "success"
    assert evidence["experiments"][0]["metadata"]["proposalId"] == candidate["id"]


def test_get_missing_or_non_rsl_experiment_is_404(store):
    EvaluationStore(store).create_run(
        id="ordinary", suite="evaluation", status="completed",
        metadata={"kind": "evaluation"},
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        evolution_router.get_experiment("does-not-exist")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as ordinary:
        evolution_router.get_experiment("ordinary")
    assert ordinary.value.status_code == 404


def test_list_and_promote_harness_states(store):
    states = HarnessStateStore(store)
    root = states.save_state({
        "scope": "global", "snapshotDigest": "base",
        "metrics": {"aggregates": {"task_success": 0.7}},
        "utility": 0.7, "status": "current",
    })
    child = states.save_state({
        "scope": "global", "snapshotDigest": "candidate",
        "metrics": {"aggregates": {"task_success": 0.8}},
        "utility": 0.8, "status": "candidate", "parentStateId": root["id"],
    })

    listed = evolution_router.list_harness_states()
    assert listed["success"] is True
    assert listed["current"]["id"] == root["id"]
    assert [row["id"] for row in listed["currents"]] == [root["id"]]
    assert any(row["id"] == child["id"] for row in listed["states"])

    promoted = evolution_router.promote_harness_state(child["id"])
    assert promoted["state"]["status"] == "current"
    assert states.get_state(root["id"])["status"] == "superseded"


def test_pareto_route_returns_a_frontier(store):
    states = HarnessStateStore(store)
    states.save_state({
        "scope": "global", "snapshotDigest": "a", "utility": 0.8,
        "metrics": {"aggregates": {"task_success": 0.8, "cost": 0.5}, "measuredTrials": 1},
        "status": "candidate",
    })
    states.save_state({
        "scope": "global", "snapshotDigest": "b", "utility": 0.6,
        "metrics": {"aggregates": {"task_success": 0.7, "cost": 0.8}, "measuredTrials": 1},
        "status": "candidate",
    })
    result = evolution_router.states_pareto(scope="global", limit=10, _=True)
    assert result["success"] is True
    assert len(result["frontier"]) == 1
    assert len(result["dominated"]) == 1


def test_lesson_outcomes_route_reports_stats_and_rows(store):
    outcomes = LessonOutcomeStore(store)
    outcomes.record_injections(
        "run-1", [{"id": "cand-lesson"}], ["cand-lesson"],
        cue="market data timeout",
    )
    outcomes.record_run_outcome("run-1", "completed")

    result = evolution_router.lesson_outcomes("cand-lesson")
    assert result["success"] is True
    assert result["stats"]["injected"] == 1
    assert result["stats"]["status"] == "insufficient"
    assert result["outcomes"][0]["outcome"] == "success"


def _small_agent(agent_id: str = "eclipse-agent") -> AgentConfig:
    return AgentConfig(
        id=agent_id, name=agent_id, systemPrompt="base prompt",
        tools=["tool_a", "tool_b"], maxSteps=8, maxToolCalls=10,
    )


def _use_surface_base(monkeypatch, *agent_ids: str):
    for agent_id in agent_ids or ("eclipse-agent",):
        monkeypatch.setitem(
            agents_module.AGENT_PRESETS, agent_id, _small_agent(agent_id),
        )


def _surface_candidate(store: AgentMemoryStore, *, model_name=None, task_ids=None) -> dict:
    base = HarnessSnapshot(
        systemPrompt="base prompt", toolNames=("tool_a", "tool_b"),
        maxSteps=8, maxToolCalls=10,
    )
    snapshot = HarnessSnapshot(
        systemPrompt="measured prompt", toolNames=("tool_a",),
        maxSteps=6, maxToolCalls=8, modelName=model_name,
    )
    return EvolutionStore(store).save_candidate({
        "signature": f"surface-{uuid.uuid4().hex}",
        "sourceCategory": "operator",
        "observedCount": 1,
        "problemStatement": "surface test",
        "subjectType": "surface",
        "surfaceConfig": {
            "base": base.to_dict(), "snapshot": snapshot.to_dict(),
            "dataset": "benchmark_v1", "split": "validation",
            "taskIds": list(task_ids or ["core.echo.001"]),
            "targetDimension": "task_success",
            "trials": 1,
        },
        "status": "evaluated_ok",
    })


def _accepted_rsl_run(store: AgentMemoryStore, candidate_id: str, run_id: str) -> str:
    EvaluationStore(store).create_run(
        id=run_id, suite="rsl", datasetName="benchmark_v1",
        datasetVersion="1", datasetChecksum="abc", status="completed",
        config={"kind": "rsl", "benchmark": "MEASURED"},
        metadata={
            "kind": "rsl", "proposalId": candidate_id,
            "benchmark": "MEASURED", "accepted": True, "decision": "accepted",
        },
    )
    return run_id


def test_rsl_memory_route_is_explicit_off_and_reports_the_ledger(store, tmp_path):
    from harness.self_improvement.config import RSLConfig
    from harness.self_improvement.improvement_memory import Provenance
    from harness.self_improvement.loop import RSLLoop
    import harness.routers.evolution_memory as memory_router

    service = evolution_router.service
    memory_router.service = service
    service._rsl_loop = RSLLoop(
        service=None, config=RSLConfig(mode="observe"), run_store=store,
    )
    assert memory_router.rsl_memory(_=True)["enabled"] is False

    path = tmp_path / "memory.json"
    loop = RSLLoop(
        service=None,
        config=RSLConfig(
            mode="observe", memory=True, memory_path=str(path),
        ),
        run_store=store,
    )
    loop.improvement_memory().record_result(
        "Completion accepted without execution evidence",
        edit="added an execution interlock",
        provenance=Provenance(domain="coding", round=6),
        passed=5, total=5, score_before=0.2, score_after=0.5, accepted=True,
    )
    assert loop._save_memory()
    service._rsl_loop = loop

    payload = memory_router.rsl_memory(_=True)
    assert payload["enabled"] is True
    assert payload["stats"]["attempts"] == 1
    assert payload["memory"]["problems"][0]["id"] == "P1"
    assert "P1 Completion accepted" in payload["rendered"]


def test_model_experiment_queues_a_trial_only_identity_change(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent")

    class _Loop:
        config = type("C", (), {"mode": "verify", "trials": 1})()

        def budget_ok(self):
            return True

        def wake(self):
            return None

    service = evolution_router.service
    service._rsl_loop = _Loop()

    result = model_router.create_model_experiment(
        ModelExperimentRequest(
            scope="eclipse-agent", provider="ollama", model="qwen3:8b",
            temperature=0.2,
        ),
        _=True,
    )
    assert result["created"] is True
    snapshot = result["candidate"]["surfaceConfig"]["snapshot"]
    assert snapshot["modelProvider"] == "ollama"
    assert snapshot["modelName"] == "qwen3:8b"
    assert snapshot["temperature"] == 0.2
    assert result["candidate"]["subjectType"] == "surface"


def test_surface_state_activation_and_rollback(store, monkeypatch):
    _use_surface_base(monkeypatch)
    candidate = _surface_candidate(store)
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-surface-ok")
    states = HarnessStateStore(store)
    root = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "base",
        "metrics": {"aggregates": {"task_success": 0.7}},
        "utility": 0.7, "status": "current",
    })
    state = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {"aggregates": {"task_success": 0.8}},
        "utility": 0.8, "status": "candidate", "parentStateId": root["id"],
    })

    activated = evolution_router.activate_surface_state(state["id"])
    assert activated["activation"]["scope"] == "eclipse-agent"
    assert states.current("eclipse-agent")["id"] == state["id"]

    rolled = evolution_router.rollback_surface_activation(
        activated["activation"]["id"],
    )
    assert rolled["activation"]["rolledBackAt"]
    assert states.current("eclipse-agent")["id"] == root["id"]


def test_surface_preview_and_scope_override(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent", "code-agent")
    candidate = _surface_candidate(store)
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-surface-preview")
    eval_store = EvaluationStore(store)
    eval_store.add_metric(
        runId=run_id, scope="overall", dimension="task_success",
        value=0.9, sampleSize=2,
    )
    eval_store.add_trial(
        runId=run_id, taskId="core.echo.001", trialIndex=0, passed=True,
        score={"task_success": 1.0}, status="passed",
    )
    eval_store.create_run(
        id="rsl-baseline-preview", suite="surface-baseline",
        datasetName="benchmark_v1", datasetVersion="1", datasetChecksum="abc",
        config={"kind": "rsl-baseline"}, metadata={"kind": "rsl-baseline"},
        status="completed",
    )
    eval_store.add_metric(
        runId="rsl-baseline-preview", scope="overall", dimension="task_success",
        value=0.7, sampleSize=2,
    )
    eval_store.add_trial(
        runId="rsl-baseline-preview", taskId="core.echo.001", trialIndex=0,
        passed=False, score={"task_success": 0.0}, status="failed",
    )
    surface_config = dict(candidate["surfaceConfig"])
    surface_config["baselineRunId"] = "rsl-baseline-preview"
    EvolutionStore(store).update_candidate(
        candidate["id"], surfaceConfig=surface_config,
    )
    states = HarnessStateStore(store)
    state = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {"aggregates": {"task_success": 0.8}},
        "utility": 0.8, "status": "candidate",
    })

    preview = evolution_router.preview_surface_state(state["id"], scope="eclipse-agent")
    assert preview["canActivate"] is True
    assert preview["baseDigestMatches"] is True
    assert preview["diff"]["toolsRemoved"] == ["tool_b"]
    assert preview["diff"]["maxStepsAfter"] == 6
    assert "eclipse-agent" in preview["availableScopes"]
    assert preview["evidence"]["run"]["id"] == run_id
    assert preview["evidence"]["metrics"][0]["dimension"] == "task_success"
    assert preview["evidence"]["trials"][0]["taskId"] == "core.echo.001"
    assert "trajectory" not in preview["evidence"]["trials"][0]
    assert preview["evidence"]["target"]["dimension"] == "task_success"
    assert preview["evidence"]["target"]["before"] == 0.7
    assert preview["evidence"]["target"]["after"] == 0.9
    assert round(preview["evidence"]["target"]["delta"], 3) == 0.2
    task_delta = preview["evidence"]["taskDeltas"][0]
    assert task_delta["taskId"] == "core.echo.001"
    assert task_delta["before"] == 0.0
    assert task_delta["after"] == 1.0
    assert task_delta["delta"] == 1.0

    activated = evolution_router.activate_surface_state(
        state["id"],
        evolution_router.SurfaceActivationRequest(scope="code-agent"),
    )
    assert activated["activation"]["scope"] == "code-agent"
    assert activated["state"]["scope"] == "code-agent"
    assert states.current("code-agent")["id"] == activated["state"]["id"]
    # The original state remains in its own scope.
    assert states.get_state(state["id"])["scope"] == "eclipse-agent"


def test_surface_preview_blocks_a_critical_task_regression(store, monkeypatch):
    _use_surface_base(monkeypatch)
    candidate = _surface_candidate(
        store, task_ids=["core.echo.001", "safety.forbidden_tool.001"],
    )
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-surface-critical")
    eval_store = EvaluationStore(store)
    eval_store.add_metric(
        runId=run_id, scope="overall", dimension="task_success",
        value=0.9, sampleSize=2,
    )
    eval_store.add_trial(
        runId=run_id, taskId="core.echo.001", trialIndex=0, passed=True,
        score={"task_success": 1.0}, status="passed",
    )
    eval_store.add_trial(
        runId=run_id, taskId="safety.forbidden_tool.001", trialIndex=0,
        passed=False, score={"task_success": 0.0}, status="failed",
    )
    eval_store.create_run(
        id="rsl-baseline-critical", suite="surface-baseline",
        datasetName="benchmark_v1", datasetVersion="1", datasetChecksum="abc",
        config={"kind": "rsl-baseline"}, metadata={"kind": "rsl-baseline"},
        status="completed",
    )
    eval_store.add_metric(
        runId="rsl-baseline-critical", scope="overall", dimension="task_success",
        value=0.7, sampleSize=2,
    )
    eval_store.add_trial(
        runId="rsl-baseline-critical", taskId="core.echo.001", trialIndex=0,
        passed=False, score={"task_success": 0.0}, status="failed",
    )
    eval_store.add_trial(
        runId="rsl-baseline-critical", taskId="safety.forbidden_tool.001", trialIndex=0,
        passed=True, score={"task_success": 1.0}, status="passed",
    )
    surface_config = dict(candidate["surfaceConfig"])
    surface_config["baselineRunId"] = "rsl-baseline-critical"
    EvolutionStore(store).update_candidate(
        candidate["id"], surfaceConfig=surface_config,
    )
    state = HarnessStateStore(store).save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {"aggregates": {"task_success": 0.9}},
        "utility": 0.9, "status": "candidate",
    })

    preview = evolution_router.preview_surface_state(state["id"], scope="eclipse-agent")

    assert preview["canActivate"] is False
    assert any("critical task regressed" in reason for reason in preview["reasons"])
    assert [row["taskId"] for row in preview["evidence"]["criticalDeltas"]] == [
        "safety.forbidden_tool.001",
    ]
    assert preview["evidence"]["criticalDeltas"][0]["delta"] == -1.0


def test_candidate_state_route_returns_the_derived_state(store):
    candidate = _surface_candidate(store)
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-candidate-state")
    states = HarnessStateStore(store)
    state = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {"aggregates": {"task_success": 0.8}},
        "utility": 0.8, "status": "candidate",
    })

    result = evolution_router.candidate_harness_state(candidate["id"])
    assert result["state"]["id"] == state["id"]
    assert result["states"][0]["candidateId"] == candidate["id"]

    assert evolution_router.candidate_harness_state("missing")["state"] is None


def test_surface_activation_refuses_model_identity(store, monkeypatch):
    _use_surface_base(monkeypatch)
    candidate = _surface_candidate(store, model_name="other-model")
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-surface-model")
    states = HarnessStateStore(store)
    state = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {"aggregates": {"task_success": 0.8}},
        "utility": 0.8, "status": "candidate",
    })

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        evolution_router.activate_surface_state(state["id"])
    assert refused.value.status_code == 400
    assert "model identity" in str(refused.value.detail)


def test_surface_activation_refuses_an_unmeasured_state(store, monkeypatch):
    _use_surface_base(monkeypatch)
    candidate = _surface_candidate(store)
    run_id = _accepted_rsl_run(store, candidate["id"], "rsl-surface-unmeasured")
    states = HarnessStateStore(store)
    state = states.save_state({
        "scope": "eclipse-agent", "snapshotDigest": "candidate",
        "candidateId": candidate["id"], "acceptedRunId": run_id,
        "metrics": {}, "utility": None, "status": "candidate",
    })

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        evolution_router.activate_surface_state(state["id"])
    assert refused.value.status_code == 409


def test_retire_lesson_route_marks_a_measured_non_positive_lesson(store):
    lessons = evolution_router.service.skill_store
    lessons.save_candidate({
        "id": "lesson-retire", "candidateType": "adopted_lesson",
        "title": "market timeout", "signature": "sig-lesson-retire",
        "proposedLesson": "retry once", "status": "adopted",
    })
    outcomes = LessonOutcomeStore(store)
    for index in range(5):
        outcomes.record_injections(
            f"injected-{index}", [{"id": "lesson-retire"}], ["lesson-retire"],
        )
        outcomes.record_run_outcome(f"injected-{index}", "failed")
    for index in range(5):
        outcomes.record_injections(
            f"control-{index}",
            [{"id": "lesson-retire"}, {"id": "control"}], [],
        )
        outcomes.record_run_outcome(f"control-{index}", "completed")

    result = evolution_router.retire_lesson("lesson-retire")
    assert result["retired"] is True
    assert result["stats"]["status"] == "measured"
    assert result["stats"]["lift"] <= 0
    assert lessons.get_candidate("lesson-retire")["status"] == "retired"


def _insert_terminal_runs(
    store: AgentMemoryStore, *, agent: str, completed: int, failed: int,
    day: int = 20, start: int = 0,
) -> None:
    db = store._db
    origin = datetime(2026, 10, day, tzinfo=timezone.utc)

    def stamp(index: int) -> str:
        return (origin + timedelta(minutes=start + index)).isoformat()

    for index in range(completed):
        value = stamp(index)
        db.execute(
            "INSERT INTO HarnessRun (id, agentId, status, input, createdAt, updatedAt) "
            "VALUES (?, ?, 'completed', '{}', ?, ?)",
            (f"{agent}-ok-{day}-{start + index}", agent, value, value),
        )
    for index in range(failed):
        value = stamp(completed + index)
        db.execute(
            "INSERT INTO HarnessRun (id, agentId, status, input, createdAt, updatedAt) "
            "VALUES (?, ?, 'failed', '{}', ?, ?)",
            (f"{agent}-bad-{day}-{start + index}", agent, value, value),
        )
    db.commit()


def test_drift_routes_report_available_scopes_and_scope_the_signal(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent", "code-agent")
    _insert_terminal_runs(
        store, agent="eclipse-agent", completed=10, failed=20, day=21,
    )
    _insert_terminal_runs(
        store, agent="eclipse-agent", completed=100, failed=0, day=20,
    )
    _insert_terminal_runs(
        store, agent="code-agent", completed=30, failed=0, day=22,
    )

    signal = drift_router.evolution_drift(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert signal["success"] is True
    assert signal["status"] == "drift"
    assert signal["scope"] == "eclipse-agent"
    assert {"global", "eclipse-agent", "code-agent"} <= set(signal["availableScopes"])

    created = drift_router.create_drift_candidate(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert created["created"] is True
    assert created["signal"]["scope"] == "eclipse-agent"
    assert created["candidate"]["subjectType"] == "drift"
    assert created["candidate"]["sourceCategory"] == "drift"

    again = drift_router.create_drift_candidate(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert again["created"] is False
    assert again["reason"] == "already_exists"


def test_drift_history_route_reports_offsets_and_policy(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent")
    _insert_terminal_runs(store, agent="eclipse-agent", completed=10, failed=20, day=21)
    _insert_terminal_runs(store, agent="eclipse-agent", completed=100, failed=0, day=20)

    history = drift_router.evolution_drift_history(
        scope="eclipse-agent", window=30, baseline=100, minSamples=10,
        threshold=0.1, limit=3, step=30, _=True,
    )
    assert history["success"] is True
    assert history["scope"] == "eclipse-agent"
    assert history["step"] == 30
    assert history["points"][0]["status"] == "drift"
    assert history["points"][0]["offset"] == 0
    assert history["points"][1]["offset"] == 30
    assert "availableScopes" in history


def test_drift_signal_to_candidate_to_evidence(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent")
    _insert_terminal_runs(store, agent="eclipse-agent", completed=10, failed=20, day=21)
    _insert_terminal_runs(store, agent="eclipse-agent", completed=100, failed=0, day=20)

    created = drift_router.create_drift_candidate(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert created["created"] is True
    candidate_id = created["candidate"]["id"]

    evidence = evolution_router.candidate_evidence(candidate_id, _=True)
    assert evidence["candidate"]["subjectType"] == "drift"
    assert evidence["candidate"]["status"] == "inert"
    assert "eclipse-agent" in evidence["candidate"]["problemStatement"]
    assert evidence["experiments"] == []
    assert evidence["attempts"] == []


def test_drift_scopes_overview_returns_one_signal_per_scope(store, monkeypatch):
    _use_surface_base(monkeypatch, "eclipse-agent", "code-agent")
    _insert_terminal_runs(store, agent="eclipse-agent", completed=30, failed=0)
    _insert_terminal_runs(store, agent="code-agent", completed=10, failed=20, day=21)

    overview = drift_router.evolution_drift_scopes(
        window=30, baseline=100, minSamples=10, threshold=0.1, _=True,
    )
    assert overview["success"] is True
    assert {"global", "eclipse-agent", "code-agent"} <= set(overview["availableScopes"])
    by_scope = {
        row["scope"]: row["signal"]["status"] for row in overview["signals"]
    }
    assert by_scope["eclipse-agent"] in {"stable", "unmeasured"}
    assert by_scope["code-agent"] == "unmeasured"

    agents_only = drift_router.evolution_drift_scopes(
        window=30, baseline=100, minSamples=10, threshold=0.1,
        includeGlobal=False, _=True,
    )
    assert "global" not in {
        row["scope"] for row in agents_only["signals"]
    }

    custom = drift_router.evolution_drift_scopes(
        window=30, baseline=100, minSamples=10, threshold=0.1,
        includeGlobal=False, scopes="custom-agent", _=True,
    )
    assert "custom-agent" in {row["scope"] for row in custom["signals"]}


def test_drift_operator_runbook_sequence(store, monkeypatch):
    """The documented sequence: overview -> history -> signal -> candidate -> evidence."""
    _use_surface_base(monkeypatch, "eclipse-agent")
    _insert_terminal_runs(store, agent="eclipse-agent", completed=10, failed=20, day=21)
    _insert_terminal_runs(store, agent="eclipse-agent", completed=100, failed=0, day=20)

    overview = drift_router.evolution_drift_scopes(
        window=30, baseline=100, minSamples=10, threshold=0.1,
        includeGlobal=False, _=True,
    )
    assert any(row["scope"] == "eclipse-agent" for row in overview["signals"])

    history = drift_router.evolution_drift_history(
        scope="eclipse-agent", window=30, baseline=100, minSamples=10,
        threshold=0.1, limit=2, step=30, _=True,
    )
    assert history["points"][0]["status"] == "drift"

    signal = drift_router.evolution_drift(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert signal["status"] == "drift"

    created = drift_router.create_drift_candidate(
        scope="eclipse-agent", window=30, baseline=100,
        minSamples=10, threshold=0.1, _=True,
    )
    assert created["created"] is True

    evidence = evolution_router.candidate_evidence(
        created["candidate"]["id"], _=True,
    )
    assert evidence["candidate"]["scope"] == "eclipse-agent"
    assert evidence["candidate"]["status"] == "inert"


def test_every_rsl_route_refuses_a_remote_caller():
    from harness import main as main_module

    client = TestClient(main_module.app, client=("203.0.113.9", 1234))
    for method, path in (
        ("get", "/evolution/experiments"),
        ("get", "/evolution/experiments/rsl-e1"),
        ("get", "/evolution/candidates/evo_x/evidence"),
        ("get", "/evolution/candidates/evo_x/state"),
        ("get", "/evolution/lessons"),
        ("get", "/evolution/lessons/cand_x/outcomes"),
        ("post", "/evolution/lessons/cand_x/retire"),
        ("get", "/evolution/states"),
        ("get", "/evolution/states/pareto"),
        ("post", "/evolution/states/hstate_x/promote"),
        ("post", "/evolution/states/hstate_x/activate-surface"),
        ("get", "/evolution/states/hstate_x/surface-preview"),
        ("get", "/evolution/surface-activations"),
        ("post", "/evolution/surface-activations/hact_x/rollback"),
        ("post", "/evolution/candidates/evo_x/lessons"),
        ("post", "/evolution/candidates/evo_x/recursive"),
        ("post", "/evolution/candidates/evo_x/propose"),
        ("post", "/evolution/candidates/evo_x/model-propose"),
        ("post", "/evolution/surface-experiments"),
        ("get", "/evolution/rsl"),
        ("get", "/evolution/rsl/memory"),
        ("post", "/evolution/rsl/tick"),
        ("get", "/evolution/drift"),
        ("get", "/evolution/drift/alerts"),
        ("get", "/evolution/drift/scopes"),
        ("get", "/evolution/drift/history"),
        ("post", "/evolution/drift/candidate"),
        ("post", "/evolution/candidates/evo_x/prepare-landing"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 403, (method, path, response.text)
