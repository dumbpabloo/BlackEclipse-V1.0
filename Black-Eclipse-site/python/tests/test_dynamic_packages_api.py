"""Operator API for dynamic packages."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from harness.dynamic.runtime import dynamic_packages, reset_dynamic
from harness.routers.dynamic import router

SOURCE = "def handle(payload):\n    return {'ok': payload.get('value')}\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_DYNAMIC_DIR", str(tmp_path / "dynamic"))
    reset_dynamic()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    reset_dynamic()


def test_define_run_invoke_stop(client):
    defined = client.post("/dynamic/demo/define", json={
        "sessionId": "s1", "source": SOURCE,
    })
    assert defined.status_code == 200, defined.text
    package_id = defined.json()["id"]

    refused = client.post("/dynamic/demo/run", json={
        "sessionId": "s1", "packageId": package_id,
    })
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "DYNAMIC_APPROVAL_REQUIRED"

    ran = client.post("/dynamic/demo/run", json={
        "sessionId": "s1", "packageId": package_id, "approved": True,
    })
    assert ran.status_code == 200

    invoked = client.post("/dynamic/demo/invoke", json={
        "sessionId": "s1", "payload": {"value": 5},
    })
    assert invoked.status_code == 200
    assert invoked.json()["result"] == {"ok": 5}

    stopped = client.post("/dynamic/demo/stop", json={"sessionId": "s1"})
    assert stopped.json()["stopped"] is True
    assert client.post("/dynamic/demo/undefine", json={"sessionId": "s1"}).status_code == 200


def test_unknown_session_has_no_packages(client):
    response = client.get("/dynamic", params={"sessionId": "nobody"})
    assert response.status_code == 200
    assert response.json() == {"plugins": []}
