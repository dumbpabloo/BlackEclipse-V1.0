"""Branch parity, twenty-third tier: run-state honesty and slash-command matching.

References fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):
`agent/src/core/state.py` and `agent/cli/commands/slash_router.py`.

Two contracts, both pure and both easy to get subtly wrong:

* **run state** — three terminal statuses, with `cancelled` kept distinct from `failed`
  (a user stop is not an outage; conflating them made the reference's Reports view
  contradict its own chat history), `failed` requiring a reason, `success` forbidding
  one, and a write that flushes + fsyncs + renames so a crash cannot leave a truncated
  state file that reads as "still running".
* **slash matching** — the scoring ladder `100+len` / `50+len` / `10+matched` / `0`,
  with a bare `/` scoring 1 so everything shows in registry order, aliases resolved
  before scoring, ties broken by registry order, and a fresh list returned.
"""

from __future__ import annotations

import json
import os

import pytest

from harness import slash_commands as S
from harness.core import run_state as R


# -- run state: the three terminal statuses ------------------------------

def test_the_terminal_vocabulary_is_the_references():
    assert R.TERMINAL_STATUSES == ("success", "failed", "cancelled")


def test_a_cancelled_run_is_DISTINCT_from_a_failed_one():
    assert R.state_payload("cancelled", "user pressed stop") == {
        "status": "cancelled", "reason": "user pressed stop",
    }
    assert R.state_payload("failed", "broker unreachable") == {
        "status": "failed", "reason": "broker unreachable",
    }
    assert R.state_payload("cancelled")["reason"] == "cancelled by user"


def test_failure_requires_a_reason_and_success_forbids_one():
    with pytest.raises(R.RunStateError):
        R.state_payload("failed")
    with pytest.raises(R.RunStateError):
        R.state_payload("success", "with caveats")
    assert R.state_payload("success") == {"status": "success"}


def test_an_unknown_status_is_refused_with_the_vocabulary():
    with pytest.raises(R.RunStateError) as exc:
        R.state_payload("abandoned")
    for status in R.TERMINAL_STATUSES:
        assert status in str(exc.value)


def test_is_terminal_recognises_the_three_and_nothing_else():
    for status in ("success", "FAILED", " Cancelled "):
        assert R.is_terminal(status) is True
    for status in ("running", "", None, "pending"):
        assert R.is_terminal(status) is False


def test_the_state_write_is_durable_and_atomic(tmp_path):
    path = R.write_state(tmp_path / "state.json", {"status": "success"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "success"}
    # no temporary file survives the rename
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_the_write_replaces_an_older_state(tmp_path):
    path = tmp_path / "state.json"
    R.write_state(path, {"status": "failed", "reason": "x"})
    R.write_state(path, {"status": "cancelled"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "cancelled"}


# -- slash matching: the scoring ladder ----------------------------------

COMMANDS = [
    S.Command("help", "Show commands"),
    S.Command("model", "Switch model"),
    S.Command("memory", "Manage memory"),
    S.Command("quit", "Exit"),
]
ALIASES = {"q": "quit", "exit": "quit", "?": "help"}


@pytest.mark.parametrize("text,expected", [
    ("/me arg", "me"), ("/", ""), ("not a slash", ""), ("   /model", "model"),
    ("/model\tnow", "model"),
])
def test_token_parsing(text, expected):
    assert S.parse_token(text) == expected


def test_the_score_ladder_is_the_references():
    assert S.fuzzy_score("mo", "model") == S.PREFIX_SCORE + 2       # 102
    assert S.fuzzy_score("odel", "model") == S.SUBSTRING_SCORE + 4   # 54
    assert S.fuzzy_score("mdl", "model") == S.SUBSEQUENCE_SCORE + 3  # 13
    assert S.fuzzy_score("zzz", "model") == 0
    assert S.fuzzy_score("", "model") == 1                           # bare slash


def test_a_prefix_beats_a_substring_beats_a_subsequence():
    assert S.fuzzy_score("mem", "memory") > S.fuzzy_score("mor", "memory") > 0
    assert (S.fuzzy_score("model", "model")
            > S.fuzzy_score("odel", "model")
            > S.fuzzy_score("mdl", "model"))


def test_a_bare_slash_lists_everything_in_registry_order():
    names = [c.name for c in S.match_commands("/", COMMANDS)]
    assert names == ["help", "model", "memory", "quit"]


def test_a_plain_question_matches_nothing():
    assert S.match_commands("what is the model doing", COMMANDS) == []


def test_an_alias_is_resolved_before_scoring():
    assert [c.name for c in S.match_commands("/q", COMMANDS, ALIASES)] == ["quit"]
    assert [c.name for c in S.match_commands("/?", COMMANDS, ALIASES)] == ["help"]


def test_ties_break_by_registry_order():
    # "m" prefixes both model and memory equally: registry order decides
    assert [c.name for c in S.match_commands("/m", COMMANDS)] == ["model", "memory"]


def test_a_better_match_sorts_first_whatever_the_registry_order():
    assert [c.name for c in S.match_commands("/mem", COMMANDS)] == ["memory"]


def test_the_result_is_a_fresh_list_of_distinct_objects():
    result = S.match_commands("/", COMMANDS)
    assert result is not COMMANDS
    assert all(a is not b for a, b in zip(result, COMMANDS))
    with pytest.raises(Exception):
        result[0].name = "mutated"          # Command is frozen: the registry is safe


def test_find_exact_resolves_a_name_or_alias_and_returns_none_otherwise():
    assert S.find_exact("/model", COMMANDS).name == "model"
    assert S.find_exact("exit", COMMANDS, ALIASES).name == "quit"
    assert S.find_exact("nonexistent", COMMANDS) is None
