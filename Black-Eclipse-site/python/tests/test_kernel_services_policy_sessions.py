"""Kernel policy/sessions services: authority parity and a real consumer."""

from __future__ import annotations

import sys
import types

from harness.agents import CODE_AGENT
from harness.code.policy import AUTONOMY_PROFILES, allowed_tools, code_grants
from harness.code.tools import register_code_tools
from harness.kernel.services.policy import PolicyService
from harness.kernel.services.runtime import KernelRuntime
from harness.kernel.services.sessions import SessionService
from harness.surface import resolve_surface_detail
from harness.tools import ToolRegistry


class _Store:
    def __init__(self):
        self.sessions = {
            "chat-1": {"id": "chat-1", "title": "First"},
            "chat-2": {"id": "chat-2", "title": "Second"},
        }
        self.runs = {"chat-1": ["run-1", "run-2"]}
        self.run_rows = {"run-1": {"id": "run-1", "agentId": "eclipse-agent", "status": "completed"}}

    def get_chat_session(self, session_id):
        return self.sessions.get(session_id)

    def list_chat_sessions(self, *, limit=50):
        return list(self.sessions.values())[:limit]

    def session_runs(self, session_id):
        return list(self.runs.get(session_id, []))

    def active_session_run(self, session_id):
        rows = self.runs.get(session_id) or []
        for run_id in reversed(rows):
            if run_id in self.run_rows:
                return self.run_rows[run_id]
        return None

    def get_run(self, run_id):
        return self.run_rows.get(run_id)


def test_session_service_is_an_honest_provider_over_the_store():
    service = SessionService()
    assert service.available is False
    assert service.get("chat-1") is None
    assert service.list() == []
    assert service.runs("chat-1") == []
    assert service.active_run("chat-1") is None
    assert service.current("run-1") is None

    store = _Store()
    service.attach(store)
    assert service.available is True
    assert service.get("chat-1") == store.sessions["chat-1"]
    assert [row["id"] for row in service.list()] == ["chat-1", "chat-2"]
    assert service.runs("chat-1") == ["run-1", "run-2"]
    assert service.active_run("chat-1")["status"] == "completed"
    assert service.current("run-1")["agentId"] == "eclipse-agent"
    assert service.describe()["provider"] == "AgentMemoryStore"


def test_policy_service_matches_the_authority():
    policy = PolicyService()
    assert policy.autonomy_profiles() == tuple(AUTONOMY_PROFILES)
    assert policy.normalize_autonomy("plan_only") == "plan_only"
    assert policy.normalize_autonomy("nonsense") is None
    declared = ("write_file", "run_command", "read_file")
    assert policy.allowed_tools("plan_only", declared) == allowed_tools("plan_only", declared)
    assert policy.code_grants("edit_verify") == code_grants("edit_verify")
    assert policy.describe()["autonomyProfiles"] == list(AUTONOMY_PROFILES)


def test_surface_autonomy_narrowing_is_identical_through_the_runtime():
    registry = ToolRegistry()
    register_code_tools(registry)
    from harness.kernel.services import runtime as runtime_module

    off = resolve_surface_detail(CODE_AGENT, registry, {}, "plan_only")
    runtime = KernelRuntime(enabled=True).configure(registry)
    original = runtime_module.kernel_runtime
    try:
        runtime_module.kernel_runtime = runtime
        on = resolve_surface_detail(CODE_AGENT, registry, {}, "plan_only")
    finally:
        runtime_module.kernel_runtime = original
    assert on.allowed == off.allowed
    assert on.policyRemoved == off.policyRemoved


async def test_policy_and_sessions_are_kernel_services_a_row_can_inject():
    module = types.ModuleType("kernel_policy_fixtures")

    def reader(ctx, config):
        sessions = ctx.require("sessions")
        policy = ctx.require("policy")
        ctx.provide("policy_view", {
            "profiles": list(policy.autonomy_profiles()),
            "session": sessions.get("chat-1"),
        })

    module.reader = reader
    sys.modules["kernel_policy_fixtures"] = module
    from harness.kernel.composition import Composition, Row

    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    runtime.attach_sessions(_Store())
    try:
        await runtime.supervisor.apply(Composition(rows=[
            Row(id="reader", name="kernel_policy_fixtures:reader"),
        ]))
        view = runtime.kernel.root.require("policy_view")
        assert "plan_only" in view["profiles"]
        assert view["session"]["title"] == "First"
    finally:
        sys.modules.pop("kernel_policy_fixtures", None)
        await runtime.kernel.shutdown()


async def test_runtime_provides_policy_and_sessions_with_attached_store():
    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    assert runtime.kernel.root.require("policy") is runtime.policy_authority
    assert runtime.kernel.root.require("sessions").available is False
    runtime.attach_sessions(_Store())
    assert runtime.kernel.root.require("sessions").get("chat-1")["title"] == "First"
    payload = runtime.inspect()
    assert "plan_only" in payload["policy"]["autonomyProfiles"]
    assert payload["sessions"]["available"] is True
    await runtime.kernel.shutdown()


def test_boot_wiring_binds_the_model_provider_registry(monkeypatch):
    from harness.kernel.services import wiring

    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    captured = {}
    monkeypatch.setattr(runtime, "attach_models", lambda providers: captured.setdefault("providers", providers))
    service = types.SimpleNamespace(kernel_runtime=runtime)
    wiring.install_kernel_surfaces(service)
    assert "providers" in captured


def test_kernel_default_mode_is_on_with_an_explicit_off_switch(monkeypatch):
    monkeypatch.delenv("ECLIPSE_KERNEL", raising=False)
    assert KernelRuntime().mode == "on"
    assert KernelRuntime().enabled is True
    monkeypatch.setenv("ECLIPSE_KERNEL", "off")
    assert KernelRuntime().mode == "off"
    assert KernelRuntime().enabled is False
