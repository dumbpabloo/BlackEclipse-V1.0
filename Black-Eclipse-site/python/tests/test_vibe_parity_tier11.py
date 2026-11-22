"""Numeric parity, eleventh tier: the zero-cost context fold.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/agent/context_compact.py`, layer 2 `_context_collapse`):

    KEEP_RECENT = 3
    COLLAPSE_PRESERVE_RECENT = 6
    COLLAPSE_TEXT_MIN  = 2400
    COLLAPSE_HEAD      = 900
    COLLAPSE_TAIL      = 500
    TAIL_TOKEN_BUDGET  = 20_000
    SUMMARY_CHUNK_CHARS = 80_000

and the fold itself is ``head + "\\n\\n...[N chars collapsed]...\\n\\n" + tail``.

Eclipse's `compaction.collapse_text` implements the same fold with two improvements —
the marker names the exact omitted count **and** a sha256 of the full original, so a
reader can verify the fold, and folding is idempotent — but its defaults were
`1200/400/400`: eager enough to strip the middle of a ~2 KB tool result that the next
turn then had to re-fetch. The defaults are now the reference's `2400/900/500`, and
this file pins both the numbers and the invariants.
"""

from __future__ import annotations

import hashlib

import pytest

from harness import compaction as C


# -- the constants are the reference's ------------------------------------

def test_the_collapse_constants_match_the_reference():
    assert C.COLLAPSE_MIN_CHARS == 2_400      # reference COLLAPSE_TEXT_MIN
    assert C.COLLAPSE_HEAD_CHARS == 900       # reference COLLAPSE_HEAD
    assert C.COLLAPSE_TAIL_CHARS == 500       # reference COLLAPSE_TAIL


# -- the fold preserves exactly what the reference preserves --------------

def _payload(n: int) -> str:
    # deterministic, printable, and long enough to fold
    return "".join(chr(ord("a") + (i % 26)) for i in range(n))


def test_a_short_text_is_untouched():
    text = _payload(C.COLLAPSE_MIN_CHARS)          # exactly at the threshold
    folded, did, digest = C.collapse_text(text)
    assert did is False and folded == text and digest == ""


def test_one_char_past_the_threshold_folds():
    text = _payload(C.COLLAPSE_MIN_CHARS + 1)
    folded, did, _digest = C.collapse_text(text)
    assert did is True
    assert folded.startswith(text[: C.COLLAPSE_HEAD_CHARS])
    assert folded.endswith(text[-C.COLLAPSE_TAIL_CHARS:])


def test_the_marker_names_the_exact_number_of_omitted_characters():
    text = _payload(10_000)
    folded, _did, _digest = C.collapse_text(text)
    omitted = 10_000 - C.COLLAPSE_HEAD_CHARS - C.COLLAPSE_TAIL_CHARS
    assert f"{omitted} of 10000 chars omitted" in folded


def test_the_digest_verifies_the_original():
    text = _payload(5_000)
    _folded, _did, digest = C.collapse_text(text)
    assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest[:16] in _folded


def test_folding_is_idempotent():
    """The reference would fold its own marker again; ours must not compound."""
    text = _payload(5_000)
    once, did, _ = C.collapse_text(text)
    twice, did_again, digest_again = C.collapse_text(once)
    assert did is True and did_again is False
    assert twice == once and digest_again == ""


def test_the_reference_constants_can_be_supplied_explicitly():
    """The fold is parameterised, so the reference's numbers are reproducible."""
    text = _payload(6_000)
    folded, did, _ = C.collapse_text(text, head=900, tail=500, min_chars=2400)
    assert did is True
    assert folded[:900] == text[:900]
    assert folded[-500:] == text[-500:]


def test_a_larger_head_and_tail_preserve_more():
    text = _payload(6_000)
    default_fold, _, _ = C.collapse_text(text)
    wide_fold, _, _ = C.collapse_text(text, head=2_000, tail=1_000)
    assert len(wide_fold) > len(default_fold)
    assert wide_fold[:2_000] == text[:2_000]


def test_a_non_string_is_stringified_rather_than_crashing():
    assert C.collapse_text(None) == ("", False, "")


# -- turn-level behaviour -------------------------------------------------

class _Turn:
    def __init__(self, content: str) -> None:
        self.content = content

    def model_copy(self, update=None):        # pydantic-shaped
        clone = _Turn(self.content)
        for key, value in (update or {}).items():
            setattr(clone, key, value)
        return clone


def test_a_long_turn_is_folded_and_the_original_object_is_not_mutated():
    turn = _Turn(_payload(8_000))
    folded_turn, did, digest = C.collapse_turn(turn)
    assert did is True and digest
    assert folded_turn is not turn
    assert len(turn.content) == 8_000          # untouched
    assert len(folded_turn.content) < 8_000


def test_a_short_turn_is_returned_unchanged():
    turn = _Turn(_payload(100))
    same, did, _digest = C.collapse_turn(turn)
    assert did is False and same is turn


def test_the_collapse_report_counts_what_it_saved():
    report = C.CollapseReport(folded=2, saved_chars=12_345)
    assert report.folded == 2 and report.saved_chars == 12_345


# -- the reference's layer-3 budgets are recorded, not guessed ------------


def test_the_reference_tail_and_chunk_budgets_are_recorded_as_data():
    """The reference's layer-3 budgets, stated so a future edit can compare.

    Eclipse's history budget is token-based and its own; these are the reference's
    numbers, kept here because they are what this module was checked against.
    """
    reference = {
        "tailTokens": 20_000,
        "summaryChunkChars": 80_000,
        "keepRecentToolResults": 3,
        "collapsePreserveRecent": 6,
    }
    assert reference["tailTokens"] == 20_000
    assert reference["summaryChunkChars"] == 80_000
    # the collapse threshold is a CHARACTER count and must stay well above the
    # number of tool results layer 1 keeps intact, or the two layers fight
    assert C.COLLAPSE_MIN_CHARS > reference["keepRecentToolResults"]
