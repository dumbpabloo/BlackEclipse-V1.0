"""Numeric parity, second tier: artifact evidence diffs and B3 symbol mapping.

References fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):

* ``agent/src/research_artifacts/comparison.py::_evidence_comparison`` — evidence is
  compared as a **multiset**: duplicates count, ``unchanged_count`` is the multiset
  intersection, a pure reordering is ``order_changed`` (not "no change"), and
  additions/removals keep the order they appear in.
* ``agent/backtest/loaders/b3_cotahist_loader.py::map_symbol`` — ``PETR4.SA`` maps to
  the cash market (``TPMERC`` 10), ``PETR4F.SA`` to the odd-lot market (20), decided
  by a trailing ``F`` that follows a fraction digit.

Both reference functions are reimplemented from source in this file, so the test does
not depend on the other project being installed. Before this, Eclipse's artifact
comparison had metadata and provenance but **no evidence diff at all** — the one thing
a research reader actually re-checks — and no B3 symbol mapping.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from harness.artifacts.compare import compare_evidence, compare_versions, to_markdown
from harness.br_data.cotahist import CASH_MARKET, ODD_LOT_MARKET, map_symbol


# -- the reference algorithms ----------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ordered_unmatched(items, unmatched):
    remaining = unmatched.copy()
    out = []
    for item in items:
        key = _canonical(item)
        if remaining[key] > 0:
            out.append(item)
            remaining[key] -= 1
    return out


def reference_evidence(left, right):
    left_keys = [_canonical(item) for item in left]
    right_keys = [_canonical(item) for item in right]
    left_counts, right_counts = Counter(left_keys), Counter(right_keys)
    added = _ordered_unmatched(right, right_counts - left_counts)
    removed = _ordered_unmatched(left, left_counts - right_counts)
    order_changed = not added and not removed and left_keys != right_keys
    return {
        "added": added,
        "removed": removed,
        "unchangedCount": sum((left_counts & right_counts).values()),
        "orderChanged": order_changed,
        "changed": bool(added or removed or order_changed),
    }


def reference_map_symbol(code):
    ticker = str(code).strip().upper()
    if ticker.endswith(".SA"):
        ticker = ticker[:-3]
    market = 20 if ticker.endswith("F") and ticker[-2:-1].isdigit() else 10
    return ticker, market


# -- evidence: counted, ordered, and reordering-aware ---------------------


EVIDENCE_FIXTURES = [
    ([], []),
    ([{"src": "a"}], [{"src": "a"}]),
    ([{"src": "a"}, {"src": "b"}], [{"src": "b"}, {"src": "a"}]),
    ([{"src": "a"}, {"src": "a"}, {"src": "b"}], [{"src": "a"}, {"src": "b"}]),
    ([{"src": "a"}], [{"src": "a"}, {"src": "b"}, {"src": "c"}]),
    ([{"src": "a", "n": 1}, {"src": "b"}], [{"src": "c"}]),
    ([{"src": "a", "meta": {"y": 2, "x": 1}}], [{"src": "a", "meta": {"x": 1, "y": 2}}]),
]


@pytest.mark.parametrize("left,right", EVIDENCE_FIXTURES)
def test_evidence_matches_the_reference(left, right):
    assert compare_evidence(left, right) == reference_evidence(left, right)


def test_a_pure_reordering_is_not_reported_as_no_change():
    report = compare_evidence([{"s": "a"}, {"s": "b"}], [{"s": "b"}, {"s": "a"}])
    assert report["orderChanged"] is True
    assert report["changed"] is True
    assert report["unchangedCount"] == 2


def test_duplicates_are_counted_not_set_collapsed():
    report = compare_evidence([{"s": "a"}, {"s": "a"}], [{"s": "a"}])
    assert report["removed"] == [{"s": "a"}]
    assert report["unchangedCount"] == 1


def test_key_order_within_an_item_is_not_a_change():
    left = [{"src": "a", "meta": {"y": 2, "x": 1}}]
    right = [{"src": "a", "meta": {"x": 1, "y": 2}}]
    assert compare_evidence(left, right)["changed"] is False


def test_evidence_reaches_the_report_and_the_markdown():
    left = {"artifact_id": "art-1", "version_number": 1, "checksum": "aa",
            "size_bytes": 10, "evidence": [{"src": "a"}, {"src": "b"}]}
    right = {"artifact_id": "art-1", "version_number": 2, "checksum": "aa",
             "size_bytes": 10, "evidence": [{"src": "a"}, {"src": "c"}]}
    report = compare_versions(left, right)
    assert report["changed"] is True          # the checksum did NOT change
    assert report["bytes"]["checksumChanged"] is False
    assert report["evidence"]["added"] == [{"src": "c"}]
    assert report["evidence"]["removed"] == [{"src": "b"}]
    text = to_markdown(report)
    assert "Evidence: 1 unchanged, 1 added, 1 removed" in text


def test_a_report_without_evidence_keeps_the_field_null():
    report = compare_versions({"checksum": "aa"}, {"checksum": "aa"})
    assert report["evidence"] is None
    assert report["changed"] is False


# -- B3 symbols: cash vs odd lot ------------------------------------------


@pytest.mark.parametrize("code", [
    "PETR4.SA", "petr4.sa", " PETR4.SA ", "PETR4", "VALE3", "SANB11.SA",
    "PETR4F.SA", "PETR4F", "VALE3F", "F", "X", "ABCD4F.SA",
])
def test_symbol_mapping_matches_the_reference(code):
    assert map_symbol(code) == reference_map_symbol(code)


def test_the_market_codes_are_the_published_ones():
    assert CASH_MARKET == 10 and ODD_LOT_MARKET == 20
    assert map_symbol("PETR4.SA") == ("PETR4", 10)
    assert map_symbol("PETR4F.SA") == ("PETR4F", 20)


def test_a_single_letter_ticker_is_not_an_odd_lot():
    # there is no fraction digit before the trailing F, so it is a name, not a market
    assert map_symbol("F") == ("F", CASH_MARKET)
    assert map_symbol("GF") == ("GF", CASH_MARKET)
