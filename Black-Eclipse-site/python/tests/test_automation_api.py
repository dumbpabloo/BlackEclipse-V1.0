"""Native Automation Engine API — operator gating + full CRUD/lifecycle cycle.

Mirrors the jobs API convention: a directly reachable runtime must not expose
automation control without proof; the legacy `/jobs` surface is the SAME store.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AGENT_MEMORY_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="be-auto-api-"), "agent-memory.db"))
os.environ["HARNESS_MCP_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="be-auto-cfg-"), "mcp-servers.json")
os.environ.setdefault("HARNESS_CALLBACK_URL", "http://127.0.0.1:3000")


@pytest.fixture(scope="module")
def client():
    from harness import main as main_module

    with TestClient(main_module.app) as test_client:
        yield test_client


OPERATOR_ROUTES = [
    ("GET", "/automations", None),
    ("GET", "/automations/metrics", None),
    ("GET", "/automations/audit", None),
    ("GET", "/automations/statuses", None),
    ("POST", "/automations", {"name": "x", "trigger": {"type": "manual"}, "action": {"type": "run_agent", "objective": "x"}}),
    ("GET", "/automations/nope", None),
    ("PATCH", "/automations/nope", {"name": "x"}),
    ("DELETE", "/automations/nope", None),
    ("POST", "/automations/nope/enable", None),
    ("POST", "/automations/nope/pause", None),
    ("POST", "/automations/nope/run", None),
    ("POST", "/automations/nope/dry-run", None),
    ("POST", "/automations/nope/test", None),
    ("GET", "/automations/nope/status", None),
    ("GET", "/automations/nope/history", None),
    ("GET", "/automations/nope/versions", None),
    ("GET", "/automations/executions/nope", None),
]


@pytest.mark.parametrize("method,path,body", OPERATOR_ROUTES)
def test_every_operator_route_refuses_a_non_operator(monkeypatch, method, path, body):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    from starlette.testclient import TestClient as TC

    import harness.main as main_module
    with TC(main_module.app, client=("203.0.113.9", 5555)) as remote:
        res = remote.request(method, path, json=body)
        assert res.status_code == 403, f"{method} {path}: {res.text}"


@pytest.mark.parametrize("method,path,body", OPERATOR_ROUTES)
def test_every_operator_route_allows_loopback(client, method, path, body):
    res = client.request(method, path, json=body)
    assert res.status_code != 403, f"{method} {path}: {res.text}"


def test_bot_audit_records_crud_lifecycle_and_run(client):
    created = client.post("/automations", json={
        "name": "Audited bot",
        "state": "draft",
        "trigger": {"type": "manual"},
        "action": {"type": "send_notification", "notificationTemplate": "hi"},
    }).json()["automation"]
    bot_id = created["id"]

    client.patch(f"/automations/{bot_id}", json={"name": "Audited bot 2"})
    client.post(f"/automations/{bot_id}/enable")
    client.post(f"/automations/{bot_id}/run")
    client.delete(f"/automations/{bot_id}")

    events = client.get(f"/automations/audit?automationId={bot_id}").json()["events"]
    actions = {event["action"] for event in events}
    assert {"created", "updated", "lifecycle", "run", "deleted"} <= actions
    # Deleting the bot retains its audit trail.
    assert any(event["action"] == "deleted" for event in events)


def test_webhook_token_is_generated_and_required(client):
    created = client.post("/automations", json={
        "name": "External wake",
        "state": "active",
        "trigger": {"type": "webhook"},
        "action": {"type": "send_notification", "notificationTemplate": "wake"},
    }).json()["automation"]
    automation_id = created["id"]
    secret = created["trigger"]["webhookSecret"]
    assert secret and len(secret) >= 24

    assert client.post(f"/automations/webhook/{automation_id}", json={}).status_code == 403
    assert client.post(
        f"/automations/webhook/{automation_id}",
        json={}, headers={"x-eclipse-webhook-token": "wrong"},
    ).status_code == 403

    fired = client.post(
        f"/automations/webhook/{automation_id}",
        json={"deliveryId": "ext-1"},
        headers={"x-eclipse-webhook-token": secret},
    )
    assert fired.status_code == 200, fired.text
    assert fired.json()["executionId"]

    # An edit must not silently rotate the caller's token.
    updated = client.patch(f"/automations/{automation_id}", json={"name": "External wake 2"}).json()
    assert updated["automation"]["trigger"]["webhookSecret"] == secret


def test_create_validate_dry_run_and_history(client):
    created = client.post("/automations", json={
        "name": "Morning brief",
        "state": "draft",
        "trigger": {"type": "cron", "cron": "0 8 * * *", "timezone": "UTC"},
        "action": {"type": "run_agent", "objective": "brief me"},
        "delivery": {"adapter": "in_app"},
    }).json()["automation"]
    automation_id = created["id"]
    assert created["state"] == "draft"
    assert created["trigger"]["cron"] == "0 8 * * *"

    listing = client.get("/automations").json()["automations"]
    assert any(a["id"] == automation_id for a in listing)

    assert client.post(f"/automations/{automation_id}/validate").json()["valid"] is True

    preview = client.post(f"/automations/{automation_id}/dry-run").json()["dryRun"]
    assert any("run agent" in step for step in preview["plannedActions"])

    # Activate -> next run is computed.
    enabled = client.post(f"/automations/{automation_id}/enable").json()["automation"]
    assert enabled["state"] == "active"
    status = client.get(f"/automations/{automation_id}/status").json()["status"]
    assert status["nextRunAt"] is not None

    # Pause does not delete; resume restores.
    assert client.post(f"/automations/{automation_id}/pause").json()["automation"]["state"] == "paused"
    # A paused automation can still be run MANUALLY (pause stops the scheduler,
    # not an explicit operator request).
    paused_run = client.post(f"/automations/{automation_id}/run")
    assert paused_run.status_code == 200, paused_run.text
    assert paused_run.json()["executionId"]
    assert client.post(f"/automations/{automation_id}/resume").json()["automation"]["state"] == "active"

    assert client.get(f"/automations/{automation_id}/history").json()["success"] is True
    assert client.get(f"/automations/{automation_id}/versions").json()["success"] is True

    assert client.delete(f"/automations/{automation_id}").json()["deleted"] == automation_id
    assert client.get(f"/automations/{automation_id}").status_code == 404


def test_statuses_returns_latest_run_for_every_automation(client):
    created = client.post("/automations", json={
        "name": "Batch status",
        "trigger": {"type": "manual"},
        "action": {"type": "run_agent", "objective": "x"},
    }).json()["automation"]
    aid = created["id"]

    statuses = client.get("/automations/statuses").json()["statuses"]
    assert aid in statuses
    assert statuses[aid]["automationId"] == aid
    assert statuses[aid]["lastExecution"] is None

    # The `ids` filter (the client may ask for a subset) is honored.
    filtered = client.get(f"/automations/statuses?ids={aid}").json()["statuses"]
    assert set(filtered) == {aid}

    client.delete(f"/automations/{aid}")


def test_invalid_cron_is_refused(client):
    res = client.post("/automations", json={
        "name": "Bad",
        "trigger": {"type": "cron", "cron": "not a cron", "timezone": "UTC"},
        "action": {"type": "run_agent", "objective": "x"},
    })
    assert res.status_code == 400


def test_unknown_timezone_is_refused(client):
    res = client.post("/automations", json={
        "name": "Bad tz",
        "trigger": {"type": "cron", "cron": "0 8 * * *", "timezone": "Mars/Olympus"},
        "action": {"type": "run_agent", "objective": "x"},
    })
    assert res.status_code == 400


def test_invalid_transition_is_refused(client):
    created = client.post("/automations", json={
        "name": "Lifecycle", "state": "draft",
        "trigger": {"type": "manual"}, "action": {"type": "run_agent", "objective": "x"},
    }).json()["automation"]
    aid = created["id"]
    client.post(f"/automations/{aid}/archive")
    res = client.post(f"/automations/{aid}/resume")
    assert res.status_code == 400
    client.delete(f"/automations/{aid}")


def test_legacy_jobs_are_the_same_automations(client):
    """A job created through /jobs is an automation in /automations (one store)."""
    job = client.post("/jobs", json={
        "name": "Shared", "kind": "manual",
        "target": {"kind": "objective", "objective": "x"},
    }).json()["job"]
    automation = client.get(f"/automations/{job['id']}")
    assert automation.status_code == 200
    assert automation.json()["automation"]["id"] == job["id"]
    client.delete(f"/jobs/{job['id']}")


def test_monitor_catalogue_is_listed(client):
    res = client.get("/automations/monitors")
    assert res.status_code == 200
    monitors = res.json()["monitors"]
    # 4 portfolio shapes + 3 ledger shapes.
    assert len(monitors) == 7
    assert {m["id"] for m in monitors} >= {"cash_buffer_watch", "concentration_watch",
                                           "ledger_close_blocked"}


def test_a_monitor_is_created_switched_off(client):
    res = client.post("/automations/monitors", json={
        "monitorId": "cash_buffer_watch",
        "params": {"portfolio_id": "pf_1", "threshold": 0.05},
    })
    assert res.status_code == 200
    automation = res.json()["automation"]
    assert automation["state"] == "disabled"
    assert "bot" in automation["tags"]
    assert automation["trigger"]["condition"]["expression"] == "pf.pf_1.cash_pct < 0.05"
    client.delete(f"/automations/{automation['id']}")


def test_deleting_a_bot_purges_its_private_memory(client, monkeypatch):
    from harness.deps import service

    created = client.post("/automations", json={
        "name": "Memory purge bot",
        "state": "active",
        "trigger": {"type": "manual"},
        "action": {"type": "run_agent", "objective": "remember"},
        "tags": ["bot"],
    }).json()["automation"]
    calls: list[str] = []
    monkeypatch.setattr(
        service.memory,
        "forget_conversation",
        lambda conversation_id: (calls.append(conversation_id) or {"archived": []}),
    )
    res = client.delete(f"/automations/{created['id']}")
    assert res.status_code == 200
    assert calls == [f"bot-{created['id']}"]



def test_a_monitor_missing_a_parameter_is_refused(client):
    res = client.post("/automations/monitors", json={
        "monitorId": "position_weight_breach", "params": {"portfolio_id": "pf_1"},
    })
    assert res.status_code == 400
