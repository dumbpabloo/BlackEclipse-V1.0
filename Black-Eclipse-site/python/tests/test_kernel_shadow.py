"""Shadow mode: load and validate, never change live behavior."""

from __future__ import annotations

import json
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.agents import AGENT_PRESETS, get_agent
from harness.evolution.runtime import reset_assets
from harness.kernel.profile_boot import ProfileBoot
from harness.kernel.services.runtime import KernelRuntime, kernel_runtime
from harness.routers.evolution_assets import router
from harness.schemas import ModelSettings
from harness.tools import ToolDefinition, ToolRegistry


def _tool(name: str):
    async def handler(args, ctx):
        return name

    return ToolDefinition(name=name, description=name, input_schema={}, handler=handler)


def _registry():
    registry = ToolRegistry()
    registry.register(_tool("web_search"))
    return registry


@pytest.fixture(autouse=True)
def _fixture_module():
    module = types.ModuleType("kernel_shadow_fixtures")

    def good(ctx, config):
        ctx.provide("shadow_row", config or {})

    module.good = good
    sys.modules["kernel_shadow_fixtures"] = module
    yield
    sys.modules.pop("kernel_shadow_fixtures", None)


def test_runtime_modes_parse_and_shadow_has_no_live_effect(monkeypatch):
    monkeypatch.setenv("ECLIPSE_KERNEL", "shadow")
    runtime = KernelRuntime().configure(_registry())
    assert runtime.mode == "shadow"
    assert runtime.enabled is True
    assert runtime.active is False
    base = runtime._registry
    assert runtime.view_for("run-1", base) is base
    assert runtime.resolve_agent("eclipse-agent") is get_agent("eclipse-agent")
    settings = ModelSettings(provider="deepseek", model="deepseek-chat")
    runtime.model_routes.set("eclipse-agent", provider="deepseek", model="deepseek-flash")
    assert runtime.apply_model_route("eclipse-agent", settings).model == "deepseek-chat"
    resolution, prompt = runtime.resolve_surface(
        AGENT_PRESETS["eclipse-agent"], base, {}, None,
    )
    from harness.surface import resolve_surface_run

    expected, expected_prompt = resolve_surface_run(AGENT_PRESETS["eclipse-agent"], base, {}, None)
    assert resolution.allowed == expected.allowed
    assert prompt == expected_prompt
    assert runtime.status()["mode"] == "shadow"
    assert runtime.status()["active"] is False

    monkeypatch.setenv("ECLIPSE_KERNEL", "on")
    assert KernelRuntime().mode == "on"
    assert KernelRuntime().active is True
    monkeypatch.setenv("ECLIPSE_KERNEL", "off")
    assert KernelRuntime().mode == "off"
    monkeypatch.setenv("ECLIPSE_KERNEL", "shadow")
    assert KernelRuntime(enabled=True).mode == "on"


def _profile(tmp_path, rows):
    root = tmp_path / "profiles"
    profile = root / "web"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(json.dumps({"name": "web", "bundles": [], "patch": "cordis.patch.yaml"}))
    (profile / "cordis.patch.yaml").write_text(rows, encoding="utf-8")
    return root


async def test_profile_boot_in_shadow_reports_but_does_not_mount(tmp_path):
    root = _profile(tmp_path, """
- insert:
    - id: good
      name: kernel_shadow_fixtures:good
      config: {v: 1}
    - id: broken
      name: missing.module:apply
""")
    runtime = KernelRuntime(mode="shadow").configure(_registry())
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    status = await boot.apply()
    assert status["status"] == "shadow"
    assert status["active"] is False
    assert status["ok"] is False
    assert boot.supervisor is None
    assert runtime.kernel.root.get("shadow_row") is None
    assert any(row["code"] == "KERNEL_SHADOW_ROW_INVALID" for row in status["diagnostics"])
    assert runtime.composition_rows._supervisor is runtime.supervisor
    await runtime.kernel.shutdown()


def test_evolution_apply_is_refused_in_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("ECLIPSE_EVOLUTION_JOURNAL", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("ECLIPSE_KERNEL", "shadow")
    kernel_runtime.reset(mode="shadow", registry=_registry())
    reset_assets()
    app = FastAPI()
    app.include_router(router)
    try:
        with TestClient(app) as client:
            listed = client.get("/evolution/assets")
            assert listed.json()["kernelMode"] == "shadow"
            proposed = client.post("/evolution/assets/propose", json={
                "kind": "skill",
                "target": "learned.shadow",
                "mutations": [{
                    "op": "add", "kind": "skill", "target": "learned.shadow",
                    "content": {"name": "Shadow", "instructions": "# Shadow"},
                    "rationale": "shadow check",
                }],
            })
            assert proposed.status_code == 200, proposed.text
            candidate = proposed.json()
            assert client.post(f"/evolution/assets/{candidate['id']}/admit").status_code == 200
            refused = client.post(f"/evolution/assets/{candidate['id']}/apply")
            assert refused.status_code == 409
            assert refused.json()["detail"]["code"] == "KERNEL_SHADOW_READONLY"
            assert kernel_runtime.skills.get("learned.shadow") is None
    finally:
        reset_assets()
        kernel_runtime.reset(mode="off")
