"""Plugin-code hot reload: re-import changed code with an exact unload."""

from __future__ import annotations

import pytest

from harness.events import EventBus
from harness.persistence import AgentMemoryStore
from harness.plugins.errors import NotFound, PluginError
from harness.plugins.manager import PluginManager
from harness.plugins.model import PluginState
from harness.tools import ToolRegistry

from plugins_helpers import BUNDLED, env, package

TOOL = "plug_acme_hot_ping"


def _plugin_source(value: int) -> str:
    return (
        "from harness.plugins.sdk import Plugin, tool\n\n\n"
        "VALUE = %d\n\n\n"
        "class Impl(Plugin):\n"
        "    @tool(name=\"ping\", description=\"ping\", schema={}, permission=\"read\")\n"
        "    async def ping(self, args, ctx):\n"
        "        return {\"v\": VALUE}\n\n\n"
        "plugin = Impl\n" % value
    )


async def _result(env):
    tool = env.registry.get(TOOL)
    assert tool is not None
    return await tool.handler({}, env.ctx())


async def test_reload_reimports_changed_code_and_keeps_one_contribution(env):
    await env.manager.install_package(**package("acme-hot", tools=[]), enable=True, operator=True)
    path = env.manager.directory_of("acme-hot") / "plugin.py"
    path.write_text(_plugin_source(1), encoding="utf-8")
    await env.manager.reload("acme-hot", operator=True)
    assert (await _result(env))["v"] == 1

    before = env.manager.contributions.descriptors("acme-hot")
    path.write_text(_plugin_source(2), encoding="utf-8")
    result = await env.manager.reload("acme-hot", operator=True)

    assert (await _result(env))["v"] == 2
    assert result["changed"] is True
    assert result["evictedModules"]
    assert len(env.manager.contributions.descriptors("acme-hot")) == len(before)
    assert env.manager.store.get_plugin("acme-hot").status is PluginState.RUNNING
    history = [row["action"] for row in env.manager.store.list_history("acme-hot")]
    assert "reload" in history


async def test_reload_route_is_operator_gated_and_reports_evidence(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from harness.deps import require_operator
    from harness.routers import plugins as plugins_router

    await env.manager.install_package(**package("acme-route", tools=[]), enable=True, operator=True)
    path = env.manager.directory_of("acme-route") / "plugin.py"
    path.write_text(_plugin_source(3), encoding="utf-8")
    monkeypatch.setattr(plugins_router, "service", type("S", (), {"plugins": env.manager})())
    app = FastAPI()
    app.include_router(plugins_router.router)
    app.dependency_overrides[require_operator] = lambda: True
    with TestClient(app) as client:
        response = client.post("/plugins/acme-route/reload")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reloaded"] is True
        assert body["changed"] is True
    tool = env.registry.get("plug_acme_route_ping")
    assert (await tool.handler({}, env.ctx()))["v"] == 3


async def test_watcher_polls_changed_code_and_skips_pinned_plugins(env):
    from harness.plugins.hot_reload import PluginWatcher

    await env.manager.install_package(**package("acme-watch", tools=[]), enable=True, operator=True)
    path = env.manager.directory_of("acme-watch") / "plugin.py"
    path.write_text(_plugin_source(1), encoding="utf-8")
    await env.manager.reload("acme-watch", operator=True)

    watcher = PluginWatcher(env.manager, interval=0.05)
    assert await watcher.poll() == []
    path.write_text(_plugin_source(4), encoding="utf-8")
    results = await watcher.poll()
    assert len(results) == 1
    assert watcher.reloads == 1
    assert (await env.registry.get("plug_acme_watch_ping").handler({}, env.ctx()))["v"] == 4
    assert await watcher.poll() == []
    assert watcher.reloads == 1


async def test_start_plugins_starts_the_watcher_only_when_enabled(env, monkeypatch):
    from harness.plugins import bootstrap

    await env.manager.install_package(**package("acme-boot", tools=[]), enable=True, operator=True)
    service = type("S", (), {"plugins": env.manager})()
    monkeypatch.setenv("HARNESS_PLUGIN_HOT_RELOAD", "1")
    result = await bootstrap.start_plugins(service)
    assert result["watch"] is True
    assert getattr(env.manager, "watcher", None) is not None
    await bootstrap.stop_plugins(service)
    assert getattr(env.manager, "watcher", None) is None


async def test_reload_refuses_untrusted_transitions(env):
    with pytest.raises(PluginError) as missing:
        await env.manager.reload("absent", operator=True)
    assert isinstance(missing.value, NotFound)

    await env.install_hello(enable=False)
    with pytest.raises(PluginError) as not_running:
        await env.manager.reload("hello-world", operator=True)
    assert not_running.value.code == "PLUGIN_NOT_RUNNING"

    await env.manager.enable("hello-world", operator=True)
    with pytest.raises(PluginError) as bundled:
        await env.manager.reload("hello-world", operator=True)
    assert bundled.value.code in {"PLUGIN_RELOAD_BUNDLED", "PLUGIN_RELOAD_TRUSTED"}

    await env.manager.install_package(**package("acme-operator"), enable=True, operator=True)
    await env.manager.disable("acme-operator")
    with pytest.raises(PluginError) as operator_only:
        await env.manager.reload("acme-operator", operator=False)
    assert operator_only.value.code == "PLUGIN_RELOAD_OPERATOR"


async def test_failed_reload_unloads_exactly_and_can_be_reenabled(tmp_path):
    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    registry = ToolRegistry()
    manager = PluginManager(store, registry, EventBus(), None,
                            plugins_dir=tmp_path / "plugins", bundled_dir=BUNDLED)
    await manager.install_package(**package("acme-broken", tools=[]), enable=True, operator=True)
    path = manager.directory_of("acme-broken") / "plugin.py"
    path.write_text(_plugin_source(1), encoding="utf-8")
    await manager.reload("acme-broken", operator=True)
    baseline = len(registry.names())
    broken_tool = "plug_acme_broken_ping"

    path.write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
    with pytest.raises(Exception):
        await manager.reload("acme-broken", operator=True)

    assert registry.get(broken_tool) is None
    assert len(registry.names()) == baseline - 1
    assert manager.store.get_plugin("acme-broken").status is PluginState.FAILED
    assert manager.is_active("acme-broken") is False

    # Restoring the previous good source matches the pre-reload checksum, so a
    # normal enable is allowed and never duplicates the contribution.
    path.write_text(_plugin_source(1), encoding="utf-8")
    await manager.enable("acme-broken", operator=True)
    tool = registry.get(broken_tool)
    assert tool is not None
    assert (await tool.handler({}, None))["v"] == 1
    assert len(registry.names()) == baseline
