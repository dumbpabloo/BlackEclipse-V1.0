"""Tamper-evident hash chain for accounting audit events.

Append-only is a *policy* enforced by the store; a hash chain makes it a
*property* an operator can verify. Each audit event seals an immutable payload
with SHA-256 over the previous event's hash plus a canonical JSON encoding of
this event. Editing, deleting or reordering a row breaks verification and names
the first bad record.

Used by the General Ledger (`LedgerAuditEvent`) and the Portfolio Accounting
Engine (`PortfolioAuditEvent`). The module is pure: stores own persistence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Iterable, Mapping, Optional

#: The prev-hash of the first event in a ledger/portfolio chain.
GENESIS = "GENESIS"

#: Receipt states. `OK` means sealed and verified; `UNSEALED` means historical
#: rows exist that predate the chain (honest, not silently "verified").
STATUS_OK = "OK"
STATUS_BROKEN = "BROKEN"
STATUS_UNSEALED = "UNSEALED"


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def event_hash(prev_hash: Optional[str], payload: Mapping[str, Any]) -> str:
    """SHA-256 of `prev_hash \\n canonical(payload)`; never keyed, never truncated."""
    previous = prev_hash or GENESIS
    body = canonical_json(dict(payload))
    return hashlib.sha256(f"{previous}\n{body}".encode("utf-8")).hexdigest()


def _field(record: Any, key: str) -> Any:
    """Read a field from a Mapping or a sqlite3.Row without assuming `.get`."""
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return None


def verify_chain(
    records: Iterable[Mapping[str, Any]],
    *,
    payload_of: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    hash_key: str = "hash",
    prev_key: str = "prevHash",
) -> dict[str, Any]:
    """Recompute a chain in append order and return a named verdict.

    A historical row with no hash is reported as UNSEALED rather than as a
    failure: the store seals legacy rows on upgrade, but a caller reading a
    database it cannot write should still get an honest answer.
    """
    previous = GENESIS
    checked = 0
    unsealed = 0
    for record in records:
        checked += 1
        stored_hash = _field(record, hash_key)
        if not stored_hash:
            unsealed += 1
            continue
        stored_prev = _field(record, prev_key) or GENESIS
        if stored_prev != previous:
            return {
                "status": STATUS_BROKEN,
                "ok": False,
                "checked": checked,
                "brokenAt": _field(record, "id"),
                "reason": "PREV_HASH_MISMATCH",
                "detail": {
                    "expectedPrev": previous,
                    "storedPrev": stored_prev,
                },
            }
        expected = event_hash(stored_prev, payload_of(record))
        if stored_hash != expected:
            return {
                "status": STATUS_BROKEN,
                "ok": False,
                "checked": checked,
                "brokenAt": _field(record, "id"),
                "reason": "HASH_MISMATCH",
                "detail": {"expected": expected, "stored": stored_hash},
            }
        previous = stored_hash
    if unsealed:
        return {
            "status": STATUS_UNSEALED,
            "ok": False,
            "checked": checked,
            "unsealed": unsealed,
            "head": previous,
            "reason": "LEGACY_UNSEALED_EVENTS",
        }
    return {"status": STATUS_OK, "ok": True, "checked": checked, "head": previous}
