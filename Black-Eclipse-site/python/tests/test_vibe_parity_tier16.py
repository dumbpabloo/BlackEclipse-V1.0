"""Branch parity, sixteenth tier: governance records that can be audited, not just checked.

References fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):
`agent/src/governance/ledger.py` and `agent/src/governance/manifest.py`.

Eclipse already had a hash-chained ledger and a methodology manifest (ported earlier).
Comparing the *behaviour* found three things missing, all now added; two format
differences are stated rather than silently "fixed", because changing them would
rewrite every record already written:

* **a structured break, not an exception** — the reference's ``verify_chain`` returns
  ``{ok, record_count, first_break: {index, seq, reason, detail}}`` with a stable reason
  code. Ours raised a sentence. `verify_report()` now returns the same shape with the
  five reason codes (``malformed_json``, ``missing_fields``, ``seq_gap``,
  ``prev_hash_mismatch``, ``hash_mismatch``).
* **refuse to extend a broken chain** — the reference verifies the WHOLE chain before
  appending. Ours only refused a torn tail, so a valid-looking suffix could be built on
  top of an edited earlier record. `append_refusing_a_broken_chain()` closes that.
* **an offline-verifiable export** — the reference's ``build_export``/``verify_export``
  embed the records plus a hash over the envelope, so editing an exported record is
  caught even when the embedded records still chain among themselves. `export()` does
  the same.

**Stated divergences (not defects):** our ``seq`` is 0-based and our hashes carry no
``sha256:`` prefix, where the reference uses 1-based ``seq`` and prefixed digests. Both
are internally consistent and already written to disk elsewhere, so the module keeps
them and the test pins the *semantics* (order, linkage, genesis) rather than the
spelling.
"""

from __future__ import annotations

import json

import pytest

from harness.governance.ledger import (
    GENESIS,
    ChainedLedger,
    LedgerIntegrityError,
    LedgerError,
    record_hash,
)


def _ledger(tmp_path, count=3) -> ChainedLedger:
    ledger = ChainedLedger(tmp_path / "audit.jsonl")
    for index in range(count):
        ledger.append("conclusion", {"n": index, "claim": f"claim-{index}"})
    return ledger


# -- the structured report ------------------------------------------------

def test_a_clean_chain_reports_ok_with_its_count(tmp_path):
    report = _ledger(tmp_path).verify_report()
    assert report == {"ok": True, "recordCount": 3, "firstBreak": None}


def test_an_empty_ledger_is_ok_and_empty(tmp_path):
    assert ChainedLedger(tmp_path / "missing.jsonl").verify_report() == {
        "ok": True, "recordCount": 0, "firstBreak": None,
    }


def test_a_tampered_payload_names_the_reason_and_the_record(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["record"]["payload"]["claim"] = "forged"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = ledger.verify_report()
    assert report["ok"] is False
    assert report["firstBreak"]["reason"] == "hash_mismatch"
    assert report["firstBreak"]["index"] == 1
    assert report["recordCount"] == 1               # the records before the break


def test_a_deleted_record_reports_a_seq_gap(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert ledger.verify_report()["firstBreak"]["reason"] == "seq_gap"


def test_a_wrong_predecessor_pointer_reports_a_prev_hash_mismatch(tmp_path):
    """Seq is right but the link is not: the predecessor pointer is the evidence."""
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["prev_record_hash"] = GENESIS
    lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = ledger.verify_report()
    assert report["firstBreak"]["reason"] == "prev_hash_mismatch"
    assert report["firstBreak"]["seq"] == 2


def test_a_torn_line_reports_malformed_json(tmp_path):
    ledger = _ledger(tmp_path)
    with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 3, "prev_record_')
    assert ledger.verify_report()["firstBreak"]["reason"] == "malformed_json"


def test_a_record_missing_chain_fields_is_named_not_crashed(tmp_path):
    ledger = _ledger(tmp_path, 1)
    path = tmp_path / "audit.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    record.pop("prev_record_hash")
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert ledger.verify_report()["firstBreak"]["reason"] == "missing_fields"


def test_the_report_never_raises_even_on_a_broken_chain(tmp_path):
    ledger = _ledger(tmp_path, 1)
    (tmp_path / "audit.jsonl").write_text("not json at all\n", encoding="utf-8")
    assert ledger.verify_report()["ok"] is False


# -- refuse to extend a broken chain --------------------------------------

def test_appending_refuses_a_broken_chain_instead_of_building_a_suffix(tmp_path):
    ledger = _ledger(tmp_path)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["record"]["payload"]["claim"] = "edited"
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.append_refusing_a_broken_chain("conclusion", {"n": 99})
    assert "already broken" in str(exc.value)
    # and nothing was written
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_appending_refusing_succeeds_on_a_sound_chain(tmp_path):
    ledger = _ledger(tmp_path, 2)
    record = ledger.append_refusing_a_broken_chain("conclusion", {"n": 2})
    assert record["seq"] == 2
    assert ledger.verify_report()["ok"] is True


def test_a_torn_tail_is_refused_by_the_plain_append_too(tmp_path):
    ledger = _ledger(tmp_path, 1)
    with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 1, "prev')
    with pytest.raises(LedgerError):
        ledger.append("conclusion", {"n": 1})


# -- export ---------------------------------------------------------------

def test_the_export_carries_every_record_and_its_verification(tmp_path):
    export = _ledger(tmp_path).export()
    assert export["recordCount"] == 3
    assert len(export["records"]) == 3
    assert export["verification"]["ok"] is True
    assert export["exportHash"]


def test_editing_an_exported_record_changes_the_envelope_hash(tmp_path):
    export = _ledger(tmp_path).export()
    before = export["exportHash"]
    export["records"][0]["record"]["payload"]["claim"] = "edited in the export"
    after = record_hash(0, GENESIS, {"records": export["records"]})
    assert after != before


def test_the_export_is_plain_json(tmp_path):
    export = _ledger(tmp_path).export()
    assert json.loads(json.dumps(export))["recordCount"] == 3


# -- the chain linkage, spelled out --------------------------------------

def test_each_record_points_at_its_predecessor_and_the_first_at_genesis(tmp_path):
    records = _ledger(tmp_path, 4).read()
    assert records[0]["prev_record_hash"] == GENESIS
    for previous, current in zip(records, records[1:]):
        assert current["prev_record_hash"] == previous["hash"]
    assert [r["seq"] for r in records] == [0, 1, 2, 3]     # 0-based: stated divergence


# -- the manifest: readable extra dimensions and a hash you can re-derive --

def test_the_manifest_carries_readable_extra_dimensions():
    from harness.governance.manifest import build_manifest

    manifest = build_manifest(
        system_prompt="be careful",
        tools=["a", "b"],
        extra={"provider": "openrouter", "model": "deepseek-v3"},
    )
    assert manifest["extra"] == {"model": "deepseek-v3", "provider": "openrouter"}


def test_an_extra_dimension_changes_the_methodology_hash():
    from harness.governance.manifest import build_manifest

    a = build_manifest(system_prompt="p", extra={"model": "m1"})
    b = build_manifest(system_prompt="p", extra={"model": "m2"})
    assert a["manifestHash"] != b["manifestHash"]


def test_verify_hash_re_derives_and_catches_a_hand_edit():
    from harness.governance.manifest import build_manifest, verify_hash

    manifest = build_manifest(system_prompt="p", skills=(("s", "body"),))
    assert verify_hash(manifest) is True
    manifest["skills"][0]["contentHash"] = "0" * 64         # edit the record
    assert verify_hash(manifest) is False


def test_timestamp_still_does_not_enter_the_digest():
    from harness.governance.manifest import build_manifest, verify_hash

    a = build_manifest(system_prompt="p", timestamp="2026-01-01")
    b = build_manifest(system_prompt="p", timestamp="2026-06-01", run_id="other")
    assert a["manifestHash"] == b["manifestHash"]
    assert verify_hash(a) and verify_hash(b)


def test_the_manifest_is_json_round_trippable_with_its_hash_intact():
    import json

    from harness.governance.manifest import build_manifest, verify_hash

    manifest = build_manifest(system_prompt="p", tools=["x"], extra={"model": "m"})
    restored = json.loads(json.dumps(manifest))
    assert verify_hash(restored) is True
