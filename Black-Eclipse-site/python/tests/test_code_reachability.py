"""Phase 14 G3 — a coding request in chat reaches a WRITE tool.

This is the gate the user's complaint is about: assert the BOUND tool schemas,
not the prompt. A `code_change` turn (autonomy `edit_verify`) binds
`workspace_patch_apply` and `command_run`; a finance turn (no autonomy) binds no
code tool; a `code_question` turn (autonomy `plan_only`) binds read tools and no
write tool. External actions are never bound.
"""

from __future__ import annotations

from harness.agents import CHAT_CODE_TOOLS, ECLIPSE_AGENT
from harness.code.policy import EXTERNAL_ACTION_TOOLS, WRITE_TOOLS
from harness.code.tools import register_code_tools
from harness.graph import resolve_surface
from harness.tools import ToolRegistry

def _allowed(reg: ToolRegistry, autonomy: str | None) -> set[str]:
    allowed, _prompt = resolve_surface(ECLIPSE_AGENT, reg, {}, autonomy)
    return set(allowed or ())

# The read half of the coding preset. `workspace_read`/`workspace_list`/
# `workspace_diff` are the older workspace tools and are now OUTSIDE the preset;
# `read_file`/`list_directory`/`search_text` are the replacements, and
# `workspace_status`/`workspace_docs` remain for project context.
READ_TOOLS = {
    "read_file", "list_directory", "search_text", "search_files",
    "search_symbols", "workspace_status", "workspace_docs",
}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_code_tools(reg)
    return reg


def _bound(reg: ToolRegistry, autonomy: str | None) -> set[str]:
    """The tool schemas the model would actually see for this turn.

    Uses the REAL resolver (`resolve_surface`), which applies the chat code
    profile inside the same place the run's surface is computed — so this asserts
    the bound set, not the prompt.
    """
    allowed, _prompt = resolve_surface(ECLIPSE_AGENT, reg, {}, autonomy)
    names = {s["name"] for s in reg.schemas(allowed)}
    return names


def test_a_code_change_turn_binds_write_and_execute() -> None:
    reg = _registry()
    bound = _bound(reg, "edit_verify")
    assert "code_edit" in bound
    assert "command_run" in bound
    # Undo is the checkpoint path now (the legacy `workspace_revert` is outside
    # the coding preset), and the local commit path is allowed (the git engine is
    # not part of this test's registry, so assert the allowlist, not schemas).
    assert {"checkpoint_undo_turn", "checkpoint_restore"} <= bound
    assert {"git_stage", "git_commit"} <= _allowed(reg, "edit_verify")
    # Every bound tool must be registered (no schema for a nonexistent tool).
    assert bound <= set(reg.names())


def test_a_code_change_turn_never_binds_an_external_action() -> None:
    reg = _registry()
    bound = _bound(reg, "edit_verify")
    assert not (set(EXTERNAL_ACTION_TOOLS) & bound)


def test_a_finance_turn_binds_no_change_or_execute_tool() -> None:
    reg = _registry()
    # No autonomy: the ordinary chat path, exactly as before this phase.
    allowed = _allowed(reg, None)
    # The specific write/execute tools the phase opens are NOT bound for a
    # finance turn. (`scaffold_kit` was already an ECLIPSE_AGENT tool before this
    # phase and refuses without a workspace-bound profile; it is unchanged here.)
    assert "code_edit" not in allowed
    assert "workspace_revert" not in allowed
    assert "command_run" not in allowed
    assert not (READ_TOOLS & allowed)
    # The finance agent's own tools are still present.
    assert "market_get_quote" in allowed
    assert "research_search" in allowed


def test_a_code_question_turn_has_read_tools_and_no_write_tool() -> None:
    reg = _registry()
    bound = _bound(reg, "plan_only")
    assert READ_TOOLS <= bound
    assert not (set(WRITE_TOOLS) & bound)
    assert not (set(EXTERNAL_ACTION_TOOLS) & bound)


def test_an_unknown_autonomy_binds_no_write_tool() -> None:
    # Fail-closed: a value the front door would reject still cannot widen.
    reg = _registry()
    bound = _bound(reg, "nonsense")
    assert not (set(WRITE_TOOLS) & bound)
    assert not (set(EXTERNAL_ACTION_TOOLS) & bound)


def test_the_external_actions_are_not_in_the_chat_profile_at_all() -> None:
    for name in EXTERNAL_ACTION_TOOLS:
        assert name not in CHAT_CODE_TOOLS


# ── the chat capability view (§6.4) ──────────────────────────────────────────


def test_capability_view_derives_from_the_verification_blob() -> None:
    from harness.code.capability import capability_from_verification

    # An ordinary turn: no blob, or a blob with no chat code profile → no view.
    assert capability_from_verification(None) is None
    assert capability_from_verification({}) is None
    assert capability_from_verification({"records": [], "claims": []}) is None
    # `approve_external` is not a profile chat ever sends → no view.
    assert capability_from_verification({"autonomy": "approve_external"}) is None

    view = capability_from_verification({
        "autonomy": "edit_verify",
        "records": [{"commandExecutionId": "c1", "commandClass": "unit_test", "exitCode": 0}],
        "claims": [],
    })
    assert view is not None
    assert view["kind"] == "code"
    assert view["profile"] == "edit_verify"
    assert view["headline"] == "code.chat.headline.editVerify"
    # The verification sub-object never carries the profile keys.
    assert "autonomy" not in view["verification"]
    assert "workspaceId" not in view["verification"]


def test_capability_view_for_a_read_only_turn_is_plan_only() -> None:
    from harness.code.capability import capability_from_verification

    view = capability_from_verification({"autonomy": "plan_only", "records": [], "claims": []})
    assert view is not None
    assert view["profile"] == "plan_only"
    assert view["headline"] == "code.chat.headline.planOnly"
