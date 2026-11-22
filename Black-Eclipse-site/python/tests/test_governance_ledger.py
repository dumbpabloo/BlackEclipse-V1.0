"""The governance ledger: tamper evidence that a database audit table cannot give.

The tests do not just read the code — they *tamper* with a real file on disk and
assert that verification names the break, because a chain that only works when
nobody edits it is not a chain.
"""

from __future__ import annotations

import json

import pytest

from harness.governance import ChainedLedger, LedgerIntegrityError, record_hash
from harness.governance.ledger import GENESIS


def _ledger(tmp_path, count=3) -> ChainedLedger:
    ledger = ChainedLedger(tmp_path / "audit.jsonl")
    for index in range(count):
        ledger.append("conclusion", {"n": index, "claim": f"claim-{index}"})
    return ledger


# -- the happy path --------------------------------------------------------


def test_appends_are_sequenced_and_chained(tmp_path):
    ledger = _ledger(tmp_path)
    records = ledger.read()
    assert [r["seq"] for r in records] == [0, 1, 2]
    assert records[0]["prev_record_hash"] == GENESIS
    for previous, current in zip(records, records[1:]):
        assert current["prev_record_hash"] == previous["hash"]
    assert ledger.verify() == 3


def test_a_record_hash_covers_sequence_predecessor_and_payload(tmp_path):
    record = _ledger(tmp_path, 1).read()[0]
    assert record["hash"] == record_hash(0, GENESIS, record["record"])


def test_an_empty_ledger_verifies_as_zero(tmp_path):
    ledger = ChainedLedger(tmp_path / "missing.jsonl")
    assert ledger.read() == []
    assert ledger.tail() is None
    assert ledger.verify() == 0


# -- tamper evidence ------------------------------------------------------


def test_editing_a_payload_breaks_the_chain(tmp_path):
    ledger = _ledger(tmp_path)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["record"]["payload"]["claim"] = "a different conclusion"
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.verify()
    assert "record 0" in str(exc.value) and "hash mismatch" in str(exc.value)


def test_deleting_a_record_is_detected_as_a_sequence_break(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.verify()
    assert "seq" in str(exc.value)


def test_reordering_two_records_breaks_the_predecessor_link(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIntegrityError):
        ledger.verify()


def test_rewriting_a_whole_record_with_a_matching_own_hash_still_fails(tmp_path):
    """The attacker can recompute a record's hash but not its successor's prev."""
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["record"]["payload"]["claim"] = "forged"
    record["hash"] = record_hash(1, record["prev_record_hash"], record["record"])
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.verify()
    assert "record 2" in str(exc.value)


def test_a_torn_trailing_line_is_named_not_silently_ignored(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 3, "prev_record_h')

    # the readable prefix still parses, so appends keep a sane sequence
    assert [r["seq"] for r in ledger.read()] == [0, 1, 2]
    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.verify()
    assert "truncated tail" in str(exc.value)


def test_appending_after_a_torn_tail_is_refused_not_buried(tmp_path):
    from harness.governance import LedgerError

    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 3, "prev_record_h')

    # Appending would bury the torn record behind a good line and make the file
    # unverifiable for an invisible reason, so the append is refused instead.
    with pytest.raises(LedgerError) as exc:
        ledger.append("conclusion", {"n": 3})
    assert "partial record" in str(exc.value)
    # and nothing was written
    assert [r["seq"] for r in ledger.read()] == [0, 1, 2]


# -- the composition with a manifest record -------------------------------


def test_a_manifest_record_and_its_conclusions_chain_together(tmp_path):
    from harness.governance import build_manifest

    ledger = ChainedLedger(tmp_path / "audit.jsonl")
    manifest = build_manifest(system_prompt="be careful", tools=["a", "b"])
    ledger.append("manifest", manifest)
    ledger.append("conclusion", {"claim": "x", "manifestHash": manifest["manifestHash"]})

    records = ledger.read()
    assert ledger.verify() == 2
    assert records[1]["record"]["payload"]["manifestHash"] == records[0]["record"][
        "payload"
    ]["manifestHash"]


def test_a_record_needs_a_kind(tmp_path):
    from harness.governance import LedgerError

    with pytest.raises(LedgerError):
        ChainedLedger(tmp_path / "audit.jsonl").append("  ", {})
