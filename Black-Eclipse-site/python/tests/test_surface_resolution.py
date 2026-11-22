"""Round 3 / W2 (ADR-H280) - a surface EXTENDS the agent's declared tools.

The defect this file is the gate for: a chat coding turn used to REPLACE the
finance agent's whole allowlist with the code profile. Measured against a real
`AgenticService` on 2026-10-20, that silently cost the model **441 tools** -
the whole task/plan family (`create_plan`, `create_task`, `read_plan`, ...),
the whole web/research family (`web_search`, `web_extract`, `research_query`,
...), the unified search family (`semantic_search`, `hybrid_search`, ...), the
truthful-capability diagnostics (`get_capability_matrix`) and the
response-contract tools (`response_block_schema`, `response_validate`).

Two of those losses were not merely inconvenient. `response_block_schema` is
what `graph.execute_run` checks before injecting the live response-schema digest
and the response-mode plan into the system prompt, so a coding turn produced a
DEGRADED structured response with no error. And the stated intent in
`agents.py` was always ADDITIVE - the code did the opposite.

The rules this file asserts:

1. A surface never removes a base tool. A tool may leave the surface only
   through the autonomy profile or a disabled store, and BOTH are named.
   Anything a resolution cannot explain is reported as `removed`, and it is
   always empty.
2. Both halves are narrowed through the ONE policy authority, so the autonomy
   profile still removes every write/execute/external tool it bans.
3. The surface's marginal schema cost is MEASURED and ratcheted, not assumed.
4. The resolution is PUBLISHED, so a missing tool is always explainable.
"""

from __future__ import annotations

import json
import os
import tempfile

from harness.agents import CHAT_CODE_TOOLS, CODE_AGENT, ECLIPSE_AGENT
from harness.code.policy import EXTERNAL_ACTION_TOOLS, WRITE_TOOLS
from harness.code.tools import register_code_tools
from harness.research.web_tools import register_research_engine_tools
from harness.surface import (
    MAX_SURFACE_ADDED_SCHEMA_CHARS,
    SURFACE_BASE,
    SURFACE_CHAT_CODE,
    resolve_surface_detail,
)
from harness.tasks.tools import register_task_tools
from harness.tools import ToolRegistry

# The tools the defect actually cost a coding turn. Each one is a tool the
# model is expected to be able to call on a coding turn and could not.
LOST_ON_A_CODING_TURN = (
    # the task/plan runtime
    "create_plan", "create_task", "read_plan", "run_plan", "update_task",
    "complete_task", "start_task", "list_tasks",
    # the web/research family
    "web_search", "web_extract", "research_query", "claim_verify",
    # the unified retrieval/search family
    "semantic_search", "hybrid_search", "search_evidence", "search_documents",
    # the response contract
    "response_block_schema", "response_validate",
)


def _registry() -> ToolRegistry:
    """A registry with the code, task and research surfaces registered."""
    reg = ToolRegistry()
    register_code_tools(reg)
    register_task_tools(reg)
    register_research_engine_tools(reg)
    return reg


def _resolution(agent=ECLIPSE_AGENT, autonomy: str | None = "edit_verify",
                settings: dict | None = None):
    return resolve_surface_detail(agent, _registry(), settings or {}, autonomy)


def test_a_task_run_does_not_get_the_chat_code_surface():
    """A task-dispatched run keeps the agent's own surface.

    The chat code profile is for a coding REQUEST. A finance goal turn also
    carries an autonomy, but must keep `get_historical_prices`/`analysis_run`
    instead of being replaced by the code subset.
    """
    res = resolve_surface_detail(
        ECLIPSE_AGENT, _registry(), {}, "plan_only", apply_code_surface=False,
    )
    assert res.surfaceId == SURFACE_BASE
    assert res.surface is None
    assert "web_search" in (res.allowed or set())


# ── the rule: a surface EXTENDS, it never REPLACES ─────────────────────────


def test_a_coding_turn_does_not_lose_the_finance_agent_tools() -> None:
    """The defect: 441 base tools were dropped on a chat coding turn."""
    resolution = _resolution(autonomy="edit_verify")
    allowed = set(resolution.allowed or ())
    # Every tool the defect cost must be back.
    for name in LOST_ON_A_CODING_TURN:
        if name in _registry().names():
            assert name in allowed, f"{name} is missing from a coding turn"


def test_the_base_survives_the_surface_except_for_named_reasons() -> None:
    """A base tool may leave only through the profile or a disabled store.

    The defect dropped 441 base tools and named NONE of them. This asserts the
    opposite: whatever the base loses is accounted for.
    """
    resolution = _resolution(autonomy="edit_verify")
    base = set(resolution.base or ())
    allowed = set(resolution.allowed or ())
    unexplained = base - allowed - set(resolution.policyRemoved) - set(resolution.disabled)
    assert unexplained == set(), f"the surface dropped {len(unexplained)} base tools silently"
    # And the resolution says so itself.
    assert resolution.removed == ()


def test_the_surface_removes_nothing_from_the_base() -> None:
    """The invariant, asserted directly: `removed` is EMPTY.

    A tool may leave the surface only through the autonomy profile or a
    disabled store, and BOTH are named. Anything the resolution cannot
    explain is a defect, not a report - and it is reported as `removed`.
    """
    resolution = _resolution(autonomy="edit_verify")
    assert resolution.surfaceId == SURFACE_CHAT_CODE
    assert resolution.removed == ()
    # What the base did lose is named, not silent.
    assert set(resolution.base or ()) - set(resolution.allowed or ()) <= set(
        resolution.disabled
    ) | set(resolution.policyRemoved)


def test_the_surface_adds_the_tools_the_base_lacks() -> None:
    from harness.code.policy import allowed_tools

    resolution = _resolution(autonomy="edit_verify")
    assert resolution.added, "the surface added nothing"
    # The added set is exactly the surface's tools the base lacked, AFTER the
    # ONE policy authority narrowed the surface - so a tool the profile bans
    # (the cluster/infra/cloud mutations) is in neither half.
    # Gap #6: the chat code half is ALSO narrowed to the coding preset, so the
    # added set is the preset-narrowed surface minus the base. The important
    # property is unchanged: the base's task/research/response tools are not lost.
    from harness.code.policy import coding_surface

    surface_narrowed = set(allowed_tools("edit_verify", tuple(CHAT_CODE_TOOLS)))
    surface_narrowed &= set(coding_surface("edit_verify"))
    expected = surface_narrowed - set(resolution.base or ())
    assert set(resolution.added) == expected
    for name in LOST_ON_A_CODING_TURN:
        assert name in set(resolution.allowed or ()), name
    # Every added tool is a name the surface really declares.
    assert set(resolution.added) <= set(CHAT_CODE_TOOLS)


def test_an_ordinary_chat_turn_is_unchanged() -> None:
    """No autonomy, no surface: the ordinary finance surface, exactly as before."""
    resolution = _resolution(autonomy=None)
    assert resolution.surfaceId == SURFACE_BASE
    assert resolution.added == ()
    assert resolution.removed == ()
    allowed = set(resolution.allowed or ())
    assert "market_get_quote" in allowed
    # A finance turn still binds no code tool.
    assert "code_edit" not in allowed
    assert "command_run" not in allowed


# ── the rule: BOTH halves go through the ONE policy authority ──────────────


def test_plan_only_removes_the_write_tools_from_both_halves() -> None:
    resolution = _resolution(autonomy="plan_only")
    allowed = set(resolution.allowed or ())
    assert "read_file" in allowed
    assert not (set(WRITE_TOOLS) & allowed)
    # And the base's write tools are gone too, not just the surface's.
    assert "write_file" not in allowed
    assert "patch_file" not in allowed
    # The TASK runtime is a planner, not a code writer: creating a task is
    # what `plan_only` is FOR, so it stays. That is the policy's call, and the
    # policy is the ONE authority for it.
    assert "create_task" in allowed


def test_edit_verify_keeps_writes_but_never_an_external_action() -> None:
    resolution = _resolution(autonomy="edit_verify")
    allowed = set(resolution.allowed or ())
    assert "code_edit" in allowed
    assert "command_run" in allowed
    assert not (set(EXTERNAL_ACTION_TOOLS) & allowed)


def test_an_unknown_autonomy_fails_closed() -> None:
    resolution = _resolution(autonomy="nonsense")
    allowed = set(resolution.allowed or ())
    assert not (set(WRITE_TOOLS) & allowed)
    assert not (set(EXTERNAL_ACTION_TOOLS) & allowed)


def test_the_policy_removed_tools_are_named() -> None:
    """A tool missing because of the autonomy profile is NAMED, not silent."""
    resolution = _resolution(autonomy="plan_only")
    assert resolution.policyRemoved, "the policy removed tools and said nothing"
    # They are named because the profile removed them - not because the surface did.
    assert not (set(resolution.policyRemoved) & set(resolution.added))


def test_a_disabled_store_names_the_tools_it_removed() -> None:
    resolution = _resolution(autonomy="edit_verify", settings={"memory_enabled": False})
    assert "memory" in resolution.disabled
    allowed = set(resolution.allowed or ())
    assert "memory" not in allowed


# ── the rule: the marginal cost is MEASURED and ratcheted ──────────────────


def test_the_added_schema_cost_is_measured() -> None:
    resolution = _resolution(autonomy="edit_verify")
    assert resolution.addedSchemaChars > 0, "the surface's cost was not measured"
    assert resolution.boundTools > 0


def test_the_added_schema_cost_stays_under_the_ratchet() -> None:
    """A ratchet, not a promise: the surface cannot silently grow."""
    resolution = _resolution(autonomy="edit_verify")
    assert resolution.addedSchemaChars <= MAX_SURFACE_ADDED_SCHEMA_CHARS, (
        f"the code surface adds {resolution.addedSchemaChars} schema characters, "
        f"over the {MAX_SURFACE_ADDED_SCHEMA_CHARS} ceiling"
    )


# ── the rule: the resolution is PUBLISHED ──────────────────────────────────


def test_the_resolution_publishes_what_it_added_and_lost() -> None:
    resolution = _resolution(autonomy="edit_verify")
    blob = resolution.to_dict()
    assert blob["surfaceId"] == SURFACE_CHAT_CODE
    assert blob["applied"] is True
    assert blob["addedTools"] == len(resolution.added)
    assert blob["addedSchemaChars"] == resolution.addedSchemaChars
    assert blob["boundTools"] == resolution.boundTools
    assert blob["removed"] == []
    assert blob["maxAddedSchemaChars"] == MAX_SURFACE_ADDED_SCHEMA_CHARS
    # The published blob is JSON-serialisable (it is persisted on the run row).
    json.dumps(blob)


def test_a_base_run_publishes_that_no_surface_was_applied() -> None:
    blob = _resolution(autonomy=None).to_dict()
    assert blob["surfaceId"] == SURFACE_BASE
    assert blob["applied"] is False
    assert blob["added"] == []


def test_the_resolution_summarises_in_one_honest_line() -> None:
    summary = _resolution(autonomy="edit_verify").summary()
    assert "surface=chat_code" in summary
    assert "added=" in summary


# ── the code-agent preset is unaffected ────────────────────────────────────


def test_the_code_agent_preset_keeps_its_own_closed_surface() -> None:
    """The union is for a surface ON a base. The code agent HAS no base."""
    resolution = resolve_surface_detail(CODE_AGENT, _registry(), {}, "edit_verify")
    allowed = set(resolution.allowed or ())
    assert "code_edit" in allowed
    # The code agent never reached a finance tool and still does not.
    assert "market_get_quote" not in allowed


# ── the rule: the resolution is PERSISTED on the run ───────────────────────


def test_the_surface_is_persisted_on_the_run_row(tmp_path) -> None:
    """The published blob must survive a round trip through the run row.

    The run row is the only place a reader can learn what a run's surface was.
    A blob that only exists in memory is a claim, not a record.
    """
    from harness.persistence import AgentMemoryStore

    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    store.create_run("run-1", "eclipse-agent", {"query": "x"})
    resolution = _resolution(autonomy="edit_verify")
    store.update_run(
        "run-1",
        surface=resolution.to_dict(),
        autonomy=resolution.autonomy,
    )
    row = store.get_run("run-1")
    assert row is not None
    assert row["surface"]["surfaceId"] == SURFACE_CHAT_CODE
    assert row["surface"]["addedTools"] == len(resolution.added)
    assert row["surface"]["removed"] == []
    assert row["autonomy"] == "edit_verify"


def test_a_fresh_database_carries_the_run_columns_the_answer_seam_writes(tmp_path) -> None:
    """The `codeVerification` / `surface` columns must exist on a FRESH database.

    `agent-memory.db` is gitignored, so every new clone gets an empty one. The
    answer seam writes `codeVerification` UNCONDITIONALLY, and that column was
    in neither the CREATE TABLE nor the migration list - so a fresh install
    raised `no such column: codeVerification` and every run crashed AFTER the
    model had already produced its answer. The live repository masked it
    because its database predates the column.
    """
    from harness.persistence import AgentMemoryStore

    store = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    try:
        columns = {
            row["name"]
            for row in store._db.execute("PRAGMA table_info(HarnessRun)").fetchall()
        }
        for name in ("codeVerification", "surface", "autonomy", "workspaceId"):
            assert name in columns, f"HarnessRun.{name} is missing on a fresh database"
        store.create_run("run-2", "eclipse-agent", {"query": "x"})
        # The write the answer seam performs, on a fresh database.
        store.update_run("run-2", codeVerification={"records": [], "claims": []})
        assert store.get_run("run-2")["codeVerification"] == {"records": [], "claims": []}
    finally:
        store.close()
