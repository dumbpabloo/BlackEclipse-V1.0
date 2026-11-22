"""HTTP evaluator: no invented baseline, and a measured landing path."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from harness.evolution.runtime import asset_evolution, reset_assets
from harness.kernel.services.runtime import kernel_runtime
from harness.routers.evolution_assets import router
from harness.tools import ToolRegistry


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_EVOLUTION_JOURNAL", str(tmp_path / "journal.jsonl"))
    kernel_runtime.reset(enabled=True, registry=ToolRegistry())
    reset_assets()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    reset_assets()
    kernel_runtime.reset(enabled=False)


def _skill_candidate(client):
    response = client.post("/evolution/assets/propose", json={
        "kind": "skill",
        "target": "learned.http-demo",
        "mutations": [{
            "op": "add", "kind": "skill", "target": "learned.http-demo",
            "content": {"name": "HTTP demo", "instructions": "# Demo\nDo it."},
            "rationale": "recurred",
        }],
    })
    assert response.status_code == 200, response.text
    candidate = response.json()
    admitted = client.post(f"/evolution/assets/{candidate['id']}/admit")
    assert admitted.status_code == 200, admitted.text
    return candidate


def test_evaluate_refuses_without_a_baseline(client):
    candidate = _skill_candidate(client)
    response = client.post(f"/evolution/assets/{candidate['id']}/evaluate", json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "EVALUATION_BASELINE_REQUIRED"
    status = next(
        row for row in client.get("/evolution/assets").json()["candidates"]
        if row["id"] == candidate["id"]
    )
    assert status["status"] == "admitted"
    assert kernel_runtime.skills.get("learned.http-demo") is None


def test_evaluate_runs_the_deterministic_suite_and_never_fakes_acceptance(client):
    candidate = _skill_candidate(client)
    response = client.post(f"/evolution/assets/{candidate['id']}/evaluate", json={
        "suite": "core",
        "baseline": {"aggregates": {"task_success": 1.0}},
        "targetDimension": "task_success",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"measured_bad", "unmeasured"}
    assert body["live"] is False
    assert body["decision"]["accepted"] is False
    assert kernel_runtime.skills.get("learned.http-demo") is None


def test_evaluate_unknown_candidate_is_an_error(client):
    response = client.post("/evolution/assets/evo_missing/evaluate", json={
        "baseline": {"aggregates": {"task_success": 0.1}},
    })
    assert response.status_code in {400, 404}
