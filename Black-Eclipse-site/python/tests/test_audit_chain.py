"""Tamper-evident audit chains for both books of record.

Append-only is a store policy; these tests prove the hash chain makes edits,
deletions and reordering *detectable* and names the first bad event.
"""

from __future__ import annotations

import json

from harness.audit_chain import GENESIS, event_hash, verify_chain
from ledger_helpers import line, make_ledger, make_service as make_ledger_service, post
from portfolio_helpers import make_service as make_portfolio_service


# ── pure chain ───────────────────────────────────────────────────────────────


def test_hash_is_deterministic_and_links_to_the_previous_event():
    a = event_hash(GENESIS, {"id": "a", "kind": "POST"})
    b = event_hash(a, {"id": "b", "kind": "POST"})
    assert a == event_hash(GENESIS, {"id": "a", "kind": "POST"})
    assert a != b
    # Changing any field changes the hash.
    assert event_hash(a, {"id": "b", "kind": "REVERSE"}) != b


def test_verify_chain_names_the_first_edited_record():
    records = [
        {"id": "a", "payload": {"x": 1}, "prevHash": GENESIS, "hash": event_hash(GENESIS, {"x": 1})},
    ]
    records[0]["payload"]["x"] = 2
    result = verify_chain(records, payload_of=lambda r: r["payload"])
    assert result["ok"] is False
    assert result["status"] == "BROKEN"
    assert result["brokenAt"] == "a"
    assert result["reason"] == "HASH_MISMATCH"


def test_verify_chain_reports_legacy_rows_as_unsealed_not_verified():
    result = verify_chain(
        [{"id": "old", "payload": {"x": 1}, "prevHash": None, "hash": None}],
        payload_of=lambda r: r["payload"],
    )
    assert result["ok"] is False
    assert result["status"] == "UNSEALED"
    assert result["unsealed"] == 1


# ── portfolio ────────────────────────────────────────────────────────────────


def test_portfolio_audit_chain_verifies_then_detects_tampering(tmp_path):
    service = make_portfolio_service(tmp_path)
    pid = service.create_portfolio("Chain fund")["id"]
    service.add_transaction(pid, {"type": "BUY", "symbol": "AAPL", "quantity": "1", "price": "100"})

    clean = service.verify_audit(pid)
    assert clean["ok"] is True, clean
    assert clean["checked"] >= 1
    assert clean["head"] != GENESIS

    db = service.store._db  # noqa: SLF001 - the test tampers behind the API
    event_id = db.execute(
        "SELECT id FROM PortfolioAuditEvent WHERE portfolioId=? ORDER BY rowid ASC LIMIT 1",
        (pid,),
    ).fetchone()["id"]
    db.execute(
        "UPDATE PortfolioAuditEvent SET after=? WHERE id=?",
        (json.dumps({"tampered": True}), event_id),
    )
    db.commit()

    broken = service.verify_audit(pid)
    assert broken["ok"] is False
    assert broken["status"] == "BROKEN"
    assert broken["brokenAt"] == event_id
    assert broken["reason"] == "HASH_MISMATCH"


# ── general ledger ───────────────────────────────────────────────────────────


def test_ledger_audit_chain_verifies_then_detects_tampering(tmp_path):
    service = make_ledger_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-01-05", [
        line("1000", debit="100"),
        line("4000", credit="100"),
    ])

    clean = service.verify_audit(ledger_id)
    assert clean["ok"] is True, clean
    assert clean["checked"] >= 2
    assert clean["head"] != GENESIS

    db = service.store._db  # noqa: SLF001
    event_id = db.execute(
        "SELECT id FROM LedgerAuditEvent WHERE ledgerId=? ORDER BY rowid ASC LIMIT 1",
        (ledger_id,),
    ).fetchone()["id"]
    db.execute(
        "UPDATE LedgerAuditEvent SET detail=? WHERE id=?",
        (json.dumps({"tampered": True}), event_id),
    )
    db.commit()

    broken = service.verify_audit(ledger_id)
    assert broken["ok"] is False
    assert broken["status"] == "BROKEN"
    assert broken["brokenAt"] == event_id


def test_legacy_rows_are_sealed_on_upgrade_without_changing_content(tmp_path):
    service = make_ledger_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-01-05", [line("1000", debit="5"), line("4000", credit="5")])

    db = service.store._db  # noqa: SLF001
    query = ("SELECT id, kind, detail, createdAt FROM LedgerAuditEvent "
             "WHERE ledgerId=? ORDER BY rowid ASC")
    before = [dict(row) for row in db.execute(query, (ledger_id,)).fetchall()]
    db.execute("UPDATE LedgerAuditEvent SET prevHash=NULL, hash=NULL WHERE ledgerId=?",
               (ledger_id,))
    db.commit()
    assert service.verify_audit(ledger_id)["status"] == "UNSEALED"

    # A store open (the upgrade path) seals the legacy rows.
    service.store._ensure_schema()  # noqa: SLF001

    sealed = service.verify_audit(ledger_id)
    assert sealed["ok"] is True, sealed
    after = [dict(row) for row in db.execute(query, (ledger_id,)).fetchall()]
    assert after == before, "sealing must add hashes, never rewrite event content"


def test_deleting_an_audit_event_breaks_the_chain(tmp_path):
    service = make_ledger_service(tmp_path)
    ledger_id = make_ledger(service)
    post(service, ledger_id, "2026-01-05", [line("1000", debit="10"), line("4000", credit="10")])
    post(service, ledger_id, "2026-01-06", [line("1000", debit="20"), line("4000", credit="20")])

    db = service.store._db  # noqa: SLF001
    middle = db.execute(
        "SELECT id FROM LedgerAuditEvent WHERE ledgerId=? ORDER BY rowid ASC LIMIT 1 OFFSET 1",
        (ledger_id,),
    ).fetchone()
    assert middle is not None
    db.execute("DELETE FROM LedgerAuditEvent WHERE id=?", (middle["id"],))
    db.commit()

    broken = service.verify_audit(ledger_id)
    assert broken["ok"] is False
    assert broken["status"] == "BROKEN"
    assert broken["reason"] == "PREV_HASH_MISMATCH"
