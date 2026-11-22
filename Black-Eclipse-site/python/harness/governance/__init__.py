"""Governance: a tamper-evident ledger and a methodology fingerprint.

Two small, independent pieces, both deliberately without dependencies:

* :mod:`harness.governance.ledger` — hash-chained append-only JSONL, so the
  record of a conclusion can be proven unaltered.
* :mod:`harness.governance.manifest` — prompt + skills + tools + packages
  hashed into one day-independent ``manifestHash``.

They compose: append a ``manifest`` record, then ``conclusion`` records that
cite it, and a reader can verify both the chain and that the conclusion was
produced by the system it claims.
"""

from .ledger import (
    GENESIS,
    ChainedLedger,
    LedgerError,
    LedgerIntegrityError,
    record_hash,
)
from .manifest import HASHED_FIELDS, build_manifest, manifest_diff, manifest_hash

__all__ = [
    "GENESIS",
    "HASHED_FIELDS",
    "ChainedLedger",
    "LedgerError",
    "LedgerIntegrityError",
    "build_manifest",
    "manifest_diff",
    "manifest_hash",
    "record_hash",
]
