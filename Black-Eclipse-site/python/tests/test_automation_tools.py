"""Bot / Automation agent tools — real create→list→steer→delete over the service.

These tools are thin wrappers over the ONE AutomationService. This test boots the
runtime, exercises the tool handlers directly, and asserts that what the chat
agent does is exactly what the Bots workspace would see.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AGENT_MEMORY_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="be-bot-tools-"), "agent-memory.db"))
os.environ.setdefault("HARNESS_MCP_CONFIG", os.path.join(tempfile.mkdtemp(prefix="be-bot-cfg-"), "mcp-servers.json"))
os.environ.setdefault("HARNESS_CALLBACK_URL", "http://127.0.0.1:3000")


@pytest.fixture(scope="module")
def ctx():
    from harness import main as main_module
    from harness.automation.tools import set_automation_service
    from harness.persistence import AgentMemoryStore
    from harness.service import AgenticService
    from harness.tools import ToolContext, ToolRegistry  # noqa: F401

    # Self-contained: a PRIOR test's app shutdown runs `boot._shutdown`, which
    # closes the process-wide store; the cached automation singleton would then
    # hold a CLOSED database (order-dependent `Cannot operate on a closed
    # database`). Build this module's own live service + store so it never depends
    # on the shared singleton's lifecycle.
    with TestClient(main_module.app):
        store = AgentMemoryStore(
            os.path.join(tempfile.mkdtemp(prefix="be-bot-tools-local-"), "agent-memory.db")
        )
        svc = AgenticService(store=store)
        set_automation_service(svc.automation_service())
        try:
            yield ToolContext(run_id="test-run", log=lambda *a, **k: None)
        finally:
            store.close()


def run(coro):
    return asyncio.run(coro)


def test_bot_tools_are_registered():
    from harness.deps import service

    for name in ("bot_create", "bot_list", "bot_get", "bot_run", "bot_pause", "bot_delete", "bot_history"):
        assert service.registry.get(name) is not None, name


def test_service_singleton_is_wired(ctx):
    # Depend on `ctx` so the singleton is installed by THIS module. Without the
    # fixture this passed alone but failed after a prior module's app shutdown
    # had cleared the process-wide singleton.
    from harness.automation import tools

    assert tools.get_automation_service() is not None


def test_full_bot_lifecycle_via_tools(ctx):
    from harness.automation import tools

    # create
    _, data = run(tools._create(
        {"name": "Tool test bot", "objective": "Summarize the market", "triggerType": "manual", "state": "active"},
        ctx,
    ))
    assert data["created"] is True
    bot_id = data["bot"]["id"]

    # list includes it
    _, listed = run(tools._list({"limit": 500}, ctx))
    assert any(b["id"] == bot_id for b in listed["bots"])

    # get
    _, got = run(tools._get({"botId": bot_id}, ctx))
    assert got["id"] == bot_id
    assert got["action"]["objective"] == "Summarize the market"

    # update name + objective
    _, updated = run(tools._update({"botId": bot_id, "name": "Renamed tool bot", "objective": "New instructions"}, ctx))
    assert updated["updated"] is True
    assert updated["bot"]["name"] == "Renamed tool bot"
    assert updated["bot"]["action"]["objective"] == "New instructions"

    # pause then resume
    _, paused = run(tools._lifecycle_handler("pause")({"botId": bot_id}, ctx))
    assert paused.get("state") == "paused", paused
    _, resumed = run(tools._lifecycle_handler("resume")({"botId": bot_id}, ctx))
    assert resumed.get("state") == "active", resumed

    # status + history are real reads
    _, status = run(tools._status({"botId": bot_id}, ctx))
    assert status["automationId"] == bot_id
    _, history = run(tools._history({"botId": bot_id}, ctx))
    assert isinstance(history["runs"], list)

    # delete
    # Deleting is destructive, so the tool asks for a confirmation first.
    _, pending = run(tools._delete({"botId": bot_id}, ctx))
    assert pending["needsConfirmation"] is True
    _, deleted = run(tools._delete(
        {"botId": bot_id, "confirm": True, "proposalId": pending["proposalId"]}, ctx))
    assert deleted["deleted"] is True
    _, gone = run(tools._get({"botId": bot_id}, ctx))
    assert gone["found"] is False


def test_create_requires_an_objective(ctx):
    from harness.automation import tools

    _, data = run(tools._create({"name": "no objective"}, ctx))
    assert data["created"] is False


def test_update_missing_bot_is_honest(ctx):
    from harness.automation import tools

    _, data = run(tools._update({"botId": "does-not-exist", "name": "x"}, ctx))
    assert data["updated"] is False


def test_bot_list_excludes_jobs_and_event_rules(ctx):
    from harness.automation import tools

    svc = tools._require_service()
    bot = svc.create(
        {
            "name": "Real bot",
            "state": "active",
            "trigger": {"type": "manual"},
            "action": {"type": "run_agent", "objective": "do bot work"},
            "tags": ["bot"],
        },
        created_by="test",
    )
    job = svc.create(
        {
            "name": "Agent job",
            "state": "active",
            "trigger": {"type": "manual"},
            "action": {"type": "run_agent", "objective": "do job work"},
        },
        created_by="test",
    )
    _, listed = run(tools._list({"limit": 500}, ctx))
    ids = {b["id"] for b in listed["bots"]}
    assert bot.id in ids
    assert job.id not in ids
    svc.delete(bot.id)
    svc.delete(job.id)


def test_a_cron_bot_without_an_expression_is_refused_clearly(ctx):
    from harness.automation import tools

    _, data = run(tools._create(
        {"name": "No schedule", "objective": "run daily", "triggerType": "cron", "state": "active"},
        ctx,
    ))
    assert data["created"] is False
    assert data["reason"] == "cron expression required"


def test_bot_delete_purges_private_memory(ctx, monkeypatch):
    from harness.automation import tools

    svc = tools._require_service()
    bot = svc.create(
        {
            "name": "Memory bot",
            "state": "active",
            "trigger": {"type": "manual"},
            "action": {"type": "run_agent", "objective": "remember"},
            "tags": ["bot"],
        },
        created_by="test",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        svc.memory,
        "forget_conversation",
        lambda conversation_id: (calls.append(conversation_id) or {"archived": []}),
    )
    _, pending = run(tools._delete({"botId": bot.id}, ctx))
    _, deleted = run(tools._delete(
        {"botId": bot.id, "confirm": True, "proposalId": pending["proposalId"]}, ctx))
    assert deleted["deleted"] is True
    assert calls == [f"bot-{bot.id}"]