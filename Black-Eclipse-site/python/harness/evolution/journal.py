"""A durable, hash-chained intent journal for evolution actions.

Every external action writes an intent *before* it runs and a completion after
it settles. ``resume()`` exposes intents that never completed, so a crash is
reconciled instead of being repeated. Candidate snapshots ride the same journal,
which is how a restarted process knows what is already applied.

One writer per path. The file is append-only JSONL; a torn final line is
discarded on read and never rewritten.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .assets import Candidate


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class JournalRecord:
    seq: int
    kind: str
    key: str
    payload: dict[str, Any]
    at: float
    prev: str
    hash: str


class EvolutionJournal:
    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
        self._records: list[JournalRecord] = []
        self._last_hash = ""
        self._seq = 0
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    break  # a torn tail is not evidence
                record = JournalRecord(
                    seq=int(raw["seq"]),
                    kind=str(raw["kind"]),
                    key=str(raw["key"]),
                    payload=dict(raw.get("payload") or {}),
                    at=float(raw.get("at") or 0),
                    prev=str(raw.get("prev") or ""),
                    hash=str(raw.get("hash") or ""),
                )
                self._records.append(record)
                self._last_hash = record.hash
                self._seq = max(self._seq, record.seq)

    def append(self, kind: str, key: str, payload: Optional[dict[str, Any]] = None) -> JournalRecord:
        self._seq += 1
        stored = copy.deepcopy(payload or {})
        body = {
            "seq": self._seq,
            "kind": str(kind),
            "key": str(key),
            "payload": stored,
            "at": time.time(),
            "prev": self._last_hash,
        }
        digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        record = JournalRecord(
            seq=body["seq"], kind=body["kind"], key=body["key"],
            payload=body["payload"], at=body["at"], prev=body["prev"], hash=digest,
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical({**body, "hash": digest}) + "\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self._records.append(record)
        self._last_hash = digest
        return record

    # -- intents --------------------------------------------------------------

    def begin(self, key: str, payload: Optional[dict[str, Any]] = None) -> bool:
        """Record an intent unless the key is already pending or complete."""
        if key in self.pending() or key in self.completed():
            return False
        self.append("intent", key, payload)
        return True

    def finish(self, key: str, result: Optional[dict[str, Any]] = None) -> None:
        self.append("complete", key, result)

    def pending(self) -> set[str]:
        finished = self.completed()
        return {
            record.key for record in self._records
            if record.kind == "intent" and record.key not in finished
        }

    def completed(self) -> set[str]:
        return {record.key for record in self._records if record.kind == "complete"}

    # -- candidate state ------------------------------------------------------

    def snapshot(self, candidate: Candidate) -> None:
        self.append("candidate", candidate.id, candidate.to_dict())

    def latest_candidates(self) -> dict[str, Candidate]:
        from .assets import candidate_from_dict  # noqa: PLC0415

        latest: dict[str, Candidate] = {}
        for record in self._records:
            if record.kind != "candidate":
                continue
            try:
                latest[record.key] = candidate_from_dict(record.payload)
            except Exception:  # noqa: BLE001 - an invalid snapshot is ignored, not trusted
                continue
        return latest

    def records(self) -> list[JournalRecord]:
        return list(self._records)

    def verify(self) -> tuple[bool, str]:
        prev = ""
        for record in self._records:
            body = {
                "seq": record.seq, "kind": record.kind, "key": record.key,
                "payload": record.payload, "at": record.at, "prev": prev,
            }
            expected = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            if record.prev != prev or record.hash != expected:
                return False, f"journal broken at seq {record.seq}"
            prev = record.hash
        return True, ""
