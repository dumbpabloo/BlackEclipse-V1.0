"""Bounded retention for the agent-memory store.

`agent-memory.db` grows with one row per event, message, tool execution and
checkpoint. On a long-lived install it reached multi-gigabytes, which slows every
SQLite write and widens the window in which a busy runtime looks unresponsive.

This module prunes the CHILD rows of TERMINAL runs older than a cutoff and then
checkpoints the WAL. It deliberately:

  * never touches a `created`/`running` run (live work is sacred),
  * never deletes the `HarnessRun` row itself (run history stays visible),
  * defaults to a dry run, so an operator sees the counts first,
  * only vacuums when asked (VACUUM rewrites the file and needs free disk space).

CLI::

    python -m harness.retention --days 30            # report only
    python -m harness.retention --days 30 --apply    # prune + WAL checkpoint
    python -m harness.retention --days 30 --apply --vacuum
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from typing import Any, Optional

from .persistence import AgentMemoryStore, now_iso

#: Child tables keyed by the run they belong to. Each has a `runId` column.
RUN_CHILD_TABLES: tuple[str, ...] = (
    "HarnessEvent",
    "RunMessage",
    "RunCheckpoint",
    "HarnessToolExecution",
    "ToolExecutionIdempotency",
    "RunLease",
)

_TERMINAL_PREDICATE = (
    "status NOT IN ('created', 'running') AND "
    "COALESCE(finishedAt, updatedAt, startedAt, createdAt) < ?"
)


def _cutoff_iso(days: int, *, now: Optional[dt.datetime] = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return (moment - dt.timedelta(days=max(0, int(days)))).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def plan(
    store: Any, *, older_than_days: int = 30, now: Optional[dt.datetime] = None
) -> dict[str, Any]:
    """What WOULD be pruned: terminal runs older than the cutoff and row counts."""
    cutoff = _cutoff_iso(older_than_days, now=now)
    db = store._db  # noqa: SLF001 - the shared store connection
    runs = [
        dict(row) for row in db.execute(
            f"SELECT id, status, COALESCE(finishedAt, updatedAt, startedAt, createdAt) AS at "
            f"FROM HarnessRun WHERE {_TERMINAL_PREDICATE}",
            (cutoff,),
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table in RUN_CHILD_TABLES:
        if not _table_exists(db, table):
            continue
        counts[table] = int(db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE runId IN ("
            f"SELECT id FROM HarnessRun WHERE {_TERMINAL_PREDICATE})",
            (cutoff,),
        ).fetchone()["n"])
    leases_for_unknown_runs = 0
    try:
        leases_for_unknown_runs = int(db.execute(
            "SELECT COUNT(*) AS n FROM RunLease "
            "WHERE runId NOT IN (SELECT id FROM HarnessRun)"
        ).fetchone()["n"])
    except Exception:  # noqa: BLE001 - no lease table
        pass
    size_bytes = 0
    try:
        size_bytes = os.path.getsize(str(getattr(store, "path", "")))
    except OSError:
        pass
    return {
        "cutoff": cutoff,
        "terminalRuns": len(runs),
        "rows": counts,
        "orphanLeases": leases_for_unknown_runs,
        "databaseBytes": size_bytes,
    }


def apply(
    store: Any,
    *,
    older_than_days: int = 30,
    now: Optional[dt.datetime] = None,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Delete the planned child rows, checkpoint the WAL, optionally vacuum."""
    cutoff = _cutoff_iso(older_than_days, now=now)
    db = store._db  # noqa: SLF001
    before = plan(store, older_than_days=older_than_days, now=now)
    deleted: dict[str, int] = {}
    for table in RUN_CHILD_TABLES:
        if not _table_exists(db, table):
            continue
        cursor = db.execute(
            f"DELETE FROM {table} WHERE runId IN ("
            f"SELECT id FROM HarnessRun WHERE {_TERMINAL_PREDICATE})",
            (cutoff,),
        )
        deleted[table] = int(cursor.rowcount or 0)
    # A lease whose run no longer exists can never be useful and blocks nothing.
    try:
        cursor = db.execute(
            "DELETE FROM RunLease WHERE runId NOT IN (SELECT id FROM HarnessRun)"
        )
        deleted["orphanLeases"] = int(cursor.rowcount or 0)
    except Exception:  # noqa: BLE001 - no lease table
        pass
    db.commit()
    checkpoint = _checkpoint(db)
    vacuumed = False
    if vacuum:
        try:
            db.execute("VACUUM")
            vacuumed = True
        except sqlite3.OperationalError:
            vacuumed = False
    after = plan(store, older_than_days=older_than_days, now=now)
    return {
        "cutoff": cutoff,
        "planned": before,
        "deleted": deleted,
        "walCheckpoint": checkpoint,
        "vacuumed": vacuumed,
        "after": after,
        "appliedAt": now_iso(),
    }


def _checkpoint(db: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError:
        return {"ok": False}
    if row is None:
        return {"ok": True}
    values = list(row)
    # (busy, log_frames, checkpointed_frames)
    return {
        "ok": int(values[0]) == 0 if values else True,
        "logFrames": int(values[1]) if len(values) > 1 else None,
        "checkpointedFrames": int(values[2]) if len(values) > 2 else None,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="path to agent-memory.db (default: runtime path)")
    parser.add_argument("--days", type=int, default=30, help="retain terminal runs newer than this")
    parser.add_argument("--apply", action="store_true", help="delete (default is report only)")
    parser.add_argument("--vacuum", action="store_true", help="rewrite the file after pruning")
    args = parser.parse_args(argv)

    store = AgentMemoryStore(args.db) if args.db else AgentMemoryStore()
    try:
        if args.apply:
            result = apply(store, older_than_days=args.days, vacuum=args.vacuum)
        else:
            result = {"dryRun": True, **plan(store, older_than_days=args.days)}
    finally:
        store.close()
    import json  # noqa: PLC0415 - CLI only

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["apply", "plan", "RUN_CHILD_TABLES", "main"]
