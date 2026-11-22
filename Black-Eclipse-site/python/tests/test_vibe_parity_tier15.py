"""Branch parity, fifteenth tier: how a factor says "I cannot run on this data".

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/src/factors/data_availability.py`).

Eclipse already refused a factor whose declared inputs were absent
(`contracts.require_inputs` → `MISSING_INPUT`). What it had no notion of is the
reference's **per-field status and point-in-time safety**:

* ``failed`` vs ``unknown`` — a REQUIRED input that is absent failed; an optional one
  that is absent is merely unknown;
* a **fundamental** with a filing date but no publication timestamp stays ``unknown``
  — a one-bar shift is useful protection, not proof, so calling it ``verified`` would
  overstate what the data supports;
* ``pitSafe`` is true **only** for ``verified``, which is precisely the distinction
  between data that looks available and data that was knowable at the time.

Also pinned: the availability mode by field kind (bar close, filing date, session
date, effective date), which is what turns "we have it" into "we have it as of when".
"""

from __future__ import annotations

import pytest

from harness.factors import contracts as C


# -- the vocabulary -------------------------------------------------------

def test_the_status_vocabulary_is_the_references():
    assert C.FIELD_STATUSES == ("verified", "assumed", "unknown", "failed")


@pytest.mark.parametrize("field,mode", [
    ("open", "bar_close"), ("close", "bar_close"), ("vwap", "bar_close"),
    ("amount", "bar_close"), ("volume", "bar_close"),
    ("fund:revenue", "filing_date"), ("fund:eps", "filing_date"),
    ("val:pe", "session_date"), ("val:ev_ebitda", "session_date"),
    ("sector", "effective_date"), ("universe:membership", "effective_date"),
    ("analyst:rating", "unknown"),
])
def test_the_availability_mode_follows_the_field_kind(field, mode):
    assert C.availability_mode(field) == mode


# -- the status decision order -------------------------------------------

def test_a_required_field_that_is_absent_FAILED():
    [record] = C.field_availability(["vwap"], required=["vwap"])
    assert record["status"] == "failed"
    assert record["pitSafe"] is False


def test_an_optional_field_that_is_absent_is_only_unknown():
    [record] = C.field_availability(["vwap"])
    assert record["status"] == "unknown"


def test_a_present_bar_with_a_source_is_assumed_not_verified():
    [record] = C.field_availability(["close"], present=["close"],
                                   sources={"close": "jdds"})
    assert record["status"] == "assumed"
    assert record["pitSafe"] is False          # assumed is not point-in-time proof


def test_a_present_bar_with_no_source_is_unknown():
    [record] = C.field_availability(["close"], present=["close"])
    assert record["status"] == "unknown"


def test_a_fundamental_is_UNKNOWN_even_when_present_and_sourced():
    """A filing date is not a publication timestamp; this is the reference's point."""
    [record] = C.field_availability(["fund:revenue"], present=["fund:revenue"],
                                   sources={"fund:revenue": "sec"})
    assert record["status"] == "unknown"
    assert record["pitSafe"] is False
    assert "publication timestamp" in record["reason"]


def test_universe_membership_needs_the_point_in_time_flag():
    [naive] = C.field_availability(["universe:membership"])
    assert naive["status"] == "unknown"
    [pit] = C.field_availability(["universe:membership"], pit_membership=True)
    assert pit["status"] == "verified"
    assert pit["pitSafe"] is True


def test_an_explicit_status_wins_over_every_inference():
    [record] = C.field_availability(
        ["close"], present=["close"],
        sources={"close": "jdds"},
        explicit={"close": "verified"},
    )
    assert record["status"] == "verified" and record["pitSafe"] is True


def test_an_explicit_status_outside_the_vocabulary_is_ignored():
    [record] = C.field_availability(["close"], present=["close"],
                                   explicit={"close": "probably-fine"})
    assert record["status"] == "unknown"


def test_pit_safe_is_true_for_verified_and_nothing_else():
    records = C.field_availability(
        ["a", "b", "c", "d"],
        present=["a", "b", "c"],
        sources={"a": "s", "b": "s", "c": "s"},
        required=["d"],
        explicit={"a": "verified", "b": "assumed", "c": "unknown"},
    )
    flags = {r["field"]: r["pitSafe"] for r in records}
    assert flags == {"a": True, "b": False, "c": False, "d": False}


# -- the existing refusal still holds ------------------------------------

def test_require_inputs_still_raises_MISSING_INPUT_naming_what_is_missing():
    factor = {"name": "alpha_vwap", "requiresInputs": ["vwap", "industry"]}
    with pytest.raises(C.FactorInputError) as exc:
        C.require_inputs(factor, ["close", "open"])
    assert exc.value.code == "MISSING_INPUT"
    assert exc.value.missing == ("vwap", "industry")


def test_describe_reports_runnable_and_why():
    factor = {"name": "x", "requiresInputs": ["vwap"]}
    assert C.describe(factor, ["vwap"])["runnable"] is True
    blocked = C.describe(factor, [])
    assert blocked["runnable"] is False and blocked["missing"] == ["vwap"]


def test_the_two_views_agree_on_the_same_fixture():
    """The refusal and the status list must not disagree about an absent input."""
    factor = {"name": "x", "requiresInputs": ["vwap"]}
    records = C.field_availability(["vwap"], required=C.required_inputs(factor))
    assert records[0]["status"] == "failed"
    assert C.describe(factor, [])["runnable"] is False
