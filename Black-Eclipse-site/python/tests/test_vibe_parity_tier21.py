"""Branch parity, twenty-first tier: the manual research-task board's rules.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/src/pixel_agents/office.py`).

Eclipse's board (`spawn/board.py`) is a generic key/value board — write, read, search —
with no notion of a *task* that is assigned, completed, and whose load is derived. The
reference's office is exactly that, and its three invariants are the ones a naive board
gets wrong:

* a **completed** task cannot be reassigned;
* a **re-completion preserves the first recorded result** (a retry or duplicate tool call
  must not rewrite the answer that was actually produced);
* an agent's **load is derived** from the tasks, never stored, so it cannot drift.

Ported as pure rules (`spawn/office_rules.py`); the SQLite layer stays out of scope and
is said so.
"""

from __future__ import annotations

import pytest

from harness.spawn import office_rules as O


AGENTS = [
    {"id": "agent-macro", "name": "Macro", "role": "macro"},
    {"id": "agent-equity", "name": "Equity", "role": "equity"},
]


def _task(**overrides):
    base = {"id": "t1", "title": "Read the 10-K", "workstream": "equity",
            "priority": "normal", "status": "queued", "assigned_to": None,
            "result": ""}
    base.update(overrides)
    return base


# -- vocabularies and limits ---------------------------------------------

def test_the_vocabularies_are_the_references():
    assert O.PRIORITIES == ("low", "normal", "high")
    assert O.RANKS == ("Operator", "Senior", "Lead")


def test_a_bad_priority_or_rank_is_refused():
    with pytest.raises(O.BoardError):
        O.validate_priority("urgent")
    with pytest.raises(O.BoardError):
        O.validate_rank("Chief")
    assert O.validate_priority("HIGH") == "high"
    assert O.validate_rank("Lead") == "Lead"


def test_the_field_limits_are_the_references():
    assert (O.MAX_ID_CHARS, O.MAX_TITLE_CHARS) == (100, 200)
    assert (O.MAX_DESCRIPTION_CHARS, O.MAX_RESULT_CHARS) == (2_000, 2_000)


def test_a_title_longer_than_two_hundred_characters_is_refused():
    with pytest.raises(O.BoardError):
        O.validate_text("x" * 201, "title", maximum=O.MAX_TITLE_CHARS)
    assert O.validate_text("  ok  ", "title", maximum=50) == "ok"


def test_a_description_may_be_empty_but_a_title_may_not():
    assert O.validate_text("", "description", maximum=50, empty=True) == ""
    with pytest.raises(O.BoardError):
        O.validate_text("   ", "title", maximum=50)


# -- assignment ----------------------------------------------------------

def test_an_explicit_agent_is_assigned_and_the_task_becomes_assigned():
    updated = O.assign(_task(), AGENTS, agent_id="agent-macro", now="2026-01-01")
    assert updated["assigned_to"] == "agent-macro"
    assert updated["status"] == "assigned"
    assert updated["updated_at"] == "2026-01-01"


def test_auto_assign_picks_the_agent_whose_role_matches_the_workstream():
    assert O.assign(_task(), AGENTS)["assigned_to"] == "agent-equity"


def test_auto_assign_with_no_matching_role_is_refused_actionably():
    with pytest.raises(O.BoardError) as exc:
        O.assign(_task(workstream="credit"), AGENTS)
    assert "choose an agent explicitly" in str(exc.value)


def test_an_unknown_agent_id_is_refused():
    with pytest.raises(O.BoardError):
        O.assign(_task(), AGENTS, agent_id="agent-ghost")


def test_a_COMPLETED_task_cannot_be_assigned():
    with pytest.raises(O.BoardError) as exc:
        O.assign(_task(status="completed", assigned_to="agent-equity"), AGENTS,
                 agent_id="agent-macro")
    assert "completed tasks cannot be assigned" in str(exc.value)


def test_assignment_does_not_mutate_the_input_task():
    task = _task()
    O.assign(task, AGENTS, agent_id="agent-macro")
    assert task["assigned_to"] is None and task["status"] == "queued"


def test_reassignment_moves_an_unfinished_task_without_touching_others():
    first = O.assign(_task(), AGENTS, agent_id="agent-equity")
    second = O.assign(first, AGENTS, agent_id="agent-macro")
    assert second["assigned_to"] == "agent-macro"


# -- completion ----------------------------------------------------------

def test_completion_records_the_result_and_the_status():
    done = O.complete(_task(status="assigned"), "EPS grew 12%", now="2026-01-02")
    assert done["status"] == "completed"
    assert done["result"] == "EPS grew 12%"
    assert done["updated_at"] == "2026-01-02"


def test_a_SECOND_completion_preserves_the_first_result():
    first = O.complete(_task(), "original finding")
    second = O.complete(first, "a retry that must not overwrite")
    assert second["result"] == "original finding"
    assert second["status"] == "completed"


def test_an_over_long_result_is_refused_rather_than_truncated_silently():
    with pytest.raises(O.BoardError):
        O.complete(_task(), "x" * (O.MAX_RESULT_CHARS + 1))


def test_an_empty_result_is_refused():
    with pytest.raises(O.BoardError):
        O.complete(_task(), "   ")


# -- the derived dashboard ----------------------------------------------

def test_load_is_derived_from_assigned_unfinished_tasks():
    tasks = [
        _task(id="t1", status="assigned", assigned_to="agent-macro"),
        _task(id="t2", status="queued", assigned_to="agent-macro"),
        _task(id="t3", status="completed", assigned_to="agent-macro"),
        _task(id="t4", status="assigned", assigned_to="agent-equity"),
    ]
    board = O.dashboard(AGENTS, tasks)
    by_id = {agent["id"]: agent for agent in board["agents"]}
    assert by_id["agent-macro"]["load"] == 2 and by_id["agent-macro"]["status"] == "busy"
    assert by_id["agent-equity"]["load"] == 1


def test_an_idle_agent_reports_idle_with_zero_load():
    board = O.dashboard(AGENTS, [])
    assert [a["status"] for a in board["agents"]] == ["idle", "idle"]


def test_the_counts_are_consistent_with_each_other():
    tasks = [_task(id="a", status="completed"), _task(id="b", status="queued")]
    board = O.dashboard(AGENTS, tasks)
    assert board["taskCount"] == 2
    assert board["completed"] == 1
    assert board["pending"] == 1
    assert board["completed"] + board["pending"] == board["taskCount"]


def test_the_board_labels_itself_as_manual_planning():
    """Assignment is organizational metadata, never trading authority."""
    assert O.dashboard(AGENTS, [])["mode"] == "manual_planning"


def test_the_dashboard_does_not_mutate_its_inputs():
    agents = [dict(agent) for agent in AGENTS]
    tasks = [_task(status="assigned", assigned_to="agent-macro")]
    O.dashboard(agents, tasks)
    assert "load" not in agents[0]
    assert "status" in tasks[0] and tasks[0]["status"] == "assigned"
