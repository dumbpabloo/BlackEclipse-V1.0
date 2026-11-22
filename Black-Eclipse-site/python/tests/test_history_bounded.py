"""Milestone 5: the `memory.history_bounded` visibility event (success path)."""

from __future__ import annotations

import os
import tempfile

import pytest
from langchain_core.messages import AIMessage

from harness import graph as graph_module
from harness.persistence import AgentMemoryStore
from harness.schemas import AgentConfig, ModelSettings, RunInput, RunRequest
from harness.tools import DataBridge, ToolRegistry
from harness.finance_tools import register_finance_tools


class FakeBridge(DataBridge):
    async def fetch(self, capability, params):
        return {"status": "UNAVAILABLE"}


class EchoModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return AIMessage(content="ok")

    async def astream(self, messages):
        yield await self.ainvoke(messages)


@pytest.fixture()
def store():
    directory = tempfile.mkdtemp(prefix="be-bounded-")
    s = AgentMemoryStore(os.path.join(directory, "agent-memory.db"))
    yield s
    s.close()


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_finance_tools(reg)
    return reg


def _history(n, chars=2000, secret=False):
    turns = []
    for i in range(n):
        content = "x" * chars
        if secret and i == 0:
            content = "MY SECRET IS sk-abcdefghijklmnopqrstuvwxyz " + content
        turns.append({"role": "user" if i % 2 == 0 else "assistant", "content": content})
    return turns


def _req(run_id, history, window=None, provider="openai", model="gpt-4o"):
    context = {"_context_budget_override": {"window": window}} if window else None
    return RunRequest(
        runId=run_id,
        input=RunInput(query="continue", history=history, context=context),
        agent=AgentConfig(id="a", name="A", systemPrompt="sys", tools=[], maxSteps=3),
        modelSettings=ModelSettings(provider=provider, model=model),
        callbackBaseUrl="http://localhost:3000",
    )


@pytest.mark.asyncio
async def test_trimmed_run_emits_exactly_one_history_bounded(store, monkeypatch):
    monkeypatch.setattr(graph_module, "build_chat_model", lambda s: EchoModel())
    await graph_module.execute_run(
        _req("run_trim", _history(30, chars=5000), window=20_000),
        _registry(), FakeBridge(), store,
    )
    events = [e for e in store.list_events("run_trim") if e["type"] == "memory.history_bounded"]
    assert len(events) == 1
    data = events[0]["data"]
    assert data["droppedTurns"] > 0
    assert data["keptTurns"] > 0
    assert data["keptTurns"] + data["droppedTurns"] == 30
    assert data["droppedTokensEstimate"] > 0
    assert data["window"] == 20_000
    assert data["windowKnown"] is True
    assert data["policy"] == "measured_model_window"
    assert "keepTailTokens" in data
    # Never any dropped text / memory content.
    assert "content" not in data and "text" not in data and "dropped" not in data


@pytest.mark.asyncio
async def test_untrimmed_run_emits_none(store, monkeypatch):
    monkeypatch.setattr(graph_module, "build_chat_model", lambda s: EchoModel())
    await graph_module.execute_run(
        _req("run_short", _history(3), window=20_000), _registry(), FakeBridge(), store
    )
    events = [e["type"] for e in store.list_events("run_short")]
    assert "memory.history_bounded" not in events


@pytest.mark.asyncio
async def test_unknown_window_reports_window_known_false(store, monkeypatch):
    # Unknown window → conservative ceiling. A 30-turn history is trimmed by the
    # constants and the event honestly says the window is unknown.
    monkeypatch.setattr(graph_module, "build_chat_model", lambda s: EchoModel())
    await graph_module.execute_run(
        _req("run_unknown", _history(30), window=None, provider="ollama", model="llama3.1"),
        _registry(), FakeBridge(), store,
    )
    events = [e for e in store.list_events("run_unknown") if e["type"] == "memory.history_bounded"]
    assert len(events) == 1
    assert events[0]["data"]["windowKnown"] is False
    assert events[0]["data"]["window"] is None


@pytest.mark.asyncio
async def test_event_carries_no_secret_content(store, monkeypatch):
    monkeypatch.setattr(graph_module, "build_chat_model", lambda s: EchoModel())
    await graph_module.execute_run(
        _req("run_secret", _history(30, chars=5000, secret=True), window=20_000),
        _registry(), FakeBridge(), store,
    )
    events = [e for e in store.list_events("run_secret") if e["type"] == "memory.history_bounded"]
    assert len(events) == 1
    serialized = str(events[0]["data"])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
