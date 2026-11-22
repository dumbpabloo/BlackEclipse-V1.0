"""One append-only execution log across the planes (Round 5 / C2).

Run events, Fabric job events, graph-runtime events, task events and
orchestration events were five separate tables with five emitters. This module is
the canonical log: `(source, entityId, seq, type, at, data)`. The legacy tables
remain as projections for their existing readers, so the change is additive and
rollback-safe; new cross-plane readers use THIS log.

The log is intentionally narrow: it stores what happened and to which entity,
not a second copy of every payload. Callers bound `data` the same way they bound
the legacy row.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .migrations import Migration, MigrationRunner
from .persistence import now_iso

EXECUTION_LOG_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="2026-11-11-002-execution-event-log",
        description="ExecutionEvent: the one cross-plane append-only log",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS ExecutionEvent (
              id        TEXT PRIMARY KEY,
              source    TEXT NOT NULL,
              entityId  TEXT NOT NULL,
              seq       INTEGER NOT NULL,
              type      TEXT NOT NULL,
              at        TEXT NOT NULL,
              data      TEXT,
              parentId  TEXT,
              UNIQUE (source, entityId, seq)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_execution_event_entity ON ExecutionEvent(entityId, seq)",
            "CREATE INDEX IF NOT EXISTS idx_execution_event_source ON ExecutionEvent(source, at)",
        ),
    ),
)

#: The canonical sources. A new plane must add itself here.
SOURCES = frozenset({"run", "fabric", "graph", "task", "orchestration"})

#: How many events a reader may replay in one call, mirroring the reference bus's
#: bounded buffer. A reconnect replays from the buffer, never from the whole log.
DEFAULT_REPLAY_LIMIT = 500


def replay_after(
    events: list[dict[str, Any]],
    last_event_id: Optional[str] = None,
    *,
    replay_all: bool = False,
) -> list[dict[str, Any]]:
    """Events strictly AFTER ``last_event_id`` (reference replay semantics).

    Ported from Vibe-Trading's `session/events.py::_replay_locked`, and the three
    branches are the point:

    * **no id** — return nothing unless ``replay_all``: a first connect must fetch
      history deliberately, not race the live stream.
    * **id found** — everything after it, in order.
    * **id not found** (evicted or from another session) — nothing, unless
      ``replay_all``, in which case the whole buffer. Silently restarting from the
      beginning is how a client shows duplicate events; saying "I cannot resume"
      and re-sending everything on request is honest.
    """
    if not last_event_id:
        return list(events) if replay_all else []
    found = False
    out: list[dict[str, Any]] = []
    for event in events:
        if found:
            out.append(event)
        elif event.get("id") == last_event_id:
            found = True
    if not found and replay_all:
        return list(events)
    return out


class ExecutionEventLog:
    """Append + read the one execution log over the agent-memory connection."""

    def __init__(self, db: Any, namespace: str = "agent-memory") -> None:
        self._db = db
        MigrationRunner(db, namespace).apply(EXECUTION_LOG_MIGRATIONS)

    def append(
        self,
        source: str,
        entity_id: str,
        event_type: str,
        data: Optional[dict[str, Any]] = None,
        *,
        parent_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Append one event. Returns the row, or None when the write failed.

        Fail-open by contract: losing a log row must never fail the work it
        describes. The caller's own projection remains authoritative.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown execution-log source {source!r}")
        if not entity_id:
            return None
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), -1) AS m FROM ExecutionEvent "
                "WHERE source=? AND entityId=?",
                (source, entity_id),
            ).fetchone()
            seq = int(row["m"]) + 1
            event = {
                "id": f"xevt_{source}_{entity_id}_{seq}",
                "source": source,
                "entityId": entity_id,
                "seq": seq,
                "type": event_type,
                "at": now_iso(),
                "data": data or {},
                "parentId": parent_id,
            }
            payload = json.dumps(event["data"], ensure_ascii=False, default=str)
            if len(payload) > 200_000:
                payload = json.dumps(
                    {"truncated": True, "chars": len(payload)},
                    ensure_ascii=False,
                )
            self._db.execute(
                "INSERT INTO ExecutionEvent "
                "(id, source, entityId, seq, type, at, data, parentId) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["id"], source, entity_id, seq, event_type,
                    event["at"], payload, parent_id,
                ),
            )
            self._db.execute("COMMIT")
            return event
        except Exception:  # noqa: BLE001 - the log must never break the work
            try:
                self._db.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            return None

    def list(self, entity_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM ExecutionEvent WHERE entityId=? ORDER BY source, seq ASC LIMIT ?",
            (entity_id, max(1, min(int(limit), 5_000))),
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_source(self, source: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM ExecutionEvent WHERE source=? ORDER BY at DESC, seq DESC LIMIT ?",
            (source, max(1, min(int(limit), 5_000))),
        ).fetchall()
        return [self._row(row) for row in rows]

    def replay(
        self,
        entity_id: str,
        last_event_id: Optional[str] = None,
        *,
        replay_all: bool = False,
        limit: int = DEFAULT_REPLAY_LIMIT,
    ) -> list[dict[str, Any]]:
        """Reconnect recovery: what to send a client that saw ``last_event_id``.

        The buffer is bounded like the reference's (last ``limit`` events), so a
        long-dead client is told it cannot resume by getting an empty list rather
        than a flood — unless it explicitly asks for ``replay_all``.
        """
        events = self.list(entity_id, limit=limit)
        return replay_after(events, last_event_id, replay_all=replay_all)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        try:
            data = json.loads(row["data"]) if row["data"] else {}
        except (TypeError, ValueError):
            data = {}
        return {
            "id": row["id"],
            "source": row["source"],
            "entityId": row["entityId"],
            "seq": row["seq"],
            "type": row["type"],
            "at": row["at"],
            "data": data,
            "parentId": row["parentId"],
        }


__all__ = [
    "DEFAULT_REPLAY_LIMIT",
    "EXECUTION_LOG_MIGRATIONS",
    "SOURCES",
    "ExecutionEventLog",
    "replay_after",
]
