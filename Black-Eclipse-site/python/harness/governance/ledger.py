"""Hash-chained, append-only audit ledger for research conclusions.

Every record embeds ``seq`` and ``prev_record_hash`` and then hashes
``seq + prev_record_hash + canonical(payload)``. Editing, inserting or reordering
any earlier record therefore breaks every later hash, so the ledger can *prove*
that it has not been rewritten — which a plain SQLite audit table cannot do
without trusting the database.

This is deliberately small and separate from the operational audit tables:
those answer "what happened"; this one answers "has the account of what happened
been altered since". It is the port of Vibe's ``governance/ledger.py``.

Concurrency: appends take an exclusive advisory lock on a sibling ``.lock``
file (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows), read the tail,
write one line, then ``fsync``. A crash mid-append can leave a partial last
line; :meth:`verify` reports it as a truncated tail rather than silently
accepting it, and :meth:`read` stops at the first unparseable line.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

#: The ``prev_record_hash`` of record zero, so the first record has no special case.
GENESIS = "0" * 64

__all__ = [
    "GENESIS",
    "ChainedLedger",
    "LedgerError",
    "LedgerIntegrityError",
    "record_hash",
]


class LedgerError(RuntimeError):
    """The ledger could not be read or written."""


class LedgerIntegrityError(LedgerError):
    """The chain is broken: a record was edited, removed or reordered."""


def _break(index: int, seq: Any, reason: str, detail: str, count: int) -> dict[str, Any]:
    """The reference's structured break record: a reason code, not a sentence."""
    return {
        "ok": False,
        "recordCount": count,
        "firstBreak": {"index": index, "seq": seq, "reason": reason, "detail": detail},
    }


def _canonical(payload: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, so hashes survive a re-dump."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def record_hash(seq: int, prev_hash: str, payload: Any) -> str:
    """The digest a record must carry: sequence, predecessor, then content."""
    body = f"{seq}\n{prev_hash}\n{_canonical(payload)}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Advisory exclusive lock over one byte of a sibling lock file."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt":  # pragma: no cover - windows only
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ChainedLedger:
    """Append records to ``path``; every record is bound to its predecessor."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    # -- reading ---------------------------------------------------------

    def read(self) -> list[dict[str, Any]]:
        """Every complete record, in order. A partial trailing line is skipped."""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    records.append(json.loads(text))
                except json.JSONDecodeError:
                    break  # a torn tail: verify() names it
        return records

    def tail(self) -> Optional[dict[str, Any]]:
        """The last complete record, or ``None`` for an empty ledger."""
        records = self.read()
        return records[-1] if records else None

    def __len__(self) -> int:
        return len(self.read())

    # -- writing ---------------------------------------------------------

    def append(
        self,
        kind: str,
        payload: Any,
        *,
        at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append one record and return it (with ``seq`` and ``hash``).

        ``kind`` is a short label (``"conclusion"``, ``"run"``, ``"manifest"``);
        it is part of the hashed payload so a record cannot be re-labelled.
        """
        from ..persistence import now_iso  # local import keeps this module standalone

        if not str(kind).strip():
            raise LedgerError("a ledger record needs a non-empty kind")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            self._refuse_torn_tail()
            last = self.tail()
            seq = (last["seq"] + 1) if last else 0
            prev = last["hash"] if last else GENESIS
            body = {"kind": str(kind), "at": at or now_iso(), "payload": payload}
            record = {
                "seq": seq,
                "prev_record_hash": prev,
                "record": body,
                "hash": record_hash(seq, prev, body),
            }
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    # -- verification ----------------------------------------------------

    def _refuse_torn_tail(self) -> None:
        """Refuse to append after a partial line.

        Appending would bury the torn record behind a valid one and make the
        chain unverifiable for reasons the operator could not see. Losing audit
        data is the operator's decision to make, not this module's.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise LedgerError(
                    "the ledger ends in a partial record; refusing to append. "
                    "Inspect the file: a torn tail is evidence, and truncating it "
                    "must be a conscious decision."
                )

    def verify_report(self) -> dict[str, Any]:
        """Verify the whole chain and return a structured result, never raising.

        The reference (`governance/ledger.py::verify_chain`) reports
        ``{ok, record_count, first_break: {index, seq, reason, detail}}`` with a
        stable reason code instead of an exception, which is what a UI or an
        API route can actually act on. Same walk, same guarantees.

        Reasons: ``malformed_json``, ``missing_fields``, ``seq_gap``,
        ``prev_hash_mismatch``, ``hash_mismatch``.
        """
        if not self.path.exists():
            return {"ok": True, "recordCount": 0, "firstBreak": None}
        expected_seq = 0
        expected_prev = GENESIS
        count = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    return _break(index, None, "malformed_json", str(exc), count)
                missing = [key for key in ("seq", "prev_record_hash", "record", "hash")
                           if key not in record]
                if missing:
                    return _break(index, record.get("seq"), "missing_fields",
                                  f"missing {missing}", count)
                seq = record["seq"]
                if seq != expected_seq:
                    return _break(index, seq, "seq_gap",
                                  f"expected seq={expected_seq}, found {seq!r}", count)
                if record["prev_record_hash"] != expected_prev:
                    return _break(index, seq, "prev_hash_mismatch",
                                  "prev_record_hash does not follow its predecessor", count)
                body = record["record"]
                if not isinstance(body, dict) or "kind" not in body:
                    return _break(index, seq, "missing_fields", "missing record.kind", count)
                if record["hash"] != record_hash(seq, expected_prev, body):
                    return _break(index, seq, "hash_mismatch",
                                  "the payload was edited", count)
                expected_seq += 1
                expected_prev = record["hash"]
                count += 1
        return {"ok": True, "recordCount": count, "firstBreak": None}

    def append_refusing_a_broken_chain(
        self,
        kind: str,
        payload: Any,
        *,
        at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append only after the WHOLE existing chain verifies.

        A chain that "heals" itself by building a valid-looking suffix on top of a
        tampered history defeats the point of the chain, so this refuses with the
        break named — the reference's ``LedgerCorruptionError`` contract. Cost is
        O(n) per append; these are conclusions, not ticks.
        """
        report = self.verify_report()
        if not report["ok"]:
            break_info = report["firstBreak"]
            raise LedgerIntegrityError(
                f"the ledger chain is already broken at index={break_info['index']} "
                f"seq={break_info['seq']} reason={break_info['reason']}: "
                f"{break_info['detail']}"
            )
        return self.append(kind, payload, at=at)

    def export(self) -> dict[str, Any]:
        """A copy that can be re-verified without the original file.

        Embeds every record plus the verification outcome and a hash over the
        envelope, so editing an exported record is caught even when the embedded
        records still chain among themselves (the reference's
        ``build_export``/``verify_export`` pair).
        """
        records = self.read()
        envelope = {"records": records}
        return {
            "format": "eclipse-governance-ledger-export/v1",
            "recordCount": len(records),
            "records": records,
            "verification": self.verify_report(),
            "exportHash": record_hash(0, GENESIS, envelope),
        }

    def verify(self) -> int:
        """Re-hash the whole chain and return the record count.

        Raises :class:`LedgerIntegrityError` on the first break, naming the
        sequence number and *why* it failed, because "tampered" without a
        location is not actionable.
        """
        if not self.path.exists():
            return 0
        expected_seq = 0
        expected_prev = GENESIS
        count = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LedgerIntegrityError(
                        f"line {number}: not JSON (truncated tail?)"
                    ) from exc
                seq = record.get("seq")
                if seq != expected_seq:
                    raise LedgerIntegrityError(
                        f"line {number}: seq {seq!r}, expected {expected_seq}"
                    )
                if record.get("prev_record_hash") != expected_prev:
                    raise LedgerIntegrityError(
                        f"record {seq}: prev_record_hash does not follow its predecessor"
                    )
                body = record.get("record")
                if not isinstance(body, dict) or "kind" not in body:
                    raise LedgerIntegrityError(f"record {seq}: missing 'record.kind'")
                if record.get("hash") != record_hash(seq, expected_prev, body):
                    raise LedgerIntegrityError(
                        f"record {seq}: hash mismatch — the payload was edited"
                    )
                expected_seq += 1
                expected_prev = record["hash"]
                count += 1
        return count
