"""Source-document attachments: artifact reference + frozen content hash.

The bytes live in the ONE artifact store. The ledger keeps only the artifact id
and the hash that was true when the document was attached, so replacing the
artifact later is detectable rather than silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harness.ledger.errors import LedgerError
from ledger_helpers import line, make_ledger, make_service, post


@dataclass
class _Artifact:
    id: str
    checksum: str | None
    name: str = "receipt.pdf"
    mime_type: str = "application/pdf"
    size_bytes: int = 128


class _Artifacts:
    def __init__(self) -> None:
        self.items: dict[str, _Artifact] = {}

    def add(self, artifact_id: str, checksum: str | None = "sha256-abc") -> _Artifact:
        artifact = _Artifact(id=artifact_id, checksum=checksum)
        self.items[artifact_id] = artifact
        return artifact

    def get(self, artifact_id: str) -> _Artifact:
        if artifact_id not in self.items:
            raise KeyError(artifact_id)
        return self.items[artifact_id]


def _posted_entry(service, ledger_id: str) -> dict:
    return post(service, ledger_id, "2026-01-10", [
        line("1000", debit="50"),
        line("4000", credit="50"),
    ])


def test_attach_freezes_the_artifact_hash_and_shows_on_the_entry(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    entry = _posted_entry(service, ledger_id)
    artifacts = _Artifacts()
    artifacts.add("art_1", "sha256-abc")

    attachment = service.attach_document(
        ledger_id, entry["id"], "art_1", artifact_service=artifacts, note="bank receipt",
    )
    assert attachment["artifactId"] == "art_1"
    assert attachment["checksum"] == "sha256-abc"
    assert attachment["note"] == "bank receipt"

    # The entry read carries its evidence.
    full = service.get_entry(ledger_id, entry["id"])
    assert len(full["attachments"]) == 1
    assert full["attachments"][0]["artifactId"] == "art_1"

    listed = service.list_attachments(ledger_id, entry_id=entry["id"])
    assert [row["id"] for row in listed] == [attachment["id"]]

    verified = service.verify_attachment(ledger_id, attachment["id"], artifact_service=artifacts)
    assert verified["status"] == "MATCHED"
    assert verified["ok"] is True

    # Attaching is an audited act (and the audit chain still verifies).
    kinds = [event["kind"] for event in service.audit_log(ledger_id)]
    assert "DOCUMENT_ATTACHED" in kinds
    assert service.verify_audit(ledger_id)["ok"] is True


def test_a_changed_or_deleted_artifact_is_detected(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    entry = _posted_entry(service, ledger_id)
    artifacts = _Artifacts()
    artifacts.add("art_1", "sha256-abc")
    attachment = service.attach_document(ledger_id, entry["id"], "art_1", artifact_service=artifacts)

    artifacts.items["art_1"].checksum = "sha256-CHANGED"
    changed = service.verify_attachment(ledger_id, attachment["id"], artifact_service=artifacts)
    assert changed["status"] == "CHANGED"
    assert changed["ok"] is False
    assert changed["storedChecksum"] == "sha256-abc"
    assert changed["currentChecksum"] == "sha256-CHANGED"

    del artifacts.items["art_1"]
    missing = service.verify_attachment(ledger_id, attachment["id"], artifact_service=artifacts)
    assert missing["status"] == "MISSING"
    assert missing["ok"] is False
    assert missing["reason"] == "ARTIFACT_MISSING"


def test_attach_refuses_a_missing_unhashed_or_unavailable_artifact(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    entry = _posted_entry(service, ledger_id)

    with pytest.raises(LedgerError) as info:
        service.attach_document(ledger_id, entry["id"], "art_missing",
                                artifact_service=_Artifacts())
    assert info.value.code == "ARTIFACT_NOT_FOUND"

    unhashed = _Artifacts()
    unhashed.add("art_unhashed", None)
    with pytest.raises(LedgerError) as info:
        service.attach_document(ledger_id, entry["id"], "art_unhashed",
                                artifact_service=unhashed)
    assert info.value.code == "ARTIFACT_UNHASHED"

    with pytest.raises(LedgerError) as info:
        service.attach_document(ledger_id, entry["id"], "art_1", artifact_service=None)
    assert info.value.code == "ARTIFACT_STORE_UNAVAILABLE"


def test_duplicate_attach_is_refused_and_detach_removes_the_reference(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    entry = _posted_entry(service, ledger_id)
    artifacts = _Artifacts()
    artifacts.add("art_1")
    attachment = service.attach_document(ledger_id, entry["id"], "art_1",
                                         artifact_service=artifacts)

    with pytest.raises(LedgerError) as info:
        service.attach_document(ledger_id, entry["id"], "art_1", artifact_service=artifacts)
    assert info.value.code == "ATTACHMENT_EXISTS"

    removed = service.detach_document(ledger_id, attachment["id"])
    assert removed["removed"] is True
    assert service.list_attachments(ledger_id, entry_id=entry["id"]) == []
    with pytest.raises(LedgerError) as info:
        service.verify_attachment(ledger_id, attachment["id"], artifact_service=artifacts)
    assert info.value.code == "ATTACHMENT_NOT_FOUND"
    kinds = [event["kind"] for event in service.audit_log(ledger_id)]
    assert "DOCUMENT_DETACHED" in kinds


def test_deleting_a_ledger_removes_its_attachments(tmp_path):
    service = make_service(tmp_path)
    ledger_id = make_ledger(service)
    entry = _posted_entry(service, ledger_id)
    artifacts = _Artifacts()
    artifacts.add("art_1")
    service.attach_document(ledger_id, entry["id"], "art_1", artifact_service=artifacts)

    assert service.delete_ledger(ledger_id) is True
    db = service.store._db  # noqa: SLF001
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM LedgerAttachment WHERE ledgerId=?", (ledger_id,)
    ).fetchone()["n"]
    assert remaining == 0


def test_cannot_attach_an_entry_from_another_ledger(tmp_path):
    service = make_service(tmp_path)
    first = make_ledger(service)
    second = make_ledger(service)
    entry = _posted_entry(service, first)
    artifacts = _Artifacts()
    artifacts.add("art_1")

    with pytest.raises(LedgerError) as info:
        service.attach_document(second, entry["id"], "art_1", artifact_service=artifacts)
    assert info.value.code == "ENTRY_NOT_FOUND"
