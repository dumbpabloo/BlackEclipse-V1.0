"""Operator API for evolution assets: propose/admit/apply/rollback."""

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


def _propose(client, text="Prefer the filing index."):
    response = client.post("/evolution/assets/propose", json={
        "kind": "prompt_section",
        "target": "learned.filings",
        "mutations": [{
            "op": "add", "kind": "prompt_section", "target": "learned.filings",
            "content": {"text": text, "scope": "eclipse-agent"}, "rationale": "recurred",
        }],
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_propose_admit_apply_rollback(client):
    candidate = _propose(client)
    assert candidate["status"] == "proposed"

    admitted = client.post(f"/evolution/assets/{candidate['id']}/admit")
    assert admitted.status_code == 200
    assert admitted.json()["status"] == "admitted"

    applied = client.post(f"/evolution/assets/{candidate['id']}/apply")
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert kernel_runtime.prompt.render_sections("eclipse-agent") == "Prefer the filing index."

    listed = client.get("/evolution/assets").json()
    assert listed["journal"]["verified"] is True
    assert any(row["id"] == candidate["id"] for row in listed["candidates"])

    rolled = client.post(f"/evolution/assets/{candidate['id']}/rollback")
    assert rolled.status_code == 200
    assert rolled.json()["status"] == "rolled_back"
    assert kernel_runtime.prompt.render_sections("eclipse-agent") == ""


def test_apply_is_refused_when_the_kernel_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_EVOLUTION_JOURNAL", str(tmp_path / "journal.jsonl"))
    kernel_runtime.reset(enabled=False)
    reset_assets()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        candidate = _propose(test_client)
        response = test_client.post(f"/evolution/assets/{candidate['id']}/apply")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ECLIPSE_KERNEL_OFF"
    reset_assets()


def test_invalid_mutation_is_a_named_400(client):
    response = client.post("/evolution/assets/propose", json={
        "kind": "prompt_section",
        "target": "x",
        "mutations": [{"op": "add", "kind": "prompt_section", "target": "x", "content": {}, "rationale": ""}],
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] in {"EVOLUTION_RATIONALE_REQUIRED", "EVOLUTION_CONTENT_INVALID"}


def test_observe_recurrence_creates_a_candidate(client):
    result = None
    for index in range(3):
        result = client.post("/evolution/assets/observe", json={
            "kind": "repeated_tool", "key": "web_search", "evidenceRefs": [f"run-{index}"],
        })
    assert result is not None and result.status_code == 200
    payload = result.json()
    assert payload["status"] == "proposed"
    assert payload["candidate"]["mutations"][0]["content"]["text"].startswith("Before repeating")


def test_shadow_observe_marks_the_candidate_unappliable(client, monkeypatch):
    monkeypatch.setenv("ECLIPSE_EVOLUTION_MODE", "shadow")
    reset_assets()
    result = None
    for _ in range(3):
        result = client.post("/evolution/assets/observe", json={
            "kind": "recovered_failure", "key": "research",
        })
    payload = result.json()
    assert payload["status"] == "shadow"
    candidate_id = payload["candidate"]["id"]
    client.post(f"/evolution/assets/{candidate_id}/admit")
    response = client.post(f"/evolution/assets/{candidate_id}/apply")
    assert response.status_code == 400
    assert "shadow" in response.json()["detail"]["message"]
