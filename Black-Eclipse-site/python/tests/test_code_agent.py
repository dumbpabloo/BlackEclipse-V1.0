"""Phase 5 â€” the code-agent allowlist IS the autonomy boundary.

Structural, not prompt-based: a write/external tool the profile does not permit is
REMOVED before the model sees the schema, so the registry returns TOOL_FORBIDDEN.
"""

from __future__ import annotations

from harness.agents import CODE_AGENT, CODE_AGENT_TOOLS, ECLIPSE_AGENT, CODE_NAVIGATOR
from harness.automation.tools import TOOL_NAMES as AUTOMATION_TOOLS
from harness.code import policy
from harness.code.policy import EXTERNAL_ACTION_TOOLS, WRITE_TOOLS, allowed_tools
from harness.graph import effective_allowlist
from harness.tools import ToolRegistry
from harness.code.tools import register_code_tools

FINANCE_TOOL_PREFIXES = ("market_", "fundamentals_", "research_", "macro_", "get_")
CODE_TOOL_PREFIXES = ("workspace_", "patch_", "command_", "code_")


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_code_tools(reg)
    return reg


# â”€â”€ the policy authority is the only narrowing seam â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_plan_only_removes_write_and_external():
    declared = ("workspace_read", "code_edit", "command_run", "code_commit")
    result = allowed_tools("plan_only", declared)
    assert "code_edit" not in result
    assert "command_run" not in result
    assert "code_commit" not in result
    assert "workspace_read" in result


def test_edit_verify_has_write_but_not_external():
    declared = tuple(WRITE_TOOLS) + tuple(EXTERNAL_ACTION_TOOLS) + ("workspace_read",)
    result = allowed_tools("edit_verify", declared)
    assert set(WRITE_TOOLS) <= set(result)
    assert not (set(EXTERNAL_ACTION_TOOLS) & set(result))


def test_approve_external_has_everything():
    declared = tuple(WRITE_TOOLS) + tuple(EXTERNAL_ACTION_TOOLS)
    result = allowed_tools("approve_external", declared)
    assert set(declared) <= set(result)


def test_unknown_autonomy_is_read_only_subset():
    declared = tuple(WRITE_TOOLS) + tuple(EXTERNAL_ACTION_TOOLS) + ("workspace_read",)
    result = allowed_tools("nonsense", declared)
    assert not (set(WRITE_TOOLS) & set(result))
    assert not (set(EXTERNAL_ACTION_TOOLS) & set(result))
    assert "workspace_read" in result


# â”€â”€ the code-agent preset â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_code_agent_has_no_finance_tool():
    # The web research engine tools share the `research_` prefix with the finance
    # retrieval tools but are a READ-ONLY, provider-agnostic research capability
    # (no finance data permission of their own), so they are exempt here.
    from harness.agents import WEB_RESEARCH_TOOLS
    from harness.tasks.tools import TASK_TOOLS

    for name in CODE_AGENT.tools:
        if name in WEB_RESEARCH_TOOLS or name in TASK_TOOLS:
            continue
        assert not name.startswith(FINANCE_TOOL_PREFIXES), name
    # `delegate` (Phase 7) and `analysis_run` (Phase 8) are shared ORCHESTRATION /
    # COMPUTE capabilities, not finance tools: neither holds a data permission of
    # its own, and both are gated (delegation by narrowing, analysis by being off
    # per run). The Application Runtime tools (`runtime_*`/`package_*`/â€¦ and the
    # orchestration/infra/cloud tools) are also shared DevOps capabilities with no
    # finance data permission.
    from harness.execution.application.tools import ALL_TOOL_NAMES as APP_TOOLS
    from harness.agents import (
        BROWSER_TOOLS,
        COMPUTER_TOOLS,
        DOCUMENT_TOOLS,
        SEARCH_TOOLS,
        SOURCE_TOOLS,
        VISION_TOOLS,
        WEB_RESEARCH_TOOLS,
    )

    app_runtime = set(APP_TOOLS)
    # Browser, Computer Use, Vision and Document Intelligence tools are SHARED
    # interactive/observation/understanding capabilities (like delegate/analysis),
    # not finance tools: they carry no finance data permission of their own and
    # their effectful halves are narrowed by the run's autonomy profile (browser
    # policy engine / computer policy engine + finance firewall). The web research
    # engine tools are likewise shared READ-ONLY capabilities, as are the unified
    # search tools (read-only retrieval over the shared index).
    assert not (set(CODE_AGENT.tools) & set(ECLIPSE_AGENT.tools) - {
        "memory", "skill_list", "skill_view", "session_search", "session_open",
        "delegate", "analysis_run", "scaffold_kit",
        # W7 delegation lifecycle: shared, not finance tools (like `delegate`).
        "delegate_async", "delegate_status", "delegate_wait", "delegate_result",
        "delegate_cancel", "delegate_steer",
        # Round 5 / C3: shared discovery + full-output pager + confined code,
        # not finance tools.
        "tool_search", "load_tool_schema", "tool_invoke", "read_tool_output", "tool_code",
        "spawn", "board_write", "board_read", "board_search",
        "capability_request", "capability_build", "capability_status",
        "capability_external", "capability_adapter",
        "capability_activate", "capability_rollback", "capability_remove",
        "python_env_install", "python_env_remove", "python_env_list", "python_env_status",
    } - app_runtime - set(BROWSER_TOOLS) - set(COMPUTER_TOOLS) - set(VISION_TOOLS)
        - set(WEB_RESEARCH_TOOLS) - set(DOCUMENT_TOOLS) - set(SEARCH_TOOLS)
        - set(SOURCE_TOOLS) - set(TASK_TOOLS) - set(AUTOMATION_TOOLS))


def test_code_agent_does_not_allow_mcp():
    assert getattr(CODE_AGENT, "allowMcpTools", False) is False


def test_finance_agent_unchanged_no_code_tool():
    for name in ECLIPSE_AGENT.tools:
        assert not name.startswith(CODE_TOOL_PREFIXES) or name.startswith(
            ("runtime_", "package_", "container_", "compose_", "environment_", "port_", "health_",
             "k8s_", "helm_", "kustomize_", "terraform_", "cloud_", "deploy_")), name
    # The finance agent's shared capability sets are declared EXACTLY ONCE and are
    # disjoint (a tool belongs to one set), and no tool name is duplicated. This is
    # the durable invariant; the finance base list itself legitimately grows (new
    # providers/market tools), so a hardcoded total would be a false gate.
    from harness.agents import (  # noqa: PLC0415
        BROWSER_TOOLS,
        COMPUTER_TOOLS,
        VISION_TOOLS,
        WEB_RESEARCH_TOOLS,
    )

    shared = set(BROWSER_TOOLS) | set(COMPUTER_TOOLS) | set(VISION_TOOLS) | set(WEB_RESEARCH_TOOLS)
    assert (
        len(BROWSER_TOOLS) + len(COMPUTER_TOOLS) + len(VISION_TOOLS) + len(WEB_RESEARCH_TOOLS)
        == len(shared)
    ), "a shared capability set overlaps another"
    assert shared <= set(ECLIPSE_AGENT.tools), "a shared capability set is not fully declared"
    assert len(ECLIPSE_AGENT.tools) == len(set(ECLIPSE_AGENT.tools)), "a tool is declared twice"
    assert "runtime_inspect" in ECLIPSE_AGENT.tools
    assert "runtime_start" not in ECLIPSE_AGENT.tools
    assert "k8s_get" in ECLIPSE_AGENT.tools
    assert "k8s_apply" not in ECLIPSE_AGENT.tools
    assert "delegate" in ECLIPSE_AGENT.tools
    assert "analysis_run" in ECLIPSE_AGENT.tools
    assert "scaffold_kit" in ECLIPSE_AGENT.tools
    assert "bot_create" in ECLIPSE_AGENT.tools
    # A scheduled-agent request must not dead-end in a coding turn either: the
    # same bot surface (and its bot_schedule approval) stays reachable.
    assert "bot_create" in CODE_AGENT_TOOLS
    assert ECLIPSE_AGENT.allowMcpTools is True


def test_code_agent_registered_as_a_preset():
    from harness.agents import AGENT_PRESETS

    assert AGENT_PRESETS["code-agent"] is CODE_AGENT


# â”€â”€ the allowlist is structural â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_plan_only_allowlist_has_no_write_tool():
    allowed = effective_allowlist(CODE_AGENT, _registry(), "plan_only")
    assert allowed is not None
    assert not (set(allowed) & set(WRITE_TOOLS))
    assert not (set(allowed) & set(EXTERNAL_ACTION_TOOLS))


def test_edit_verify_allowlist_has_write_no_external():
    allowed = effective_allowlist(CODE_AGENT, _registry(), "edit_verify")
    # Gap #6: the coding preset keeps the CORE edit surface and drops the
    # non-coding runtime/container/cloud/orchestration/package/browser/computer
    # tools. A write tool outside the preset is not silently present.
    for name in ("code_edit", "write_file", "create_file", "apply_diff", "code_get_diagnostics", "code_todo"):
        assert name in allowed, name
    assert "container_run" not in allowed
    assert "runtime_start" not in allowed
    assert "package_install" not in allowed
    assert not (set(allowed) & set(EXTERNAL_ACTION_TOOLS))


def test_approve_external_allowlist_has_external():
    allowed = effective_allowlist(CODE_AGENT, _registry(), "approve_external")
    assert set(EXTERNAL_ACTION_TOOLS) <= set(allowed)


def test_finance_agent_allowlist_is_not_narrowed_without_autonomy():
    allowed = effective_allowlist(ECLIPSE_AGENT, _registry(), None)
    assert set(ECLIPSE_AGENT.tools) <= set(allowed)


def test_registry_refuses_a_write_tool_absent_from_plan_only():
    import asyncio

    from harness.tools import ToolContext

    registry = _registry()
    allowed = effective_allowlist(CODE_AGENT, registry, "plan_only")
    ctx = ToolContext(run_id="r", log=lambda *_: None)
    result = asyncio.run(registry.execute(
        "code_edit", "c1",
        {"path": "x.py", "oldText": "a", "newText": "b"},
        ctx, allowed=allowed,
    ))
    assert result.ok is False
    assert result.error is not None and result.error.code == "TOOL_FORBIDDEN"
