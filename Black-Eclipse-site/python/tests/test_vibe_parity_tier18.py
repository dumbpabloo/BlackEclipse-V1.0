"""Branch parity, eighteenth tier: the hypothesis registry contract.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/src/hypotheses/registry.py`).

Eclipse pre-registers a hypothesis as a statement plus an acceptance rule before a trial
(`quant/store.py`), which is the methodological core. What it had no notion of is the
reference's **registry**: a derived id, a closed status vocabulary, and a ranked search
whose `limit=0` really means zero. Those three are the behaviours a naive implementation
gets wrong, so they are pinned here.
"""

from __future__ import annotations

import hashlib

import pytest

from harness.hypotheses.registry import (
    HYPOTHESIS_STATUSES,
    hypothesis_id,
    search,
    validate_status,
)


# -- the status vocabulary ------------------------------------------------

def test_the_statuses_are_the_references():
    assert HYPOTHESIS_STATUSES == (
        "exploring", "testing", "validated", "rejected", "monitoring",
    )


@pytest.mark.parametrize("status", HYPOTHESIS_STATUSES)
def test_a_known_status_is_lower_cased(status):
    assert validate_status(status.upper()) == status
    assert validate_status(f"  {status}  ") == status


def test_an_unknown_status_names_the_vocabulary():
    with pytest.raises(ValueError) as exc:
        validate_status("promising")
    message = str(exc.value)
    for status in HYPOTHESIS_STATUSES:
        assert status in message


# -- the derived id -------------------------------------------------------

def test_the_id_is_hyp_plus_twelve_hex_of_the_seed():
    identifier = hypothesis_id("Momentum decays", "2026-01-01T00:00:00Z")
    expected = "hyp_" + hashlib.sha256(
        "momentum decays|2026-01-01T00:00:00Z".encode()
    ).hexdigest()[:12]
    assert identifier == expected


def test_the_id_is_stable_across_calls_and_case_insensitive_in_the_title():
    a = hypothesis_id("Momentum Decays", "2026-01-01T00:00:00Z")
    b = hypothesis_id("  momentum decays ", "2026-01-01T00:00:00Z")
    assert a == b


def test_a_different_timestamp_gives_a_different_id():
    a = hypothesis_id("same title", "2026-01-01T00:00:00Z")
    b = hypothesis_id("same title", "2026-01-02T00:00:00Z")
    assert a != b


def test_a_collision_gets_a_numeric_suffix():
    first = hypothesis_id("dup", "2026-01-01T00:00:00Z")
    second = hypothesis_id("dup", "2026-01-01T00:00:00Z", existing={first})
    third = hypothesis_id("dup", "2026-01-01T00:00:00Z", existing={first, second})
    assert second == first + "_2"
    assert third == first + "_3"


def test_an_unrelated_existing_id_does_not_shift_the_suffix():
    first = hypothesis_id("dup", "2026-01-01T00:00:00Z")
    assert hypothesis_id("dup", "2026-01-01T00:00:00Z", existing={"hyp_other"}) == first


# -- ranked search --------------------------------------------------------

RECORDS = [
    {"hypothesis_id": "hyp_a", "title": "Momentum decay in mid caps",
     "thesis": "ranking fades", "status": "testing", "updated_at": "2026-01-03"},
    {"hypothesis_id": "hyp_b", "title": "Carry in FX",
     "thesis": "rate differentials", "status": "validated", "updated_at": "2026-01-05"},
    {"hypothesis_id": "hyp_c", "title": "Momentum plus carry",
     "thesis": "momentum and carry interact", "status": "monitoring",
     "updated_at": "2026-01-04"},
]


def test_a_token_overlap_is_ranked_above_a_single_hit():
    hits = search(RECORDS, query="momentum carry")
    assert [h["hypothesis_id"] for h in hits] == ["hyp_c", "hyp_b", "hyp_a"]
    # hyp_c carries both tokens, so it must lead regardless of the recency tie-break
    assert hits[0]["hypothesis_id"] == "hyp_c"


def test_a_single_token_hit_still_matches():
    hits = search(RECORDS, query="momentum")
    assert {h["hypothesis_id"] for h in hits} == {"hyp_a", "hyp_c"}


def test_an_empty_query_matches_everything_newest_first():
    hits = search(RECORDS)
    assert [h["hypothesis_id"] for h in hits] == ["hyp_b", "hyp_c", "hyp_a"]


def test_a_status_filter_is_applied_with_the_query():
    assert [h["hypothesis_id"] for h in search(RECORDS, status="VALIDATED")] == ["hyp_b"]
    assert search(RECORDS, query="momentum", status="validated") == []


def test_the_search_includes_links_and_notes_in_the_haystack():
    records = [{"hypothesis_id": "hyp_x", "title": "unrelated", "status": "testing",
                "run_cards": [{"notes": "walk-forward degraded sharply"}],
                "updated_at": "2026-01-01"}]
    assert search(records, query="walk-forward") != []


def test_limit_zero_returns_nothing():
    """An explicit 0 is a request for zero results, not a floor of one."""
    assert search(RECORDS, limit=0) == []


def test_the_limit_is_clamped_to_one_hundred():
    many = [{"hypothesis_id": f"hyp_{i}", "title": "x", "status": "exploring",
             "updated_at": f"2026-01-{i:02d}"} for i in range(1, 29)]
    assert len(search(many, limit=1000)) == 28
    assert len(search(many, limit=5)) == 5


def test_a_negative_limit_returns_nothing_rather_than_raising():
    assert search(RECORDS, limit=-3) == []


def test_ties_break_by_the_most_recently_updated():
    tied = [
        {"hypothesis_id": "hyp_old", "title": "carry", "status": "testing",
         "updated_at": "2026-01-01"},
        {"hypothesis_id": "hyp_new", "title": "carry", "status": "testing",
         "updated_at": "2026-02-01"},
    ]
    assert [h["hypothesis_id"] for h in search(tied, query="carry")] == ["hyp_new", "hyp_old"]


def test_an_unknown_status_filter_is_refused():
    with pytest.raises(ValueError):
        search(RECORDS, status="promising")


def test_cjk_titles_are_searchable_character_by_character():
    records = [{"hypothesis_id": "hyp_zh", "title": "动量因子衰减", "status": "exploring",
                "updated_at": "2026-01-01"}]
    assert search(records, query="动量") != []
